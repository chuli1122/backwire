"""Optional MCP adapter; importing backwire itself never requires mcp."""
from __future__ import annotations

import math
from typing import Any

from .hub import BackwireHub

MAX_TIMEOUT = 300.0
MAX_FILE_CHARS = 200_000


def _timeout(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("timeout must be a finite positive number <= 300")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a finite positive number <= 300") from exc
    if not math.isfinite(result) or result <= 0 or result > MAX_TIMEOUT:
        raise ValueError("timeout must be a finite positive number <= 300")
    return result


def _max_chars(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_FILE_CHARS:
        raise ValueError(f"max_chars must be a positive integer <= {MAX_FILE_CHARS}")
    return value


def _offset(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("offset must be a non-negative integer")
    return value

TOOL_NAMES = (
    "list_devices",
    "run_command",
    "read_file",
    "write_file",
    "screenshot",
    "mouse_click",
    "keyboard_type",
    "hotkey",
    "scroll",
)


def create_mcp_server(hub: BackwireHub) -> Any:
    """Create an MCP server compatible with both mcp v1 (FastMCP) and v2 (MCPServer)."""
    try:
        from mcp.server.mcpserver import MCPServer as _ServerCls
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as _ServerCls  # type: ignore[no-redef]
        except ImportError as exc:
            raise RuntimeError("MCP support is optional; install backwire[mcp]") from exc

    server = _ServerCls("backwire")
    if not callable(getattr(server, "tool", None)):
        raise RuntimeError("installed mcp SDK does not expose a supported .tool() decorator")

    @server.tool()
    async def list_devices() -> list[dict[str, Any]]:
        """List online reverse-connected devices, their kinds, and granted capabilities."""
        return await hub.list_devices()

    @server.tool()
    async def run_command(command: str, device: str | None = None, timeout: int = 60) -> Any:
        """Run a shell command on a connected device (requires --allow-shell on the client)."""
        checked_timeout = _timeout(timeout)
        return await hub.invoke(
            "run_bash",
            {"command": command, "timeout": checked_timeout},
            device=device,
            timeout=min(MAX_TIMEOUT, checked_timeout + 5.0),
        )

    @server.tool()
    async def read_file(
        path: str,
        device: str | None = None,
        max_chars: int = 16000,
        offset: int = 0,
    ) -> Any:
        """Read a UTF-8 text file on a connected device with optional byte-offset pagination."""
        checked_max_chars = _max_chars(max_chars)
        checked_offset = _offset(offset)
        return await hub.invoke(
            "read_file",
            {"path": path, "max_chars": checked_max_chars, "offset": checked_offset},
            device=device,
        )

    @server.tool()
    async def write_file(path: str, content: str, device: str | None = None) -> Any:
        """Write a UTF-8 text file on a connected device (requires --allow-write)."""
        return await hub.invoke(
            "write_file",
            {"path": path, "content": content},
            device=device,
        )

    @server.tool()
    async def screenshot(device: str | None = None) -> Any:
        """Capture a screenshot on a connected desktop device."""
        return await hub.invoke("screenshot", {}, device=device)

    @server.tool()
    async def mouse_click(
        x: int,
        y: int,
        button: str = "left",
        device: str | None = None,
    ) -> Any:
        """Click at screen coordinates on a connected desktop device (requires --allow-input)."""
        return await hub.invoke(
            "mouse_click",
            {"x": x, "y": y, "button": button},
            device=device,
        )

    @server.tool()
    async def keyboard_type(text: str, device: str | None = None) -> Any:
        """Type text on a connected desktop device (requires --allow-input)."""
        return await hub.invoke("keyboard_type", {"text": text}, device=device)

    @server.tool()
    async def hotkey(keys: list[str], device: str | None = None) -> Any:
        """Trigger a keyboard shortcut on a connected desktop device (requires --allow-input)."""
        return await hub.invoke("hotkey", {"keys": keys}, device=device)

    @server.tool()
    async def scroll(
        clicks: int = -3,
        x: int | None = None,
        y: int | None = None,
        device: str | None = None,
    ) -> Any:
        """Scroll vertically on a connected desktop device (requires --allow-input)."""
        return await hub.invoke(
            "scroll",
            {"clicks": clicks, "x": x, "y": y},
            device=device,
        )

    return server
