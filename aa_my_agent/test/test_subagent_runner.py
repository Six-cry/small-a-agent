import json
from types import SimpleNamespace

from aa_my_agent.subagents import runner


class FakeMessages:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return next(self._responses)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def response(stop_reason: str, text: str):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=100,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def test_subagent_max_tokens_gets_one_tool_free_finalize_retry(monkeypatch):
    fake_client = FakeClient([
        response("max_tokens", "被截断的研究内容"),
        response("end_turn", "精简、完整并带来源的研究结论"),
    ])
    monkeypatch.setattr(runner, "client", fake_client)

    report = json.loads(runner.spawn_subagent("研究杭州国庆出行"))

    assert report["status"] == "completed"
    assert report["outcome"] == "success"
    assert report["objective_met"] is True
    assert report["output"] == "精简、完整并带来源的研究结论"
    assert fake_client.messages.calls[1]["tools"] == []


def test_subagent_second_truncation_is_completed_but_degraded(monkeypatch):
    fake_client = FakeClient([
        response("max_tokens", "第一次被截断"),
        response("max_tokens", "第二次仍被截断"),
    ])
    monkeypatch.setattr(runner, "client", fake_client)

    report = json.loads(runner.spawn_subagent("研究杭州国庆出行"))

    assert report["status"] == "completed"
    assert report["outcome"] == "degraded"
    assert report["objective_met"] is False
    assert report["reason"] == "max_tokens"
    assert report["output"] == "第二次仍被截断"


def test_subagent_skips_successful_duplicate_web_request(monkeypatch):
    first = SimpleNamespace(
        type="tool_use",
        id="search-1",
        name="web_search",
        input={"query": "杭州 国庆", "max_results": 3},
    )
    duplicate = SimpleNamespace(
        type="tool_use",
        id="search-2",
        name="web_search",
        input={"query": "  杭州   国庆  ", "max_results": 3},
    )
    tool_round = SimpleNamespace(
        stop_reason="tool_use",
        content=[first, duplicate],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=100,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    fake_client = FakeClient([
        tool_round,
        response("end_turn", "已有证据，结束研究。"),
    ])
    executed = []

    def fake_execute(block, _handlers):
        executed.append(block.id)
        return '{"results": [{"url": "https://example.com"}]}'

    monkeypatch.setattr(runner, "client", fake_client)
    monkeypatch.setattr(runner, "execute_subagent_tool", fake_execute)

    report = json.loads(runner.spawn_subagent("研究杭州国庆出行"))

    assert executed == ["search-1"]
    assert report["tool_calls"] == 2
    assert report["duplicate_web_calls"] == 1


def test_subagent_web_budget_blocks_extra_real_requests(monkeypatch):
    first = SimpleNamespace(
        type="tool_use",
        id="search-1",
        name="web_search",
        input={"query": "杭州 国庆"},
    )
    second = SimpleNamespace(
        type="tool_use",
        id="search-2",
        name="web_search",
        input={"query": "杭州 灵隐"},
    )
    tool_round = SimpleNamespace(
        stop_reason="tool_use",
        content=[first, second],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=100,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    fake_client = FakeClient([
        tool_round,
        response("end_turn", "根据已有证据结束研究。"),
    ])
    executed = []

    def fake_execute(block, _handlers):
        executed.append(block.id)
        return '{"results": [{"url": "https://example.com"}]}'

    monkeypatch.setattr(runner, "client", fake_client)
    monkeypatch.setattr(runner, "execute_subagent_tool", fake_execute)
    monkeypatch.setattr(runner, "MAX_SUBAGENT_WEB_REQUESTS", 1)

    report = json.loads(runner.spawn_subagent("研究杭州国庆出行"))

    assert executed == ["search-1"]
    assert report["web_requests"] == 1
    assert report["web_budget_exhausted"] is True
    assert report["blocked_web_calls"] == 1
    assert all(
        tool["name"] not in {"web_search", "fetch_url"}
        for tool in fake_client.messages.calls[1]["tools"]
    )
