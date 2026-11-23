import hmac
import os

from fastapi import Depends, FastAPI, Header, HTTPException

from backwire import BackwireConfig, BackwireHub, create_ws_router


def _required_token(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.startswith("REPLACE_WITH_"):
        raise RuntimeError(f"{name} must be set to a non-placeholder secret before startup")
    return value


DEVICE_TOKEN = _required_token("BACKWIRE_DEVICE_TOKEN")
APP_TOKEN = _required_token("BACKWIRE_APP_TOKEN")
config = BackwireConfig.with_tokens({"my-device": DEVICE_TOKEN})
hub = BackwireHub(config)
app = FastAPI()
app.include_router(create_ws_router(hub, config))


async def require_app_token(authorization: str | None = Header(default=None)) -> None:
    """Example application-layer auth; replace with the app's auth system."""
    if not authorization or not hmac.compare_digest(authorization, f"Bearer {APP_TOKEN}"):
        raise HTTPException(status_code=401, detail="authentication required")


@app.get("/devices", dependencies=[Depends(require_app_token)])
async def devices():
    return await hub.list_devices()


@app.post("/run", dependencies=[Depends(require_app_token)])
async def run(command: str, device: str):
    # Production applications should prefer a command allowlist; arbitrary shell is dangerous.
    return await hub.invoke("run_bash", {"command": command}, device=device)
