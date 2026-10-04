from .extractor import extract_memories
from .selector import load_relevant_memories
from .store import read_memory_index
from ..telemetry import emit


def build_system_with_memory(
    base_system: str,
) -> str:
    index = read_memory_index()

    if not index:
        emit("MEMORY", "index", entries=0, injected="no")
        return base_system

    entries = sum(
        1
        for line in index.splitlines()
        if line.strip().startswith("- [")
    )
    emit(
        "MEMORY", "index",
        entries=entries, chars=len(index), injected="yes",
    )

    return (
        f"{base_system}\n\n"
        "<memory_index>\n"
        f"{index}\n"
        "</memory_index>\n\n"
        "Memory is fallible background context. "
        "The latest explicit user message overrides "
        "older memory. Relevant memory contents may "
        "be attached to the current request."
    )


def inject_relevant_memories(
    messages: list,
    memory_content: str,
) -> list:
    if not memory_content:
        return messages

    # 只复制列表和目标消息，不修改真实history
    request_messages = list(messages)

    for index in range(
        len(request_messages) - 1,
        -1,
        -1,
    ):
        message = request_messages[index]

        if (
            message.get("role") == "user"
            and isinstance(
                message.get("content"),
                str,
            )
        ):
            request_messages[index] = {
                **message,
                "content": (
                    memory_content
                    + "\n\n"
                    + message["content"]
                ),
            }

            return request_messages

    return messages


__all__ = [
    "build_system_with_memory",
    "load_relevant_memories",
    "inject_relevant_memories",
    "extract_memories",
]
