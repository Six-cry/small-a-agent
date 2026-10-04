"""Small-a's narrow Google Calendar MCP adapter.

The OAuth refresh token is supplied by the parent process through environment
variables. Only event listing and insertion are exposed to the agent.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations


TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: tuple[str, float, str] | None = None

server = MCPServer(
    "small-a-google-calendar",
    description="Read upcoming Google Calendar events and create confirmed events.",
)


def _credentials() -> tuple[str, str, str]:
    names = (
        "GOOGLE_CALENDAR_CLIENT_ID",
        "GOOGLE_CALENDAR_CLIENT_SECRET",
        "GOOGLE_CALENDAR_REFRESH_TOKEN",
    )
    values = tuple(os.getenv(name, "").strip() for name in names)
    missing = [name for name, value in zip(names, values) if not value]
    if missing:
        raise ToolError(
            "Google Calendar is not configured; set " + ", ".join(missing) + " in aa_my_agent/.env."
        )
    return values  # type: ignore[return-value]


def _http_json(
    method: str,
    url: str,
    *,
    data: dict[str, str] | None = None,
    json_body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    token_request: bool = False,
) -> dict[str, object]:
    try:
        response = requests.request(
            method, url, data=data, json=json_body, headers=headers, timeout=20
        )
    except requests.RequestException:
        raise ToolError("Google Calendar network request failed; check the connection.") from None
    if response.status_code >= 400:
        if token_request and response.status_code == 400:
            # Only inspect a documented error code. Never expose Google's full
            # response, URL, request body, or a potentially sensitive description.
            try:
                error_code = response.json().get("error")
            except (ValueError, AttributeError):
                error_code = None
            if error_code == "invalid_grant":
                raise ToolError(
                    "Google Calendar authorization expired or was revoked; run "
                    "python -m aa_my_agent.mcp_servers.google_calendar_auth --authorize again."
                ) from None
            if error_code == "invalid_client":
                raise ToolError("Google Calendar OAuth client ID or secret was rejected.") from None
        raise ToolError(f"Google Calendar request failed (HTTP {response.status_code}).") from None
    try:
        payload = response.json()
    except ValueError:
        raise ToolError("Google Calendar returned invalid JSON.") from None
    if not isinstance(payload, dict):
        raise ToolError("Google Calendar returned an invalid response.")
    return payload


def _access_token() -> str:
    global _TOKEN_CACHE
    client_id, client_secret, refresh_token = _credentials()
    fingerprint = hashlib.sha256(
        f"{client_id}\0{client_secret}\0{refresh_token}".encode("utf-8")
    ).hexdigest()
    with _TOKEN_LOCK:
        if _TOKEN_CACHE and _TOKEN_CACHE[2] == fingerprint and _TOKEN_CACHE[1] > time.monotonic():
            return _TOKEN_CACHE[0]
        payload = _http_json(
            "POST",
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            token_request=True,
        )
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise ToolError("Google Calendar token response did not contain an access token.")
        expires_in = payload.get("expires_in", 3600)
        lifetime = int(expires_in) if isinstance(expires_in, (int, float)) else 3600
        _TOKEN_CACHE = (token, time.monotonic() + max(0, lifetime - 60), fingerprint)
        return token


def _calendar_url(calendar_id: str) -> str:
    if not isinstance(calendar_id, str) or not calendar_id.strip() or len(calendar_id) > 256:
        raise ToolError("calendar_id must be a non-empty calendar identifier.")
    return EVENTS_URL.format(calendar_id=quote(calendar_id.strip(), safe=""))


def _parse_timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        raise ToolError(f"{field} must be an RFC3339 timestamp with a UTC offset, e.g. 2026-10-01T09:00:00+08:00.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(f"{field} is not a valid date and time.") from None
    if parsed.utcoffset() is None:
        raise ToolError(f"{field} must include a UTC offset.")
    return parsed


def _check_time_zone(value: str, timestamp: datetime, field: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 100:
        raise ToolError("time_zone must be an IANA time zone such as Asia/Shanghai.")
    try:
        zone = ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ToolError("time_zone must be a valid IANA time zone such as Asia/Shanghai.") from None
    local = timestamp.astimezone(zone)
    if local.replace(tzinfo=None) != timestamp.replace(tzinfo=None) or local.utcoffset() != timestamp.utcoffset():
        raise ToolError(f"{field} UTC offset does not match time_zone at that date.")


def _event_view(item: object) -> dict[str, object]:
    if not isinstance(item, dict):
        return {}
    result: dict[str, object] = {}
    for key in ("id", "summary", "start", "end", "location", "htmlLink", "status"):
        value = item.get(key)
        if value is not None:
            result[key] = value
    description = item.get("description")
    if isinstance(description, str) and description:
        result["description"] = description[:800]
    return result


@server.tool(
    description=(
        "List upcoming events on the user's Google Calendar. Read-only. "
        "Use to avoid conflicts when planning travel; does not change the calendar."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
def list_calendar_events(
    limit: int = 10,
    from_time: str | None = None,
    calendar_id: str = "primary",
) -> dict[str, object]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ToolError("limit must be an integer from 1 to 50.")
    if from_time is None:
        from_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
    else:
        _parse_timestamp(from_time, "from_time")
    url = _calendar_url(calendar_id) + "?" + urlencode(
        {
            "maxResults": limit,
            "singleEvents": "true",
            "orderBy": "startTime",
            "timeMin": from_time,
            "fields": "items(id,summary,description,location,start,end,htmlLink,status),nextPageToken",
        }
    )
    payload = _http_json("GET", url, headers={"Authorization": f"Bearer {_access_token()}"})
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise ToolError("Google Calendar returned an invalid events list.")
    return {
        "calendar_id": calendar_id,
        "from_time": from_time,
        "events": [view for item in items if (view := _event_view(item))],
        "has_more": bool(payload.get("nextPageToken")),
    }


@server.tool(
    description=(
        "Create one event on the user's Google Calendar after the user approves the exact "
        "title, date, time, time zone, and location. This writes to the calendar. "
        "Do not retry after an uncertain response without checking for a duplicate event."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    ),
)
def create_calendar_event(
    title: str,
    start_time: str,
    end_time: str,
    time_zone: str,
    location: str = "",
    description: str = "",
    calendar_id: str = "primary",
) -> dict[str, object]:
    if not isinstance(title, str) or not title.strip() or len(title) > 300:
        raise ToolError("title must be a non-empty string of at most 300 characters.")
    start = _parse_timestamp(start_time, "start_time")
    end = _parse_timestamp(end_time, "end_time")
    if end.astimezone(timezone.utc) <= start.astimezone(timezone.utc):
        raise ToolError("end_time must be later than start_time.")
    _check_time_zone(time_zone, start, "start_time")
    _check_time_zone(time_zone, end, "end_time")
    if not isinstance(location, str) or len(location) > 1000:
        raise ToolError("location must be a string of at most 1000 characters.")
    if not isinstance(description, str) or len(description) > 5000:
        raise ToolError("description must be a string of at most 5000 characters.")
    url = _calendar_url(calendar_id)
    event = {
        "summary": title.strip(),
        "start": {"dateTime": start_time, "timeZone": time_zone},
        "end": {"dateTime": end_time, "timeZone": time_zone},
    }
    if location.strip():
        event["location"] = location.strip()
    if description.strip():
        event["description"] = description.strip()
    payload = _http_json(
        "POST",
        url,
        json_body=event,
        headers={"Authorization": f"Bearer {_access_token()}"},
    )
    result = _event_view(payload)
    if not result.get("id"):
        raise ToolError("Google Calendar did not confirm the created event ID; check the calendar before retrying.")
    return result


if __name__ == "__main__":
    server.run("stdio")
