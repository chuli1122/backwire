"""Standalone FastAPI application factory."""
from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .config import BackwireConfig, resolve_client_ip
from .hub import BackwireHub
from .router import create_ws_router


def _build_transport_security(allowed_hosts: list[str]) -> Any | None:
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError:
        return None
    if allowed_hosts:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(allowed_hosts),
        )
    # Reverse-proxied public deployments rely on Bearer auth and TLS proxy Host validation.
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def create_app(config: BackwireConfig | None = None, enable_mcp: bool = True) -> FastAPI:
    cfg = config or BackwireConfig.from_env()
    hub = BackwireHub(cfg)
    mcp_server: Any | None = None
    if enable_mcp:
        try:
            from .mcp_adapter import create_mcp_server
            mcp_server = create_mcp_server(hub)
        except RuntimeError:
            mcp_server = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        session_manager = getattr(mcp_server, "session_manager", None)
        run = getattr(session_manager, "run", None)
        if callable(run):
            async with run():
                yield
        else:
            yield

    app = FastAPI(title="backwire", version="0.1.0", lifespan=lifespan)
    app.state.backwire_hub = hub
    app.include_router(create_ws_router(hub, cfg))

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"ok": True}

    if mcp_server is not None:
        http_factory = getattr(mcp_server, "streamable_http_app", None) or getattr(mcp_server, "http_app", None)
        if callable(http_factory):
            app.add_middleware(_MCPAuthMiddleware, config=cfg, rate_limiter=hub.rate_limiter)
            settings = getattr(mcp_server, "settings", None)
            if settings is not None:
                if hasattr(settings, "streamable_http_path"):
                    # Keep the public prefix here; SDK versions otherwise may add /mcp twice.
                    settings.streamable_http_path = "/"
                if hasattr(settings, "transport_security"):
                    settings.transport_security = _build_transport_security(cfg.mcp_allowed_hosts)

            call_kwargs: dict[str, Any] = {}
            sig_params = inspect.signature(http_factory).parameters
            if "transport_security" in sig_params:
                ts = _build_transport_security(cfg.mcp_allowed_hosts)
                if ts is not None:
                    call_kwargs["transport_security"] = ts

            http_app = http_factory(**call_kwargs)
            # FastMCP/MCPServer versions differ: some expose routes rooted at /, others at /mcp.
            route_paths = {getattr(route, "path", "") for route in getattr(http_app, "routes", ())}
            app.mount("/mcp" if "/mcp" not in route_paths else "/", http_app)
        else:
            app.state.backwire_mcp = mcp_server
    else:
        app.state.backwire_mcp = None
    return app


class _MCPAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, config: BackwireConfig, rate_limiter) -> None:
        super().__init__(app)
        self.config = config
        self.rate_limiter = rate_limiter

    async def dispatch(self, request, call_next):
        if request.url.path == "/mcp" or request.url.path.startswith("/mcp/"):
            direct_host = request.client.host if request.client else "unknown"
            peer = resolve_client_ip(direct_host, request.headers, self.config.trusted_proxies)
            unauthorized = {"WWW-Authenticate": "Bearer"}
            if not await self.rate_limiter.allowed(peer):
                return JSONResponse({"detail": "too many authentication failures"}, status_code=429, headers=unauthorized)
            if "token" in request.query_params:
                await self.rate_limiter.failure(peer)
                return JSONResponse({"detail": "query token is disabled"}, status_code=401, headers=unauthorized)
            authorization = request.headers.get("authorization", "")
            token = None
            if authorization[:7].lower() == "bearer ":
                token = authorization[7:]
            if token is None or not self.config.authenticate_mcp(token):
                await self.rate_limiter.failure(peer)
                return JSONResponse({"detail": "authentication required"}, status_code=401, headers=unauthorized)
            await self.rate_limiter.success(peer)
        return await call_next(request)
