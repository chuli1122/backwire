import asyncio
import json

import pytest

from backwire import BackwireConfig, BackwireHub, Capability, DeviceKind
from backwire.protocol import DeviceHello


class FakeWebSocket:
    def __init__(self):
        self.messages = []
        self.closed = False
        self.sent = asyncio.Event()

    async def send_json(self, message):
        self.messages.append(message)
        self.sent.set()

    async def close(self, **kwargs):
        self.closed = True


async def register(hub, name, kind, caps):
    ws = FakeWebSocket()
    session = await hub.register(DeviceHello(name=name, kind=kind, capabilities=caps), ws)
    return session, ws


def test_desktop_is_preferred_and_headless_gui_capabilities_are_removed():
    async def run():
        hub = BackwireHub(BackwireConfig.with_tokens({"a": "x", "b": "y"}))
        _, headless_ws = await register(hub, "a", DeviceKind.headless, ["read", "screen", "input"])
        _, desktop_ws = await register(hub, "b", DeviceKind.desktop, ["read", "screen"])
        devices = await hub.list_devices()
        assert devices[0]["capabilities"] == ["read"]
        task = asyncio.create_task(hub.invoke("screenshot", {}))
        await asyncio.wait_for(desktop_ws.sent.wait(), timeout=2)
        assert len(desktop_ws.messages) == 1
        request_id = desktop_ws.messages[0]["id"]
        await hub.handle_result({"id": request_id, "result": {"ok": True}})
        assert await task == {"ok": True}
    asyncio.run(run())


def test_capability_missing_is_rejected_without_send():
    async def run():
        hub = BackwireHub(BackwireConfig.with_tokens({"a": "x"}))
        _, ws = await register(hub, "a", DeviceKind.headless, ["read"])
        with pytest.raises(PermissionError):
            await hub.invoke("run_bash", {"command": "id"})
        assert ws.messages == []
    asyncio.run(run())


def test_audit_redacts_sensitive_values(tmp_path):
    async def run():
        audit = tmp_path / "audit.jsonl"
        config = BackwireConfig.with_tokens({"a": "x"}, audit_log_path=str(audit))
        hub = BackwireHub(config)
        await register(hub, "a", DeviceKind.headless, ["read"])
        hub._audit("test", value="Bearer supersecret token=abc secret=def password=ghi api_key=jkl")
        text = audit.read_text()
        assert "supersecret" not in text
        assert "abc" not in text
        assert "[REDACTED]" in text
    asyncio.run(run())


def test_audit_redacts_dict_secrets_write_content_and_rejects_symlink(tmp_path):
    async def run():
        audit = tmp_path / "audit.jsonl"
        config = BackwireConfig.with_tokens({"a": "x"}, audit_log_path=str(audit))
        hub = BackwireHub(config)
        _, ws = await register(hub, "a", DeviceKind.headless, ["read", "write"])

        hub._audit(
            "secret_check",
            meta={"password": "my-db-password", "api_key": "sk-secret-key", "nested": {"token": "tok-123"}},
        )
        task = asyncio.create_task(
            hub.invoke("write_file", {"path": "note.txt", "content": "TOP_SECRET_BODY_CONTENT"}, device="a")
        )
        await asyncio.wait_for(ws.sent.wait(), timeout=2)
        request_id = ws.messages[0]["id"]
        await hub.handle_result({"id": request_id, "result": {"written": True}})
        await task

        text = audit.read_text(encoding="utf-8")
        assert "my-db-password" not in text
        assert "sk-secret-key" not in text
        assert "tok-123" not in text
        assert "TOP_SECRET_BODY_CONTENT" not in text
        assert "<23 chars>" in text

    asyncio.run(run())

    real_file = tmp_path / "real.jsonl"
    real_file.write_text("", encoding="utf-8")
    link = tmp_path / "audit-link.jsonl"
    link.symlink_to(real_file)
    hub_with_link = BackwireHub(BackwireConfig.with_tokens({"a": "x"}, audit_log_path=str(link)))
    with pytest.raises(ValueError, match="symbolic link"):
        hub_with_link._audit("connect", device="a")


def test_hub_pending_limits_are_enforced():
    async def run():
        config = BackwireConfig.with_tokens(
            {"a": "x", "b": "y"},
            max_pending_per_device=1,
            max_pending_total=2,
        )
        hub = BackwireHub(config)
        _, ws_a = await register(hub, "a", DeviceKind.headless, ["read"])
        _, ws_b = await register(hub, "b", DeviceKind.headless, ["read"])

        t1 = asyncio.create_task(hub.invoke("read_file", {"path": "1.txt"}, device="a", timeout=5))
        await asyncio.wait_for(ws_a.sent.wait(), timeout=2)
        with pytest.raises(RuntimeError, match="device pending request limit"):
            await hub.invoke("read_file", {"path": "2.txt"}, device="a", timeout=5)

        t2 = asyncio.create_task(hub.invoke("read_file", {"path": "3.txt"}, device="b", timeout=5))
        await asyncio.wait_for(ws_b.sent.wait(), timeout=2)
        # Now total pending == 2 (limit reached)
        _, _ = await register(hub, "c", DeviceKind.headless, ["read"])
        with pytest.raises(RuntimeError, match="pending request limit"):
            await hub.invoke("read_file", {"path": "4.txt"}, device="c", timeout=5)

        await hub.handle_result({"id": ws_a.messages[0]["id"], "result": {"content": "ok"}})
        await hub.handle_result({"id": ws_b.messages[0]["id"], "result": {"content": "ok"}})
        assert await t1 == {"content": "ok"}
        assert await t2 == {"content": "ok"}

    asyncio.run(run())


def test_hub_read_file_rejects_invalid_pagination_args():
    async def run():
        hub = BackwireHub(BackwireConfig.with_tokens({"a": "x"}))
        _, ws = await register(hub, "a", DeviceKind.headless, ["read"])
        with pytest.raises(ValueError, match="offset"):
            await hub.invoke("read_file", {"path": "a.txt", "offset": -1}, device="a")
        with pytest.raises(ValueError, match="max_chars"):
            await hub.invoke("read_file", {"path": "a.txt", "max_chars": 0}, device="a")
        assert ws.messages == []

    asyncio.run(run())

