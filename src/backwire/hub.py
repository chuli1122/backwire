"""Multi-device WebSocket session hub."""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from .config import BackwireConfig
from .protocol import (
    Capability,
    DeviceHello,
    DeviceKind,
    HEADLESS_ALLOWED_CAPABILITIES,
    capability_for_tool,
    public_capabilities,
)


class AuthRateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0, max_tracked_peers: int = 4096) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_tracked_peers = max_tracked_peers
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allowed(self, address: str) -> bool:
        now = time.monotonic()
        async with self._lock:
            events = self._events.get(address)
            if not events:
                return True
            while events and now - events[0] >= self.window_seconds:
                events.popleft()
            if not events:
                self._events.pop(address, None)
            return len(events) < self.limit if events else True

    async def failure(self, address: str) -> None:
        now = time.monotonic()
        async with self._lock:
            for peer, events in list(self._events.items()):
                while events and now - events[0] >= self.window_seconds:
                    events.popleft()
                if not events:
                    self._events.pop(peer, None)
            if address not in self._events and len(self._events) >= self.max_tracked_peers:
                oldest = min(self._events, key=lambda peer: self._events[peer][0])
                self._events.pop(oldest, None)
            events = self._events[address]
            events.append(now)

    async def success(self, address: str) -> None:
        async with self._lock:
            self._events.pop(address, None)


@dataclass
class _Pending:
    device: str
    session: "DeviceSession"
    future: asyncio.Future[Any]


@dataclass
class DeviceSession:
    name: str
    kind: DeviceKind
    capabilities: set[Capability]
    websocket: WebSocket
    peer: str
    pending_ids: set[str] = field(default_factory=set)


_SENSITIVE = re.compile(
    r"(?i)(bearer\s+|authorization\s*:\s*basic\s+|--(?:token|password)(?:\s+|=)|(?:token|secret|password|passwd|api[_-]?key)\s*[=:]\s*|[A-Z0-9_]*(?:SECRET|KEY)[A-Z0-9_]*\s*=\s*)[^\s,;\"']+"
)
_SENSITIVE_KEYS = {"token", "secret", "password", "passwd", "api_key", "apikey", "authorization", "cookie"}
_MAX_AUDIT_RECORD = 16 * 1024
_MAX_FILE_CHARS = 200_000


def _redact(value: Any, *, field: str | None = None) -> Any:
    if field and field.lower() in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, str):
        return _SENSITIVE.sub(lambda m: m.group(1) + "[REDACTED]", value)
    if isinstance(value, dict):
        return {str(k): _redact(v, field=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _audit_fields(fields: dict[str, Any]) -> dict[str, Any]:
    result = _redact(fields)
    if isinstance(result, dict):
        if "arguments" in result and isinstance(result["arguments"], dict):
            args = result["arguments"]
            if "content" in args:
                original = fields.get("arguments", {}).get("content", "")
                args["content"] = f"<{len(str(original))} chars>"
            if "command" in args:
                args["command"] = str(args["command"])[:512]
        return result
    return fields


class BackwireHub:
    def __init__(self, config: BackwireConfig | None = None) -> None:
        self.config = config or BackwireConfig.from_env()
        self._sessions: dict[str, DeviceSession] = {}
        self._pending: dict[str, _Pending] = {}
        self._lock = asyncio.Lock()
        self.rate_limiter = AuthRateLimiter(
            self.config.max_auth_failures_per_minute,
            max_tracked_peers=self.config.max_tracked_peers,
        )
        # Kept as an alias for the device WebSocket router.
        self.auth_limiter = self.rate_limiter

    async def register(self, hello: DeviceHello, websocket: WebSocket, peer: str = "unknown") -> DeviceSession:
        capabilities = set(hello.capabilities)
        if hello.kind is DeviceKind.headless:
            capabilities &= HEADLESS_ALLOWED_CAPABILITIES
        session = DeviceSession(hello.name, hello.kind, capabilities, websocket, peer)
        old: DeviceSession | None
        async with self._lock:
            old = self._sessions.get(hello.name)
            self._sessions[hello.name] = session
            if old is not None:
                await self._fail_session_pending_locked(old.name, "replaced by a newer connection", session=old)
        if old is not None:
            try:
                await old.websocket.close(code=4009, reason="replaced by newer connection")
            except Exception:
                pass
        self._audit("connect", device=hello.name, kind=hello.kind.value, capabilities=public_capabilities(capabilities), peer=peer)
        return session

    async def unregister(self, session: DeviceSession) -> None:
        async with self._lock:
            if self._sessions.get(session.name) is session:
                self._sessions.pop(session.name, None)
                await self._fail_session_pending_locked(session.name, "device disconnected", session=session)
        self._audit("disconnect", device=session.name)

    async def _fail_session_pending_locked(self, device: str, reason: str, session: DeviceSession | None = None) -> None:
        for request_id, pending in list(self._pending.items()):
            if pending.device != device or (session is not None and pending.session is not session):
                continue
            self._pending.pop(request_id, None)
            pending.session.pending_ids.discard(request_id)
            if not pending.future.done():
                pending.future.set_exception(ConnectionError(reason))

    async def list_devices(self) -> list[dict[str, Any]]:
        async with self._lock:
            sessions = list(self._sessions.values())
        return [
            {
                "name": s.name,
                "kind": s.kind.value,
                "capabilities": public_capabilities(s.capabilities),
                "peer": s.peer,
            }
            for s in sessions
        ]

    async def _choose(self, device: str | None) -> DeviceSession:
        async with self._lock:
            if device:
                selected = self._sessions.get(device)
            elif self.config.default_device:
                selected = self._sessions.get(self.config.default_device)
            else:
                selected = None
            if selected is not None:
                return selected
            sessions = list(self._sessions.values())
        if not sessions:
            raise LookupError("no devices connected")
        if self.config.prefer_desktop_default:
            for session in sessions:
                if session.kind is DeviceKind.desktop:
                    return session
        return sessions[0]

    async def invoke(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        device: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        required = capability_for_tool(tool)
        if required is None:
            raise ValueError(f"unknown tool: {tool}")
        effective_timeout = self.config.command_timeout_seconds if timeout is None else timeout
        if not isinstance(effective_timeout, (int, float)) or isinstance(effective_timeout, bool) or not math.isfinite(effective_timeout) or effective_timeout <= 0 or effective_timeout > 300:
            raise ValueError("timeout must be a finite positive number <= 300")
        args = arguments or {}
        if tool == "read_file":
            if "offset" in args:
                offset = args["offset"]
                if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                    raise ValueError("offset must be a non-negative integer")
            if "max_chars" in args:
                max_chars = args["max_chars"]
                if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 0 < max_chars <= _MAX_FILE_CHARS:
                    raise ValueError(f"max_chars must be a positive integer <= {_MAX_FILE_CHARS}")
        session = await self._choose(device)
        if required not in session.capabilities:
            self._audit("denied", device=session.name, tool=tool, reason="capability missing")
            raise PermissionError(f"device {session.name!r} lacks capability {required.value!r}")
        request_id = uuid.uuid4().hex
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        pending = _Pending(session.name, session, future)
        async with self._lock:
            current = self._sessions.get(session.name)
            if current is not session:
                raise ConnectionError("device connection changed")
            if len(self._pending) >= self.config.max_pending_total:
                raise RuntimeError("pending request limit reached")
            if len(session.pending_ids) >= self.config.max_pending_per_device:
                raise RuntimeError("device pending request limit reached")
            self._pending[request_id] = pending
            session.pending_ids.add(request_id)
        message = {"id": request_id, "tool": tool, "arguments": arguments or {}}
        self._audit("invoke", device=session.name, tool=tool, arguments=message["arguments"])
        deadline = time.monotonic() + float(effective_timeout)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(session.websocket.send_json(message), timeout=remaining)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            return await asyncio.wait_for(future, timeout=remaining)
        except asyncio.TimeoutError:
            if not future.done():
                future.cancel()
            self._audit("timeout", device=session.name, tool=tool, request_id=request_id)
            raise
        finally:
            async with self._lock:
                self._pending.pop(request_id, None)
                session.pending_ids.discard(request_id)

    async def handle_result(self, message: dict[str, Any], session: DeviceSession | None = None) -> bool:
        request_id = message.get("id")
        if not isinstance(request_id, str):
            return False
        async with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or (session is not None and pending.session is not session):
                return False
            if pending.future.done():
                return False
            if "error" in message:
                pending.future.set_exception(RuntimeError(str(message["error"])))
            else:
                pending.future.set_result(message.get("result"))
        return True

    def _audit(self, event: str, **fields: Any) -> None:
        path = self.config.audit_log_path
        if not path:
            return
        record = {"ts": time.time(), "event": event, **_audit_fields(fields)}
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise ValueError("audit log path must not be a symbolic link")
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        if len(payload) > _MAX_AUDIT_RECORD:
            payload = payload[:_MAX_AUDIT_RECORD] + "…"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        mode = 0o600
        fd = os.open(target, flags, mode)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, mode)
            elif os.name != "nt":
                os.chmod(target, mode)
            handle = os.fdopen(fd, "a", encoding="utf-8")
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        with handle:
            handle.write(payload + "\n")

    @property
    def sessions(self) -> dict[str, DeviceSession]:
        return self._sessions
