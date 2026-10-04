import json
import re
from time import perf_counter

from ..config import (
    client,
    MODEL,
    MEMORY_MAX_SELECTED,
)
from ..telemetry import emit, record_model_response
from .store import list_memories, read_memory


def _block_text(block) -> str:
    if isinstance(block, dict):
        return str(block.get("text", ""))

    return str(getattr(block, "text", ""))


def _message_text(message: dict) -> str:
    content = message.get("content", "")

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        return " ".join(
            _block_text(block)
            for block in content
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
            )
            or getattr(block, "type", None) == "text"
        )

    return ""


def _recent_user_text(messages: list) -> str:
    collected = []

    for message in reversed(messages):
        if message.get("role") != "user":
            continue

        text = _message_text(message).strip()

        if not text:
            continue

        collected.append(text)

        if len(collected) >= 3:
            break

    return "\n".join(reversed(collected))[:2000]


def select_relevant_memories(
    messages: list,
) -> list[str]:
    started = perf_counter()
    memories = list_memories()

    if not memories:
        emit("MEMORY", "select", available=0, selected=0, reason="empty-store")
        return []

    recent = _recent_user_text(messages)

    if not recent:
        emit(
            "MEMORY", "select",
            available=len(memories), selected=0, reason="no-user-text",
        )
        return []

    catalog = "\n".join(
        f"{index}: {memory.name}"
        f" — {memory.description}"
        for index, memory in enumerate(memories)
    )

    prompt = (
        "根据当前用户对话，从记忆目录中选择真正相关的记忆。\n"
        "只返回JSON整数数组，例如[0, 2]；"
        "没有相关记忆时返回[]。\n"
        f"最多选择{MEMORY_MAX_SELECTED}条。\n\n"
        f"当前对话：\n{recent}\n\n"
        f"记忆目录：\n{catalog}"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            max_tokens=200,
        )

        text = "\n".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        )
        usage = record_model_response("memory_select", response)

        match = re.search(
            r"\[[\d,\s]*\]",
            text,
        )

        if not match:
            emit(
                "MEMORY", "select",
                available=len(memories), selected=0,
                reason="invalid-model-output",
                latency=f"{perf_counter() - started:.2f}s", model=MODEL,
                tokens=(f"in={usage['input']},out={usage['output']},"
                        f"cache_read={usage['cache_read']},"
                        f"cache_create={usage['cache_create']}"),
            )
            return []

        indices = json.loads(match.group())

        selected = []

        for index in indices:
            if not isinstance(index, int):
                continue

            if 0 <= index < len(memories):
                selected.append(
                    memories[index].filename
                )

            if len(selected) >= MEMORY_MAX_SELECTED:
                break

        files_text = ",".join(selected) if selected else "-"
        emit(
            "MEMORY", "select",
            available=len(memories), selected=len(selected), files=files_text,
            latency=f"{perf_counter() - started:.2f}s", model=MODEL,
            tokens=(f"in={usage['input']},out={usage['output']},"
                    f"cache_read={usage['cache_read']},"
                    f"cache_create={usage['cache_create']}"),
        )
        return selected

    except Exception as exc:
        emit(
            "MEMORY", "select", status="error",
            error=type(exc).__name__,
            latency=f"{perf_counter() - started:.2f}s",
        )
        return []


def load_relevant_memories(
    messages: list,
) -> str:
    filenames = select_relevant_memories(messages)

    if not filenames:
        emit("MEMORY", "load", selected=0, loaded=0, chars=0)
        return ""

    contents = []

    for filename in filenames:
        content = read_memory(filename)

        if content:
            contents.append(content)

    if not contents:
        emit(
            "MEMORY", "load",
            selected=len(filenames), loaded=0, chars=0,
        )
        return ""

    loaded_chars = sum(len(content) for content in contents)
    emit(
        "MEMORY", "load",
        selected=len(filenames), loaded=len(contents), chars=loaded_chars,
    )

    return (
        "<relevant_memories>\n"
        "These are persistent background facts. "
        "The latest explicit user message always has "
        "higher priority than an older memory.\n\n"
        + "\n\n".join(contents)
        + "\n</relevant_memories>"
    )
