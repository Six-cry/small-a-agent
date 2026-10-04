from .hooks.hooks import trigger_hooks
from .config import (
    AGENT_MAX_TOOL_ROUNDS,
    COMPACT_TARGET,
    CONTEXT_LIMIT,
    FALLBACK_MODEL,
    MAX_REACTIVE_RETRIES,
    MODEL,
    MODEL_DEFAULT_MAX_TOKENS,
    MODEL_ESCALATED_MAX_TOKENS,
    MODEL_MAX_CONSECUTIVE_529,
    MODEL_MAX_OUTPUT_CONTINUATIONS,
    MODEL_MAX_TRANSIENT_RETRIES,
    MODEL_RETRY_BASE_SECONDS,
    MODEL_RETRY_MAX_SECONDS,
    client,
)
from .tools.tools import (
    TOOLS,
    TOOL_HANDLERS,
    get_active_tools,
    get_tool_handler,
    get_tool_permission,
)
from .tools import definitions
from .tools.web_circuit import TavilyCircuitBreaker
from .tools.web_runtime import (
    observe_web_runtime,
    web_circuit_block_result,
)
from .subagents.research_manager import ResearchManager
from copy import deepcopy
from dataclasses import dataclass
import json
from time import perf_counter
from uuid import uuid4
from .context.compaction import (
    apply_compaction_pipeline,
    compact_history,
    estimate_size,
    reactive_compact,
)
from .prompt import get_system_prompt
from .telemetry import (
    emit,
    record_model_response,
    set_turn_id,
    start_turn,
    turn_model_metrics,
)
from .memory import (
    load_relevant_memories,
    inject_relevant_memories,
    extract_memories,
)
from .recovery import (
    ModelRecoveryState,
    call_model_with_retry,
    classify_model_error,
)
from .mcp.turn_cache import read_call_cache_key

rounds_since_todo = 0 # 计数器


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def add_response(self, response) -> "TokenUsage":
        usage = getattr(response, "usage", None)
        self.input_tokens += _usage_value(usage, "input_tokens")
        self.output_tokens += _usage_value(usage, "output_tokens")
        self.cache_read_input_tokens += _usage_value(
            usage,
            "cache_read_input_tokens",
        )
        self.cache_creation_input_tokens += _usage_value(
            usage,
            "cache_creation_input_tokens",
        )
        return self


@dataclass(frozen=True)
class AgentLoopResult:
    status: str
    text: str
    model_rounds: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def _usage_value(usage, field: str) -> int:
    if usage is None:
        return 0

    if isinstance(usage, dict):
        value = usage.get(field, 0)
    else:
        value = getattr(usage, field, 0)

    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def extract_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    def block_text(block) -> str:
        if isinstance(block, dict):
            return str(block.get("text", "")) if block.get("type") == "text" else ""
        return (
            str(getattr(block, "text", ""))
            if getattr(block, "type", None) == "text"
            else ""
        )

    return "\n".join(
        block_text(block)
        for block in content
        if block_text(block).strip()
    ).strip()


def _estimate_model_request_chars(
    system_prompt: str,
    request_messages: list,
    tools: list,
) -> int:
    """Estimate the actual request, including prompt, tools, and memories."""
    return len(json.dumps(
        {
            "system": system_prompt,
            "messages": request_messages,
            "tools": tools,
        },
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    ))


def _ensure_waiting_question(
    final_text: str,
    turn_messages: list,
    todos: list,
) -> str:
    """等待用户时，确保 CLI 最终确实展示了模型提出的问题。"""
    waiting = [
        str(todo.get("content", "")).strip()
        for todo in todos
        if todo.get("status") == "waiting_for_user"
    ]
    if not waiting or any(mark in final_text for mark in ("?", "？")):
        return final_text

    # 工具调用轮中的文字不会被 main.py 打印；优先找回其中真实的问题。
    for message in reversed(turn_messages[:-1]):
        if message.get("role") != "assistant":
            continue
        earlier_text = extract_text(message.get("content"))
        if any(mark in earlier_text for mark in ("?", "？")):
            if final_text and final_text not in earlier_text:
                return f"{earlier_text}\n\n{final_text}"
            return earlier_text

    items = "；".join(item for item in waiting if item)
    question = f"为了继续，请你确认：{items or '缺少的行程信息'}？"
    return f"{final_text}\n\n{question}".strip()


def agent_loop(messages: list) -> AgentLoopResult:
    global rounds_since_todo

    rounds_since_todo = 0
    turn_id = uuid4().hex[:8]
    start_turn(turn_id)
    turn_started = perf_counter()
    # 本轮消息单独保存，不受 messages 的上下文压缩影响。Stop Hook、
    # 日志统计以及后续记忆提取都应读取这份未压缩记录。
    turn_messages = [deepcopy(messages[-1])] if messages else []

    def append_turn_message(message: dict) -> None:
        messages.append(message)
        turn_messages.append(deepcopy(message))

    model_rounds = 0
    tool_calls = 0
    blocked_tools = 0
    failed_tools = 0
    subagent_failed_tools = 0
    subagent_reused = 0
    mcp_cache_hits = 0
    # Keep successful read calls outside ``messages``. L2/L4 compaction can
    # remove old tool results, but cannot erase this per-user-turn record.
    mcp_read_cache: dict[str, object] = {}
    research_manager = ResearchManager()
    web_circuit = TavilyCircuitBreaker(threshold=2)
    token_usage = TokenUsage()

    recovery_state = ModelRecoveryState(MODEL, FALLBACK_MODEL)
    max_tokens = MODEL_DEFAULT_MAX_TOKENS
    max_tokens_escalated = False
    output_continuations = 0
    partial_outputs: list[str] = []
    finalize_only = False
    tool_rounds = 0
    loop_limit_finalize_attempted = False

    emit(
        "AGENT",
        "TurnStart",
        color="\033[90m",
        turn_id=turn_id,
        model=MODEL,
        history_messages=len(messages),
        history_chars=estimate_size(messages),
    )

    relevant_memories = load_relevant_memories(
        messages
    )

    while True:
        if rounds_since_todo >= 3 and messages:
            append_turn_message({
                "role": "user",
                "content": "<reminder>Update your todos.</reminder>",
            })
            rounds_since_todo = 0

        try:
            apply_compaction_pipeline(
                messages,
                todos=definitions.CURRENT_TODOS,
            )
        except Exception as exc:
            failure_text = (
                "本轮上下文压缩失败，但小 A 进程仍可继续使用。"
                f"错误类型：{type(exc).__name__}。请稍后重试当前请求。"
            )
            append_turn_message({"role": "assistant", "content": failure_text})
            emit(
                "AGENT", "TurnEnd", color="\033[31m",
                status="failed", status_reason="compaction_error",
                turn_id=turn_id, model_rounds=model_rounds,
                tool_calls=tool_calls, error=type(exc).__name__,
                elapsed=f"{perf_counter() - turn_started:.2f}s",
            )
            set_turn_id(None)
            return AgentLoopResult(
                status="failed",
                text=failure_text,
                model_rounds=model_rounds,
                tool_calls=tool_calls,
                input_tokens=token_usage.input_tokens,
                output_tokens=token_usage.output_tokens,
            )

        model_started = perf_counter()
        try:
            active_tools = (
                []
                if finalize_only
                else web_circuit.available_tools(get_active_tools())
            )
            system_prompt = get_system_prompt(
                tool["name"] for tool in active_tools
            )
            request_messages = inject_relevant_memories(
                messages,
                relevant_memories,
            )

            request_chars = _estimate_model_request_chars(
                system_prompt,
                request_messages,
                active_tools,
            )
            history_chars = estimate_size(messages)
            non_history_chars = max(
                0,
                request_chars - estimate_size(request_messages),
            ) + len(relevant_memories)
            history_budget = max(
                4_000,
                CONTEXT_LIMIT - non_history_chars - 2_000,
            )
            if (
                request_chars > CONTEXT_LIMIT
                and history_chars > history_budget
            ):
                before_chars = history_chars
                messages[:] = compact_history(
                    messages,
                    todos=definitions.CURRENT_TODOS,
                    keep_recent=4,
                    marker="Request budget compact",
                    target_size=min(COMPACT_TARGET, history_budget),
                )
                after_chars = estimate_size(messages)
                emit(
                    "COMPACT", "RequestBudget", color="\033[33m",
                    turn_id=turn_id,
                    request_chars=request_chars,
                    history_budget=history_budget,
                    history_chars=f"{before_chars}->{after_chars}",
                )
                if after_chars < before_chars:
                    continue

            model_rounds += 1
            response = call_model_with_retry(
                lambda active_model: client.messages.create(
                    model=active_model,
                    system=system_prompt,
                    messages=request_messages,
                    tools=active_tools,
                    max_tokens=max_tokens,
                ),
                recovery_state,
                max_retries=MODEL_MAX_TRANSIENT_RETRIES,
                base_delay_seconds=MODEL_RETRY_BASE_SECONDS,
                max_delay_seconds=MODEL_RETRY_MAX_SECONDS,
                max_consecutive_overloads=MODEL_MAX_CONSECUTIVE_529,
                on_event=lambda event, **fields: emit(
                    "RECOVERY",
                    "ModelRetry" if event == "retry" else "RetryExhausted",
                    color="\033[33m" if event == "retry" else "\033[31m",
                    turn_id=turn_id,
                    **fields,
                ),
            )

        except Exception as exc:
            error_kind = classify_model_error(exc)
            if (
                error_kind == "context_length"
                and recovery_state.context_compactions
                < MAX_REACTIVE_RETRIES
            ):
                recovery_state.context_compactions += 1
                emit(
                    "COMPACT",
                    "Reactive",
                    color="\033[31m",
                    turn_id=turn_id,
                    retry=recovery_state.context_compactions,
                    reason="context-error",
                )
                try:
                    messages[:] = reactive_compact(
                        messages,
                        todos=definitions.CURRENT_TODOS,
                    )
                except Exception as compact_exc:
                    failure_text = (
                        "上下文超限后的应急压缩失败，但小 A 进程仍可继续使用。"
                        f"错误类型：{type(compact_exc).__name__}。"
                        "请稍后重试当前请求。"
                    )
                    append_turn_message({
                        "role": "assistant",
                        "content": failure_text,
                    })
                    emit(
                        "AGENT", "TurnEnd", color="\033[31m",
                        status="failed",
                        status_reason="reactive_compaction_error",
                        turn_id=turn_id,
                        model_rounds=model_rounds,
                        tool_calls=tool_calls,
                        error=type(compact_exc).__name__,
                        elapsed=f"{perf_counter() - turn_started:.2f}s",
                    )
                    set_turn_id(None)
                    return AgentLoopResult(
                        status="failed",
                        text=failure_text,
                        model_rounds=model_rounds,
                        tool_calls=tool_calls,
                        input_tokens=token_usage.input_tokens,
                        output_tokens=token_usage.output_tokens,
                    )
                continue  # 用压缩后的上下文重试

            failure_text = (
                "本轮模型请求失败，但小 A 仍可继续使用。"
                f"错误类型：{error_kind}（{type(exc).__name__}）。"
                "请稍后重试当前请求。"
            )
            append_turn_message({
                "role": "assistant",
                "content": failure_text,
            })
            emit(
                "AGENT",
                f"ModelRound #{model_rounds}",
                color="\033[90m",
                turn_id=turn_id,
                status="error",
                latency=f"{perf_counter() - model_started:.2f}s",
                reason=error_kind,
                error=type(exc).__name__,
            )
            emit(
                "AGENT", "TurnEnd", color="\033[90m",
                status="failed",
                status_reason=error_kind,
                turn_id=turn_id,
                model_rounds=model_rounds,
                tool_calls=tool_calls,
                elapsed=f"{perf_counter() - turn_started:.2f}s",
                transient_retries=recovery_state.transient_retries,
                fallback_switches=recovery_state.fallback_switches,
                context_compactions=recovery_state.context_compactions,
                mcp_cache_hits=mcp_cache_hits,
            )
            set_turn_id(None)
            return AgentLoopResult(
                status="failed",
                text=failure_text,
                model_rounds=model_rounds,
                tool_calls=tool_calls,
                input_tokens=token_usage.input_tokens,
                output_tokens=token_usage.output_tokens,
            )

        model_latency = perf_counter() - model_started
        record_model_response("main", response)
        round_usage = TokenUsage().add_response(response)
        token_usage.add_response(response)
        tool_names = [
            block.name
            for block in response.content
            if getattr(block, "type", None) == "tool_use"
        ]
        tools_text = ",".join(tool_names) if tool_names else "-"
        emit(
            "AGENT",
            f"ModelRound #{model_rounds}",
            color="\033[90m",
            turn_id=turn_id,
            stop=response.stop_reason,
            latency=f"{model_latency:.2f}s",
            model=recovery_state.current_model,
            tools=tools_text,
            tokens=(
                f"in={round_usage.input_tokens},"
                f"out={round_usage.output_tokens},"
                f"cache_read={round_usage.cache_read_input_tokens},"
                f"cache_create={round_usage.cache_creation_input_tokens}"
            ),
            cumulative=(
                f"in={token_usage.input_tokens},"
                f"out={token_usage.output_tokens}"
            ),
        )
        if response.stop_reason == "max_tokens":
            current_partial = extract_text(response.content)
            if (
                not max_tokens_escalated
                and MODEL_ESCALATED_MAX_TOKENS > max_tokens
            ):
                previous_max_tokens = max_tokens
                max_tokens = MODEL_ESCALATED_MAX_TOKENS
                max_tokens_escalated = True
                emit(
                    "RECOVERY", "OutputLimit", color="\033[33m",
                    turn_id=turn_id,
                    action="escalate",
                    previous_max_tokens=previous_max_tokens,
                    max_tokens=max_tokens,
                    discarded_partial_chars=len(current_partial),
                )
                continue

            if current_partial:
                partial_outputs.append(current_partial)
                append_turn_message({
                    "role": "assistant",
                    "content": current_partial,
                })

            if output_continuations < MODEL_MAX_OUTPUT_CONTINUATIONS:
                output_continuations += 1
                finalize_only = True
                append_turn_message({
                    "role": "user",
                    "content": (
                        "<reminder>The previous answer was truncated. "
                        "Do not call more tools. Return one concise, "
                        "self-contained final answer now. Preserve all "
                        "confirmed user constraints and source URLs. "
                        "Do not repeat your reasoning.</reminder>"
                    ),
                })
                emit(
                    "RECOVERY", "OutputLimit", color="\033[33m",
                    turn_id=turn_id,
                    action="continue",
                    attempt=output_continuations,
                    max_continuations=MODEL_MAX_OUTPUT_CONTINUATIONS,
                    partial_chars=len(current_partial),
                )
                continue
            emit(
                "RECOVERY", "OutputLimit", color="\033[31m",
                turn_id=turn_id,
                action="exhausted",
                continuations=output_continuations,
            )
        else:
            append_turn_message({
                "role": "assistant",
                "content": response.content,
            })

        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", turn_messages)
            if force:
                append_turn_message({
                    "role": "user",
                    "content": (
                        str(force)
                        + "\n<visibility>"
                        + "The previous attempted assistant "
                        + "response was not shown to the user. "
                        + "Include the complete answer in the "
                        + "eventual final response."
                        + "</visibility>"
                    ),
                })
                continue

            # 新增：这一轮真正结束后，提取长期记忆
            extract_memories(
                turn_messages
            )

            todo_waiting = any(
                todo["status"] == "waiting_for_user"
                for todo in definitions.CURRENT_TODOS
            )
            active_todos = sum(
                1
                for todo in definitions.CURRENT_TODOS
                if todo.get("status") in {"pending", "in_progress"}
            )
            current_text = extract_text(response.content)
            if partial_outputs:
                completed_parts = list(partial_outputs)
                if response.stop_reason != "max_tokens" and current_text:
                    completed_parts.append(current_text)
                raw_final_text = "\n".join(completed_parts).strip()
            else:
                raw_final_text = current_text
            if not raw_final_text and response.stop_reason == "max_tokens":
                raw_final_text = (
                    "回答多次达到输出上限，未能生成可用的最终文本。"
                )
            # 只把模型明确写入 Todo 的等待状态当成真正阻塞。
            # 可选追问不会再把已经交付的任务误判为 waiting。
            waiting_for_user = todo_waiting
            status = (
                "waiting_for_user"
                if waiting_for_user
                else "completed"
            )
            final_text = raw_final_text
            final_text = _ensure_waiting_question(
                final_text,
                turn_messages,
                definitions.CURRENT_TODOS,
            )
            all_model_metrics = turn_model_metrics()
            component_calls = ",".join(
                f"{name}={values['calls']}"
                for name, values in all_model_metrics["components"].items()
            ) or "-"
            total_model_usage = all_model_metrics["total"]
            emit(
                "AGENT",
                "TurnEnd",
                color="\033[90m",
                status=status,
                status_reason=(
                    "todo_waiting"
                    if todo_waiting
                    else (
                        "end_turn_with_open_todos"
                        if active_todos
                        else "end_turn"
                    )
                ),
                open_todos=active_todos,
                turn_id=turn_id,
                model_rounds=model_rounds,
                tool_calls=tool_calls,
                blocked=blocked_tools,
                failed=failed_tools,
                subagent_failed=subagent_failed_tools,
                subagent_reused=subagent_reused,
                mcp_cache_hits=mcp_cache_hits,
                web_circuit=("open" if web_circuit.is_open else "closed"),
                web_error=web_circuit.last_error,
                finish_reason=(
                    "max_tokens"
                    if response.stop_reason == "max_tokens"
                    else response.stop_reason
                ),
                elapsed=f"{perf_counter() - turn_started:.2f}s",
                model_calls=(
                    f"total={total_model_usage['calls']},"
                    f"{component_calls}"
                ),
                tokens_scope="main-agent-only",
                tokens=(
                    f"in={token_usage.input_tokens},"
                    f"out={token_usage.output_tokens},"
                    f"cache_read={token_usage.cache_read_input_tokens},"
                    f"cache_create={token_usage.cache_creation_input_tokens}"
                ),
                all_model_tokens=(
                    f"in={total_model_usage['input']},"
                    f"out={total_model_usage['output']},"
                    f"cache_read={total_model_usage['cache_read']},"
                    f"cache_create={total_model_usage['cache_create']}"
                ),
                transient_retries=recovery_state.transient_retries,
                fallback_switches=recovery_state.fallback_switches,
                context_compactions=recovery_state.context_compactions,
                output_continuations=output_continuations,
            )
            set_turn_id(None)
            return AgentLoopResult(
                status=status,
                text=final_text,
                model_rounds=model_rounds,
                tool_calls=tool_calls,
                input_tokens=token_usage.input_tokens,
                output_tokens=token_usage.output_tokens,
            )

        tool_rounds += 1
        if tool_rounds >= AGENT_MAX_TOOL_ROUNDS:
            limit_results = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                tool_calls += 1
                blocked_tools += 1
                limit_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": (
                        "Permission denied: per-turn tool-round safety limit "
                        f"reached ({AGENT_MAX_TOOL_ROUNDS})."
                    ),
                })
            if not loop_limit_finalize_attempted:
                limit_results.append({
                    "type": "text",
                    "text": (
                        "<reminder>The per-turn tool-round safety limit was "
                        "reached. Do not call tools. Give a concise final "
                        "status using only confirmed results already present, "
                        "and clearly state any unfinished work.</reminder>"
                    ),
                })
            append_turn_message({"role": "user", "content": limit_results})
            emit(
                "AGENT", "ToolRoundLimit", color="\033[31m",
                turn_id=turn_id,
                limit=AGENT_MAX_TOOL_ROUNDS,
                requested=len(limit_results),
                action=(
                    "stop"
                    if loop_limit_finalize_attempted
                    else "finalize-without-tools"
                ),
            )
            if loop_limit_finalize_attempted:
                failure_text = (
                    "本轮已达到工具调用安全上限，且模型未能在禁用工具后结束。"
                    "为避免重复执行和额外费用，本轮已停止；你可以下一轮继续。"
                )
                append_turn_message({
                    "role": "assistant",
                    "content": failure_text,
                })
                set_turn_id(None)
                return AgentLoopResult(
                    status="failed",
                    text=failure_text,
                    model_rounds=model_rounds,
                    tool_calls=tool_calls,
                    input_tokens=token_usage.input_tokens,
                    output_tokens=token_usage.output_tokens,
                )
            loop_limit_finalize_attempted = True
            finalize_only = True
            continue

        rounds_since_todo += 1
        results = []
        research_batch = research_manager.prepare_batch(
            response.content,
            turn_id,
        )

        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_calls += 1
            tool_started = perf_counter()

            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                blocked_tools += 1
                trigger_hooks(
                    "PostToolUse",
                    block,
                    str(blocked),
                    perf_counter() - tool_started,
                    "blocked",
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(blocked),
                    }
                )
                continue

            research_resolution = research_manager.resolve(
                block,
                research_batch,
                turn_id,
            )
            if research_resolution.reused:
                subagent_reused += 1
                output = research_resolution.reused_output
                trigger_hooks(
                    "PostToolUse", block, output,
                    perf_counter() - tool_started, "ok",
                )
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })
                continue

            if web_circuit.blocks(block.name):
                blocked_tools += 1
                results.append(web_circuit_block_result(
                    block, web_circuit, turn_id, tool_started,
                ))
                continue

            execution_block = research_resolution.execution_block
            handler = get_tool_handler(execution_block.name)
            tool_status = "ok"
            mcp_cache_key = read_call_cache_key(
                execution_block.name,
                execution_block.input,
                get_tool_permission(execution_block.name),
            )
            if mcp_cache_key is not None and mcp_cache_key in mcp_read_cache:
                output = mcp_read_cache[mcp_cache_key]
                mcp_cache_hits += 1
                emit(
                    "MCP",
                    "ResultCacheHit",
                    color="\033[36m",
                    turn_id=turn_id,
                    tool=execution_block.name,
                )
            else:
                try:
                    if handler is None:
                        raise LookupError(
                            f"No handler registered for {execution_block.name}"
                        )
                    output = handler(**execution_block.input)
                except Exception as e:
                    output = f"Tool execution failed: {type(e).__name__}: {e}"
                    tool_status = "error"

            output_text = str(output).lstrip().casefold()
            if output_text.startswith((
                "error:",
                "tool execution failed:",
                "mcp tool error",
                "mcp tool unavailable:",
            )):
                tool_status = "error"
            if tool_status == "error":
                failed_tools += 1
            elif mcp_cache_key is not None:
                mcp_read_cache[mcp_cache_key] = output

            output, subagent_failures = observe_web_runtime(
                execution_block,
                output,
                web_circuit,
                turn_id,
            )
            subagent_failed_tools += subagent_failures

            research_manager.observe(
                block,
                execution_block,
                output,
                research_batch,
            )

            trigger_hooks(
                "PostToolUse",
                block,
                output,
                perf_counter() - tool_started,
                tool_status,
            )

            if block.name == "todo_write": # 调用这个函数就是更新了最新的人物列表，提醒步数清0
                rounds_since_todo = 0

            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        append_turn_message({"role": "user", "content": results})
