"""Command-line entry points."""
from __future__ import annotations

import argparse
import asyncio
import inspect
import os


def main_server() -> None:
    parser = argparse.ArgumentParser(prog="backwire-server")
    parser.add_argument("--host", default=os.getenv("BACKWIRE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("BACKWIRE_PORT", "8000")))
    parser.add_argument("--no-mcp", action="store_true")
    args = parser.parse_args()
    import uvicorn
    from .config import BackwireConfig
    from .server import create_app
    cfg = BackwireConfig.from_env()
    app = create_app(config=cfg, enable_mcp=not args.no_mcp)
    uvicorn.run(app, host=args.host, port=args.port, ws_max_size=cfg.max_message_bytes)


def main_mcp() -> None:
    parser = argparse.ArgumentParser(prog="backwire-mcp")
    parser.add_argument("--ws-host", default=os.getenv("BACKWIRE_WS_HOST", "127.0.0.1"))
    parser.add_argument("--ws-port", type=int, default=int(os.getenv("BACKWIRE_WS_PORT", "8765")))
    args = parser.parse_args()

    from .config import BackwireConfig
    from .hub import BackwireHub
    from .mcp_adapter import create_mcp_server
    from .router import create_ws_router
    from fastapi import FastAPI
    import uvicorn

    async def run() -> None:
        config = BackwireConfig.from_env()
        hub = BackwireHub(config)
        mcp_server = create_mcp_server(hub)
        ws_app = FastAPI()
        ws_app.include_router(create_ws_router(hub, config))
        ws_config = uvicorn.Config(
            ws_app, host=args.ws_host, port=args.ws_port, log_level="warning",
            ws_max_size=config.max_message_bytes,
        )
        ws_server = uvicorn.Server(ws_config)
        ws_task = asyncio.create_task(ws_server.serve())
        try:
            run_stdio = getattr(mcp_server, "run", None)
            if run_stdio is None:
                raise RuntimeError("installed MCP SDK does not expose stdio run")
            if inspect.iscoroutinefunction(run_stdio):
                await run_stdio(transport="stdio")
            else:
                await asyncio.to_thread(run_stdio, transport="stdio")
        finally:
            ws_server.should_exit = True
            await ws_task

    asyncio.run(run())


if __name__ == "__main__":
    main_server()
