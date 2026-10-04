"""Per-turn cache keys for safe, read-only MCP calls."""

from __future__ import annotations

import json
from typing import Any


def read_call_cache_key(
    tool_name: str,
    arguments: dict[str, Any],
    permission: str | None,
) -> str | None:
    """Return a stable key only for explicitly read-only MCP calls.

    The caller owns the cache lifetime. ``agent_loop`` creates one dictionary
    per user turn, so context compaction cannot erase it and no result can leak
    into a later turn where external data may have changed.
    """

    if not tool_name.startswith("mcp__") or permission != "read":
        return None
    try:
        canonical_arguments = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        # Model tool inputs should be JSON values. If a custom integration
        # violates that contract, skip caching instead of guessing identity.
        return None
    return f"{tool_name}\0{canonical_arguments}"
