"""MCP client integration for small-a.

The public helpers intentionally expose only configured servers.  The model
cannot create arbitrary MCP connections at runtime.
"""

from .runtime import (
    get_mcp_handler,
    get_mcp_permission,
    get_mcp_tools,
    initialize_mcp,
    is_mcp_tool,
    shutdown_mcp,
)

__all__ = [
    "get_mcp_handler",
    "get_mcp_permission",
    "get_mcp_tools",
    "initialize_mcp",
    "is_mcp_tool",
    "shutdown_mcp",
]
