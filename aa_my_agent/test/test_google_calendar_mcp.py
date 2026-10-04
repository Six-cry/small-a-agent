from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from aa_my_agent.mcp.manager import MCPManager, public_tool_name
from aa_my_agent.mcp.settings import MCPServerConfig, MCPSettings
from aa_my_agent.mcp_servers import google_calendar as calendar
from aa_my_agent.mcp_servers import google_calendar_auth as auth


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


def test_calendar_lists_and_creates_events_with_minimal_fields(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_CALENDAR_REFRESH_TOKEN", "refresh-secret")
    monkeypatch.setattr(calendar, "_TOKEN_CACHE", None)
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        if url == calendar.TOKEN_URL:
            assert kwargs["data"]["refresh_token"] == "refresh-secret"
            return FakeResponse({"access_token": "access-secret", "expires_in": 3600})
        assert kwargs["headers"]["Authorization"] == "Bearer access-secret"
        if method == "GET":
            query = parse_qs(urlsplit(url).query)
            assert query["singleEvents"] == ["true"]
            assert query["orderBy"] == ["startTime"]
            assert query["maxResults"] == ["5"]
            return FakeResponse(
                {
                    "items": [
                        {
                            "id": "event-1",
                            "summary": "Flight",
                            "start": {"dateTime": "2026-10-01T09:00:00+08:00"},
                            "end": {"dateTime": "2026-10-01T11:00:00+08:00"},
                            "secretProviderField": "discard-me",
                        }
                    ],
                    "nextPageToken": "page-2",
                }
            )
        event = kwargs["json"]
        assert event["summary"] == "Airport transfer"
        assert event["start"]["timeZone"] == "Asia/Shanghai"
        assert event["location"] == "Shanghai Hongqiao"
        return FakeResponse(
            {
                "id": "event-2",
                "summary": event["summary"],
                "start": event["start"],
                "end": event["end"],
                "htmlLink": "https://calendar.google.com/event?eid=event-2",
                "secretProviderField": "discard-me",
            }
        )

    monkeypatch.setattr(calendar.requests, "request", fake_request)
    listed = calendar.list_calendar_events(
        limit=5, from_time="2026-10-01T00:00:00+08:00"
    )
    created = calendar.create_calendar_event(
        title="Airport transfer",
        start_time="2026-10-01T09:00:00+08:00",
        end_time="2026-10-01T10:00:00+08:00",
        time_zone="Asia/Shanghai",
        location="Shanghai Hongqiao",
    )

    assert listed["has_more"] is True
    assert listed["events"][0]["summary"] == "Flight"
    assert "secretProviderField" not in listed["events"][0]
    assert created["id"] == "event-2"
    assert "secretProviderField" not in created
    assert [method for method, _url in calls] == ["POST", "GET", "POST"]


def test_calendar_rejects_invalid_times_before_network(monkeypatch):
    monkeypatch.setattr(calendar, "_access_token", lambda: pytest.fail("unexpected network access"))
    with pytest.raises(ToolError, match="UTC offset"):
        calendar.create_calendar_event("Trip", "2026-10-01T09:00:00", "2026-10-01T10:00:00+08:00", "Asia/Shanghai")
    with pytest.raises(ToolError, match="later"):
        calendar.create_calendar_event("Trip", "2026-10-01T10:00:00+08:00", "2026-10-01T09:00:00+08:00", "Asia/Shanghai")
    with pytest.raises(ToolError, match="does not match"):
        calendar.create_calendar_event("Trip", "2026-10-01T09:00:00+00:00", "2026-10-01T10:00:00+00:00", "Asia/Shanghai")


def test_expired_refresh_token_error_does_not_expose_server_details(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_CALENDAR_REFRESH_TOKEN", "refresh-secret")
    monkeypatch.setattr(calendar, "_TOKEN_CACHE", None)

    def fail_request(method, url, **kwargs):
        return FakeResponse(
            {"error": "invalid_grant", "error_description": "secret-value"},
            status_code=400,
        )

    monkeypatch.setattr(calendar.requests, "request", fail_request)
    with pytest.raises(ToolError, match="expired or was revoked") as error:
        calendar.list_calendar_events()
    assert "secret-value" not in str(error.value)
    assert "refresh-secret" not in str(error.value)
    assert "--authorize" in str(error.value)


def test_calendar_tools_discover_with_read_and_write_permissions(tmp_path):
    config = MCPServerConfig(name="google-calendar", transport="stdio", command="ignored")
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
            public_tool_name("google-calendar", "list_calendar_events")
        ) == "read"
        assert manager.permission_for(
            public_tool_name("google-calendar", "create_calendar_event")
        ) == "write"
    finally:
        manager.close()


def test_auth_helper_preserves_env_and_requires_explicit_authorize(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    env_path.write_bytes(
        b'GOOGLE_CALENDAR_CLIENT_ID="id"\r\n'
        b'OTHER_SETTING="keep"\r\n'
        b'GOOGLE_CALENDAR_REFRESH_TOKEN="old"\r\n'
    )
    monkeypatch.setattr(auth, "ENV_PATH", env_path)
    monkeypatch.setattr("sys.argv", ["google_calendar_auth"])
    assert auth.main() == 0
    assert b'GOOGLE_CALENDAR_REFRESH_TOKEN="old"' in env_path.read_bytes()
    auth._save_refresh_token(env_path, "new-private-token")
    updated = env_path.read_bytes()
    assert b'OTHER_SETTING="keep"\r\n' in updated
    assert b'GOOGLE_CALENDAR_REFRESH_TOKEN="new-private-token"\r\n' in updated
    assert "new-private-token" not in capsys.readouterr().out


def test_auth_helper_loads_client_from_env_file_and_requests_offline_pkce(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        'GOOGLE_CALENDAR_CLIENT_ID="desktop-client"\n'
        'GOOGLE_CALENDAR_CLIENT_SECRET="desktop-secret"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_SECRET", raising=False)
    assert auth._client_credentials(env_path) == ("desktop-client", "desktop-secret")

    url = auth._authorization_url(
        "desktop-client", "http://127.0.0.1:9876", "state-value", "v" * 64
    )
    query = parse_qs(urlsplit(url).query)
    assert query["scope"] == [calendar.CALENDAR_SCOPE]
    assert query["access_type"] == ["offline"]
    assert query["code_challenge_method"] == ["S256"]
    assert "desktop-secret" not in url

    def fake_post(url, *, data, timeout):
        assert url == calendar.TOKEN_URL
        assert data["code_verifier"] == "v" * 64
        assert data["grant_type"] == "authorization_code"
        return FakeResponse({"refresh_token": "private-token"})

    monkeypatch.setattr(auth.requests, "post", fake_post)
    assert auth._exchange_code(
        "desktop-client", "desktop-secret", "one-time-code", "http://127.0.0.1:9876", "v" * 64
    ) == "private-token"
