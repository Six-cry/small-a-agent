"""Narrow DingTalk calendar MCP bridge for one configured user's primary calendar.

Only bounded event operations are exposed. App credentials and the target
Union ID stay in the parent process environment; access tokens stay in memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations


TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
EVENTS_URL = "https://api.dingtalk.com/v1.0/calendar/users/{user_id}/calendars/primary/events"
MAX_RESPONSE_BYTES = 1_000_000
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: tuple[str, float, str] | None = None

server = MCPServer(
    "small-a-dingtalk-calendar",
    description="Read and manage events on the configured user's DingTalk primary calendar.",
)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ToolError(f"DingTalk calendar is not configured; set {name} in aa_my_agent/.env.")
    return value


def _request_json(
    method: str,
    url: str,
    *,
    json_body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    try:
        with requests.request(
            method,
            url,
            json=json_body,
            headers={"Accept": "application/json", **(headers or {})},
            timeout=(3, 15),
            stream=True,
            allow_redirects=False,
        ) as response:
            status = response.status_code
            if status == 401:
                raise ToolError("DingTalk rejected the application credentials (HTTP 401).")
            if status == 400:
                if url == TOKEN_URL:
                    raise ToolError("DingTalk rejected the Client ID or Client Secret (HTTP 400).")
                raise ToolError("DingTalk rejected the calendar request (HTTP 400); check app permissions and user scope.")
            if status == 403:
                raise ToolError("DingTalk denied calendar access (HTTP 403); check app permissions and user scope.")
            if status == 429:
                raise ToolError("DingTalk calendar rate limit reached (HTTP 429).")
            if status < 200 or status >= 300:
                raise ToolError(f"DingTalk calendar request failed (HTTP {status}).")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65_536):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ToolError("DingTalk calendar response is too large; narrow the time range.")
    except requests.RequestException:
        raise ToolError("DingTalk calendar is temporarily unreachable.") from None
    if method == "DELETE" and not body:
        return {}
    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ToolError("DingTalk calendar returned invalid JSON.") from None
    if not isinstance(result, dict):
        raise ToolError("DingTalk calendar returned an invalid response.")
    if result.get("code") not in (None, 0, "0"):
        raise ToolError("DingTalk rejected the calendar request; check app permissions and calendar user.")
    return result


def _access_token() -> str:
    global _TOKEN_CACHE
    client_id = _required_env("DINGTALK_CLIENT_ID")
    client_secret = _required_env("DINGTALK_CLIENT_SECRET")
    fingerprint = hashlib.sha256(f"{client_id}\0{client_secret}".encode()).hexdigest()
    with _TOKEN_LOCK:
        if _TOKEN_CACHE and _TOKEN_CACHE[2] == fingerprint and _TOKEN_CACHE[1] > time.monotonic():
            return _TOKEN_CACHE[0]
        result = _request_json(
            "POST",
            TOKEN_URL,
            json_body={"appKey": client_id, "appSecret": client_secret},
        )
        token = result.get("accessToken")
        if not isinstance(token, str) or not token:
            raise ToolError("DingTalk did not return an application access token.")
        lifetime = result.get("expireIn", 7200)
        seconds = int(lifetime) if isinstance(lifetime, (int, float)) and not isinstance(lifetime, bool) else 7200
        _TOKEN_CACHE = (token, time.monotonic() + max(0, seconds - 60), fingerprint)
        return token


def _events_url() -> str:
    user_id = _required_env("DINGTALK_CALENDAR_UNION_ID")
    if len(user_id) > 256 or any(ord(char) < 32 for char in user_id):
        raise ToolError("DINGTALK_CALENDAR_UNION_ID is invalid.")
    return EVENTS_URL.format(user_id=quote(user_id, safe=""))


def _event_url(event_id: str) -> str:
    if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 256:
        raise ToolError("event_id must be a non-empty identifier of at most 256 characters.")
    if event_id != event_id.strip() or event_id in {".", ".."} or "/" in event_id or any(ord(char) < 32 for char in event_id):
        raise ToolError("event_id is invalid.")
    return _events_url() + "/" + quote(event_id, safe="")


def _timestamp(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        raise ToolError(f"{label} must be an RFC3339 date-time with a UTC offset.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(f"{label} is not a valid date-time.") from None
    if parsed.utcoffset() is None:
        raise ToolError(f"{label} must include a UTC offset.")
    return parsed


def _check_zone(value: str, timestamp: datetime, label: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 100:
        raise ToolError("time_zone must be an IANA time zone such as Asia/Shanghai.")
    try:
        zone = ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ToolError("time_zone must be a valid IANA time zone.") from None
    local = timestamp.astimezone(zone)
    if local.replace(tzinfo=None) != timestamp.replace(tzinfo=None) or local.utcoffset() != timestamp.utcoffset():
        raise ToolError(f"{label} UTC offset does not match time_zone at that date.")


def _event_view(item: object) -> dict[str, object]:
    if not isinstance(item, dict):
        return {}
    result = {key: item[key] for key in ("id", "summary", "start", "end") if key in item}
    location = item.get("location")
    if isinstance(location, dict) and isinstance(location.get("displayName"), str):
        result["location"] = location["displayName"][:300]
    description = item.get("description")
    if isinstance(description, str):
        result["description"] = description[:800]
    return result


def _load_event(event_id: str) -> dict[str, object]:
    result = _request_json(
        "GET",
        _event_url(event_id),
        headers={"x-acs-dingtalk-access-token": _access_token()},
    )
    if result.get("id") != event_id or not isinstance(result.get("summary"), str):
        raise ToolError("DingTalk did not return the requested event details; no change was made.")
    return result


def _verified_write_target(event_id: str, expected_title: str, expected_start_time: str) -> dict[str, object]:
    if not isinstance(expected_title, str) or not expected_title or len(expected_title) > 300:
        raise ToolError("expected_title must be the current event title.")
    expected_start = _timestamp(expected_start_time, "expected_start_time")
    current = _load_event(event_id)
    start = current.get("start")
    start_value = start.get("dateTime") if isinstance(start, dict) else None
    if not isinstance(start_value, str):
        raise ToolError("Only timed events can be changed by small-a; no change was made.")
    if current["summary"] != expected_title or _timestamp(start_value, "current start") != expected_start:
        raise ToolError("The event title or start time changed; refresh the event before trying again.")
    organizer = current.get("organizer")
    if not isinstance(organizer, dict) or organizer.get("id") != _required_env("DINGTALK_CALENDAR_UNION_ID"):
        raise ToolError("Only events organized by the configured user can be changed by small-a.")
    if current.get("recurrence") or current.get("seriesMasterId"):
        raise ToolError("Recurring events must be changed manually in DingTalk.")
    attendees = current.get("attendees")
    if not isinstance(attendees, list) or any(
        not isinstance(item, dict) or item.get("id") != organizer["id"] for item in attendees
    ):
        raise ToolError("Attendees could not be verified as personal-only; change this event manually in DingTalk.")
    return current


@server.tool(
    description=(
        "List events on the configured user's DingTalk primary calendar within a specified "
        "time range. Read-only; use to check travel-plan conflicts."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
def list_calendar_events(
    time_min: str,
    time_max: str,
    limit: int = 20,
    next_token: str = "",
) -> dict[str, object]:
    start = _timestamp(time_min, "time_min")
    end = _timestamp(time_max, "time_max")
    if end <= start or end - start > timedelta(days=31):
        raise ToolError("Calendar lookup range must be positive and at most 31 days.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ToolError("limit must be an integer from 1 to 50.")
    if not isinstance(next_token, str) or len(next_token) > 1000:
        raise ToolError("next_token is invalid.")
    params = {"timeMin": time_min, "timeMax": time_max, "maxResults": limit}
    if next_token:
        params["nextToken"] = next_token
    url = _events_url() + "view?" + urlencode(params)
    result = _request_json(
        "GET",
        url,
        headers={"x-acs-dingtalk-access-token": _access_token()},
    )
    events = result.get("events", [])
    if not isinstance(events, list):
        raise ToolError("DingTalk returned an invalid calendar event list.")
    continuation = result.get("nextToken", "")
    return {
        "calendar": "primary",
        "time_min": time_min,
        "time_max": time_max,
        "events": [view for item in events if (view := _event_view(item))],
        "next_token": continuation if isinstance(continuation, str) else "",
    }


@server.tool(
    description="Get one event by its exact ID from the configured user's DingTalk primary calendar. Read-only.",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
def get_calendar_event(event_id: str) -> dict[str, object]:
    return _event_view(_load_event(event_id))


@server.tool(
    description=(
        "Create one event on the configured user's DingTalk primary calendar after the user "
        "approves the exact title, date, time, time zone, and location. This writes to the "
        "calendar. Do not retry an uncertain result without checking for a duplicate."
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
) -> dict[str, object]:
    if not isinstance(title, str) or not title.strip() or len(title) > 300:
        raise ToolError("title must be a non-empty string of at most 300 characters.")
    start = _timestamp(start_time, "start_time")
    end = _timestamp(end_time, "end_time")
    if end <= start:
        raise ToolError("end_time must be later than start_time.")
    _check_zone(time_zone, start, "start_time")
    _check_zone(time_zone, end, "end_time")
    if not isinstance(location, str) or len(location) > 1000:
        raise ToolError("location must be a string of at most 1000 characters.")
    if not isinstance(description, str) or len(description) > 5000:
        raise ToolError("description must be a string of at most 5000 characters.")
    event: dict[str, object] = {
        "summary": title.strip(),
        "start": {"dateTime": start_time, "timeZone": time_zone},
        "end": {"dateTime": end_time, "timeZone": time_zone},
    }
    if location.strip():
        event["location"] = {"displayName": location.strip()}
    if description.strip():
        event["description"] = description.strip()
    result = _request_json(
        "POST",
        _events_url(),
        json_body=event,
        headers={"x-acs-dingtalk-access-token": _access_token()},
    )
    view = _event_view(result)
    if not view.get("id"):
        raise ToolError("DingTalk did not confirm the created event ID; check the calendar before retrying.")
    return view


@server.tool(
    description=(
        "Update one non-recurring personal event in the configured user's DingTalk primary calendar. "
        "First list/get the event and supply its exact ID, current title and current start time. "
        "For a time change supply both new start_time and end_time plus time_zone. "
        "This writes to DingTalk and needs the user's approval; do not retry an uncertain result."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    ),
)
def update_calendar_event(
    event_id: str,
    expected_title: str,
    expected_start_time: str,
    title: str = "",
    start_time: str = "",
    end_time: str = "",
    time_zone: str = "",
    location: str | None = None,
    description: str | None = None,
) -> dict[str, object]:
    if not isinstance(title, str) or len(title) > 300 or (title and not title.strip()):
        raise ToolError("title must be non-empty when provided and at most 300 characters.")
    if not isinstance(start_time, str) or not isinstance(end_time, str) or not isinstance(time_zone, str):
        raise ToolError("start_time, end_time and time_zone must be strings.")
    if any((start_time, end_time, time_zone)) and not all((start_time, end_time, time_zone)):
        raise ToolError("A time change needs start_time, end_time and time_zone together.")
    if location is not None and (not isinstance(location, str) or not location.strip() or len(location) > 1000):
        raise ToolError("location must be non-empty and at most 1000 characters when provided.")
    if description is not None and (not isinstance(description, str) or len(description) > 5000):
        raise ToolError("description must be at most 5000 characters.")
    if not any((title, start_time, location is not None, description is not None)):
        raise ToolError("At least one event change is required.")
    if start_time:
        start = _timestamp(start_time, "start_time")
        end = _timestamp(end_time, "end_time")
        if end <= start:
            raise ToolError("end_time must be later than start_time.")
        _check_zone(time_zone, start, "start_time")
        _check_zone(time_zone, end, "end_time")
    _verified_write_target(event_id, expected_title, expected_start_time)
    changes: dict[str, object] = {}
    if title:
        changes["summary"] = title.strip()
    if start_time:
        changes["start"] = {"dateTime": start_time, "timeZone": time_zone}
        changes["end"] = {"dateTime": end_time, "timeZone": time_zone}
    if location is not None:
        changes["location"] = {"displayName": location.strip()}
    if description is not None:
        changes["description"] = description
    result = _request_json(
        "PUT",
        _event_url(event_id),
        json_body=changes,
        headers={"x-acs-dingtalk-access-token": _access_token()},
    )
    if result.get("id") != event_id:
        raise ToolError("DingTalk did not confirm the updated event ID; check the calendar before retrying.")
    return _event_view(result)


@server.tool(
    description=(
        "Delete one non-recurring personal event from the configured user's DingTalk primary calendar. "
        "First list/get the event and supply its exact ID, current title and current start time. "
        "This permanently removes that event and needs the user's approval. "
        "Never retry an uncertain deletion without checking the calendar."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
    ),
)
def delete_calendar_event(event_id: str, expected_title: str, expected_start_time: str) -> dict[str, object]:
    _verified_write_target(event_id, expected_title, expected_start_time)
    _request_json(
        "DELETE",
        _event_url(event_id),
        headers={"x-acs-dingtalk-access-token": _access_token()},
    )
    return {"deleted": True, "event_id": event_id, "title": expected_title}


if __name__ == "__main__":
    server.run("stdio")
