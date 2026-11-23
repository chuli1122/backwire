"""FastAPI WebSocket router for reverse device connections."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .config import BackwireConfig, resolve_client_ip
from .hub import BackwireHub
from .protocol import DeviceHello, validate_device_name


def _authorization(websocket: WebSocket) -> str | None:
    value = websocket.headers.get("authorization")
    if not value or not value.startswith("Bearer "):
        return None
    token = value[7:]
    return token if token else None


def create_ws_router(hub: BackwireHub, config: BackwireConfig | None = None, path: str = "/ws/terminal") -> APIRouter:
    cfg = config or hub.config
    router = APIRouter()

    @router.websocket(path)
    async def terminal(websocket: WebSocket) -> None:
        direct_host = websocket.client.host if websocket.client else "unknown"
        peer = resolve_client_ip(direct_host, websocket.headers, cfg.trusted_proxies)
        await websocket.accept()
        device_name = websocket.headers.get("x-device-name", "")
        presented = _authorization(websocket)
        if cfg.allow_query_token and presented is None:
            presented = websocket.query_params.get("token")
        if not await hub.auth_limiter.allowed(peer):
            await websocket.close(code=4029, reason="too many authentication failures")
            return
        if "token" in websocket.query_params and not cfg.allow_query_token:
            await hub.auth_limiter.failure(peer)
            await websocket.close(code=4003, reason="query token is disabled")
            return
        if not validate_device_name(device_name):
            await hub.auth_limiter.failure(peer)
            await websocket.close(code=4004, reason="invalid device name")
            return
        if presented is None or not cfg.authenticate(device_name, presented):
            await hub.auth_limiter.failure(peer)
            await websocket.close(code=4003, reason="invalid credentials")
            return
        await hub.auth_limiter.success(peer)
        try:
            raw = await websocket.receive_text()
            if len(raw.encode("utf-8")) > cfg.max_message_bytes:
                await websocket.close(code=1009, reason="message too large")
                return
            try:
                hello_data: Any = json.loads(raw)
                hello = DeviceHello.model_validate(hello_data)
            except (ValueError, TypeError, json.JSONDecodeError):
                await websocket.close(code=4000, reason="invalid hello")
                return
            if hello.name != device_name:
                await websocket.close(code=4004, reason="hello name mismatch")
                return
            session = await hub.register(hello, websocket, peer)
            try:
                while True:
                    raw = await websocket.receive_text()
                    if len(raw.encode("utf-8")) > cfg.max_message_bytes:
                        await websocket.close(code=1009, reason="message too large")
                        return
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(message, dict):
                        await hub.handle_result(message, session)
            except WebSocketDisconnect:
                pass
            finally:
                await hub.unregister(session)
        except WebSocketDisconnect:
            return

    return router
