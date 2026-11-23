"""Wire protocol types and capability policy."""
from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")


class DeviceKind(str, Enum):
    desktop = "desktop"
    headless = "headless"


class Capability(str, Enum):
    read = "read"
    write = "write"
    shell = "shell"
    screen = "screen"
    input = "input"


TOOL_REQUIRED_CAPABILITY: dict[str, Capability] = {
    "run_bash": Capability.shell,
    "read_file": Capability.read,
    "write_file": Capability.write,
    "screenshot": Capability.screen,
    "mouse_click": Capability.input,
    "keyboard_type": Capability.input,
    "hotkey": Capability.input,
    "scroll": Capability.input,
}

HEADLESS_ALLOWED_CAPABILITIES = frozenset({Capability.read, Capability.write, Capability.shell})


class DeviceHello(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "hello"
    name: str
    kind: DeviceKind
    capabilities: set[Capability] = Field(default_factory=set)

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        if value != "hello":
            raise ValueError("hello.type must be 'hello'")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not DEVICE_NAME_RE.fullmatch(value):
            raise ValueError("invalid device name")
        return value

    @field_validator("capabilities", mode="after")
    @classmethod
    def normalize_capabilities(cls, value: set[Capability]) -> set[Capability]:
        return set(value)


def validate_device_name(name: str) -> bool:
    return bool(DEVICE_NAME_RE.fullmatch(name))


def capability_for_tool(tool: str) -> Capability | None:
    return TOOL_REQUIRED_CAPABILITY.get(tool)


def public_capabilities(capabilities: set[Capability]) -> list[str]:
    return sorted(cap.value for cap in capabilities)
