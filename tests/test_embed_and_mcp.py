import asyncio
import importlib.util
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backwire import BackwireConfig, BackwireHub, DeviceKind, create_app, create_ws_router
from backwire.mcp_adapter import TOOL_NAMES, create_mcp_server
from backwire.protocol import DeviceHello


def test_external_fastapi_can_mount_router_and_list_devices():
    config = BackwireConfig.with_tokens({"dev": "token"})
    hub = BackwireHub(config)
    app = FastAPI()
    app.include_router(create_ws_router(hub, config))
    client = TestClient(app)
    with client.websocket_connect("/ws/terminal", headers={"Authorization": "Bearer token", "X-Device-Name": "dev"}) as ws:
        ws.send_json({"type": "hello", "name": "dev", "kind": "headless", "capabilities": ["read"]})
        assert len(__import__("asyncio").run(hub.list_devices())) == 1


def test_core_app_works_without_mcp():
    app = create_app(BackwireConfig.with_tokens({"dev": "token"}), enable_mcp=False)
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_mcp_run_command_invokes_hub_with_timeout():
    async def run():
        hub = BackwireHub(BackwireConfig.with_tokens({"dev": "token"}))
        sent_event = asyncio.Event()

        class WebSocket:
            async def send_json(self, message):
                sent_event.set()

        websocket = WebSocket()
        await hub.register(DeviceHello(name="dev", kind=DeviceKind.headless, capabilities=["shell"]), websocket)
        server = create_mcp_server(hub)
        tool = next(tool for tool in server._tool_manager.list_tools() if tool.name == "run_command")
        task = asyncio.create_task(tool.fn("printf ok", device="dev", timeout=2))
        await asyncio.wait_for(sent_event.wait(), timeout=2)
        request = next(iter(hub._pending.values()))
        request_id = next(iter(request.session.pending_ids))
        await hub.handle_result({"id": request_id, "result": {"stdout": "ok"}})
        assert await task == {"stdout": "ok"}

    try:
        __import__("asyncio").run(run())
    except RuntimeError as exc:
        if "install backwire[mcp]" in str(exc):
            pytest.skip("optional mcp package is not installed in this environment")
        raise


def test_mcp_tokens_from_env(monkeypatch):
    monkeypatch.setenv("BACKWIRE_MCP_TOKEN", "single")
    monkeypatch.delenv("BACKWIRE_MCP_TOKENS_JSON", raising=False)
    assert BackwireConfig.from_env().mcp_tokens == ["single"]
    monkeypatch.delenv("BACKWIRE_MCP_TOKEN")
    monkeypatch.setenv("BACKWIRE_MCP_TOKENS_JSON", json.dumps(["one", "two"]))
    assert BackwireConfig.from_env().mcp_tokens == ["one", "two"]
    monkeypatch.setenv("BACKWIRE_MCP_ALLOWED_HOSTS", "mcp.example.com, 127.0.0.1:8000")
    assert BackwireConfig.from_env().mcp_allowed_hosts == ["mcp.example.com", "127.0.0.1:8000"]


def test_mcp_tool_names_are_stable():
    assert set(TOOL_NAMES) == {"list_devices", "run_command", "read_file", "write_file", "screenshot", "mouse_click", "keyboard_type", "hotkey", "scroll"}
    try:
        server = create_mcp_server(BackwireHub(BackwireConfig.with_tokens({"dev": "token"})))
    except RuntimeError as exc:
        assert "backwire[mcp]" in str(exc)
    else:
        names = {tool.name for tool in server._tool_manager.list_tools()}
        assert set(TOOL_NAMES) <= names


async def test_mcp_tool_annotations_are_serialized():
    pytest.importorskip("mcp")
    server = create_mcp_server(BackwireHub(BackwireConfig.with_tokens({"dev": "token"})))
    expected = {
        # readOnlyHint, destructiveHint, idempotentHint, openWorldHint
        "list_devices": (True, False, True, False),
        "read_file": (True, False, True, False),
        "screenshot": (True, False, True, False),
        "run_command": (False, True, False, True),
        "write_file": (False, True, True, False),
        "mouse_click": (False, True, False, False),
        "keyboard_type": (False, True, False, False),
        "hotkey": (False, True, False, False),
        "scroll": (False, True, False, False),
    }
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == expected.keys()
    keys = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
    for tool in tools:
        hints = json.loads(tool.model_dump_json(by_alias=True, exclude_unset=True))["annotations"]
        for key, value in zip(keys, expected[tool.name]):
            assert hints[key] is value, (tool.name, key)


def test_real_streamable_http_mcp_initialize_when_installed():
    try:
        create_mcp_server(BackwireHub(BackwireConfig.with_tokens({"dev": "token"})))
    except RuntimeError:
        pytest.skip("optional mcp package is not installed in this environment")

    cfg = BackwireConfig(tokens={"dev": "dev-secret"}, mcp_tokens=["mcp-secret"])
    app = create_app(cfg, enable_mcp=True)
    with TestClient(app) as client:
        # Device token must not authenticate /mcp
        denied = client.post(
            "/mcp",
            headers={"Authorization": "Bearer dev-secret", "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert denied.status_code == 401

        ok = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer mcp-secret",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0.1.0"},
                },
            },
        )
        assert ok.status_code == 200
        assert "backwire" in ok.text


def test_mcp_read_file_passes_offset_and_validates_args():
    async def run():
        hub = BackwireHub(BackwireConfig.with_tokens({"dev": "token"}))
        sent = []
        sent_event = asyncio.Event()

        class WebSocket:
            async def send_json(self, message):
                sent.append(message)
                sent_event.set()

        await hub.register(DeviceHello(name="dev", kind=DeviceKind.headless, capabilities=["read"]), WebSocket())
        server = create_mcp_server(hub)
        tool = next(t for t in server._tool_manager.list_tools() if t.name == "read_file")

        with pytest.raises(ValueError, match="offset"):
            await tool.fn("notes.txt", device="dev", max_chars=100, offset=-1)
        with pytest.raises(ValueError, match="max_chars"):
            await tool.fn("notes.txt", device="dev", max_chars=0, offset=0)

        task = asyncio.create_task(tool.fn("notes.txt", device="dev", max_chars=50, offset=12))
        await asyncio.wait_for(sent_event.wait(), timeout=2)
        assert sent[0]["tool"] == "read_file"
        assert sent[0]["arguments"] == {"path": "notes.txt", "max_chars": 50, "offset": 12}
        await hub.handle_result({"id": sent[0]["id"], "result": {"content": "abc", "offset": 12, "next_offset": 15, "truncated": False, "size_bytes": 15}})
        res = await task
        assert res["offset"] == 12
        assert res["next_offset"] == 15

    try:
        asyncio.run(run())
    except RuntimeError as exc:
        if "install backwire[mcp]" in str(exc):
            pytest.skip("optional mcp package is not installed in this environment")
        raise


def _load_embed_example():
    spec = importlib.util.spec_from_file_location("embed_fastapi_test_mod", "examples/embed_fastapi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_embed_example_rejects_missing_or_placeholder_tokens(monkeypatch):
    monkeypatch.delenv("BACKWIRE_DEVICE_TOKEN", raising=False)
    monkeypatch.delenv("BACKWIRE_APP_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="BACKWIRE_DEVICE_TOKEN"):
        _load_embed_example()

    monkeypatch.setenv("BACKWIRE_DEVICE_TOKEN", "REPLACE_WITH_DEVICE_TOKEN")
    monkeypatch.setenv("BACKWIRE_APP_TOKEN", "real-app-token")
    with pytest.raises(RuntimeError, match="BACKWIRE_DEVICE_TOKEN"):
        _load_embed_example()

    monkeypatch.setenv("BACKWIRE_DEVICE_TOKEN", "real-device-token")
    monkeypatch.setenv("BACKWIRE_APP_TOKEN", "REPLACE_WITH_APP_TOKEN")
    with pytest.raises(RuntimeError, match="BACKWIRE_APP_TOKEN"):
        _load_embed_example()

    monkeypatch.setenv("BACKWIRE_DEVICE_TOKEN", "real-device-token")
    monkeypatch.setenv("BACKWIRE_APP_TOKEN", "real-app-token")
    mod = _load_embed_example()
    assert mod.DEVICE_TOKEN == "real-device-token"
    assert mod.APP_TOKEN == "real-app-token"

