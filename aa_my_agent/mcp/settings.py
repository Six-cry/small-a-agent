"""Validated, secret-free MCP server configuration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "servers.json"
_SERVER_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    transport: str
    enabled: bool = True
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: Path | None = None
    env_from: tuple[str, ...] = ()
    enable_when_env: tuple[str, ...] = ()
    url: str | None = None
    token_env: str | None = None
    include_tools: tuple[str, ...] = ()
    exclude_tools: tuple[str, ...] = ()
    read_only_tools: tuple[str, ...] = ()
    allow_shadowed_tools: bool = False


@dataclass(frozen=True)
class MCPSettings:
    enabled: bool
    config_path: Path
    servers: tuple[MCPServerConfig, ...]
    connect_timeout_seconds: float = 15.0
    tool_timeout_seconds: float = 30.0
    max_output_chars: int = 50_000


def _as_string_list(value: object, field: str, server: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"MCP server {server!r}: {field} must be a list of strings")
    return tuple(value)


def _validate_remote_url(url: str, server: str) -> str:
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        raise ValueError(f"MCP server {server!r}: credentials must not be embedded in URL")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"MCP server {server!r}: invalid Streamable HTTP URL")
    if parsed.scheme != "https" and parsed.hostname.casefold() not in _LOCAL_HOSTS:
        raise ValueError(f"MCP server {server!r}: remote MCP URL must use HTTPS")
    return url


def _parse_server(name: str, raw: object, config_dir: Path) -> MCPServerConfig:
    if not _SERVER_NAME.fullmatch(name):
        raise ValueError(f"Invalid MCP server name: {name!r}")
    if not isinstance(raw, dict):
        raise ValueError(f"MCP server {name!r}: configuration must be an object")

    transport = str(raw.get("transport", "")).strip().casefold().replace("-", "_")
    if transport not in {"stdio", "streamable_http"}:
        raise ValueError(
            f"MCP server {name!r}: transport must be stdio or streamable_http"
        )

    include_tools = _as_string_list(raw.get("include_tools"), "include_tools", name)
    exclude_tools = _as_string_list(raw.get("exclude_tools"), "exclude_tools", name)
    read_only_tools = _as_string_list(raw.get("read_only_tools"), "read_only_tools", name)
    env_from = _as_string_list(raw.get("env_from"), "env_from", name)
    enable_when_env = _as_string_list(
        raw.get("enable_when_env"), "enable_when_env", name
    )
    for env_name in (*env_from, *enable_when_env):
        if not _ENV_NAME.fullmatch(env_name):
            raise ValueError(f"MCP server {name!r}: invalid environment name {env_name!r}")

    common = dict(
        name=name,
        transport=transport,
        enabled=bool(raw.get("enabled", True)),
        include_tools=include_tools,
        exclude_tools=exclude_tools,
        read_only_tools=read_only_tools,
        allow_shadowed_tools=bool(raw.get("allow_shadowed_tools", False)),
        env_from=env_from,
        enable_when_env=enable_when_env,
    )

    if transport == "stdio":
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"MCP server {name!r}: stdio command is required")
        args = _as_string_list(raw.get("args"), "args", name)
        cwd_raw = raw.get("cwd")
        cwd = None
        if cwd_raw is not None:
            if not isinstance(cwd_raw, str) or not cwd_raw.strip():
                raise ValueError(f"MCP server {name!r}: cwd must be a non-empty string")
            candidate = Path(cwd_raw)
            cwd = (config_dir / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        return MCPServerConfig(command=command.strip(), args=args, cwd=cwd, **common)

    url_raw = raw.get("url")
    if not isinstance(url_raw, str):
        raise ValueError(f"MCP server {name!r}: URL is required")
    token_env = raw.get("token_env")
    if token_env is not None:
        if not isinstance(token_env, str) or not _ENV_NAME.fullmatch(token_env):
            raise ValueError(f"MCP server {name!r}: token_env must name an environment variable")
    return MCPServerConfig(
        url=_validate_remote_url(url_raw.strip(), name),
        token_env=token_env,
        **common,
    )


def load_mcp_settings(
    *,
    enabled: bool | None = None,
    config_path: str | Path | None = None,
) -> MCPSettings:
    """Load MCP settings. A missing file is an empty configuration, not a crash."""
    is_enabled = _env_bool("MCP_ENABLED") if enabled is None else enabled
    configured_path = config_path or os.getenv("MCP_CONFIG_PATH") or DEFAULT_CONFIG_PATH
    path = Path(configured_path)
    if not path.is_absolute():
        path = (APP_DIR / path).resolve()
    else:
        path = path.resolve()

    servers: list[MCPServerConfig] = []
    # A disabled optional subsystem must not break small-a merely because an
    # unused draft configuration is malformed.
    if is_enabled and path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_servers = payload.get("servers", {}) if isinstance(payload, dict) else None
        if not isinstance(raw_servers, dict):
            raise ValueError("MCP config field 'servers' must be an object")
        servers = [
            _parse_server(name, raw, path.parent)
            for name, raw in raw_servers.items()
        ]

    return MCPSettings(
        enabled=is_enabled,
        config_path=path,
        servers=tuple(servers),
        connect_timeout_seconds=_env_float("MCP_CONNECT_TIMEOUT_SECONDS", 15.0, 1.0),
        tool_timeout_seconds=_env_float("MCP_TOOL_TIMEOUT_SECONDS", 30.0, 1.0),
        max_output_chars=_env_int("MCP_MAX_OUTPUT_CHARS", 50_000, 1_000),
    )
