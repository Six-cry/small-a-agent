"""Process-wide MCP runtime used by the tool registry and CLI."""

from __future__ import annotations

import atexit
import threading
from dataclasses import replace
from typing import Any, Callable

from .manager import MCPManager, MCP_TOOL_PREFIX
from .settings import load_mcp_settings


_manager: MCPManager | None = None
_manager_lock = threading.Lock()


def _get_manager() -> MCPManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            try:
                _manager = MCPManager(load_mcp_settings())
            except Exception as exc:
                # MCP is peripheral: a bad optional config must be visible but
                # must not prevent the existing built-in Agent from starting.
                fallback = replace(load_mcp_settings(enabled=False), enabled=True)
                _manager = MCPManager(
                    fallback,
                    initial_errors={
                        "config": f"{type(exc).__name__}: {exc}",
                    },
                )
        return _manager


def initialize_mcp() -> dict[str, Any]:
    manager = _get_manager()
    manager.start()
    return manager.status()


def get_mcp_tools() -> list[dict[str, Any]]:
    return _get_manager().tool_definitions()


def get_mcp_handler(name: str) -> Callable[..., str] | None:
    return _get_manager().handler_for(name)


def get_mcp_permission(name: str) -> str | None:
    return _get_manager().permission_for(name)


def is_mcp_tool(name: str) -> bool:
    return name.startswith(MCP_TOOL_PREFIX) and _get_manager().has_tool(name)


def shutdown_mcp() -> None:
    global _manager
    with _manager_lock:
        manager = _manager
        _manager = None
    if manager is not None:
        manager.close()


atexit.register(shutdown_mcp)
