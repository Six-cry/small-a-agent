import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from aa_my_agent.mcp.manager import MCPManager, public_tool_name
from aa_my_agent.mcp.settings import MCPServerConfig, MCPSettings
from aa_my_agent.mcp_servers import dingtalk_calendar as calendar
from aa_my_agent.hooks import hooks


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_content(self, chunk_size=65_536):
        body = json.dumps(self.payload).encode("utf-8")
        yield from (body[index : index + chunk_size] for index in range(0, len(body), chunk_size))


def test_calendar_lists_and_creates_only_for_configured_user(monkeypatch):
    monkeypatch.setenv("DINGTALK_CLIENT_ID", "app-id")
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "app-secret")
    monkeypatch.setenv("DINGTALK_CALENDAR_UNION_ID", "user-123")
    monkeypatch.setattr(calendar, "_TOKEN_CACHE", None)
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        if url == calendar.TOKEN_URL:
            assert kwargs["json"] == {"appKey": "app-id", "appSecret": "app-secret"}
            return FakeResponse({"accessToken": "token-secret", "expireIn": 7200})
        assert kwargs["headers"]["x-acs-dingtalk-access-token"] == "token-secret"
        assert "/users/user-123/calendars/primary/" in url
        if method == "GET":
            query = parse_qs(urlsplit(url).query)
            assert query["timeMin"] == ["2026-11-01T00:00:00+08:00"]
            assert query["maxResults"] == ["5"]
            assert urlsplit(url).path.endswith("/eventsview")
            return FakeResponse(
                {
                    "events": [
                        {
                            "id": "event-1",
                            "summary": "Train",
                            "start": {"dateTime": "2026-11-01T09:00:00+08:00"},
                            "end": {"dateTime": "2026-11-01T10:00:00+08:00"},
                            "secretProviderField": "discard-me",
                        }
                    ],
                    "nextToken": "page-2",
                }
            )
        event = kwargs["json"]
        assert event["summary"] == "Airport transfer"
        assert event["location"] == {"displayName": "Hongqiao station"}
        assert "attendees" not in event
        return FakeResponse({"id": "event-2", **event, "secretProviderField": "discard-me"})

    monkeypatch.setattr(calendar.requests, "request", fake_request)
    listed = calendar.list_calendar_events(
        "2026-11-01T00:00:00+08:00", "2026-11-02T00:00:00+08:00", limit=5
    )
    created = calendar.create_calendar_event(
        "Airport transfer",
        "2026-11-01T09:00:00+08:00",
        "2026-11-01T10:00:00+08:00",
        "Asia/Shanghai",
        location="Hongqiao station",
    )

    assert listed["events"][0]["summary"] == "Train"
    assert listed["next_token"] == "page-2"
    assert "secretProviderField" not in listed["events"][0]
    assert created["id"] == "event-2"
    assert "secretProviderField" not in created
    assert [method for method, _url in calls] == ["POST", "GET", "POST"]


def test_calendar_get_update_and_delete_exact_personal_event(monkeypatch):
    monkeypatch.setenv("DINGTALK_CLIENT_ID", "app-id")
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "app-secret")
    monkeypatch.setenv("DINGTALK_CALENDAR_UNION_ID", "user-123")
    monkeypatch.setattr(calendar, "_TOKEN_CACHE", None)
    calls = []
    event = {
        "id": "event-1",
        "summary": "Train",
        "start": {"dateTime": "2026-11-01T09:00:00+08:00"},
        "end": {"dateTime": "2026-11-01T10:00:00+08:00"},
        "organizer": {"id": "user-123"},
        "attendees": [{"id": "user-123"}],
        "secretProviderField": "discard-me",
    }

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        if url == calendar.TOKEN_URL:
            return FakeResponse({"accessToken": "token-secret", "expireIn": 7200})
        assert url.endswith("/users/user-123/calendars/primary/events/event-1")
        assert kwargs["headers"]["x-acs-dingtalk-access-token"] == "token-secret"
        if method == "GET":
            return FakeResponse(event)
        if method == "PUT":
            assert kwargs["json"] == {"summary": "High-speed train"}
            return FakeResponse({**event, "summary": "High-speed train"})
        assert method == "DELETE"
        assert kwargs.get("json") is None
        return FakeResponse({"requestId": "request-1"})

    monkeypatch.setattr(calendar.requests, "request", fake_request)
    viewed = calendar.get_calendar_event("event-1")
    updated = calendar.update_calendar_event(
        "event-1", "Train", "2026-11-01T09:00:00+08:00", title="High-speed train"
    )
    deleted = calendar.delete_calendar_event("event-1", "Train", "2026-11-01T09:00:00+08:00")
    assert viewed["id"] == "event-1"
    assert "secretProviderField" not in viewed
    assert updated["summary"] == "High-speed train"
    assert "secretProviderField" not in updated
    assert deleted == {"deleted": True, "event_id": "event-1", "title": "Train"}
    assert [method for method, _url, _body in calls] == ["POST", "GET", "GET", "PUT", "GET", "DELETE"]


def test_calendar_write_refuses_stale_or_shared_event(monkeypatch):
    monkeypatch.setenv("DINGTALK_CALENDAR_UNION_ID", "user-123")
    event = {
        "id": "event-1", "summary": "Train",
        "start": {"dateTime": "2026-11-01T09:00:00+08:00"},
        "organizer": {"id": "user-123"},
        "attendees": [{"id": "user-123"}],
    }
    monkeypatch.setattr(calendar, "_load_event", lambda _id: event)
    monkeypatch.setattr(calendar, "_request_json", lambda *_args, **_kwargs: pytest.fail("unexpected write"))
    with pytest.raises(ToolError, match="changed"):
        calendar.delete_calendar_event("event-1", "Other", "2026-11-01T09:00:00+08:00")
    event["attendees"] = [{"id": "other-user"}]
    with pytest.raises(ToolError, match="personal-only"):
        calendar.update_calendar_event(
            "event-1", "Train", "2026-11-01T09:00:00+08:00", title="Renamed"
        )
    event["attendees"] = [{"id": "user-123"}]
    event["recurrence"] = {"pattern": {"type": "daily"}}
    with pytest.raises(ToolError, match="Recurring"):
        calendar.delete_calendar_event("event-1", "Train", "2026-11-01T09:00:00+08:00")


def test_calendar_update_requires_complete_time_change_before_network(monkeypatch):
    monkeypatch.setattr(calendar, "_load_event", lambda _id: pytest.fail("unexpected network"))
    with pytest.raises(ToolError, match="together"):
        calendar.update_calendar_event(
            "event-1", "Train", "2026-11-01T09:00:00+08:00", start_time="2026-11-01T11:00:00+08:00"
        )
    with pytest.raises(ToolError, match="later"):
        calendar.update_calendar_event(
            "event-1", "Train", "2026-11-01T09:00:00+08:00",
            start_time="2026-11-01T11:00:00+08:00", end_time="2026-11-01T10:00:00+08:00",
            time_zone="Asia/Shanghai",
        )


def test_calendar_rejects_unsafe_event_id_before_network(monkeypatch):
    monkeypatch.setattr(calendar, "_access_token", lambda: pytest.fail("unexpected network"))
    for event_id in ("", ".", "..", "a/b", "a\n", " a"):
        with pytest.raises(ToolError, match="event_id"):
            calendar.get_calendar_event(event_id)


def test_calendar_accepts_empty_successful_delete_response(monkeypatch):
    class EmptyResponse(FakeResponse):
        def iter_content(self, chunk_size=65_536):
            return iter(())

    monkeypatch.setattr(calendar.requests, "request", lambda *_args, **_kwargs: EmptyResponse(None))
    assert calendar._request_json("DELETE", "https://api.dingtalk.com/test") == {}


def test_calendar_delete_approval_shows_exact_target(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    decision = hooks.ask_user(
        "mcp__dingtalk-calendar__delete_calendar_event",
        {
            "event_id": "event-1",
            "expected_title": "Train",
            "expected_start_time": "2026-11-01T09:00:00+08:00",
        },
        "MCP tool may change external state",
    )
    output = capsys.readouterr().out
    assert decision == "Permission denied by user"
    assert "event-1" in output
    assert "Train" in output
    assert "永久删除" in output


def test_calendar_rejects_invalid_times_before_network(monkeypatch):
    monkeypatch.setattr(calendar, "_access_token", lambda: pytest.fail("unexpected network access"))
    with pytest.raises(ToolError, match="UTC offset"):
        calendar.create_calendar_event(
            "Trip", "2026-11-01T09:00:00", "2026-11-01T10:00:00+08:00", "Asia/Shanghai"
        )
    with pytest.raises(ToolError, match="later"):
        calendar.create_calendar_event(
            "Trip", "2026-11-01T10:00:00+08:00", "2026-11-01T09:00:00+08:00", "Asia/Shanghai"
        )
    with pytest.raises(ToolError, match="does not match"):
        calendar.create_calendar_event(
            "Trip", "2026-11-01T09:00:00+00:00", "2026-11-01T10:00:00+00:00", "Asia/Shanghai"
        )
    with pytest.raises(ToolError, match="at most 31 days"):
        calendar.list_calendar_events(
            "2026-11-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00"
        )


def test_calendar_error_does_not_echo_credentials(monkeypatch):
    monkeypatch.setenv("DINGTALK_CLIENT_ID", "app-id")
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "app-secret")
    monkeypatch.setenv("DINGTALK_CALENDAR_UNION_ID", "user-123")
    monkeypatch.setattr(calendar, "_TOKEN_CACHE", None)

    def fake_request(method, url, **_kwargs):
        if url == calendar.TOKEN_URL:
            return FakeResponse({"accessToken": "token-secret", "expireIn": 7200})
        return FakeResponse({"message": "app-secret token-secret"}, status_code=403)

    monkeypatch.setattr(calendar.requests, "request", fake_request)
    with pytest.raises(ToolError, match="HTTP 403") as error:
        calendar.list_calendar_events(
            "2026-11-01T00:00:00+08:00", "2026-11-02T00:00:00+08:00"
        )
    assert "app-secret" not in str(error.value)
    assert "token-secret" not in str(error.value)


def test_calendar_tools_discover_with_read_and_write_permissions(tmp_path):
    config = MCPServerConfig(name="dingtalk-calendar", transport="stdio", command="ignored")
    settings = MCPSettings(
        enabled=True,
        config_path=tmp_path / "servers.json",
        servers=(config,),
        connect_timeout_seconds=3,
        tool_timeout_seconds=3,
    )
    manager = MCPManager(settings, client_factory=lambda _config: Client(calendar.server))
    try:
        assert manager.permission_for(
            public_tool_name("dingtalk-calendar", "list_calendar_events")
        ) == "read"
        assert manager.permission_for(
            public_tool_name("dingtalk-calendar", "create_calendar_event")
        ) == "write"
        assert manager.permission_for(public_tool_name("dingtalk-calendar", "get_calendar_event")) == "read"
        assert manager.permission_for(public_tool_name("dingtalk-calendar", "update_calendar_event")) == "write"
        assert manager.permission_for(public_tool_name("dingtalk-calendar", "delete_calendar_event")) == "write"
        assert len(manager.status()["tools"]) == 5
    finally:
        manager.close()


def test_calendar_server_starts_through_stdio_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("DINGTALK_CLIENT_ID", "test-client")
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("DINGTALK_CALENDAR_UNION_ID", "test-user")
    config = MCPServerConfig(
        name="dingtalk-calendar",
        transport="stdio",
        command=sys.executable,
        args=("-m", "aa_my_agent.mcp_servers.dingtalk_calendar"),
        cwd=Path(__file__).resolve().parents[2],
        env_from=(
            "DINGTALK_CLIENT_ID",
            "DINGTALK_CLIENT_SECRET",
            "DINGTALK_CALENDAR_UNION_ID",
        ),
    )
    settings = MCPSettings(
        enabled=True,
        config_path=tmp_path / "servers.json",
        servers=(config,),
        connect_timeout_seconds=8,
        tool_timeout_seconds=3,
    )
    manager = MCPManager(settings)
    try:
        status = manager.status()
        assert status["connected_servers"] == ["dingtalk-calendar"]
        assert len(status["tools"]) == 5
    finally:
        manager.close()


def test_public_template_disables_calendars_and_gates_dingtalk():
    path = Path(__file__).resolve().parent.parent / "mcp" / "servers.example.json"
    servers = json.loads(path.read_text(encoding="utf-8"))["servers"]
    assert servers["google-calendar"]["enabled"] is False
    dingtalk = servers["dingtalk-calendar"]
    assert dingtalk["enabled"] is False
    assert dingtalk["include_tools"] == [
        "list_calendar_events", "get_calendar_event", "create_calendar_event",
        "update_calendar_event", "delete_calendar_event",
    ]
    assert set(dingtalk["enable_when_env"]) == {
        "DINGTALK_CLIENT_ID",
        "DINGTALK_CLIENT_SECRET",
        "DINGTALK_CALENDAR_UNION_ID",
    }
