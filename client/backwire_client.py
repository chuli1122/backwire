#!/usr/bin/env python3
"""Single-file backwire device client; Python 3.9+ and websockets only."""
from __future__ import annotations

import argparse
import asyncio
import base64
import codecs
import io
import json
import math
import os
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse


MAX_FILE_CHARS = 200_000
MAX_FILE_BYTES = 512 * 1024
MAX_COMMAND_TIMEOUT = 300.0
MAX_COMMAND_OUTPUT = 200 * 1024


def validate_ws_url(url: str, *, allow_insecure_ws: bool = False) -> None:
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError("URL must include a host")
    if parsed.scheme == "wss":
        return
    if parsed.scheme == "ws" and (
        allow_insecure_ws or parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        return
    if parsed.scheme == "ws" and not allow_insecure_ws:
        raise ValueError("insecure ws:// is only allowed for localhost; use wss:// or --allow-insecure-ws")
    raise ValueError("URL must use wss:// (or a permitted local ws:// URL)")


def _validate_timeout(value: Any, *, name: str = "timeout") -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite positive number <= {MAX_COMMAND_TIMEOUT:g}")
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite positive number <= {MAX_COMMAND_TIMEOUT:g}") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_COMMAND_TIMEOUT:
        raise ValueError(f"{name} must be a finite positive number <= {MAX_COMMAND_TIMEOUT:g}")
    return timeout


def _validate_max_chars(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_FILE_CHARS:
        raise ValueError(f"max_chars must be a positive integer <= {MAX_FILE_CHARS}")
    return value


def _validate_offset(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("offset must be a non-negative integer")
    return value


def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            pass
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _run_limited_command(command: Any, cwd: str | None, timeout: float, shell_kwargs: dict[str, Any]) -> dict[str, Any]:
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **shell_kwargs,
        start_new_session=os.name != "nt",
    )
    output_lock = threading.Lock()
    terminate_lock = threading.Lock()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    output_limited = threading.Event()
    terminated = False

    def terminate_once() -> None:
        nonlocal terminated
        with terminate_lock:
            if terminated:
                return
            terminated = True
        _terminate_process(proc)

    def collect(name: str, stream: Any) -> None:
        nonlocal total
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                with output_lock:
                    remaining = MAX_COMMAND_OUTPUT - total
                    if remaining <= 0:
                        output_limited.set()
                    else:
                        output[name].extend(chunk[:remaining])
                        total += min(len(chunk), remaining)
                        if len(chunk) > remaining:
                            output_limited.set()
                if output_limited.is_set():
                    terminate_once()
                    return
        finally:
            stream.close()

    threads = [
        threading.Thread(target=collect, args=("stdout", proc.stdout), daemon=True),
        threading.Thread(target=collect, args=("stderr", proc.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    while proc.poll() is None:
        if output_limited.is_set():
            terminate_once()
            break
        if time.monotonic() >= deadline:
            timed_out = True
            terminate_once()
            break
        time.sleep(0.01)
    if proc.poll() is None:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _terminate_process(proc)
            proc.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=1)
    result = {
        "returncode": proc.returncode,
        "stdout": bytes(output["stdout"]).decode("utf-8", errors="replace"),
        "stderr": bytes(output["stderr"]).decode("utf-8", errors="replace"),
    }
    if timed_out:
        result["error"] = "command timed out"
    if output_limited.is_set():
        result["truncated"] = True
        result["error"] = "command output exceeded limit"
    return result


def _bytes_for_prefix_chars(data: bytes, max_chars: int) -> int:
    """Return the exact byte offset in `data` corresponding to the first `max_chars` decoded chars."""
    lo, hi = 0, len(data)
    while lo < hi:
        mid = (lo + hi) // 2
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        if len(dec.decode(data[:mid], final=False)) >= max_chars:
            hi = mid
        else:
            lo = mid + 1
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    if lo > 0 and len(dec.decode(data[:lo], final=False)) > max_chars:
        return lo - 1
    return lo


def _read_limited_text(path: Path, max_chars: int, offset: int = 0) -> tuple[str, bool, int, int]:
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("path is not a regular file")
    byte_limit = min(MAX_FILE_BYTES, max_chars * 4)
    chunks: list[bytes] = []
    total = 0
    with path.open("rb") as handle:
        st = os.fstat(handle.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("path is not a regular file")
        size_bytes = st.st_size
        if offset >= size_bytes:
            return "", False, offset, size_bytes
        handle.seek(offset)
        while total <= byte_limit:
            chunk = handle.read(min(64 * 1024, byte_limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    raw = b"".join(chunks)
    has_more_bytes = len(raw) > byte_limit
    window = raw[:byte_limit]
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    decoded = decoder.decode(window, final=not has_more_bytes)
    pending_len = len(decoder.getstate()[0]) if has_more_bytes else 0
    consumed = len(window) - pending_len
    if len(decoded) > max_chars:
        content = decoded[:max_chars]
        try:
            window[:consumed].decode("utf-8")
        except UnicodeDecodeError:
            consumed = _bytes_for_prefix_chars(window[:consumed], max_chars)
        else:
            consumed = len(content.encode("utf-8"))
        truncated = True
    else:
        content = decoded
        truncated = has_more_bytes or (offset + consumed < size_bytes)
    return content, truncated, offset + consumed, size_bytes


class ClientPolicy:
    def __init__(self, kind: str = "headless", workdir: Optional[str] = None, allow_shell: bool = False,
                 allow_write: bool = False, allow_input: bool = False) -> None:
        self.kind = kind
        self.workdir = Path(workdir).expanduser().resolve() if workdir else None
        self.allow_shell = allow_shell
        self.allow_write = allow_write
        self.allow_input = allow_input

    @property
    def capabilities(self) -> set[str]:
        result = {"read"}
        if self.kind == "desktop":
            result.add("screen")
        if self.allow_shell:
            result.add("shell")
        if self.allow_write:
            result.add("write")
        if self.allow_input:
            result.add("input")
        return result

    def safe_path(self, value: str) -> Path:
        if self.workdir is not None and not self.workdir.is_dir():
            raise FileNotFoundError(f"workdir does not exist or is not a directory: {self.workdir}")
        candidate = Path(value).expanduser()
        if self.workdir is None:
            return candidate.resolve()
        if not candidate.is_absolute():
            candidate = self.workdir / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.workdir)
        except ValueError as exc:
            raise PermissionError("path escapes workdir sandbox") from exc
        return resolved

    def authorize(self, tool: str) -> None:
        required = {"run_bash": "shell", "read_file": "read", "write_file": "write",
                    "screenshot": "screen", "mouse_click": "input", "keyboard_type": "input",
                    "hotkey": "input", "scroll": "input"}.get(tool)
        if required is None or required not in self.capabilities:
            raise PermissionError("capability is not enabled")


class DeviceClient:
    def __init__(self, url: str, token: str, name: str, policy: ClientPolicy, *, allow_insecure_ws: bool = False) -> None:
        validate_ws_url(url, allow_insecure_ws=allow_insecure_ws)
        if allow_insecure_ws and urlparse(url).scheme == "ws" and urlparse(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            print("WARNING: allowing insecure unencrypted ws://; device credentials and commands may be exposed", flush=True)
        self.url, self.token, self.name, self.policy = url, token, name, policy

    async def connect(self) -> Any:
        import websockets
        headers = {"Authorization": f"Bearer {self.token}", "X-Device-Name": self.name}
        try:
            return await websockets.connect(self.url, additional_headers=headers)
        except TypeError:
            return await websockets.connect(self.url, extra_headers=headers)

    async def execute(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.policy.authorize(tool)
        if tool == "run_bash":
            command = arguments.get("command")
            if not isinstance(command, str):
                raise ValueError("command must be a string")
            timeout = _validate_timeout(arguments.get("timeout", 65))
            if self.policy.workdir is not None and not self.policy.workdir.is_dir():
                return {"error": f"workdir does not exist or is not a directory: {self.policy.workdir}"}
            if os.name == "nt":
                shell_command: Any = command
                shell_kwargs = {"shell": True}
            else:
                bash = "/bin/bash" if Path("/bin/bash").is_file() else None
                shell_command = [bash, "-lc", command] if bash else command
                shell_kwargs = {"shell": False} if bash else {"shell": True}
            return await asyncio.to_thread(
                _run_limited_command, shell_command,
                str(self.policy.workdir) if self.policy.workdir else None, timeout, shell_kwargs,
            )
        if tool == "read_file":
            path = self.policy.safe_path(str(arguments["path"]))
            max_chars = _validate_max_chars(arguments.get("max_chars", 16000))
            offset = _validate_offset(arguments.get("offset", 0))
            content, truncated, next_offset, size_bytes = await asyncio.to_thread(
                _read_limited_text, path, max_chars, offset,
            )
            return {
                "path": str(path),
                "content": content,
                "offset": offset,
                "next_offset": next_offset,
                "truncated": truncated,
                "size_bytes": size_bytes,
            }
        if tool == "write_file":
            path = self.policy.safe_path(str(arguments["path"]))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(arguments.get("content", "")), encoding="utf-8")
            return {"path": str(path), "written": True}
        if tool == "screenshot":
            try:
                import pyautogui
            except ImportError as exc:
                raise RuntimeError("install backwire[desktop] for screenshot") from exc
            image = await asyncio.to_thread(pyautogui.screenshot)
            buffer = io.BytesIO()
            await asyncio.to_thread(image.save, buffer, format="PNG")
            return {
                "mime_type": "image/png",
                "width": image.width,
                "height": image.height,
                "image_base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
            }
        try:
            import pyautogui
        except ImportError as exc:
            raise RuntimeError("install backwire[desktop] for input control") from exc
        if tool == "mouse_click":
            await asyncio.to_thread(pyautogui.click, x=arguments.get("x"), y=arguments.get("y"), clicks=arguments.get("clicks", 1))
        elif tool == "keyboard_type":
            await asyncio.to_thread(pyautogui.write, str(arguments.get("text", "")), interval=0)
        elif tool == "hotkey":
            await asyncio.to_thread(pyautogui.hotkey, *[str(x) for x in arguments.get("keys", [])])
        elif tool == "scroll":
            await asyncio.to_thread(pyautogui.scroll, int(arguments.get("clicks", 0)))
        return {"ok": True}

    async def run(self) -> None:
        delay = 1.0
        while True:
            try:
                async with await self.connect() as websocket:
                    await websocket.send(json.dumps({"type": "hello", "name": self.name, "kind": self.policy.kind,
                                                      "capabilities": sorted(self.policy.capabilities)}))
                    delay = 1.0
                    async for raw in websocket:
                        if len(raw.encode("utf-8")) > 8 * 1024 * 1024:
                            await websocket.close(code=1009)
                            break
                        request: Any = None
                        try:
                            request = json.loads(raw)
                            result = await self.execute(request["tool"], request.get("arguments", {}))
                            response = {"id": request.get("id"), "result": result}
                        except Exception as exc:
                            response = {"id": request.get("id") if isinstance(request, dict) else None, "error": str(exc)}
                        await websocket.send(json.dumps(response, ensure_ascii=False))
            except Exception:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="backwire reverse WebSocket client")
    parser.add_argument("--url", default=os.getenv("BACKWIRE_URL"))
    parser.add_argument("--token", default=os.getenv("BACKWIRE_TOKEN"))
    parser.add_argument("--name", default=os.getenv("BACKWIRE_NAME"))
    parser.add_argument("--kind", choices=("headless", "desktop"), default=os.getenv("BACKWIRE_KIND", "headless"))
    parser.add_argument("--workdir", default=os.getenv("BACKWIRE_WORKDIR"))
    parser.add_argument("--allow-shell", action="store_true")
    parser.add_argument("--allow-write", action="store_true")
    parser.add_argument("--allow-input", action="store_true")
    parser.add_argument("--allow-insecure-ws", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.url or not args.token or not args.name:
        raise SystemExit("--url, --token and --name are required (or BACKWIRE_URL/TOKEN/NAME)")
    policy = ClientPolicy(args.kind, args.workdir, args.allow_shell, args.allow_write, args.allow_input)
    try:
        client = DeviceClient(args.url, args.token, args.name, policy, allow_insecure_ws=args.allow_insecure_ws)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    asyncio.run(client.run())


if __name__ == "__main__":
    main()
