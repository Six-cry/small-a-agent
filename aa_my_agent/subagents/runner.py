import json
import time

from ..config import (
    MAX_SUBAGENT_WEB_REQUESTS,
    MODEL,
    SUB_SYSTEM,
    SUB_WORKSPACE_SYSTEM,
    client,
)
from ..tools.definitions import (
    run_bash,
    run_calc,
    run_edit,
    run_glob,
    run_read,
    run_search_knowledge,
    run_write,
    run_web_search,
    run_fetch_url,
)
from ..tools.web_circuit import TavilyCircuitBreaker
from ..tools.schemas import WEB_SEARCH_TOOL, FETCH_URL_TOOL
from ..telemetry import emit, record_model_response

MAX_SUBAGENT_ROUNDS = 30


SUB_TOOLS = [
    WEB_SEARCH_TOOL,
    FETCH_URL_TOOL,
    {
        "name": "read_file",
        "description": (
            "Read file contents. Use offset and limit to read "
            "large files in multiple chunks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to read."},
                "offset": {"type": "integer", "minimum": 0, "description": (
                    "Number of lines to skip before reading. "
                    "Defaults to 0."
                ),},
                "limit": {"type": "integer", "minimum": 1, "description": (
                    "Maximum number of lines to return. "
                    "Omit to read all remaining lines."
                ),},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "glob",
        "description": "Find files matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "calculate",
        "description": "Calculate a mathematical expression.",
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string"},
            },
            "required": ["expression"],
        },
    },
    {
        "name": "search_knowledge",
        "description": "Search the local RAG knowledge base.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 3,
                },
            },
            "required": ["query"],
        },
    },
]


SUB_HANDLERS = {
    "read_file": run_read,
    "glob": run_glob,
    "calculate": run_calc,
    "search_knowledge": run_search_knowledge,
    "web_search": run_web_search,
    "fetch_url": run_fetch_url,
}

WORKSPACE_SUB_TOOLS = [
    *SUB_TOOLS,
    {
        "name": "bash",
        "description": "Run a shell command in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": "Write UTF-8 text content to a workspace file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_file",
        "description": "Replace the first exact text occurrence in a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
    },
]

WORKSPACE_SUB_HANDLERS = {
    **SUB_HANDLERS,
    "bash": run_bash,
    "write_file": run_write,
    "edit_file": run_edit,
}


def extract_text(content) -> str:
    """从 Anthropic 返回的内容块中提取文本。"""
    if not isinstance(content, list):
        return str(content)

    return "\n".join(
        getattr(block, "text", "")
        for block in content
        if getattr(block, "type", None) == "text"
    ).strip()


def _web_request_key(block) -> tuple[str, str] | None:
    """为网页工具生成稳定键；不同深度、域名或结果数不视为重复。"""
    if block.name == "web_search":
        query = " ".join(
            str(block.input.get("query", ""))
            .casefold()
            .split()
        )
        if not query:
            return None
        domains = sorted(
            str(domain).strip().casefold()
            for domain in (block.input.get("include_domains") or [])
            if str(domain).strip()
        )
        key = json.dumps(
            {
                "query": query,
                "search_depth": block.input.get("search_depth", "basic"),
                "max_results": block.input.get("max_results", 3),
                "include_domains": domains,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return "web_search", key

    if block.name == "fetch_url":
        url = (
            str(block.input.get("url", ""))
            .strip()
            .split("#", 1)[0]
            .rstrip("/")
            .casefold()
        )
        return ("fetch_url", url) if url else None

    return None


def execute_subagent_tool(block, handlers: dict) -> str:
    """执行子 Agent 工具，并复用现有 Hook。"""
    # 放在函数内部导入，避免 tools.py 和 hooks.py 循环导入。
    from ..hooks.hooks import trigger_hooks

    tool_started = time.perf_counter()

    # 执行工具前的 Hook
    blocked = trigger_hooks("PreToolUse", block)

    if blocked:
        output = str(blocked)

        trigger_hooks(
            "PostToolUse",
            block,
            output,
            time.perf_counter() - tool_started,
            "blocked",
        )

        return output

    # 查找工具处理函数
    handler = handlers.get(block.name)

    if handler is None:
        output = f"Error: unknown subagent tool {block.name}"

        trigger_hooks(
            "PostToolUse",
            block,
            output,
            time.perf_counter() - tool_started,
            "error",
        )

        return output

    tool_status = "ok"

    # 执行工具
    try:
        output = handler(**block.input)
    except Exception as exc:
        output = f"Error: {type(exc).__name__}: {exc}"
        tool_status = "error"

    # 某些工具不会抛异常，而是直接返回 Error 字符串
    output_text = str(output).lstrip().casefold()

    if output_text.startswith(
        ("error:", "tool execution failed:")
    ):
        tool_status = "error"

    # 执行工具后的 Hook
    trigger_hooks(
        "PostToolUse",
        block,
        output,
        time.perf_counter() - tool_started,
        tool_status,
    )

    return str(output)


def _response_token_count(response) -> int:
    """提取一次模型响应的输入与输出 Token 数。"""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0

    return int(getattr(usage, "input_tokens", 0) or 0) + int(
        getattr(usage, "output_tokens", 0) or 0
    )


def _format_subagent_result(
    *,
    status: str,
    mode: str,
    output: str,
    total_tokens: int,
    started_at: float,
    outcome: str = "success",
    objective_met: bool = True,
    reason: str | None = None,
    tool_calls: int = 0,
    failed_tool_calls: int = 0,
    blocked_web_calls: int = 0,
    duplicate_web_calls: int = 0,
    web_requests: int = 0,
    web_budget_exhausted: bool = False,
    web_circuit_open: bool = False,
    web_error: str | None = None,
) -> str:
    duration_ms = round(
        (
            time.perf_counter()
            - started_at
        )
        * 1000
    )

    return json.dumps(
        {
            "status": status,
            "outcome": outcome,
            "objective_met":
                objective_met,
            "reason": reason,
            "mode": mode,
            "tool_calls": tool_calls,
            "failed_tool_calls":
                failed_tool_calls,
            "blocked_web_calls":
                blocked_web_calls,
            "duplicate_web_calls":
                duplicate_web_calls,
            "web_requests": web_requests,
            "web_budget_exhausted": web_budget_exhausted,
            "web_circuit_open":
                web_circuit_open,
            "web_error": web_error,
            "total_tokens": total_tokens,
            "duration_ms": duration_ms,
            "total_duration_seconds": round(
                duration_ms / 1000,
                3,
            ),
            "output": output,
        },
        ensure_ascii=False,
    )


def spawn_subagent(
    instructions: str,
    mode: str = "research",
) -> str:
    """使用全新上下文执行研究或工作区子任务，并返回结论与指标。"""
    if not isinstance(instructions, str) or not instructions.strip():
        return "Error: subagent instructions cannot be empty"

    if mode not in {"research", "workspace"}:
        return "Error: subagent mode must be 'research' or 'workspace'"

    if mode == "workspace":
        sub_system = SUB_WORKSPACE_SYSTEM
        sub_tools = WORKSPACE_SUB_TOOLS
        sub_handlers = WORKSPACE_SUB_HANDLERS
    else:
        sub_system = SUB_SYSTEM
        sub_tools = SUB_TOOLS
        sub_handlers = SUB_HANDLERS

    started_at = time.perf_counter()
    total_tokens = 0
    rounds = 0
    tool_calls = 0
    failed_tool_calls = 0
    blocked_web_calls = 0
    duplicate_web_calls = 0
    web_requests = 0
    web_budget_exhausted = False
    successful_web_requests: set[tuple[str, str]] = set()
    truncation_retry_used = False
    partial_output = ""
    finalize_only = False
    web_circuit = TavilyCircuitBreaker(
        threshold=2
    )

    emit(
        "SUBAGENT", "Start", color="\033[35m",
        mode=mode, model=MODEL,
        instruction_chars=len(instructions.strip()),
        max_rounds=MAX_SUBAGENT_ROUNDS,
    )

    # 这是子 Agent 上下文隔离的核心。
    messages = [
        {
            "role": "user",
            "content": instructions.strip(),
        }
    ]

    try:
        # 正常研究仍受原有安全上限保护；如果恰好在最后一轮发生输出截断，
        # 额外允许一次“禁用工具、只收尾”的恢复请求。
        for round_index in range(1, MAX_SUBAGENT_ROUNDS + 2):
            if round_index > MAX_SUBAGENT_ROUNDS and not finalize_only:
                break
            round_started = time.perf_counter()

            active_tools = (
                []
                if finalize_only
                else web_circuit.available_tools(sub_tools)
            )
            if web_requests >= MAX_SUBAGENT_WEB_REQUESTS:
                active_tools = [
                    tool
                    for tool in active_tools
                    if tool.get("name") not in {"web_search", "fetch_url"}
                ]

            response = client.messages.create(
                model=MODEL,
                system=sub_system,
                messages=messages,
                tools=active_tools,
                max_tokens=8000,
            )
            rounds = round_index
            usage = record_model_response("subagent", response)
            total_tokens += usage["input"] + usage["output"]
            tool_names = [
                block.name
                for block in response.content
                if getattr(block, "type", None) == "tool_use"
            ]
            emit(
                "SUBAGENT", f"Round #{round_index}", color="\033[90m",
                stop=response.stop_reason,
                tools=",".join(tool_names) if tool_names else "-",
                latency=f"{time.perf_counter() - round_started:.2f}s",
                tokens=(f"in={usage['input']},out={usage['output']},"
                        f"cache_read={usage['cache_read']},"
                        f"cache_create={usage['cache_create']}"),
            )

            messages.append(
                {
                    "role": "assistant",
                    "content": response.content,
                }
            )

            if response.stop_reason == "max_tokens":
                current_partial = extract_text(response.content)
                if current_partial:
                    partial_output = current_partial

                if not truncation_retry_used:
                    truncation_retry_used = True
                    finalize_only = True
                    messages.append({
                        "role": "user",
                        "content": (
                            "<reminder>The previous answer was truncated. "
                            "Do not call more tools. Return one concise, "
                            "self-contained final summary now. Preserve "
                            "verified facts, limitations, and source URLs. "
                            "Do not repeat your reasoning.</reminder>"
                        ),
                    })
                    emit(
                        "SUBAGENT", "Truncation",
                        color="\033[33m",
                        action="finalize-retry",
                        attempt=1,
                        partial_chars=len(partial_output),
                    )
                    continue

            if response.stop_reason != "tool_use":
                result = extract_text(
                    response.content
                ) or partial_output
            
                if response.stop_reason == "max_tokens":
                    outcome = "degraded"
                    objective_met = False
                    reason = "max_tokens"
                elif web_circuit.is_open:
                    outcome = "degraded"
                    objective_met = False
                    reason = "web_unavailable"
                else:
                    outcome = "success"
                    objective_met = True
                    reason = None
            
                emit(
                    "SUBAGENT",
                    "End",
                    color="\033[35m",
                    status="completed",
                    outcome=outcome,
                    objective_met=objective_met,
                    reason=reason,
                    mode=mode,
                    rounds=rounds,
                    tool_calls=tool_calls,
                    failed_tools=failed_tool_calls,
                    blocked_web=blocked_web_calls,
                    duplicate_web=duplicate_web_calls,
                    web_requests=web_requests,
                    web_budget_exhausted=web_budget_exhausted,
                    web_circuit=(
                        "open"
                        if web_circuit.is_open
                        else "closed"
                    ),
                    web_error=web_circuit.last_error,
                    elapsed=(
                        f"{time.perf_counter() - started_at:.2f}s"
                    ),
                    tokens=total_tokens,
                )
            
                return _format_subagent_result(
                    status="completed",
                    outcome=outcome,
                    objective_met=objective_met,
                    reason=reason,
                    mode=mode,
                    output=(
                        result
                        or (
                            "Subagent finished without "
                            "returning a summary."
                        )
                    ),
                    total_tokens=total_tokens,
                    started_at=started_at,
                    tool_calls=tool_calls,
                    failed_tool_calls=
                        failed_tool_calls,
                    blocked_web_calls=
                        blocked_web_calls,
                    duplicate_web_calls=
                        duplicate_web_calls,
                    web_requests=web_requests,
                    web_budget_exhausted=web_budget_exhausted,
                    web_circuit_open=
                        web_circuit.is_open,
                    web_error=
                        web_circuit.last_error,
                )

            tool_results = []

            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_calls += 1
                web_request_key = _web_request_key(block)

                if (
                    web_request_key is not None
                    and web_request_key in successful_web_requests
                ):
                    duplicate_web_calls += 1
                    output = (
                        "Skipped duplicate web request. "
                        "Use the result already present in this "
                        "subagent conversation."
                    )
                    emit(
                        "SUBAGENT", "WebDedup",
                        action="skipped",
                        tool=block.name,
                        duplicates=duplicate_web_calls,
                    )

                elif (
                    block.name in {"web_search", "fetch_url"}
                    and web_requests >= MAX_SUBAGENT_WEB_REQUESTS
                ):
                    blocked_web_calls += 1
                    web_budget_exhausted = True
                    output = (
                        "Web research budget reached for this subagent. "
                        "Use the evidence already collected, mark any "
                        "remaining facts as unverified, and return the "
                        "final summary without more web requests."
                    )
                    emit(
                        "SUBAGENT", "ResearchBudget",
                        action="web-blocked",
                        used=web_requests,
                        limit=MAX_SUBAGENT_WEB_REQUESTS,
                    )

                elif (
                    web_circuit.blocks(block.name)
                ):
                    # 同一模型响应中可能一次请求多个网络工具。
                    # 第二个错误打开熔断后，后续请求直接拦截。
                    blocked_web_calls += 1
                    output = web_circuit.blocked_output()

                    emit(
                        "SUBAGENT",
                        "WebCircuit",
                        action="blocked",
                        tool=block.name,
                        reason=web_circuit.last_error,
                    )

                else:
                    if block.name in {"web_search", "fetch_url"}:
                        web_requests += 1
                        if web_requests >= MAX_SUBAGENT_WEB_REQUESTS:
                            web_budget_exhausted = True
                            emit(
                                "SUBAGENT", "ResearchBudget",
                                action="web-limit-reached",
                                used=web_requests,
                                limit=MAX_SUBAGENT_WEB_REQUESTS,
                            )
                    output = execute_subagent_tool(
                        block,
                        sub_handlers,
                    )

                    output_text = (
                        str(output)
                        .lstrip()
                        .casefold()
                    )

                    if output_text.startswith(
                        (
                            "error:",
                            "tool execution failed:",
                        )
                    ):
                        failed_tool_calls += 1
                    elif web_request_key is not None:
                        successful_web_requests.add(
                            web_request_key
                        )

                    output, circuit_opened = web_circuit.observe(
                        block.name,
                        output,
                    )

                    if circuit_opened:
                        emit(
                            "SUBAGENT",
                            "WebCircuit",
                            action="opened",
                            reason=web_circuit.last_error,
                            failures=(
                                web_circuit
                                .same_error_count
                            ),
                        )

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    }
                )

            messages.append(
                {
                    "role": "user",
                    "content": tool_results,
                }
            )

    except Exception as exc:
        emit(
            "SUBAGENT", "End", color="\033[35m",
            status="error", mode=mode, rounds=rounds,
            tool_calls=tool_calls,
            failed_tools=failed_tool_calls,
            blocked_web=blocked_web_calls,
            duplicate_web=duplicate_web_calls,
            web_requests=web_requests,
            web_budget_exhausted=web_budget_exhausted,
            web_circuit=("open" if web_circuit.is_open else "closed"),
            elapsed=f"{time.perf_counter() - started_at:.2f}s",
            tokens=total_tokens, error=type(exc).__name__,
        )
        return _format_subagent_result(
            status="error",
            outcome="blocked",
            objective_met=False,
            reason="subagent_error",
            mode=mode,
            output=(
                "Subagent failed - "
                f"{type(exc).__name__}: {exc}"
            ),
            total_tokens=total_tokens,
            started_at=started_at,
            tool_calls=tool_calls,
            failed_tool_calls=failed_tool_calls,
            blocked_web_calls=blocked_web_calls,
            duplicate_web_calls=duplicate_web_calls,
            web_requests=web_requests,
            web_budget_exhausted=web_budget_exhausted,
            web_circuit_open=web_circuit.is_open,
            web_error=web_circuit.last_error,
        )

    emit(
        "SUBAGENT", "End", color="\033[35m",
        status="max_rounds", mode=mode, rounds=rounds,
        tool_calls=tool_calls,
        failed_tools=failed_tool_calls,
        blocked_web=blocked_web_calls,
        duplicate_web=duplicate_web_calls,
        web_requests=web_requests,
        web_budget_exhausted=web_budget_exhausted,
        web_circuit=("open" if web_circuit.is_open else "closed"),
        elapsed=f"{time.perf_counter() - started_at:.2f}s",
        tokens=total_tokens,
    )
    return _format_subagent_result(
        status="max_rounds",
        outcome="blocked",
        objective_met=False,
        reason="max_rounds",
        mode=mode,
        output=(
            f"Subagent stopped after {MAX_SUBAGENT_ROUNDS} rounds "
            "without producing a final summary."
        ),
        total_tokens=total_tokens,
        started_at=started_at,
        tool_calls=tool_calls,
        failed_tool_calls=failed_tool_calls,
        blocked_web_calls=blocked_web_calls,
        duplicate_web_calls=duplicate_web_calls,
        web_requests=web_requests,
        web_budget_exhausted=web_budget_exhausted,
        web_circuit_open=web_circuit.is_open,
        web_error=web_circuit.last_error,
    )
