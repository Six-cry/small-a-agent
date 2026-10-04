"""Tests for the active FlyAI travel connector and its bounded output."""

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from mcp.client import Client

from aa_my_agent.mcp.manager import MCPManager, public_tool_name
from aa_my_agent.mcp.settings import MCPServerConfig, MCPSettings
from aa_my_agent.mcp_servers import flyai_travel


def _dates():
    return (date.today() + timedelta(days=14)).isoformat(), (date.today() + timedelta(days=15)).isoformat()


@pytest.fixture
def mock_flyai_runtime(tmp_path, monkeypatch):
    """Supply path fixtures for tests that already mock subprocess.run."""
    runtime = tmp_path / "flyai-runtime"
    cli = runtime / "flyai-bundle.cjs"
    node = runtime / "node" / "bin" / ("node.exe" if sys.platform == "win32" else "node")
    node.parent.mkdir(parents=True)
    cli.write_text("// Offline test fixture; subprocess.run is mocked.\n", encoding="utf-8")
    node.write_bytes(b"Offline test fixture; not an executable.")
    monkeypatch.setattr(flyai_travel, "RUNTIME", runtime)
    monkeypatch.setattr(flyai_travel, "CLI", cli)


def test_public_template_keeps_flyai_optional():
    root = Path(__file__).resolve().parents[1] / "mcp"
    servers = json.loads((root / "servers.example.json").read_text(encoding="utf-8"))["servers"]
    assert servers["duffel-travel"]["enabled"] is False
    assert servers["flyai-travel"]["include_tools"] == ["search_domestic_hotels", "search_flight_offers"]
    assert servers["flyai-travel"]["enabled"] is False
    assert [name for name, config in servers.items() if config["enabled"]] == ["travel-demo"]


def test_mcp_discovers_read_only_hotel_tool(tmp_path):
    config = MCPServerConfig(name="flyai", transport="stdio", command="unused")
    settings = MCPSettings(enabled=True, config_path=tmp_path / "servers.json", servers=(config,), connect_timeout_seconds=3, tool_timeout_seconds=3, max_output_chars=50_000)
    manager = MCPManager(settings, client_factory=lambda _: Client(flyai_travel.server))
    try:
        names = {item["name"] for item in manager.tool_definitions()}
        assert names == {public_tool_name("flyai", "search_domestic_hotels"), public_tool_name("flyai", "search_flight_offers")}
        assert all(manager.permission_for(name) == "read" for name in names)
    finally:
        manager.close()


def test_hotel_server_starts_through_stdio_manager(tmp_path):
    config = MCPServerConfig(
        name="flyai-travel", transport="stdio", command=sys.executable,
        args=("-m", "aa_my_agent.mcp_servers.flyai_travel"),
        cwd=Path(__file__).resolve().parents[2],
    )
    settings = MCPSettings(enabled=True, config_path=tmp_path / "servers.json", servers=(config,), connect_timeout_seconds=8, tool_timeout_seconds=3)
    manager = MCPManager(settings)
    try:
        assert manager.status()["connected_servers"] == ["flyai-travel"]
        assert len(manager.status()["tools"]) == 2
    finally:
        manager.close()


def test_hotel_result_is_compact_and_marks_masked_price(monkeypatch, mock_flyai_runtime):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        captured["shell"] = kwargs.get("shell", False)
        return subprocess.CompletedProcess(command, 0, json.dumps({"status": 0, "data": {"itemList": [{
            "name": "测试酒店", "address": "杭州西湖区", "star": "舒适型", "price": "¥1xx",
            "interestsPoi": "近西湖", "shId": "123", "detailUrl": "https://router.feizhu.com/ws/123",
            "mainPic": "large image", "secret": "do not include",
        }]}}, ensure_ascii=False), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.delenv("FLYAI_API_KEY", raising=False)
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "private_token")
    start, end = _dates()
    result = flyai_travel.search_domestic_hotels("杭州", start, end, poi_name="西湖")
    assert result["mode"] == "trial"
    assert result["hotels"][0]["price_masked"] is True
    assert result["hotels"][0]["detail_url"].startswith("https://router.feizhu.com/")
    assert "mainPic" not in str(result) and "private_token" not in str(result)
    assert "DUFFEL_ACCESS_TOKEN" not in captured["env"]
    assert captured["command"][2] == "search-hotel"
    assert "--poi-name" in captured["command"]
    assert captured["shell"] is False


def test_failure_is_not_reported_as_no_hotels(monkeypatch, mock_flyai_runtime):
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "secret key"))
    start, end = _dates()
    with pytest.raises(flyai_travel.FlyAILookupError, match="failed; do not treat") as error:
        flyai_travel.search_domestic_hotels("杭州", start, end)
    assert "secret key" not in str(error.value)


def test_invalid_dates_rejected_before_provider(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("provider should not run"))
    start, end = _dates()
    with pytest.raises(flyai_travel.FlyAILookupError):
        flyai_travel.search_domestic_hotels("杭州", end, start)
    with pytest.raises(flyai_travel.FlyAILookupError):
        flyai_travel.search_domestic_hotels("杭州", start, end, sort="--help")


def test_provider_bad_structure_is_error(monkeypatch, mock_flyai_runtime):
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, '{"status":0,"data":{}}', ""))
    start, end = _dates()
    with pytest.raises(flyai_travel.FlyAILookupError, match="did not contain"):
        flyai_travel.search_domestic_hotels("杭州", start, end)


def test_flight_result_is_compact_and_key_is_forwarded_only_to_flyai(monkeypatch, mock_flyai_runtime):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        payload = {"status": 0, "data": {"itemList": [{
            "adultPrice": "¥400.0", "jumpUrl": "https://router.feizhu.com/ws/flight",
            "secret": "must-not-leak", "journeys": [{"journeyType": "直达", "totalDuration": "140分钟", "segments": [{
                "marketingTransportName": "国航", "marketingTransportNo": "CA1883",
                "depStationName": "首都国际机场", "depStationCode": "PEK", "depDateTime": "2026-10-15 21:00:00",
                "arrStationName": "浦东国际机场", "arrStationCode": "PVG", "arrDateTime": "2026-10-15 23:20:00",
                "seatClassName": "经济舱", "privateFare": "must-not-leak",
            }]}],
        }]}}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload, ensure_ascii=False), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("FLYAI_API_KEY", "private_flyai_key")
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "private_duffel_token")
    departure, _ = _dates()
    result = flyai_travel.search_flight_offers("北京", "上海", departure, direct_only=True, limit=1)
    assert result["mode"] == "api_key"
    assert result["flights"][0]["adult_price_display"] == "¥400.0"
    assert result["flights"][0]["journeys"][0]["segments"][0]["flight_number"] == "CA1883"
    assert result["flights"][0]["detail_url"].startswith("https://router.feizhu.com/")
    assert "must-not-leak" not in str(result) and "private_flyai_key" not in str(result)
    assert captured["env"]["FLYAI_API_KEY"] == "private_flyai_key"
    assert "DUFFEL_ACCESS_TOKEN" not in captured["env"]
    assert captured["command"][2] == "search-flight"
    assert captured["command"][-2:] == ["--journey-type", "1"]


def test_flight_rejects_bad_dates_and_route_before_provider(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("provider should not run"))
    departure, _ = _dates()
    with pytest.raises(flyai_travel.FlyAILookupError):
        flyai_travel.search_flight_offers("北京", "北京", departure)
    with pytest.raises(flyai_travel.FlyAILookupError):
        flyai_travel.search_flight_offers("北京", "上海", "2000-01-01")
    with pytest.raises(flyai_travel.FlyAILookupError):
        flyai_travel.search_flight_offers("北京", "上海", departure, return_date="2000-01-01")


def test_flight_failure_is_not_zero_offers(monkeypatch, mock_flyai_runtime):
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "private-key"))
    departure, _ = _dates()
    with pytest.raises(flyai_travel.FlyAILookupError, match="failed; do not treat") as error:
        flyai_travel.search_flight_offers("北京", "上海", departure)
    assert "private-key" not in str(error.value)
