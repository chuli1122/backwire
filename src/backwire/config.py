"""Configuration loaded from explicit values and BACKWIRE_* environment variables."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
from typing import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _trusted_proxy_matches(address: str, trusted_proxies: Sequence[str]) -> bool:
    for proxy in trusted_proxies:
        if address == proxy:
            return True
        try:
            if ipaddress.ip_address(address) in ipaddress.ip_network(proxy, strict=False):
                return True
        except ValueError:
            continue
    return False


def _valid_ip(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def resolve_client_ip(
    direct_host: str,
    headers: Mapping[str, str],
    trusted_proxies: Sequence[str],
) -> str:
    """Resolve the client IP only when the direct peer is a trusted proxy."""
    if not _trusted_proxy_matches(direct_host, trusted_proxies):
        return direct_host

    real_ip = _valid_ip(headers.get("x-real-ip"))
    if real_ip is not None:
        return real_ip

    forwarded = headers.get("x-forwarded-for", "")
    for candidate in reversed(forwarded.split(",")):
        client_ip = _valid_ip(candidate)
        if client_ip is not None and not _trusted_proxy_matches(client_ip, trusted_proxies):
            return client_ip
    return direct_host


class BackwireConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    tokens: dict[str, str] = Field(default_factory=dict)
    mcp_tokens: list[str] = Field(default_factory=list)
    mcp_allowed_hosts: list[str] = Field(default_factory=list)
    trusted_proxies: list[str] = Field(default_factory=list)
    allow_query_token: bool = False
    max_message_bytes: int = 8 * 1024 * 1024
    command_timeout_seconds: float = 65.0
    max_auth_failures_per_minute: int = 12
    max_pending_per_device: int = 32
    max_pending_total: int = 128
    max_tracked_peers: int = 4096
    audit_log_path: str | None = None
    default_device: str | None = None
    prefer_desktop_default: bool = True
    ws_path: str = "/ws/terminal"

    @field_validator("max_message_bytes", "max_auth_failures_per_minute", "max_pending_per_device", "max_pending_total", "max_tracked_peers")
    @classmethod
    def positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be positive")
        return value

    @field_validator("command_timeout_seconds")
    @classmethod
    def positive_timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0 or value > 300:
            raise ValueError("must be finite, positive, and no greater than 300 seconds")
        return value

    @field_validator("tokens")
    @classmethod
    def validate_tokens(cls, value: dict[str, str]) -> dict[str, str]:
        for name, token in value.items():
            if not token:
                raise ValueError(f"token for {name!r} must not be empty")
            if name == "*":
                raise ValueError("wildcard token name '*' is not allowed; configure one token per device")
            from .protocol import validate_device_name
            if not validate_device_name(name):
                raise ValueError(f"invalid token device name: {name!r}")
            if token.startswith("sha256:"):
                digest = token[7:]
                if len(digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in digest):
                    raise ValueError("sha256 token must contain 64 hexadecimal characters")
        return value

    @field_validator("mcp_tokens")
    @classmethod
    def validate_mcp_tokens(cls, value: list[str]) -> list[str]:
        for token in value:
            if not token:
                raise ValueError("MCP token must not be empty")
            if token.startswith("sha256:"):
                digest = token[7:]
                if len(digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in digest):
                    raise ValueError("sha256 token must contain 64 hexadecimal characters")
        return value

    @field_validator("default_device")
    @classmethod
    def validate_default_device(cls, value: str | None) -> str | None:
        if value is not None:
            from .protocol import validate_device_name
            if not validate_device_name(value):
                raise ValueError("invalid default device name")
        return value

    @classmethod
    def from_env(cls, **overrides: object) -> "BackwireConfig":
        values: dict[str, object] = {}
        mcp_token = os.getenv("BACKWIRE_MCP_TOKEN")
        mcp_tokens_json = os.getenv("BACKWIRE_MCP_TOKENS_JSON")
        if mcp_token is not None:
            values["mcp_tokens"] = [mcp_token]
        elif mcp_tokens_json is not None:
            parsed_mcp = json.loads(mcp_tokens_json)
            if not isinstance(parsed_mcp, list):
                raise ValueError("BACKWIRE_MCP_TOKENS_JSON must be an array")
            if not all(isinstance(token, str) for token in parsed_mcp):
                raise ValueError("BACKWIRE_MCP_TOKENS_JSON must contain strings")
            values["mcp_tokens"] = parsed_mcp

        allowed_hosts_raw = os.getenv("BACKWIRE_MCP_ALLOWED_HOSTS", "").strip()
        if allowed_hosts_raw:
            values["mcp_allowed_hosts"] = [
                h.strip() for h in allowed_hosts_raw.split(",") if h.strip()
            ]

        trusted_proxies_raw = os.getenv("BACKWIRE_TRUSTED_PROXIES", "").strip()
        if trusted_proxies_raw:
            values["trusted_proxies"] = [
                proxy.strip() for proxy in trusted_proxies_raw.split(",") if proxy.strip()
            ]

        raw = os.getenv("BACKWIRE_TOKENS_JSON")
        path = os.getenv("BACKWIRE_TOKENS_FILE")
        if raw:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("BACKWIRE_TOKENS_JSON must be an object")
            values["tokens"] = {str(k): str(v) for k, v in parsed.items()}
        elif path:
            parsed = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("BACKWIRE_TOKENS_FILE must contain an object")
            values["tokens"] = {str(k): str(v) for k, v in parsed.items()}
        env_map: dict[str, tuple[str, type]] = {
            "BACKWIRE_ALLOW_QUERY_TOKEN": ("allow_query_token", bool),
            "BACKWIRE_MAX_MESSAGE_BYTES": ("max_message_bytes", int),
            "BACKWIRE_COMMAND_TIMEOUT_SECONDS": ("command_timeout_seconds", float),
            "BACKWIRE_MAX_AUTH_FAILURES_PER_MINUTE": ("max_auth_failures_per_minute", int),
            "BACKWIRE_AUDIT_LOG_PATH": ("audit_log_path", str),
            "BACKWIRE_DEFAULT_DEVICE": ("default_device", str),
            "BACKWIRE_PREFER_DESKTOP_DEFAULT": ("prefer_desktop_default", bool),
        }
        for env_name, (field, converter) in env_map.items():
            if env_name not in os.environ:
                continue
            raw_value = os.environ[env_name]
            if converter is bool:
                values[field] = raw_value.strip().lower() in {"1", "true", "yes", "on"}
            else:
                values[field] = converter(raw_value)
        values.update(overrides)
        return cls(**values)

    def token_for(self, device_name: str) -> str | None:
        return self.tokens.get(device_name)

    def authenticate_mcp(self, presented: str | None) -> bool:
        if presented is None or not self.mcp_tokens:
            return False
        for expected in self.mcp_tokens:
            if expected.startswith("sha256:"):
                try:
                    actual = hashlib.sha256(presented.encode("utf-8")).hexdigest()
                except UnicodeEncodeError:
                    continue
                if hmac.compare_digest(actual, expected[7:].lower()):
                    return True
            elif hmac.compare_digest(presented, expected):
                return True
        return False

    def authenticate(self, device_name: str, presented: str | None) -> bool:
        expected = self.token_for(device_name)
        if not expected or presented is None:
            return False
        if expected.startswith("sha256:"):
            try:
                actual = hashlib.sha256(presented.encode("utf-8")).hexdigest()
            except UnicodeEncodeError:
                return False
            return hmac.compare_digest(actual, expected[7:].lower())
        return hmac.compare_digest(presented, expected)

    @classmethod
    def with_tokens(cls, tokens: Mapping[str, str], **kwargs: object) -> "BackwireConfig":
        return cls(tokens=dict(tokens), **kwargs)
