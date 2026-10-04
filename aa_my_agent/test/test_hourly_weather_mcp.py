"""Contract checks for the local Open-Meteo MCP weather bridge."""

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from requests.exceptions import SSLError

from aa_my_agent.mcp_servers import hourly_weather


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        assert chunk_size == 8192
        yield self.payload


def test_hourly_forecast_preserves_source_values_and_uses_local_timezone(monkeypatch):
    requests = []
    geocoding = {
        "results": [{
            "name": "南京", "admin1": "江苏", "country": "中国",
            "latitude": 32.06, "longitude": 118.78, "timezone": "Asia/Shanghai",
        }]
    }
    forecast = {
        "timezone": "Asia/Shanghai",
        "hourly_units": {
            "temperature_2m": "°C", "precipitation_probability": "%",
            "precipitation": "mm", "weather_code": "wmo code",
            "wind_speed_10m": "km/h",
        },
        "hourly": {
            "time": ["2026-10-02T08:00", "2026-10-02T09:00"],
            "temperature_2m": [19.3, 20.1],
            "precipitation_probability": [60, 30],
            "precipitation": [0.4, 0.0],
            "weather_code": [61, 3],
            "wind_speed_10m": [12.2, 11.8],
        },
    }

    def fake_get(url, *, params, headers, timeout, stream):
        assert timeout == 10
        assert stream is True
        assert headers["User-Agent"].startswith("small-a-")
        requests.append((url, params))
        return FakeResponse(geocoding if len(requests) == 1 else forecast)

    monkeypatch.setattr(hourly_weather.requests, "get", fake_get)
    result = hourly_weather.get_hourly_forecast(
        "南京", "2026-10-02", country_code="CN", start_hour=8, end_hour=8
    )

    assert requests[0][0] == hourly_weather.GEOCODING_URL
    assert requests[0][1]["countryCode"] == "CN"
    assert requests[1][0] == hourly_weather.FORECAST_URL
    assert requests[1][1]["timezone"] == "Asia/Shanghai"
    assert requests[1][1]["start_date"] == "2026-10-02"
    assert result["location"]["latitude"] == 32.06
    assert result["columns"] == ["local_time", *hourly_weather.HOURLY_FIELDS]
    assert result["hours"] == [["2026-10-02T08:00", 19.3, 60, 0.4, 61, 12.2]]
    assert "preceding hour" in result["precipitation_note"]


@pytest.mark.parametrize(
    "start_date,end_date",
    [("bad", ""), ("2026-10-09", "2026-10-02"), ("2026-10-01", "2026-10-08")],
)
def test_invalid_dates_are_rejected_before_network(monkeypatch, start_date, end_date):
    monkeypatch.setattr(
        hourly_weather.requests, "get",
        lambda *_args, **_kwargs: pytest.fail("network should not be called"),
    )
    with pytest.raises(ToolError):
        hourly_weather.get_hourly_forecast("南京", start_date, end_date)


def test_provider_error_does_not_return_successful_weather(monkeypatch):
    monkeypatch.setattr(
        hourly_weather.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse({"error": True, "reason": "Invalid date"}),
    )
    with pytest.raises(ToolError, match="Invalid date"):
        hourly_weather.get_hourly_forecast("南京", "2026-10-02")


def test_network_ssl_failure_is_short_expected_mcp_tool_error(monkeypatch):
    def fail_ssl(*_args, **_kwargs):
        raise SSLError("EOF occurred in violation of protocol")

    monkeypatch.setattr(hourly_weather.requests, "get", fail_ssl)
    with pytest.raises(ToolError) as failure:
        asyncio.run(
            hourly_weather.server.call_tool(
                "get_hourly_forecast",
                {"city": "南京", "start_date": "2026-10-02"},
            )
        )
    assert type(failure.value) is ToolError
    assert str(failure.value) == (
        "Error executing tool get_hourly_forecast: "
        "Open-Meteo connection failed; check network/TLS and retry"
    )
