"""小 A 的四层上下文压缩管线。

生产管线执行顺序：
1. tool_result_budget：把单个超大工具结果保存到磁盘；
2. compact_history：超过上下文阈值时，用完整材料总结旧历史。

`snip_compact` 与 `micro_compact` 仍保留给兼容性测试和显式实验，但生产
入口不再在没有摘要覆盖的情况下调用它们。

发生有损压缩前会保存 transcript。API 仍报告上下文过长时，
reactive_compact 会执行一次更激进的应急压缩。
"""

import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from ..config import (
    client,
    MODEL,
    CONTEXT_LIMIT,
    COMPACT_TARGET,
    L2_TRIGGER_CHARS,
    L2_TARGET_CHARS,
    L2_MIN_RECLAIM_CHARS,
    L2_RESULT_PREVIEW_CHARS,
    KEEP_RECENT_TOOL_RESULTS,
    PERSIST_THRESHOLD,
    TOOL_RESULT_BUDGET_BYTES,
    TOOL_RESULTS_DIR,
    TRANSCRIPT_DIR,
)
from ..telemetry import emit, record_model_response


SNIPPED_MARKER = "[conversation middle compacted]"
TOOL_RESULT_MARKER = "[Earlier tool result compacted."
MIN_MODEL_SUMMARY_CHARS = 200


# ── 通用辅助函数 ──────────────────────────────────────────
def estimate_size(messages: list) -> int:
    """粗略估算消息序列化后的字符数，不依赖具体模型 tokenizer。"""
    return len(
        json.dumps(
            messages,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
    )


def _block_type(block):
    """兼容字典块和 Anthropic SDK 内容对象。"""
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def _message_has_tool_use(msg: dict) -> bool:
    """判断 assistant 消息是否包含 tool_use。"""
    content = msg.get("content")
    return (
        msg.get("role") == "assistant"
        and isinstance(content, list)
        and any(_block_type(block) == "tool_use" for block in content)
    )


def _is_tool_result_message(msg: dict) -> bool:
    """判断 user 消息是否包含 tool_result。"""
    content = msg.get("content")
    return (
        msg.get("role") == "user"
        and isinstance(content, list)
        and any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )
    )


def _collect_tool_results(messages: list) -> list[tuple[int, int, dict]]:
    """收集所有 tool_result 块及其位置。"""
    blocks = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                blocks.append((message_index, block_index, block))
    return blocks


def _safe_tool_id(tool_use_id: str) -> str:
    """把模型给出的工具 ID 转换为安全文件名，防止路径穿越。"""
    raw_id = str(tool_use_id or "unknown")
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_id).strip("._")
    return (safe_id or "unknown")[:80]


def _is_persisted_output(content: str) -> bool:
    return str(content).startswith("<persisted-output>")


# ── L3：超大工具结果落盘 ───────────────────────────────────
def persist_large_output(tool_use_id: str, output: str, force: bool = False) -> str:
    """保存工具输出，只在上下文中保留文件位置和前 2000 字符。"""
    output = str(output)
    if _is_persisted_output(output):
        return output
    if not force and len(output) <= PERSIST_THRESHOLD:
        return output

    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(output.encode("utf-8")).hexdigest()[:12]
    path = TOOL_RESULTS_DIR / f"{_safe_tool_id(tool_use_id)}_{digest}.txt"
    action = "reused" if path.exists() else "created"
    if not path.exists():
        path.write_text(output, encoding="utf-8")

    compacted_output = (
        "<persisted-output>\n"
        f"Full output: {path}\n"
        f"Preview:\n{output[:2000]}\n"
        "</persisted-output>"
    )
    emit(
        "COMPACT", "L3",
        action="persist-output", file_action=action,
        tool_id=_safe_tool_id(tool_use_id),
        chars=f"{len(output)}->{len(compacted_output)}", file=path,
    )
    return compacted_output


def tool_result_budget(
    messages: list,
    max_bytes: int = TOOL_RESULT_BUDGET_BYTES,
) -> list:
    """控制最新一批工具结果的大小。

    两条规则相互独立：
    - 单个结果超过 PERSIST_THRESHOLD，一定落盘；
    - 处理后总量仍超过 max_bytes，继续从最大的结果开始强制落盘。
    """
    if not messages:
        return messages

    last = messages[-1]
    content = last.get("content")
    if last.get("role") != "user" or not isinstance(content, list):
        return messages

    blocks = [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    if not blocks:
        return messages

    # 先执行“单个结果阈值”，不能依赖总预算是否超标。
    for block in blocks:
        output = str(block.get("content", ""))
        if len(output) > PERSIST_THRESHOLD:
            block["content"] = persist_large_output(
                block.get("tool_use_id", "unknown"),
                output,
            )

    def current_total() -> int:
        return sum(len(str(block.get("content", ""))) for block in blocks)

    # 如果很多中等结果加起来仍太大，则从最大的未落盘结果开始处理。
    while current_total() > max_bytes:
        candidates = [
            block
            for block in blocks
            if not _is_persisted_output(block.get("content", ""))
        ]
        if not candidates:
            break
        largest = max(candidates, key=lambda block: len(str(block.get("content", ""))))
        largest["content"] = persist_large_output(
            largest.get("tool_use_id", "unknown"),
            str(largest.get("content", "")),
            force=True,
        )

    return messages


# ── L1：裁剪消息中段 ───────────────────────────────────────
def snip_compact(
    messages: list,
    max_messages: int = 50,
    transcript_path: Path | None = None,
) -> list:
    """消息数过多时保留开头和结尾，并保护工具调用配对。"""
    if len(messages) <= max_messages:
        return messages
    if max_messages < 3:
        raise ValueError("max_messages must be at least 3")

    keep_head = min(3, max_messages - 2)
    # 还要给中间的占位消息留一个位置。
    keep_tail = max_messages - keep_head - 1
    head_end = keep_head
    tail_start = len(messages) - keep_tail

    # 如果头部最后一条是 tool_use，继续保留紧随其后的 tool_result。
    if head_end > 0 and _message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_result_message(messages[head_end]):
            head_end += 1

    # 如果尾部从 tool_result 开始，把与它配对的 tool_use 一并保留。
    if (
        tail_start > 0
        and tail_start < len(messages)
        and _is_tool_result_message(messages[tail_start])
        and _message_has_tool_use(messages[tail_start - 1])
    ):
        tail_start -= 1

    if head_end >= tail_start:
        return messages

    snipped = tail_start - head_end
    placeholder = {
        "role": "user",
        "content": (
            f"{SNIPPED_MARKER} Removed {snipped} messages. "
            "Do not inspect transcript or storage files to recover them."
        ),
    }
    return messages[:head_end] + [placeholder] + messages[tail_start:]


# ── L2：压缩旧工具结果 ─────────────────────────────────────
def _old_tool_results(
    messages: list,
    keep_recent: int,
) -> list:
    """返回不属于最近保护区的工具结果。"""
    tool_results = _collect_tool_results(messages)

    if len(tool_results) <= keep_recent:
        return []

    return (
        tool_results[:-keep_recent]
        if keep_recent
        else tool_results
    )


def _micro_replacement(output: str) -> str:
    """生成通用的工具结果压缩回执。"""
    output = str(output)

    return (
        f"{TOOL_RESULT_MARKER} "
        f"original_chars={len(output)}]\n"
        "Preview:\n"
        f"{output[:L2_RESULT_PREVIEW_CHARS]}\n"
        "Do not inspect transcript or storage files "
        "to recover omitted content."
    )


def _micro_candidates(
    messages: list,
    keep_recent: int | None = None,
) -> list[dict]:
    """找出真正能够释放上下文空间的旧工具结果。"""
    if keep_recent is None:
        keep_recent = KEEP_RECENT_TOOL_RESULTS

    candidates = []

    for _, _, block in _old_tool_results(
        messages,
        keep_recent,
    ):
        output = str(block.get("content", ""))

        if output.startswith(TOOL_RESULT_MARKER):
            continue

        replacement = _micro_replacement(output)
        reclaimable = len(output) - len(replacement)

        # 替换后必须确实变小
        if reclaimable <= 0:
            continue

        candidates.append({
            "block": block,
            "replacement": replacement,
            "reclaimable": reclaimable,
        })

    return candidates


def _micro_reclaimable_chars(
    messages: list,
    keep_recent: int | None = None,
) -> int:
    """估算本轮L2最多能够释放多少字符。"""
    return sum(
        item["reclaimable"]
        for item in _micro_candidates(
            messages,
            keep_recent,
        )
    )


def _will_micro_compact(messages: list) -> bool:
    """根据上下文水位和预计收益判断是否执行L2。"""
    current_size = estimate_size(messages)

    if current_size <= L2_TRIGGER_CHARS:
        return False

    reclaimable = _micro_reclaimable_chars(messages)

    return reclaimable >= L2_MIN_RECLAIM_CHARS


def micro_compact(
    messages: list,
    transcript_path: Path | None = None,
    keep_recent: int | None = None,
    target_size: int | None = None,
) -> list:
    """
    从最旧的工具结果开始压缩。

    target_size为None时压缩所有有收益的旧结果；
    指定target_size时，达到目标大小后立即停止。
    """
    if keep_recent is None:
        keep_recent = KEEP_RECENT_TOOL_RESULTS

    if keep_recent < 0:
        raise ValueError(
            "keep_recent must be non-negative"
        )

    if target_size is not None and target_size <= 0:
        raise ValueError(
            "target_size must be positive"
        )

    candidates = _micro_candidates(
        messages,
        keep_recent,
    )

    for item in candidates:
        if (
            target_size is not None
            and estimate_size(messages) <= target_size
        ):
            break

        item["block"]["content"] = item["replacement"]

    return messages


# ── transcript 与 LLM 摘要 ────────────────────────────────
def _write_transcript(messages: list) -> Path:
    """把压缩前的消息写成 JSONL；随机后缀避免高频写入时覆盖。"""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / (
        f"transcript_{time.time_ns()}_{uuid.uuid4().hex[:8]}.jsonl"
    )
    with path.open("w", encoding="utf-8") as file:
        for message in messages:
            file.write(
                json.dumps(message, ensure_ascii=False, default=str) + "\n"
            )
    return path


def _summary_input(messages: list, todos: list | None = None) -> str:
    """Serialize the complete material that the summary must cover.

    The old implementation silently removed the middle after 80,000
    characters. That is dangerous for pasted itineraries because a date or
    budget can disappear before the summary model ever sees it. A provider
    context error is safer: the caller can use the explicit deterministic
    fallback instead of treating a partial input as complete.
    """
    return json.dumps(
        {"messages": messages, "current_todos": todos or []},
        ensure_ascii=False,
        default=str,
    )


def _bounded_excerpt(value: str, max_chars: int) -> str:
    """Keep both ends when a deterministic fallback must bound one value."""
    text = str(value)
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    marker = "\n...[bounded middle omitted]...\n"
    available = max(0, max_chars - len(marker))
    head = (available * 2) // 3
    tail = available - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def _message_text(message: dict) -> str:
    """只提取可展示文本，不读取 thinking，也不展开巨大工具结果。"""
    content = message.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        if _block_type(block) != "text":
            continue
        text = (
            block.get("text", "")
            if isinstance(block, dict)
            else getattr(block, "text", "")
        )
        if str(text).strip():
            parts.append(str(text).strip())
    return "\n".join(parts)


def _bounded_recent_texts(
    messages: list,
    role: str,
    *,
    max_chars: int,
    per_message: int = 2_000,
) -> list[str]:
    """从新到旧挑选指定角色的文本，再恢复时间顺序。"""
    selected = []
    used = 0
    for message in reversed(messages):
        if message.get("role") != role:
            continue
        text = _message_text(message)
        if not text or text.startswith(("<reminder>", "[Compacted")):
            continue
        text = _bounded_excerpt(text, per_message)
        remaining = max_chars - used
        if remaining <= 0:
            break
        selected.append(text[:remaining])
        used += min(len(text), remaining)
    return list(reversed(selected))

def _latest_compaction_summary(
    messages: list,
    max_chars: int = 10_000,
) -> str:
    """读取最近一次L4或应急压缩生成的摘要。"""
    prefixes = (
        "[Compacted conversation summary]",
        "[Reactive compact conversation summary]",
    )

    for message in reversed(messages):
        if message.get("role") != "user":
            continue

        content = message.get("content", "")
        if not isinstance(content, str):
            continue

        text = content.strip()

        if text.startswith(prefixes):
            return _bounded_excerpt(text, max_chars)

    return ""

def _recent_domain_tool_facts(
    messages: list,
    *,
    max_chars: int = 4_000,
    per_result: int = 1_000,
) -> list[str]:
    """限量保留时间、RAG、联网等领域工具的近期结果。"""
    tool_names = {}
    for message in messages:
        content = message.get("content")
        if message.get("role") != "assistant" or not isinstance(content, list):
            continue
        for block in content:
            if _block_type(block) != "tool_use":
                continue
            tool_id = (
                block.get("id")
                if isinstance(block, dict)
                else getattr(block, "id", None)
            )
            tool_name = (
                block.get("name")
                if isinstance(block, dict)
                else getattr(block, "name", None)
            )
            if tool_id and tool_name:
                tool_names[str(tool_id)] = str(tool_name)

    useful_tools = {
        "get_current_time",
        "search_knowledge",
        "web_search",
        "fetch_url",
        "calculate",
        "subagent_task",
    }
    selected = []
    used = 0
    for message in reversed(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block in reversed(content):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_name = tool_names.get(str(block.get("tool_use_id", "")), "")
            if tool_name not in useful_tools and not tool_name.startswith("mcp__"):
                continue
            result = str(block.get("content", "")).strip()
            if not result or result.startswith(TOOL_RESULT_MARKER):
                continue
            result = _bounded_excerpt(result, per_result)
            remaining = max_chars - used
            if remaining <= 0:
                break
            selected.append(f"{tool_name}: {result[:remaining]}")
            used += min(len(result), remaining)
        if used >= max_chars:
            break
    return list(reversed(selected))


def _fallback_summary(
    messages: list,
    todos: list | None = None,
) -> str:
    """
    模型摘要不可用时，确定性保留上一轮摘要、
    用户原话、近期工具事实和任务状态。
    """
    previous_summary = _latest_compaction_summary(messages)

    user_texts = _bounded_recent_texts(
        messages,
        "user",
        max_chars=10_000,
    )

    assistant_texts = _bounded_recent_texts(
        messages,
        "assistant",
        max_chars=2_500,
        per_message=1_000,
    )

    tool_facts = _recent_domain_tool_facts(
        messages,
        max_chars=2_500,
        per_result=1_000,
    )

    todo_text = json.dumps(
        todos or [],
        ensure_ascii=False,
        default=str,
    )[:1_500]

    sections = [
        (
            "以下是自动生成的保守型兜底摘要；"
            "用户原话和继承的历史摘要优先级最高。"
        ),

        "## 继承的上一轮会话摘要\n"
        + (
            previous_summary
            if previous_summary
            else "- 没有上一轮压缩摘要"
        ),

        "## 本轮新增用户原话（按时间顺序）\n"
        + (
            "\n".join(
                f"- {text}"
                for text in user_texts
            )
            or "- 本轮没有新增用户原话"
        ),

        "## 最近的助手可见结论\n"
        + (
            "\n".join(
                f"- {text}"
                for text in assistant_texts
            )
            or "- 未提取到"
        ),

        "## 近期关键工具结果（工具返回值，不等于用户确认）\n"
        + (
            "\n".join(
                f"- {fact}"
                for fact in tool_facts
            )
            or "- 未提取到"
        ),

        "## 当前任务状态\n"
        + (todo_text or "[]"),

        (
            "模型摘要生成失败。"
            "不要读取 transcript/storage 文件恢复上下文；"
            "缺少会影响结论的信息时，应询问用户"
            "或重新调用对应领域工具。"
        ),
    ]

    # Leave room for the summary marker and a small recent tail inside the
    # default 30k post-compaction target. A bounded, visible omission is safer
    # than an oversized fallback that immediately triggers another cycle.
    return _bounded_excerpt("\n\n".join(sections), 27_000)


def _summarize_history(messages: list, todos: list | None = None) -> str:
    """让模型生成适用于旅游 Agent 的可继续执行摘要。"""
    prompt = (
        "你正在为旅游规划 Agent“小 A”压缩旧会话。请用中文输出紧凑、具体、"
        "可供后续模型直接继续工作的摘要。\n"
        "必须保留：\n"
        "1. 用户当前目标、任务阶段和已经做出的决定；\n"
        "2. 已确认的目的地、日期、出发地、人数、预算、交通、住宿、兴趣、"
        "饮食、旅行节奏、健康与无障碍需求；\n"
        "3. 尚未确认的字段、用户明确拒绝的方案、已接受的假设；\n"
        "4. 当前 todos（尤其 waiting_for_user）和下一步行动；\n"
        "5. 本地知识库的来源文件/页码、联网来源 URL，以及价格、营业时间、"
        "班次、天气、签证等是否已核验；\n"
        "6. 已读取或修改的项目文件、关键实现结论和用户约束。\n"
        "严格区分“用户已确认”“工具查到”“模型推断”“仅是建议”；"
        "绝不能把建议或未核验信息写成事实。不要编造缺失信息。\n\n"
        + _summary_input(messages, todos)
    )
    started = time.perf_counter()
    try:
        response = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
            thinking={"type": "disabled"},  # 关闭L4摘要调用的思考
        )
        usage = record_model_response("compaction_summary", response)
    except Exception as exc:
        fallback = _fallback_summary(messages, todos)
        emit(
            "COMPACT", "SummaryFallback", color="\033[33m",
            reason="summary_call_failed",
            error=type(exc).__name__,
            summary_chars=len(fallback),
        )
        return fallback

    text_blocks = []
    for block in response.content:
        block_type = _block_type(block)
        if block_type != "text":
            continue
        text = block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
        if text.strip():
            text_blocks.append(text)
    summary = "\n".join(text_blocks).strip()
    stop_reason = getattr(response, "stop_reason", None)
    usable = (
        stop_reason != "max_tokens"
        and len(summary) >= MIN_MODEL_SUMMARY_CHARS
    )
    emit(
        "COMPACT", "SummaryModel",
        model=MODEL, latency=f"{time.perf_counter() - started:.2f}s",
        summary_chars=len(summary),
        stop=stop_reason,
        tokens=(f"in={usage['input']},out={usage['output']},"
                f"cache_read={usage['cache_read']},"
                f"cache_create={usage['cache_create']}"),
    )
    if usable:
        return summary

    reason = (
        "max_tokens"
        if stop_reason == "max_tokens"
        else "summary_too_short"
    )
    emit(
        "COMPACT", "SummaryRejected", color="\033[31m",
        reason=reason,
        summary_chars=len(summary),
        minimum=MIN_MODEL_SUMMARY_CHARS,
    )
    fallback = _fallback_summary(messages, todos)
    emit(
        "COMPACT", "SummaryFallback", color="\033[33m",
        summary_chars=len(fallback),
        user_messages=len(_bounded_recent_texts(
            messages, "user", max_chars=14_000,
        )),
    )
    return fallback


def _recent_tail_start(messages: list, keep_recent: int) -> int:
    """计算最近消息起点，并尽量让尾部从 assistant 消息开始。"""
    if keep_recent <= 0:
        return len(messages)
    start = max(0, len(messages) - keep_recent)

    # 摘要本身是 user 消息；从 assistant 开始可避免 user/user 边界，
    # 同时自然保护 assistant(tool_use) → user(tool_result) 配对。
    while start > 0 and messages[start].get("role") == "user":
        start -= 1
    return start


# ── L4：自动摘要压缩 ───────────────────────────────────────
def compact_history(
    messages: list,
    todos: list | None = None,
    keep_recent: int = 4,
    marker: str = "Compacted",
    target_size: int = COMPACT_TARGET,
) -> list:
    """保存并总结完整历史，在字符预算内尽量原样保留最近消息。"""
    if not messages:
        return messages
    if keep_recent < 0:
        raise ValueError("keep_recent must be non-negative")
    if target_size <= 0:
        raise ValueError("target_size must be positive")

    transcript_path = _write_transcript(messages)
    emit(
        "COMPACT", "TranscriptSaved", color="\033[90m",
        file=transcript_path, messages=len(messages),
        chars=estimate_size(messages),
    )

    # 摘要覆盖完整历史，因此后续为了满足大小预算而减少近期原文时，
    # 被移除的信息仍然有摘要和 transcript 两层兜底。
    summary = _summarize_history(messages, todos)
    if len(summary.strip()) < MIN_MODEL_SUMMARY_CHARS:
        # 即便测试替身或未来的摘要实现绕过了上面的校验，这里也不能
        # 允许一个近乎空白的摘要覆盖完整历史。
        emit(
            "COMPACT", "SummaryRejected", color="\033[31m",
            reason="post_validation_failed",
            summary_chars=len(summary.strip()),
            minimum=MIN_MODEL_SUMMARY_CHARS,
        )
        summary = _fallback_summary(messages, todos)
    summary_message = {
        "role": "user",
        "content": (
            f"[{marker} conversation summary]\n"
            f"{summary}"
        ),
    }
    if estimate_size([summary_message]) > target_size:
        # This can happen with the deterministic fallback or with an unusually
        # verbose provider response. Bound the already-distilled summary so a
        # compaction result cannot immediately exceed its own target.
        prefix = f"[{marker} conversation summary]\n"
        summary_message["content"] = prefix + _bounded_excerpt(
            summary,
            max(500, target_size - len(prefix) - 100),
        )

    # 从期望的保留数量开始逐步收紧。边界函数会确保尾部尽量从
    # assistant 开始，避免拆开 tool_use -> tool_result 配对。
    for recent_count in range(keep_recent, -1, -1):
        if recent_count == 0:
            recent_messages = []
        else:
            tail_start = _recent_tail_start(messages, recent_count)
            recent_messages = messages[tail_start:] if tail_start > 0 else []

        candidate = [summary_message, *recent_messages]

        # 最近一批工具结果也可能很大；摘要已经覆盖其内容，因此这里只
        # 保留最新一个完整结果，其余结果使用可追溯的占位符。
        candidate = micro_compact(
            candidate,
            transcript_path=transcript_path,
            keep_recent=1,
            target_size=target_size,
        )

        if estimate_size(candidate) <= target_size or recent_count == 0:
            return candidate

    return [summary_message]


# ── API 超长错误后的应急压缩 ───────────────────────────────
def reactive_compact(messages: list, todos: list | None = None) -> list:
    """API 报上下文过长时，更激进地只保留最近约 5 条原文。"""
    return compact_history(
        messages,
        todos=todos,
        keep_recent=5,
        marker="Reactive compact",
    )


# ── Agent 每轮调用的统一入口 ───────────────────────────────
def apply_compaction_pipeline(messages: list, todos: list | None = None) -> bool:
    """就地运行压缩管线；返回值表示本轮是否调用了 LLM 摘要。"""
    initial_count = len(messages)
    initial_size = estimate_size(messages)
    persisted_before = sum(
        1
        for _message_index, _block_index, block in _collect_tool_results(messages)
        if _is_persisted_output(block.get("content", ""))
    )
    messages[:] = tool_result_budget(messages)
    persisted_after = sum(
        1
        for _message_index, _block_index, block in _collect_tool_results(messages)
        if _is_persisted_output(block.get("content", ""))
    )
    persisted_now = max(0, persisted_after - persisted_before)

    # 阈值检查必须早于 snip/micro，否则摘要看到的已经是残缺历史。
    if estimate_size(messages) > CONTEXT_LIMIT:
        before_size = estimate_size(messages)
        before_count = len(messages)
        emit(
            "COMPACT", "L4", color="\033[33m",
            action="summarize-start", messages=before_count,
            chars=before_size, limit=CONTEXT_LIMIT,
        )
        messages[:] = compact_history(
            messages,
            todos=todos,
            target_size=COMPACT_TARGET,
        )
        after_size = estimate_size(messages)
        emit(
            "COMPACT", "L4", color="\033[33m",
            action="summarize-end",
            messages=f"{before_count}->{len(messages)}",
            chars=f"{before_size}->{after_size}", target=COMPACT_TARGET,
        )
        return True

    # Message count alone is not a context limit. The previous L1 path could
    # delete a confirmed user constraint from a very small 51-message history
    # without creating a summary. Likewise the generic L2 preview cannot know
    # which arbitrary character in a tool result is important. Production
    # compaction therefore waits for the size-triggered L4 summary. The two
    # lossy helpers remain available only for isolated compatibility tests.
    if not persisted_now:
        emit(
            "COMPACT",
            "Check",
            action="none",
            messages=initial_count,
            chars=initial_size,
            limit=CONTEXT_LIMIT,
        )

    return False
