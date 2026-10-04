import json
import re
from time import perf_counter

from ..config import client, MODEL
from ..telemetry import emit, record_model_response
from .store import (
    MEMORY_TYPES,
    read_memory_index,
    write_memory,
)


def _message_text(message: dict) -> str:
    content = message.get("content", "")

    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return ""

    texts = []

    for block in content:
        block_type = (
            block.get("type")
            if isinstance(block, dict)
            else getattr(block, "type", None)
        )

        # 不把搜索结果、工具结果存成用户记忆
        if block_type != "text":
            continue

        text = (
            block.get("text", "")
            if isinstance(block, dict)
            else getattr(block, "text", "")
        )

        if text:
            texts.append(str(text))

    return "\n".join(texts)


def _format_dialogue(
    turn_messages: list,
) -> str:
    parts = []

    for message in turn_messages:
        text = _message_text(message).strip()

        if not text:
            continue

        role = message.get("role", "unknown")
        parts.append(f"{role}: {text}")

    return "\n".join(parts)[:6000]


def extract_memories(
    turn_messages: list,
) -> int:
    started = perf_counter()
    dialogue = _format_dialogue(turn_messages)

    if not dialogue:
        emit(
            "MEMORY", "extract", decision="no-save",
            saved=0, reason="no-dialogue", latency="0.00s",
        )
        return 0

    existing_index = (
        read_memory_index() or "(none)"
    )

    prompt = f"""
从下面一轮真实对话中提取值得跨会话保存的长期记忆。

可以保存：
1. 用户明确要求“记住”的事实
2. 稳定用户偏好，例如住宿、饮食、回答风格
3. 重要限制，例如过敏、无障碍需求
4. 当前长期项目的重要状态
5. 用户明确提供的长期参考位置

不要保存：
1. 模型自己的猜测
2. 搜索结果、实时天气、票价和临时时刻
3. 普通寒暄
4. 一次性的工具输出
5. 密码、密钥、令牌等敏感信息
6. 已存在且没有变化的记忆

如果用户更新了已有事实：
- 使用已有记忆相同的name
- 新内容覆盖旧内容
- 最新用户陈述优先

只返回JSON数组：
[
  {{
    "name": "稳定且简短的标识",
    "type": "user|feedback|project|reference",
    "description": "一句话描述",
    "body": "完整事实及使用方式"
  }}
]

没有需要保存的信息时返回[]。

已有记忆：
{existing_index}

本轮对话：
{dialogue}
""".strip()

    try:
        response = client.messages.create(
            model=MODEL,
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            max_tokens=1000,
        )

        text = "\n".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        )
        usage = record_model_response("memory_extract", response)

        match = re.search(
            r"\[.*\]",
            text,
            re.DOTALL,
        )

        if not match:
            emit(
                "MEMORY", "extract", decision="no-save", saved=0,
                reason="invalid-model-output",
                latency=f"{perf_counter() - started:.2f}s", model=MODEL,
                tokens=(f"in={usage['input']},out={usage['output']},"
                        f"cache_read={usage['cache_read']},"
                        f"cache_create={usage['cache_create']}"),
            )
            return 0

        items = json.loads(match.group())
        if not isinstance(items, list):
            raise ValueError("memory extractor output must be a JSON array")
        count = 0

        for item in items[:5]:
            if not isinstance(item, dict):
                continue

            name = str(item.get("name", "")).strip()
            memory_type = str(
                item.get("type", "")
            ).strip()
            description = str(
                item.get("description", "")
            ).strip()
            body = str(item.get("body", "")).strip()

            if (
                not name
                or memory_type not in MEMORY_TYPES
                or not description
                or not body
            ):
                continue

            write_memory(
                name=name,
                memory_type=memory_type,
                description=description,
                body=body,
            )

            count += 1

        candidate_count = len(items[:5])
        emit(
            "MEMORY", "extract",
            decision="saved" if count else "no-save",
            candidates=candidate_count, saved=count,
            rejected=candidate_count - count,
            latency=f"{perf_counter() - started:.2f}s", model=MODEL,
            tokens=(f"in={usage['input']},out={usage['output']},"
                    f"cache_read={usage['cache_read']},"
                    f"cache_create={usage['cache_create']}"),
        )

        return count

    except Exception as exc:
        emit(
            "MEMORY", "extract", status="error",
            error=type(exc).__name__,
            latency=f"{perf_counter() - started:.2f}s",
        )
        return 0
