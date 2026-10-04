"""Minimal local server that proves small-a can discover and call MCP tools."""

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations


server = MCPServer(
    "small-a-travel-demo",
    description="Local read-only MCP connection check for small-a.",
)


@server.tool(
    description=(
        "Report whether the local travel MCP demo service is available. "
        "This is a connection test and does not query live travel data."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def get_travel_mcp_status() -> dict[str, object]:
    return {
        "connected": True,
        "service": "small-a-travel-demo",
        "live_travel_data": False,
        "message": "MCP discovery and tool execution are working.",
    }


if __name__ == "__main__":
    server.run("stdio")
