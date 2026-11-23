import asyncio
import importlib.util
import math
import os
import sys
import time
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("backwire_client", "client/backwire_client.py")
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


def test_default_client_policy_is_read_only():
    policy = client.ClientPolicy()
    assert policy.capabilities == {"read"}
    with pytest.raises(PermissionError):
        policy.authorize("run_bash")
    with pytest.raises(PermissionError):
        policy.authorize("write_file")


def test_desktop_and_explicit_capabilities():
    policy = client.ClientPolicy("desktop", allow_shell=True, allow_write=True, allow_input=True)
    assert policy.capabilities == {"read", "screen", "shell", "write", "input"}
    for tool in ("run_bash", "write_file", "mouse_click"):
        policy.authorize(tool)


def test_workdir_sandbox_blocks_escape(tmp_path):
    policy = client.ClientPolicy(workdir=str(tmp_path))
    assert policy.safe_path("inside.txt").parent == tmp_path
    with pytest.raises(PermissionError):
        policy.safe_path("../outside.txt")
    with pytest.raises(PermissionError):
        policy.safe_path("/tmp/outside.txt")


def test_symlink_escape_is_blocked(tmp_path):
    outside = tmp_path.parent / "outside-backwire-test"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    policy = client.ClientPolicy(workdir=str(tmp_path))
    with pytest.raises(PermissionError):
        policy.safe_path("link/file.txt")


def test_read_file_pagination_and_utf8_boundaries(tmp_path):
    text = "alpha-0123456789-中文测试-🚀-end"
    target = tmp_path / "sample.txt"
    target.write_text(text, encoding="utf-8")
    size_bytes = len(text.encode("utf-8"))

    dev = client.DeviceClient(
        "wss://example.com/ws/terminal",
        "token",
        "dev",
        client.ClientPolicy(workdir=str(tmp_path)),
    )

    chunks = []
    offset = 0
    for _ in range(20):
        res = asyncio.run(dev.execute("read_file", {"path": "sample.txt", "max_chars": 5, "offset": offset}))
        assert res["offset"] == offset
        assert res["size_bytes"] == size_bytes
        chunks.append(res["content"])
        offset = res["next_offset"]
        if not res["truncated"]:
            break
    else:
        raise AssertionError("pagination did not terminate")

    assert "".join(chunks) == text
    assert offset == size_bytes

    past_eof = asyncio.run(dev.execute("read_file", {"path": "sample.txt", "offset": size_bytes + 10}))
    assert past_eof["content"] == ""
    assert past_eof["truncated"] is False
    assert past_eof["next_offset"] == size_bytes + 10
    assert past_eof["size_bytes"] == size_bytes


def test_read_file_rejects_invalid_offset_and_non_regular_files(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("hello", encoding="utf-8")
    dev = client.DeviceClient(
        "wss://example.com/ws/terminal",
        "token",
        "dev",
        client.ClientPolicy(workdir=str(tmp_path)),
    )

    for bad_offset in (-1, -100, True, False, 1.5, "0"):
        with pytest.raises(ValueError, match="offset"):
            asyncio.run(dev.execute("read_file", {"path": "sample.txt", "offset": bad_offset}))

    for bad_max in (0, -5, True, client.MAX_FILE_CHARS + 1):
        with pytest.raises(ValueError, match="max_chars"):
            asyncio.run(dev.execute("read_file", {"path": "sample.txt", "max_chars": bad_max}))

    with pytest.raises(ValueError, match="regular file"):
        asyncio.run(dev.execute("read_file", {"path": str(tmp_path)}))


@pytest.mark.skipif(os.name == "nt", reason="POSIX special files only")
def test_read_file_rejects_dev_zero():
    dev = client.DeviceClient(
        "wss://example.com/ws/terminal",
        "token",
        "dev",
        client.ClientPolicy(workdir=None),
    )
    with pytest.raises(ValueError, match="regular file"):
        asyncio.run(dev.execute("read_file", {"path": "/dev/zero"}))


def test_run_bash_rejects_invalid_timeouts(tmp_path):
    dev = client.DeviceClient(
        "wss://example.com/ws/terminal",
        "token",
        "dev",
        client.ClientPolicy(workdir=str(tmp_path), allow_shell=True),
    )
    for bad in (0, -1, -0.1, math.nan, math.inf, -math.inf, 300.1, 301, True, "bad"):
        with pytest.raises(ValueError, match="timeout"):
            asyncio.run(dev.execute("run_bash", {"command": "echo hi", "timeout": bad}))


def test_run_bash_truncates_large_output_and_terminates(tmp_path):
    dev = client.DeviceClient(
        "wss://example.com/ws/terminal",
        "token",
        "dev",
        client.ClientPolicy(workdir=str(tmp_path), allow_shell=True),
    )
    cmd = f'"{sys.executable}" -c "import sys; sys.stdout.write(\'x\' * (512 * 1024)); sys.stdout.flush(); import time; time.sleep(10)"'
    started = time.monotonic()
    res = asyncio.run(dev.execute("run_bash", {"command": cmd, "timeout": 10}))
    elapsed = time.monotonic() - started
    assert res.get("truncated") is True
    assert "exceeded limit" in res.get("error", "")
    assert len(res["stdout"].encode("utf-8")) <= client.MAX_COMMAND_OUTPUT
    assert elapsed < 5.0


def test_run_bash_timeout_kills_subprocess(tmp_path):
    dev = client.DeviceClient(
        "wss://example.com/ws/terminal",
        "token",
        "dev",
        client.ClientPolicy(workdir=str(tmp_path), allow_shell=True),
    )
    pid_file = tmp_path / "child.pid"
    cmd = (
        f'"{sys.executable}" -c '
        f'"import os, time; open({str(pid_file)!r}, \'w\').write(str(os.getpid())); time.sleep(30)"'
    )
    started = time.monotonic()
    res = asyncio.run(dev.execute("run_bash", {"command": cmd, "timeout": 0.4}))
    elapsed = time.monotonic() - started
    assert res.get("error") == "command timed out"
    assert elapsed < 4.0
    if os.name != "nt" and pid_file.exists():
        child_pid = int(pid_file.read_text().strip())
        # Give the OS a brief moment to reap the terminated process.
        time.sleep(0.05)
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)


def test_device_client_ws_url_validation(capsys):
    policy = client.ClientPolicy()
    with pytest.raises(ValueError, match="insecure ws://"):
        client.DeviceClient("ws://example.com/ws/terminal", "tok", "dev", policy)

    # Allowed by default: wss:// and loopback ws://
    assert client.DeviceClient("wss://example.com/ws/terminal", "tok", "dev", policy).url == "wss://example.com/ws/terminal"
    assert client.DeviceClient("ws://127.0.0.1:8000/ws/terminal", "tok", "dev", policy).url == "ws://127.0.0.1:8000/ws/terminal"
    assert client.DeviceClient("ws://localhost:8000/ws/terminal", "tok", "dev", policy).url == "ws://localhost:8000/ws/terminal"
    assert client.DeviceClient("ws://[::1]:8000/ws/terminal", "tok", "dev", policy).url == "ws://[::1]:8000/ws/terminal"

    # Explicit opt-in allows non-local ws:// with warning
    insecure = client.DeviceClient(
        "ws://example.com/ws/terminal", "tok", "dev", policy, allow_insecure_ws=True,
    )
    assert insecure.url == "ws://example.com/ws/terminal"
    captured = capsys.readouterr()
    assert "WARNING" in captured.out

