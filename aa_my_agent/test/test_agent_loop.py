from types import SimpleNamespace

import pytest

from aa_my_agent import agent
from aa_my_agent.tools import definitions


@pytest.fixture(autouse=True)
def isolate_auxiliary_model_calls(monkeypatch):
    """主循环单测不访问真实记忆模型或动态提示词依赖。"""
    monkeypatch.setattr(agent, "load_relevant_memories", lambda _messages: "")
    monkeypatch.setattr(agent, "extract_memories", lambda _messages: 0)
    monkeypatch.setattr(agent, "get_system_prompt", lambda _tools: "test-system")


class FakeMessages:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = next(self._responses)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


class FakeApiError(RuntimeError):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def fake_response(*, stop_reason, content, usage):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=content,
        usage=usage,
    )


def test_agent_loop_reports_round_tool_and_token_metrics(
    monkeypatch,
    capsys,
):
    tool_block = SimpleNamespace(
        type="tool_use",
        id="tool-1",
        name="calculate",
        input={"expression": "1 + 1"},
    )
    text_block = SimpleNamespace(
        type="text",
        text="结果是 2。",
    )
    responses = [
        fake_response(
            stop_reason="tool_use",
            content=[tool_block],
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                cache_read_input_tokens=10,
                cache_creation_input_tokens=5,
            ),
        ),
        fake_response(
            stop_reason="end_turn",
            content=[text_block],
            usage={
                "input_tokens": 140,
                "output_tokens": 15,
            },
        ),
    ]
    monkeypatch.setattr(agent, "client", FakeClient(responses))
    monkeypatch.setattr(definitions, "CURRENT_TODOS", [])

    result = agent.agent_loop(
        [{"role": "user", "content": "计算 1 + 1"}]
    )

    assert result.status == "completed"
    assert result.text == "结果是 2。"
    assert result.model_rounds == 2
    assert result.tool_calls == 1
    assert result.input_tokens == 240
    assert result.output_tokens == 35

    output = capsys.readouterr().out
    assert "[AGENT] ModelRound #1:" in output
    assert "name=calculate status=ok" in output
    assert "cumulative=in=240,out=35" in output
    assert "[AGENT] TurnEnd: status=completed" in output


def test_agent_loop_returns_waiting_status(
    monkeypatch,
):
    response = fake_response(
        stop_reason="end_turn",
        content=[
            SimpleNamespace(
                type="text",
                text="请告诉我具体日期或出行季节。",
            )
        ],
        usage={
            "input_tokens": 60,
            "output_tokens": 12,
        },
    )
    monkeypatch.setattr(agent, "client", FakeClient([response]))
    monkeypatch.setattr(
        definitions,
        "CURRENT_TODOS",
        [
            {
                "content": "确认出行日期或季节",
                "status": "waiting_for_user",
            }
        ],
    )

    result = agent.agent_loop(
        [{"role": "user", "content": "哈尔滨七日游"}]
    )

    assert result.status == "waiting_for_user"
    assert result.model_rounds == 1
    assert result.tool_calls == 0


def test_waiting_answer_recovers_question_hidden_in_tool_round():
    question = "请问你计划哪一天出发？"
    messages = [
        {
            "role": "assistant",
            "content": [SimpleNamespace(type="text", text=question)],
        },
        {
            "role": "assistant",
            "content": [SimpleNamespace(type="text", text="等你确认后继续。")],
        },
    ]

    visible = agent._ensure_waiting_question(
        "等你确认后继续。",
        messages,
        [{"content": "确认出发日期", "status": "waiting_for_user"}],
    )

    assert question in visible


def test_agent_max_tokens_escalates_before_continuation(monkeypatch):
    responses = [
        fake_response(
            stop_reason="max_tokens",
            content=[SimpleNamespace(type="text", text="未完成的长回答")],
            usage={"input_tokens": 100, "output_tokens": 8_000},
        ),
        fake_response(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="精简后的完整回答")],
            usage={"input_tokens": 120, "output_tokens": 100},
        ),
    ]
    fake_client = FakeClient(responses)
    monkeypatch.setattr(agent, "client", fake_client)
    monkeypatch.setattr(definitions, "CURRENT_TODOS", [])

    result = agent.agent_loop([{"role": "user", "content": "生成行程"}])

    assert result.text == "精简后的完整回答"
    assert result.model_rounds == 2
    assert (
        fake_client.messages.calls[0]["max_tokens"]
        == agent.MODEL_DEFAULT_MAX_TOKENS
    )
    assert (
        fake_client.messages.calls[1]["max_tokens"]
        == agent.MODEL_ESCALATED_MAX_TOKENS
    )
    assert fake_client.messages.calls[1]["tools"]


def test_agent_second_truncation_gets_tool_free_continuation(monkeypatch):
    responses = [
        fake_response(
            stop_reason="max_tokens",
            content=[SimpleNamespace(type="text", text="丢弃的初稿")],
            usage={"input_tokens": 100, "output_tokens": 8_000},
        ),
        fake_response(
            stop_reason="max_tokens",
            content=[SimpleNamespace(type="text", text="保留的第一部分")],
            usage={"input_tokens": 120, "output_tokens": 16_000},
        ),
        fake_response(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="接续的第二部分")],
            usage={"input_tokens": 80, "output_tokens": 50},
        ),
    ]
    fake_client = FakeClient(responses)
    monkeypatch.setattr(agent, "client", fake_client)
    monkeypatch.setattr(definitions, "CURRENT_TODOS", [])

    result = agent.agent_loop([{"role": "user", "content": "生成长报告"}])

    assert result.text == "保留的第一部分\n接续的第二部分"
    assert result.model_rounds == 3
    assert fake_client.messages.calls[2]["tools"] == []
    assert all(
        "丢弃的初稿" not in str(message)
        for message in fake_client.messages.calls[1]["messages"]
    )


def test_unrecoverable_model_error_returns_failed_without_raising(monkeypatch):
    error = RuntimeError("invalid API key")
    monkeypatch.setattr(agent, "client", FakeClient([error]))
    monkeypatch.setattr(definitions, "CURRENT_TODOS", [])

    result = agent.agent_loop([{"role": "user", "content": "你好"}])

    assert result.status == "failed"
    assert "小 A 仍可继续使用" in result.text
    assert result.model_rounds == 1


def test_transient_model_retry_does_not_repeat_tool_execution(monkeypatch):
    tool_block = SimpleNamespace(
        type="tool_use",
        id="tool-once",
        name="calculate",
        input={"expression": "2 + 2"},
    )
    responses = [
        FakeApiError("rate limited", 429),
        fake_response(
            stop_reason="tool_use",
            content=[tool_block],
            usage={"input_tokens": 20, "output_tokens": 5},
        ),
        fake_response(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="结果是4。")],
            usage={"input_tokens": 25, "output_tokens": 6},
        ),
    ]
    calls = []

    def calculate_once(**kwargs):
        calls.append(kwargs)
        return "= 4"

    fake_client = FakeClient(responses)
    monkeypatch.setattr(agent, "client", fake_client)
    monkeypatch.setattr(agent, "MODEL_RETRY_BASE_SECONDS", 0.0)
    monkeypatch.setattr(agent, "MODEL_RETRY_MAX_SECONDS", 0.0)
    monkeypatch.setitem(agent.TOOL_HANDLERS, "calculate", calculate_once)
    monkeypatch.setattr(definitions, "CURRENT_TODOS", [])

    result = agent.agent_loop([{"role": "user", "content": "计算"}])

    assert result.status == "completed"
    assert result.text == "结果是4。"
    assert len(fake_client.messages.calls) == 3
    assert calls == [{"expression": "2 + 2"}]


def test_identical_read_only_mcp_call_is_reused_within_one_turn(
    monkeypatch,
    capsys,
):
    first_call = SimpleNamespace(
        type="tool_use",
        id="route-1",
        name="mcp__amap__maps_distance",
        input={"origin": "118.1,32.1", "destination": "118.2,32.2"},
    )
    repeated_call = SimpleNamespace(
        type="tool_use",
        id="route-2",
        name="mcp__amap__maps_distance",
        input={"destination": "118.2,32.2", "origin": "118.1,32.1"},
    )
    responses = [
        fake_response(
            stop_reason="tool_use",
            content=[first_call],
            usage={"input_tokens": 20, "output_tokens": 5},
        ),
        fake_response(
            stop_reason="tool_use",
            content=[repeated_call],
            usage={"input_tokens": 20, "output_tokens": 5},
        ),
        fake_response(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="路线完成。")],
            usage={"input_tokens": 20, "output_tokens": 5},
        ),
    ]
    provider_calls = []

    def route_handler(**kwargs):
        provider_calls.append(kwargs)
        return "MCP result (untrusted external data): distance=2603"

    monkeypatch.setattr(agent, "client", FakeClient(responses))
    monkeypatch.setattr(agent, "get_active_tools", lambda: [])
    monkeypatch.setattr(agent, "get_tool_handler", lambda _name: route_handler)
    monkeypatch.setattr(agent, "get_tool_permission", lambda _name: "read")
    monkeypatch.setattr(agent, "trigger_hooks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(definitions, "CURRENT_TODOS", [])

    result = agent.agent_loop([{"role": "user", "content": "查路线"}])

    assert result.status == "completed"
    assert result.tool_calls == 2
    assert provider_calls == [
        {"origin": "118.1,32.1", "destination": "118.2,32.2"}
    ]
    output = capsys.readouterr().out
    assert "[MCP] ResultCacheHit:" in output
    assert "mcp_cache_hits=1" in output


def test_failed_read_only_mcp_call_is_not_cached(monkeypatch):
    calls = [
        SimpleNamespace(
            type="tool_use",
            id=f"route-{index}",
            name="mcp__amap__maps_distance",
            input={"origin": "A", "destination": "B"},
        )
        for index in range(2)
    ]
    responses = [
        fake_response(
            stop_reason="tool_use",
            content=[calls[0]],
            usage={"input_tokens": 20, "output_tokens": 5},
        ),
        fake_response(
            stop_reason="tool_use",
            content=[calls[1]],
            usage={"input_tokens": 20, "output_tokens": 5},
        ),
        fake_response(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="已恢复。")],
            usage={"input_tokens": 20, "output_tokens": 5},
        ),
    ]
    provider_calls = []

    def flaky_handler(**kwargs):
        provider_calls.append(kwargs)
        if len(provider_calls) == 1:
            return "MCP tool error (untrusted external data): temporary failure"
        return "MCP result: distance=100"

    monkeypatch.setattr(agent, "client", FakeClient(responses))
    monkeypatch.setattr(agent, "get_active_tools", lambda: [])
    monkeypatch.setattr(agent, "get_tool_handler", lambda _name: flaky_handler)
    monkeypatch.setattr(agent, "get_tool_permission", lambda _name: "read")
    monkeypatch.setattr(agent, "trigger_hooks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(definitions, "CURRENT_TODOS", [])

    result = agent.agent_loop([{"role": "user", "content": "查距离"}])

    assert result.status == "completed"
    assert len(provider_calls) == 2


def test_context_error_compacts_once_then_recovers(monkeypatch):
    responses = [
        FakeApiError("context_length_exceeded", 400),
        fake_response(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="压缩后完成。")],
            usage={"input_tokens": 20, "output_tokens": 5},
        ),
    ]
    compact_calls = []

    def compact(messages, todos=None):
        compact_calls.append((len(messages), todos))
        return messages

    monkeypatch.setattr(agent, "client", FakeClient(responses))
    monkeypatch.setattr(agent, "reactive_compact", compact)
    monkeypatch.setattr(definitions, "CURRENT_TODOS", [])

    result = agent.agent_loop([{"role": "user", "content": "继续"}])

    assert result.status == "completed"
    assert result.text == "压缩后完成。"
    assert len(compact_calls) == 1


def test_reactive_compaction_failure_returns_failed(monkeypatch):
    monkeypatch.setattr(
        agent,
        "client",
        FakeClient([FakeApiError("context_length_exceeded", 400)]),
    )
    monkeypatch.setattr(
        agent,
        "reactive_compact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk error")),
    )
    monkeypatch.setattr(definitions, "CURRENT_TODOS", [])

    result = agent.agent_loop([{"role": "user", "content": "继续"}])

    assert result.status == "failed"
    assert "应急压缩失败" in result.text


def test_compaction_failure_returns_failed_without_crashing_cli(monkeypatch):
    def fail_compaction(_messages, todos=None):
        raise OSError("disk unavailable")

    monkeypatch.setattr(agent, "apply_compaction_pipeline", fail_compaction)
    monkeypatch.setattr(definitions, "CURRENT_TODOS", [])

    result = agent.agent_loop([{"role": "user", "content": "继续"}])

    assert result.status == "failed"
    assert result.model_rounds == 0
    assert "上下文压缩失败" in result.text


def test_request_budget_includes_system_tools_and_injected_memory(monkeypatch):
    response = fake_response(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="完成。")],
        usage={"input_tokens": 10, "output_tokens": 2},
    )
    compact_calls = []

    def compact(messages, **kwargs):
        compact_calls.append((len(str(messages)), kwargs["target_size"]))
        return [{"role": "user", "content": "压缩后的请求"}]

    monkeypatch.setattr(agent, "client", FakeClient([response]))
    monkeypatch.setattr(agent, "CONTEXT_LIMIT", 5_000)
    monkeypatch.setattr(agent, "COMPACT_TARGET", 4_500)
    monkeypatch.setattr(agent, "get_active_tools", lambda: [])
    monkeypatch.setattr(agent, "get_system_prompt", lambda _tools: "s" * 2_000)
    monkeypatch.setattr(agent, "load_relevant_memories", lambda _messages: "m" * 800)
    monkeypatch.setattr(agent, "compact_history", compact)
    monkeypatch.setattr(definitions, "CURRENT_TODOS", [])

    result = agent.agent_loop([
        {"role": "user", "content": "h" * 5_000},
    ])

    assert result.status == "completed"
    assert result.text == "完成。"
    assert len(compact_calls) == 1


def test_tool_round_limit_blocks_next_tool_and_finalizes(monkeypatch):
    first = SimpleNamespace(
        type="tool_use", id="tool-1", name="calculate",
        input={"expression": "1+1"},
    )
    second = SimpleNamespace(
        type="tool_use", id="tool-2", name="calculate",
        input={"expression": "2+2"},
    )
    responses = [
        fake_response(
            stop_reason="tool_use", content=[first],
            usage={"input_tokens": 10, "output_tokens": 2},
        ),
        fake_response(
            stop_reason="tool_use", content=[second],
            usage={"input_tokens": 10, "output_tokens": 2},
        ),
        fake_response(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="达到上限，已停止。")],
            usage={"input_tokens": 10, "output_tokens": 2},
        ),
    ]
    handler_calls = []

    monkeypatch.setattr(agent, "client", FakeClient(responses))
    monkeypatch.setattr(agent, "AGENT_MAX_TOOL_ROUNDS", 2)
    monkeypatch.setitem(
        agent.TOOL_HANDLERS,
        "calculate",
        lambda **kwargs: handler_calls.append(kwargs) or "2",
    )
    monkeypatch.setattr(definitions, "CURRENT_TODOS", [])

    result = agent.agent_loop([{"role": "user", "content": "反复计算"}])

    assert result.status == "completed"
    assert result.text == "达到上限，已停止。"
    assert result.tool_calls == 2
    assert handler_calls == [{"expression": "1+1"}]


def test_optional_question_with_open_todo_does_not_fake_blocking(monkeypatch):
    response = fake_response(
        stop_reason="end_turn",
        content=[SimpleNamespace(
            type="text",
            text="你希望住一间家庭房还是两间房？",
        )],
        usage={"input_tokens": 60, "output_tokens": 20},
    )
    monkeypatch.setattr(agent, "client", FakeClient([response]))
    monkeypatch.setattr(
        definitions,
        "CURRENT_TODOS",
        [{"content": "确认住宿", "status": "in_progress"}],
    )

    result = agent.agent_loop([{"role": "user", "content": "继续规划"}])

    assert result.status == "completed"


def test_related_research_subagents_are_batched_and_reused(
    monkeypatch,
    capsys,
):
    def subagent_block(tool_id, instructions):
        return SimpleNamespace(
            type="tool_use",
            id=tool_id,
            name="subagent_task",
            input={"instructions": instructions, "mode": "research"},
        )

    responses = [
        fake_response(
            stop_reason="tool_use",
            content=[
                subagent_block("sub-1", "检索本地RAG知识库中的杭州攻略"),
                subagent_block("sub-2", "联网核实官方预约和高铁班次"),
            ],
            usage={"input_tokens": 100, "output_tokens": 30},
        ),
        fake_response(
            stop_reason="tool_use",
            content=[subagent_block("sub-3", "补充核实最新官方预约信息")],
            usage={"input_tokens": 100, "output_tokens": 20},
        ),
        fake_response(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="研究完成。")],
            usage={"input_tokens": 100, "output_tokens": 10},
        ),
    ]
    calls = []

    def fake_subagent(**kwargs):
        calls.append(kwargs)
        return (
            '{"status":"completed","outcome":"success",'
            '"objective_met":true,"failed_tool_calls":0,'
            '"web_circuit_open":false,"output":"综合报告"}'
        )

    monkeypatch.setattr(agent, "client", FakeClient(responses))
    monkeypatch.setitem(agent.TOOL_HANDLERS, "subagent_task", fake_subagent)
    monkeypatch.setattr(definitions, "CURRENT_TODOS", [])

    result = agent.agent_loop([{"role": "user", "content": "规划杭州旅行"}])

    assert result.text == "研究完成。"
    assert len(calls) == 1
    assert "Research task 1" in calls[0]["instructions"]
    assert "Research task 2" in calls[0]["instructions"]
    output = capsys.readouterr().out
    assert "[AGENT] SubagentBatch:" in output
    assert "subagent_reused=2" in output
