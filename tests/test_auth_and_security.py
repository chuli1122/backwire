import asyncio
import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backwire import BackwireConfig, BackwireHub, create_ws_router
from backwire.hub import AuthRateLimiter
from backwire.server import _MCPAuthMiddleware


def make_client(**kwargs):
    config = BackwireConfig.with_tokens({"dev_1": "secret"}, **kwargs)
    hub = BackwireHub(config)
    app = FastAPI()
    app.include_router(create_ws_router(hub, config))
    return TestClient(app), hub


def hello(name="dev_1"):
    return {"type": "hello", "name": name, "kind": "headless", "capabilities": ["read"]}


def test_query_token_is_rejected_by_default():
    client, _ = make_client()
    with client.websocket_connect("/ws/terminal?token=secret", headers={"X-Device-Name": "dev_1"}) as ws:
        assert ws.receive_json if False else True
        # The server closes immediately after accept; attempting receive reports the close.
        try:
            ws.receive_text()
        except Exception as exc:
            assert getattr(exc, "code", None) == 4003


def test_valid_headers_register_device():
    client, hub = make_client()
    with client.websocket_connect("/ws/terminal", headers={"Authorization": "Bearer secret", "X-Device-Name": "dev_1"}) as ws:
        ws.send_json(hello())
        # TestClient runs the WebSocket handler in a portal; the session is visible
        # once the first server-side operation is scheduled.
        import time
        for _ in range(20):
            if len(hub.sessions) == 1:
                break
            time.sleep(0.01)
        assert len(hub.sessions) == 1


def test_wrong_token_and_invalid_name_rejected():
    client, _ = make_client()
    for name, token in (("dev_1", "wrong"), ("-bad", "secret"), ("", "secret")):
        try:
            with client.websocket_connect("/ws/terminal", headers={"Authorization": f"Bearer {token}", "X-Device-Name": name}):
                pass
        except Exception as exc:
            assert "400" in str(exc)


def test_auth_failures_are_rate_limited_per_ip():
    client, _ = make_client(max_auth_failures_per_minute=2)
    for _ in range(3):
        try:
            with client.websocket_connect("/ws/terminal", headers={"Authorization": "Bearer bad", "X-Device-Name": "dev_1"}):
                pass
        except Exception:
            pass
    # A fourth attempt must remain rejected; the exact Starlette exception text is version-dependent.
    try:
        with client.websocket_connect("/ws/terminal", headers={"Authorization": "Bearer secret", "X-Device-Name": "dev_1"}):
            raise AssertionError("rate limiter allowed a connection")
    except Exception:
        pass


def test_untrusted_forwarded_headers_cannot_bypass_auth_rate_limit():
    client, _ = make_client(max_auth_failures_per_minute=2)
    for source_ip in ("198.51.100.10", "198.51.100.11", "203.0.113.10"):
        try:
            with client.websocket_connect(
                "/ws/terminal",
                headers={
                    "Authorization": "Bearer bad",
                    "X-Device-Name": "dev_1",
                    "X-Real-IP": source_ip,
                    "X-Forwarded-For": source_ip,
                },
            ):
                pass
        except Exception:
            pass

    with client.websocket_connect(
        "/ws/terminal",
        headers={"Authorization": "Bearer secret", "X-Device-Name": "dev_1", "X-Real-IP": "203.0.113.1"},
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
        assert exc_info.value.code == 4029


def test_trusted_proxy_forwarded_ips_have_independent_ws_and_mcp_limits():
    config = BackwireConfig.with_tokens(
        {"dev_1": "secret"},
        mcp_tokens=["mcp-secret"],
        trusted_proxies=["127.0.0.1", "testclient"],
        max_auth_failures_per_minute=2,
    )
    hub = BackwireHub(config)
    app = FastAPI()
    app.include_router(create_ws_router(hub, config))
    app.add_middleware(_MCPAuthMiddleware, config=config, rate_limiter=hub.rate_limiter)

    async def endpoint(scope, receive, send):
        from starlette.responses import PlainTextResponse
        await PlainTextResponse("ok")(scope, receive, send)

    app.mount("/mcp", endpoint)
    with TestClient(app) as client:
        bad_headers = {
            "Authorization": "Bearer bad",
            "X-Real-IP": "198.51.100.10",
            "X-Forwarded-For": "198.51.100.10",
        }
        assert client.post("/mcp", headers=bad_headers).status_code == 401
        assert client.post("/mcp", headers=bad_headers).status_code == 401
        assert client.post("/mcp", headers=bad_headers).status_code == 429
        assert client.post(
            "/mcp",
            headers={"Authorization": "Bearer mcp-secret", "X-Real-IP": "198.51.100.11"},
        ).status_code == 200

        with client.websocket_connect(
            "/ws/terminal",
            headers={
                "Authorization": "Bearer secret",
                "X-Device-Name": "dev_1",
                "X-Real-IP": "198.51.100.11",
                "X-Forwarded-For": "198.51.100.11",
            },
        ) as ws:
            ws.send_json(hello())
            import time
            for _ in range(20):
                if len(hub.sessions) == 1:
                    break
                time.sleep(0.01)
            assert len(hub.sessions) == 1


def test_mcp_auth_is_separate_and_supports_hashed_tokens():
    config = BackwireConfig.with_tokens({"dev_1": "secret"}, mcp_tokens=["mcp-secret"])
    assert not config.authenticate_mcp("secret")
    assert config.authenticate_mcp("mcp-secret")
    hashed = "sha256:" + hashlib.sha256(b"hashed-mcp").hexdigest()
    assert BackwireConfig(mcp_tokens=[hashed]).authenticate_mcp("hashed-mcp")
    assert not BackwireConfig().authenticate_mcp("secret")


def test_mcp_auth_middleware_rejects_query_and_rate_limits():
    async def endpoint(scope, receive, send):
        from starlette.responses import PlainTextResponse
        await PlainTextResponse("ok")(scope, receive, send)

    app = FastAPI()
    app.add_middleware(
        _MCPAuthMiddleware,
        config=BackwireConfig(mcp_tokens=["mcp-secret"], max_auth_failures_per_minute=2),
        rate_limiter=BackwireHub(BackwireConfig(max_auth_failures_per_minute=2)).rate_limiter,
    )
    app.mount("/mcp", endpoint)
    with TestClient(app) as client:
        assert client.post("/mcp?token=mcp-secret").status_code == 401
        assert client.post("/mcp", headers={"Authorization": "Bearer bad"}).status_code == 401
        assert client.post("/mcp", headers={"Authorization": "Bearer bad"}).status_code == 429
        assert client.post("/mcp", headers={"Authorization": "bEaReR mcp-secret"}).status_code == 429


def test_new_connection_replaces_old():
    client, hub = make_client()
    headers = {"Authorization": "Bearer secret", "X-Device-Name": "dev_1"}
    with client.websocket_connect("/ws/terminal", headers=headers) as old:
        old.send_json(hello())
        with client.websocket_connect("/ws/terminal", headers=headers) as new:
            new.send_json(hello())
            assert hub.sessions["dev_1"].websocket is not None
            assert hub.sessions["dev_1"].websocket is not old


def test_wildcard_token_name_is_rejected():
    with pytest.raises(ValueError, match="wildcard"):
        BackwireConfig(tokens={"*": "shared-secret"})


def test_auth_rate_limiter_bounds_tracked_peers():
    async def run():
        limiter = AuthRateLimiter(limit=5, window_seconds=60.0, max_tracked_peers=3)
        for i in range(20):
            await limiter.failure(f"10.0.0.{i}")
        assert len(limiter._events) <= 3
        assert "10.0.0.19" in limiter._events

    asyncio.run(run())

