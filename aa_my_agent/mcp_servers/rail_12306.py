"""Official 12306 handoff; this server does not fetch live ticket data."""

from __future__ import annotations

from datetime import date
import re

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations


OFFICIAL_TICKETS_URL = "https://kyfw.12306.cn/otn/leftTicket/init"
OFFICIAL_TIMETABLE_URL = "https://kyfw.12306.cn/otn/queryTrainInfo/init"

server = MCPServer(
    "small-a-rail-12306",
    description="Read-only guide to official Chinese railway lookup pages.",
)


@server.tool(
    description=(
        "Give the official 12306 ticket and train timetable lookup pages for "
        "a Chinese rail journey. This tool DOES NOT query train availability, "
        "fares, schedules, or book tickets; ask the user to verify them on 12306."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def get_12306_official_lookup(
    origin: str,
    destination: str,
    departure_date: str,
) -> dict[str, object]:
    origin = origin.strip()
    destination = destination.strip()
    if not origin or not destination:
        raise ValueError("origin and destination are required")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", departure_date):
        raise ValueError("departure_date must be YYYY-MM-DD")
    try:
        travel_date = date.fromisoformat(departure_date)
    except ValueError as exc:
        raise ValueError("departure_date must be YYYY-MM-DD") from exc
    if travel_date < date.today():
        raise ValueError("departure_date cannot be in the past")
    return {
        "provider": "中国铁路12306",
        "live_data": False,
        "query": {
            "origin": origin,
            "destination": destination,
            "departure_date": travel_date.isoformat(),
        },
        "official_ticket_lookup_url": OFFICIAL_TICKETS_URL,
        "official_timetable_lookup_url": OFFICIAL_TIMETABLE_URL,
        "instruction": (
            "请在12306官方页面输入上述条件并核实实时余票、票价和车次；"
            "本工具没有读取这些实时数据。"
        ),
    }


if __name__ == "__main__":
    server.run("stdio")
