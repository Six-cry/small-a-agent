import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aa_my_agent.mcp.manager import MCPManager, public_tool_name
from aa_my_agent.mcp.settings import (
    MCPServerConfig,
    MCPSettings,
    load_mcp_settings,
)
from aa_my_agent.mcp.turn_cache import read_call_cache_key


def settings_for(*servers, enabled=True, max_output_chars=50_000):
    return MCPSettings(
        enabled=enabled,
        config_path=Path("test-mcp.json"),
        servers=tuple(servers),
        connect_timeout_seconds=3.0,
        tool_timeout_seconds=3.0,
        max_output_chars=max_output_chars,
    )


class FakeClient:
    def __init__(self, tools, result=None):
        self.tools = tools
        self.result = result or SimpleNamespace(
            structured_content={"ok": True},
            content=[],
            is_error=False,
        )
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def list_tools(self, *, cursor=None):
        return SimpleNamespace(tools=self.tools, next_cursor=None)

    async def call_tool(
        self,
        name,
        arguments,
        read_timeout_seconds=None,
    ):
        self.calls.append((name, arguments, read_timeout_seconds))
        return self.result


def fake_tool(name, *, read_only=None, destructive=None):
    annotations = SimpleNamespace(
        read_only_hint=read_only,
        destructive_hint=destructive,
    )
    return SimpleNamespace(
        name=name,
        description=f"Tool {name}",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
        annotations=annotations,
    )


def test_read_call_cache_key_is_canonical_and_excludes_writes():
    first = read_call_cache_key(
        "mcp__amap__maps_distance",
        {"origin": "A", "destination": "B"},
        "read",
    )
    reordered = read_call_cache_key(
        "mcp__amap__maps_distance",
        {"destination": "B", "origin": "A"},
        "read",
    )

    assert first == reordered
    assert read_call_cache_key("mcp__calendar__create_event", {}, "write") is None
    assert read_call_cache_key("read_file", {"path": "x"}, "read") is None


def test_load_settings_validates_and_resolves_stdio_config(tmp_path):
    config_path = tmp_path / "servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": {
                    "travel-demo": {
                        "transport": "stdio",
                        "command": "python",
                        "args": ["-m", "demo"],
                        "cwd": ".",
                        "env_from": ["SAFE_TOKEN"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    settings = load_mcp_settings(enabled=True, config_path=config_path)

    assert settings.enabled is True
    assert settings.servers[0].name == "travel-demo"
    assert settings.servers[0].cwd == tmp_path.resolve()
    assert settings.servers[0].env_from == ("SAFE_TOKEN",)


def test_optional_server_waits_for_credentials_then_connects(tmp_path, monkeypatch):
    config_path = tmp_path / "servers.json"
    config_path.write_text(
        json.dumps({
            "servers": {
                "optional": {
                    "transport": "stdio",
                    "command": "ignored",
                    "env_from": ["TEST_TRAVEL_TOKEN"],
                    "enable_when_env": ["TEST_TRAVEL_TOKEN"],
                }
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.delenv("TEST_TRAVEL_TOKEN", raising=False)
    settings = load_mcp_settings(enabled=True, config_path=config_path)
    manager = MCPManager(settings, client_factory=lambda _config: FakeClient([]))
    try:
        status = manager.status()
        assert status["connected_servers"] == []
        assert status["pending_servers"] == {"optional": ["TEST_TRAVEL_TOKEN"]}
        assert status["errors"] == {}
    finally:
        manager.close()

    monkeypatch.setenv("TEST_TRAVEL_TOKEN", "test-only-value")
    manager = MCPManager(settings, client_factory=lambda _config: FakeClient([]))
    try:
        status = manager.status()
        assert status["connected_servers"] == ["optional"]
        assert status["pending_servers"] == {}
    finally:
        manager.close()


def test_remote_url_requires_https_except_localhost(tmp_path):
    config_path = tmp_path / "servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": {
                    "bad": {
                        "transport": "streamable_http",
                        "url": "http://example.com/mcp",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must use HTTPS"):
        load_mcp_settings(enabled=True, config_path=config_path)


def test_disabled_mcp_does_not_parse_unused_invalid_config(tmp_path):
    config_path = tmp_path / "servers.json"
    config_path.write_text("{not-json", encoding="utf-8")

    settings = load_mcp_settings(enabled=False, config_path=config_path)

    assert settings.enabled is False
    assert settings.servers == ()


def test_manager_discovers_namespaces_and_calls_read_only_tool():
    config = MCPServerConfig(
        name="travel demo",
        transport="stdio",
        command="ignored",
    )
    fake = FakeClient([fake_tool("route lookup", read_only=True, destructive=False)])
    manager = MCPManager(settings_for(config), client_factory=lambda _config: fake)
    try:
        definitions = manager.tool_definitions()
        public_name = public_tool_name("travel demo", "route lookup")

        assert [item["name"] for item in definitions] == [public_name]
        assert manager.permission_for(public_name) == "read"
        output = manager.call_tool(public_name, {"value": "Hangzhou"})

        assert '"ok": true' in output
        assert "untrusted external data" in output
        assert fake.calls == [("route lookup", {"value": "Hangzhou"}, 3.0)]
    finally:
        manager.close()


def test_unannotated_tools_require_write_approval_policy():
    config = MCPServerConfig(name="calendar", transport="stdio", command="ignored")
    fake = FakeClient([fake_tool("create_event")])
    manager = MCPManager(settings_for(config), client_factory=lambda _config: fake)
    try:
        public_name = public_tool_name("calendar", "create_event")
        assert manager.permission_for(public_name) == "write"
    finally:
        manager.close()


def test_reviewed_local_read_only_override_does_not_override_destructive_hint():
    config = MCPServerConfig(
        name="map",
        transport="stdio",
        command="ignored",
        read_only_tools=("lookup", "dangerous"),
    )
    fake = FakeClient(
        [
            fake_tool("lookup"),
            fake_tool("dangerous", destructive=True),
        ]
    )
    manager = MCPManager(settings_for(config), client_factory=lambda _config: fake)
    try:
        assert manager.permission_for(public_tool_name("map", "lookup")) == "read"
        assert manager.permission_for(public_tool_name("map", "dangerous")) == "write"
    finally:
        manager.close()


def test_builtin_shadow_is_hidden_unless_explicitly_allowed():
    hidden = MCPServerConfig(name="provider", transport="stdio", command="ignored")
    fake = FakeClient([fake_tool("query_weather", read_only=True, destructive=False)])
    manager = MCPManager(settings_for(hidden), client_factory=lambda _config: fake)
    try:
        assert manager.tool_definitions() == []
    finally:
        manager.close()


def test_missing_stdio_environment_variable_is_reported_without_starting_server(
    monkeypatch,
):
    monkeypatch.delenv("MISSING_MCP_KEY", raising=False)
    config = MCPServerConfig(
        name="provider",
        transport="stdio",
        command="ignored",
        env_from=("MISSING_MCP_KEY",),
    )
    manager = MCPManager(settings_for(config))
    try:
        status = manager.status()
        assert status["connected_servers"] == []
        assert "MISSING_MCP_KEY" in status["errors"]["provider"]
    finally:
        manager.close()


def test_official_sdk_in_process_server_round_trip():
    from mcp.client import Client
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    server = MCPServer("small-a-test")

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    )
    def route_summary(city: str) -> dict[str, str]:
        return {"city": city, "status": "available"}

    config = MCPServerConfig(name="map", transport="stdio", command="ignored")
    manager = MCPManager(
        settings_for(config),
        client_factory=lambda _config: Client(server),
    )
    try:
        public_name = public_tool_name("map", "route_summary")
        assert manager.permission_for(public_name) == "read"
        output = manager.call_tool(public_name, {"city": "杭州"})
        assert "杭州" in output
        assert "available" in output
    finally:
        manager.close()


def test_permission_hook_auto_allows_only_annotated_read_tools(monkeypatch):
    from aa_my_agent.hooks import hooks
    from aa_my_agent.mcp import runtime
    from aa_my_agent.tools import tools as tool_registry

    config = MCPServerConfig(name="mixed", transport="stdio", command="ignored")
    fake = FakeClient(
        [
            fake_tool("lookup", read_only=True, destructive=False),
            fake_tool("create_booking"),
        ]
    )
    manager = MCPManager(settings_for(config), client_factory=lambda _config: fake)
    manager.start()
    runtime.shutdown_mcp()
    monkeypatch.setattr(runtime, "_manager", manager)
    monkeypatch.setattr(hooks, "ask_user", lambda *_args, **_kwargs: "approval-required")
    try:
        read_block = SimpleNamespace(
            name=public_tool_name("mixed", "lookup"),
            input={"value": "West Lake"},
        )
        write_block = SimpleNamespace(
            name=public_tool_name("mixed", "create_booking"),
            input={"value": "hotel"},
        )

        active_names = {item["name"] for item in tool_registry.get_active_tools()}
        assert read_block.name in active_names
        assert callable(tool_registry.get_tool_handler(read_block.name))
        assert hooks.permission_hook(read_block) is None
        assert hooks.permission_hook(write_block) == "approval-required"
    finally:
        monkeypatch.setattr(runtime, "_manager", None)
        manager.close()


def test_large_amap_transit_result_is_reduced_and_raw_copy_is_saved(tmp_path):
    def transit_option(index):
        return {
            "duration": str(1_800 + index * 60),
            "walking_distance": str(400 + index * 100),
            "segments": [
                {
                    "walking": {
                        "distance": "450",
                        "duration": "360",
                        "steps": [
                            {
                                "instruction": f"步行指引 {step}",
                                "distance": "30",
                                "duration": "24",
                                "polyline": "118.1,32.1;" * 80,
                            }
                            for step in range(8)
                        ],
                    },
                    "bus": {
                        "buslines": [
                            {
                                "name": f"地铁{index + 1}号线",
                                "departure_stop": {"name": "起点站"},
                                "arrival_stop": {"name": "终点站"},
                                "distance": "2200",
                                "duration": "900",
                                "via_stops": [
                                    {"name": f"途经站{stop}"}
                                    for stop in range(4)
                                ],
                                "polyline": "118.2,32.2;" * 100,
                            }
                        ]
                    },
                    "entrance": {"name": "2号口"},
                    "exit": {"name": "1号口"},
                }
            ],
        }

    payload = {
        "route": {
            "origin": "118.771409,32.082574",
            "destination": "118.783410,32.069333",
            "distance": "2603",
            "transits": [transit_option(index) for index in range(5)],
        }
    }
    result = SimpleNamespace(
        structured_content=payload,
        content=[],
        is_error=False,
    )
    config = MCPServerConfig(
        name="amap",
        transport="stdio",
        command="ignored",
        read_only_tools=("maps_direction_transit_integrated",),
    )
    fake = FakeClient(
        [fake_tool("maps_direction_transit_integrated")],
        result=result,
    )
    manager = MCPManager(
        settings_for(config),
        client_factory=lambda _config: fake,
        raw_result_dir=tmp_path,
    )
    try:
        output = manager.call_tool(
            public_tool_name("amap", "maps_direction_transit_integrated"),
            {
                "destination": "118.783410,32.069333",
                "origin": "118.771409,32.082574",
                "city": "南京",
            },
        )
    finally:
        manager.close()

    body = output.split("\n", 1)[1]
    reduced = json.loads(body)
    assert reduced["_meta"]["reduced"] is True
    assert reduced["_meta"]["route_options_returned"] == 5
    assert reduced["_meta"]["route_options_included"] == 3
    assert reduced["_meta"]["original_chars"] > len(body)
    assert len(reduced["route"]["transits"]) == 3
    assert (
        reduced["route"]["transits"][0]["segments"][0]["bus"]
        ["buslines"][0]["name"]
        == "地铁1号线"
    )
    assert "polyline" not in body
    raw_files = list(tmp_path.glob("amap_maps_direction_transit_integrated_*.json"))
    assert len(raw_files) == 1
    assert json.loads(raw_files[0].read_text(encoding="utf-8")) == payload
