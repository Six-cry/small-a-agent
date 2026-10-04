"""Read-only, bounded flight and hotel searches via the official FlyAI CLI."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations


server = MCPServer("small-a-flyai-travel", description="Read-only Fliggy flight and hotel candidates; no booking or payment.")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)
RUNTIME = Path(__file__).resolve().parents[1] / "mcp" / "flyai_cli" / "node_modules"
CLI = RUNTIME / "@fly-ai" / "flyai-cli" / "dist" / "flyai-bundle.cjs"
SORTS = {"distance_asc", "rate_desc", "price_asc", "price_desc", "no_rank"}
MAX_STDOUT = 1_000_000


class FlyAILookupError(ToolError):
    """A provider failure safe to show to the agent."""


def _short(value: object, size: int = 120) -> str:
    return str(value or "").strip()[:size]


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2000:
        return None
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "fliggy.com" or host.endswith(".fliggy.com") or host == "feizhu.com" or host.endswith(".feizhu.com")):
        return None
    return value


def _date(value: str, label: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise FlyAILookupError(f"{label} must use YYYY-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise FlyAILookupError(f"{label} is not a valid date.") from None


def _run_cli(command_name: str, args: list[str]) -> dict:
    if command_name not in {"search-hotel", "search-flight"}:
        raise FlyAILookupError("Unsupported FlyAI command.")
    if not CLI.is_file():
        raise FlyAILookupError("FlyAI runtime is missing. Run npm ci in aa_my_agent/mcp/flyai_cli first.")
    local_node = RUNTIME / "node" / "bin" / ("node.exe" if os.name == "nt" else "node")
    if not local_node.is_file():
        raise FlyAILookupError("Pinned FlyAI Node runtime is missing. Run npm ci in aa_my_agent/mcp/flyai_cli first.")
    allowed = ("PATH", "SystemRoot", "WINDIR", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME", "TEMP", "TMP", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "NODE_EXTRA_CA_CERTS", "FLYAI_API_KEY")
    child_env = {key: os.environ[key] for key in allowed if key in os.environ}
    try:
        completed = subprocess.run(
            [str(local_node), str(CLI), command_name, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise FlyAILookupError("FlyAI search timed out; no result was returned.") from None
    except OSError:
        raise FlyAILookupError("FlyAI runtime could not start.") from None
    if completed.returncode != 0:
        raise FlyAILookupError("FlyAI search failed; do not treat this as zero available results.")
    if len(completed.stdout) > MAX_STDOUT:
        raise FlyAILookupError("FlyAI response was unexpectedly large.")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise FlyAILookupError("FlyAI response was not valid JSON.") from None
    if not isinstance(payload, dict) or payload.get("status") != 0:
        raise FlyAILookupError("FlyAI provider rejected this search; verify dates, route and API key.")
    if not isinstance(payload.get("data"), dict) or not isinstance(payload["data"].get("itemList"), list):
        raise FlyAILookupError("FlyAI response did not contain a result list.")
    return payload


@server.tool(
    description=(
        "Search domestic Fliggy/FlyAI hotel candidates by destination and stay dates. "
        "Returns compact hotel names, indicative prices and official detail links only. "
        "Trial-mode prices can be masked; open the detail link to verify current room price, "
        "availability and policies. Search only: never books or pays."
    ),
    annotations=READ_ONLY,
)
def search_domestic_hotels(
    destination: str,
    check_in_date: str,
    check_out_date: str,
    poi_name: str | None = None,
    keywords: str | None = None,
    max_price_cny: int | None = None,
    sort: str = "no_rank",
    limit: int = 5,
) -> dict:
    if not isinstance(destination, str) or not destination.strip() or len(destination) > 80 or destination.startswith("-"):
        raise FlyAILookupError("Destination must be a city or district name (1–80 characters).")
    start, end = _date(check_in_date, "check_in_date"), _date(check_out_date, "check_out_date")
    if start < date.today() or end <= start or (end - start).days > 30:
        raise FlyAILookupError("Stay dates must be future dates with 1–30 nights.")
    if sort not in SORTS:
        raise FlyAILookupError("Unsupported hotel sort order.")
    if type(limit) is not int or not 1 <= limit <= 10:
        raise FlyAILookupError("limit must be 1–10.")
    if max_price_cny is not None and (type(max_price_cny) is not int or not 1 <= max_price_cny <= 100_000):
        raise FlyAILookupError("max_price_cny must be 1–100000.")
    args = ["--dest-name", destination.strip(), "--check-in-date", check_in_date, "--check-out-date", check_out_date, "--sort", sort]
    for flag, value in (("--poi-name", poi_name), ("--key-words", keywords)):
        if value is not None:
            if not isinstance(value, str) or not value.strip() or len(value) > 100 or value.startswith("-"):
                raise FlyAILookupError(f"Invalid {flag} value.")
            args.extend([flag, value.strip()])
    if max_price_cny is not None:
        args.extend(["--max-price", str(max_price_cny)])
    items = _run_cli("search-hotel", args)["data"]["itemList"]
    hotels = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        price = _short(item.get("price"), 40) or None
        hotels.append({
            "name": _short(item.get("name")),
            "address": _short(item.get("address"), 180),
            "star": _short(item.get("star"), 30) or None,
            "price_display": price,
            "price_masked": bool(price and ("x" in price.lower() or "*" in price)),
            "nearby": _short(item.get("interestsPoi"), 100) or None,
            "hotel_id": _short(item.get("shId"), 40) or None,
            "detail_url": _safe_url(item.get("detailUrl")),
        })
    return {
        "source": "Fliggy FlyAI",
        "mode": "api_key" if os.environ.get("FLYAI_API_KEY") else "trial",
        "destination": destination.strip(),
        "check_in_date": check_in_date,
        "check_out_date": check_out_date,
        "result_count": len(items),
        "hotels": hotels,
        "price_is_final": False,
        "note": "Indicative search results only. Check current room availability, exact price and cancellation terms on the official detail page before booking.",
    }


def _flight_segment(item: dict) -> dict:
    return {
        "airline": _short(item.get("marketingTransportName"), 60) or None,
        "flight_number": _short(item.get("marketingTransportNo"), 30) or None,
        "departure_airport": _short(item.get("depStationName"), 80) or None,
        "departure_airport_code": _short(item.get("depStationCode"), 10) or None,
        "departure_terminal": _short(item.get("depTerm"), 20) or None,
        "departure_time": _short(item.get("depDateTime"), 40) or None,
        "arrival_airport": _short(item.get("arrStationName"), 80) or None,
        "arrival_airport_code": _short(item.get("arrStationCode"), 10) or None,
        "arrival_terminal": _short(item.get("arrTerm"), 20) or None,
        "arrival_time": _short(item.get("arrDateTime"), 40) or None,
        "cabin": _short(item.get("seatClassName"), 40) or None,
    }


@server.tool(
    description=(
        "Search Fliggy/FlyAI flight candidates by origin, destination and exact departure date. "
        "Returns compact indicative adult prices, airline/flight times and official links. "
        "Never claim a fare is final or seats are available without checking the booking page. "
        "Search only: never books or pays."
    ),
    annotations=READ_ONLY,
)
def search_flight_offers(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: str | None = None,
    direct_only: bool = False,
    max_price_cny: int | None = None,
    limit: int = 5,
) -> dict:
    for label, value in (("origin", origin), ("destination", destination)):
        if not isinstance(value, str) or not value.strip() or len(value) > 80 or value.startswith("-"):
            raise FlyAILookupError(f"{label} must be a city or airport name (1–80 characters).")
    if origin.strip().casefold() == destination.strip().casefold():
        raise FlyAILookupError("Origin and destination must differ.")
    outbound = _date(departure_date, "departure_date")
    if outbound < date.today():
        raise FlyAILookupError("departure_date cannot be in the past.")
    if return_date is not None:
        inbound = _date(return_date, "return_date")
        if inbound < outbound or (inbound - outbound).days > 365:
            raise FlyAILookupError("return_date must be on or after departure_date and within 365 days.")
    if type(direct_only) is not bool:
        raise FlyAILookupError("direct_only must be true or false.")
    if type(limit) is not int or not 1 <= limit <= 10:
        raise FlyAILookupError("limit must be 1–10.")
    if max_price_cny is not None and (type(max_price_cny) is not int or not 1 <= max_price_cny <= 100_000):
        raise FlyAILookupError("max_price_cny must be 1–100000.")
    args = ["--origin", origin.strip(), "--destination", destination.strip(), "--dep-date", departure_date, "--sort-type", "3"]
    if return_date is not None:
        args.extend(["--back-date", return_date])
    if direct_only:
        args.extend(["--journey-type", "1"])
    if max_price_cny is not None:
        args.extend(["--max-price", str(max_price_cny)])
    items = _run_cli("search-flight", args)["data"]["itemList"]
    flights = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        price = _short(item.get("adultPrice"), 40) or None
        journeys = []
        for journey in item.get("journeys", [])[:2] if isinstance(item.get("journeys"), list) else []:
            if not isinstance(journey, dict):
                continue
            segments = journey.get("segments")
            journeys.append({
                "type": _short(journey.get("journeyType"), 30) or None,
                "duration": _short(journey.get("totalDuration"), 40) or None,
                "segments": [_flight_segment(segment) for segment in segments[:4] if isinstance(segment, dict)] if isinstance(segments, list) else [],
            })
        flights.append({
            "adult_price_display": price,
            "price_masked": bool(price and ("x" in price.lower() or "*" in price)),
            "journeys": journeys,
            "detail_url": _safe_url(item.get("jumpUrl")),
        })
    return {
        "source": "Fliggy FlyAI",
        "mode": "api_key" if os.environ.get("FLYAI_API_KEY") else "trial",
        "origin": origin.strip(),
        "destination": destination.strip(),
        "departure_date": departure_date,
        "return_date": return_date,
        "result_count": len(items),
        "flights": flights,
        "price_is_final": False,
        "note": "Search candidates only. Recheck fare, taxes, baggage, seat availability and schedule on the official detail page before booking.",
    }


if __name__ == "__main__":
    server.run("stdio")
