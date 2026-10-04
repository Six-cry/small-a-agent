"""Contract tests for the local Duffel search-only MCP server."""

from datetime import date, timedelta

import pytest
from mcp.client import Client

from aa_my_agent.mcp.manager import MCPManager, public_tool_name
from aa_my_agent.mcp.settings import MCPServerConfig, MCPSettings
from aa_my_agent.mcp_servers import duffel_travel


def _future_date(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def test_mcp_discovers_exact_read_only_search_tools(tmp_path, monkeypatch):
    config = MCPServerConfig(name="duffel", transport="stdio", command="unused")
    settings = MCPSettings(
        enabled=True,
        config_path=tmp_path / "servers.json",
        servers=(config,),
        connect_timeout_seconds=3.0,
        tool_timeout_seconds=3.0,
        max_output_chars=50_000,
    )
    manager = MCPManager(settings, client_factory=lambda _config: Client(duffel_travel.server))
    try:
        names = {item["name"] for item in manager.tool_definitions()}
        assert names == {
            public_tool_name("duffel", "search_flight_offers"),
            public_tool_name("duffel", "search_available_stays"),
        }
        assert all(manager.permission_for(name) == "read" for name in names)
        monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)
        output = manager.call_tool(
            public_tool_name("duffel", "search_flight_offers"),
            {"origin": "SHA", "destination": "PEK", "departure_date": _future_date(15)},
        )
        assert output.startswith("MCP tool error")
        assert "DUFFEL_ACCESS_TOKEN" in output
    finally:
        manager.close()


def test_flight_search_uses_paginated_offers_and_returns_compact_test_data(monkeypatch):
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_secret")
    calls = []

    def fake_http(url, *, token=None, body=None):
        calls.append((url, token, body))
        if "offer_requests" in url:
            return {"data": {"id": "orq_123", "live_mode": False}}
        return {
            "data": [
                {
                    "id": "off_123",
                    "total_amount": "318.40",
                    "total_currency": "GBP",
                    "expires_at": "2099-01-01T12:00:00Z",
                    "owner": {"name": "Example Air", "iata_code": "EX"},
                    "private_fares": {"secret": "should-not-reach-model"},
                    "slices": [
                        {
                            "duration": "PT2H",
                            "origin": {"iata_code": "SHA"},
                            "destination": {"iata_code": "PEK"},
                            "segments": [
                                {
                                    "departing_at": "2099-01-01T08:00:00",
                                    "arriving_at": "2099-01-01T10:00:00",
                                    "origin": {"iata_code": "SHA"},
                                    "destination": {"iata_code": "PEK"},
                                    "operating_carrier": {"name": "Example Air"},
                                    "private_fares": "should-not-reach-model",
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(duffel_travel, "_http_json", fake_http)
    result = duffel_travel.search_flight_offers(
        "sha", "pek", _future_date(15), adults=2, max_results=2
    )

    assert result["ok"] is True
    assert result["mode"] == "test"
    assert result["test_data"] is True
    assert result["offers"][0]["total_amount"] == "318.40"
    assert result["offers"][0]["slices"][0]["segments"][0]["origin"]["iata_code"] == "SHA"
    assert "private_fares" not in str(result)
    assert calls[0][0].endswith(
        "/air/offer_requests?return_offers=false&supplier_timeout=10000"
    )
    assert calls[0][2]["data"]["passengers"] == [{"type": "adult"}] * 2
    assert "offer_request_id=orq_123" in calls[1][0]
    assert "sort=total_amount" in calls[1][0]
    assert "limit=2" in calls[1][0]


def test_hotel_search_geocodes_city_to_wgs84_and_limits_response(monkeypatch):
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_secret")
    calls = []

    def fake_http(url, *, token=None, body=None):
        calls.append((url, token, body))
        if "open-meteo" in url:
            return {
                "results": [
                    {
                        "name": "南京",
                        "country": "中国",
                        "latitude": 32.0603,
                        "longitude": 118.7969,
                    }
                ]
            }
        return {
            "data": {
                "results": [
                    {
                        "id": "srr_123",
                        "cheapest_rate_total_amount": "510.00",
                        "cheapest_rate_currency": "CNY",
                        "accommodation": {
                            "id": "acc_123",
                            "name": "Example Hotel",
                            "review_score": 8.8,
                            "description": "long description omitted",
                            "location": {
                                "address": {"city_name": "南京", "country_code": "CN"}
                            },
                        },
                    }
                ]
            }
        }

    monkeypatch.setattr(duffel_travel, "_http_json", fake_http)
    result = duffel_travel.search_available_stays(
        _future_date(15),
        _future_date(17),
        city="南京",
        country_code="CN",
        adults=2,
        rooms=1,
        free_cancellation_only=True,
    )

    assert result["ok"] is True
    assert result["mode"] == "test"
    assert result["coordinate_source"] == "Open-Meteo Geocoding (WGS84)"
    assert result["hotels"][0]["cheapest_rate_total_amount"] == "510.00"
    assert "long description omitted" not in str(result)
    assert calls[0][0].startswith(duffel_travel.GEOCODING_API)
    assert "countryCode=CN" in calls[0][0]
    assert calls[1][0] == "https://api.duffel.com/stays/search"
    payload = calls[1][2]["data"]
    assert payload["location"]["geographic_coordinates"] == {
        "latitude": 32.0603,
        "longitude": 118.7969,
    }
    assert payload["free_cancellation_only"] is True


def test_hotel_search_rejects_mixed_coordinate_sources_without_network(monkeypatch):
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_secret")
    monkeypatch.setattr(
        duffel_travel,
        "_http_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network called")),
    )
    with pytest.raises(duffel_travel.TravelLookupError, match="either city or WGS84 coordinates"):
        duffel_travel.search_available_stays(
            _future_date(15),
            _future_date(17),
            city="南京",
            latitude=32.0603,
            longitude=118.7969,
        )


def test_provider_error_does_not_include_token_or_raw_error_body(monkeypatch):
    secret = "duffel_test_supersecret"
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", secret)

    class ForbiddenResponse:
        status_code = 403

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(
        duffel_travel.requests,
        "request",
        lambda *_args, **_kwargs: ForbiddenResponse(),
    )
    with pytest.raises(duffel_travel.TravelLookupError, match="HTTP 403") as error:
        duffel_travel.search_flight_offers("SHA", "PEK", _future_date(15))
    assert secret not in str(error.value)


def test_http_wrapper_accepts_created_search_response(monkeypatch):
    calls = []

    class CreatedResponse:
        status_code = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_content(self, chunk_size):
            yield b'{"data":{"id":"orq_123"}}'

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return CreatedResponse()

    monkeypatch.setattr(duffel_travel.requests, "request", fake_request)
    result = duffel_travel._http_json(
        "https://api.duffel.com/air/offer_requests",
        token="duffel_test_secret",
        body={"data": {"slices": []}},
    )
    assert result["data"]["id"] == "orq_123"
    assert calls[0][0] == "POST"
    assert calls[0][2]["headers"]["Duffel-Version"] == "v2"
    assert calls[0][2]["allow_redirects"] is False


def test_missing_duffel_token_is_reported_without_request(monkeypatch):
    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(
        duffel_travel,
        "_http_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network called")),
    )
    with pytest.raises(duffel_travel.TravelLookupError, match="DUFFEL_ACCESS_TOKEN is not configured"):
        duffel_travel.search_flight_offers("SHA", "PEK", _future_date(15))


def test_token_mode_labels_are_conservative():
    assert duffel_travel._mode("duffel_test_sample") == "test"
    assert duffel_travel._mode("duffel_live_sample") == "live"
    assert duffel_travel._mode("unrecognised_token") == "unverified"
    assert duffel_travel._mode("duffel_test_sample", live_mode=True) == "live"
