"""主Agent与Tavily熔断器之间的运行时衔接。"""

from time import perf_counter

from ..hooks.hooks import trigger_hooks
from ..telemetry import emit
from .web_circuit import TavilyCircuitBreaker


def web_circuit_block_result(
    block,
    circuit: TavilyCircuitBreaker,
    turn_id: str,
    tool_started: float,
) -> dict:
    """为同一模型响应中熔断后的多余网络调用生成配对结果。"""
    output = circuit.blocked_output()
    trigger_hooks(
        "PostToolUse",
        block,
        output,
        perf_counter() - tool_started,
        "blocked",
    )
    emit(
        "AGENT", "WebCircuit",
        action="blocked", turn_id=turn_id,
        tool=block.name, reason=circuit.last_error,
    )
    return {
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": output,
    }


def observe_web_runtime(
    block,
    output,
    circuit: TavilyCircuitBreaker,
    turn_id: str,
) -> tuple[str, int]:
    """更新网络状态并吸收Subagent报告；返回输出和内部失败数。"""
    output, opened = circuit.observe(block.name, output)
    if opened:
        emit(
            "AGENT", "WebCircuit",
            action="opened", turn_id=turn_id,
            reason=circuit.last_error,
            failures=circuit.same_error_count,
        )

    report, opened_from_subagent = circuit.adopt_subagent_result(
        block.name,
        output,
    )
    subagent_failures = 0
    if report is not None:
        try:
            subagent_failures = int(report.get("failed_tool_calls", 0) or 0)
        except (TypeError, ValueError):
            subagent_failures = 0
        emit(
            "AGENT", "SubagentOutcome",
            status=report.get("status"),
            outcome=report.get("outcome"),
            objective_met=report.get("objective_met"),
            failed_tools=subagent_failures,
        )

    if opened_from_subagent:
        emit(
            "AGENT", "WebCircuit",
            action="opened-from-subagent", turn_id=turn_id,
            reason=circuit.last_error,
        )

    return output, subagent_failures
