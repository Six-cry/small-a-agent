"""小 A 的轻量可观测性日志。

终端输出保持为适合人阅读的单行事件；启动 session 后，同一事件还会
追加到 JSONL 文件，便于之后按 turn_id、scope 或 event 检索。
"""

import json
import threading
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from uuid import uuid4


_session_path: Path | None = None
_write_lock = threading.Lock()
_turn_id: ContextVar[str | None] = ContextVar("turn_id", default=None)
_turn_model_metrics: ContextVar[dict | None] = ContextVar(
    "turn_model_metrics", default=None
)


def start_session_log(log_dir: Path) -> Path | None:
    """为当前进程创建一个结构化日志文件。"""
    global _session_path

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        _session_path = log_dir / (
            f"session_{timestamp}_{uuid4().hex[:8]}.jsonl"
        )
        _session_path.touch(exist_ok=False)
    except OSError as exc:
        _session_path = None
        print(f"[LOG] SessionDisabled: error={type(exc).__name__}")
        return None

    emit("LOG", "SessionStart", file=str(_session_path))
    return _session_path


def emit(
    scope: str,
    event: str,
    *,
    color: str | None = None,
    **fields,
) -> None:
    """输出一个终端事件，并在已启用时追加一条 JSONL 记录。"""
    global _session_path
    details = " ".join(
        f"{key}={value}"
        for key, value in fields.items()
        if value is not None
    )
    line = f"[{scope}] {event}"
    if details:
        line += f": {details}"

    if color:
        print(f"{color}{line}\033[0m")
    else:
        print(line)

    if _session_path is None:
        return

    record = {
        "timestamp": datetime.now().astimezone().isoformat(
            timespec="milliseconds"
        ),
        "scope": scope,
        "event": event,
    }
    current_turn_id = _turn_id.get()
    if current_turn_id and "turn_id" not in fields:
        record["turn_id"] = current_turn_id
    record.update(fields)
    serialized = json.dumps(
        record,
        ensure_ascii=False,
        default=str,
    )
    try:
        with _write_lock:
            with _session_path.open("a", encoding="utf-8") as file:
                file.write(serialized + "\n")
    except OSError as exc:
        _session_path = None
        print(f"[LOG] WriteDisabled: error={type(exc).__name__}")


def session_log_path() -> Path | None:
    return _session_path


def set_turn_id(turn_id: str | None) -> None:
    _turn_id.set(turn_id)
    if turn_id is None:
        _turn_model_metrics.set(None)


def start_turn(turn_id: str) -> None:
    """开始一轮关联，并清空该轮的模型调用累计值。"""
    _turn_id.set(turn_id)
    _turn_model_metrics.set({})


def response_usage(response) -> dict[str, int]:
    """兼容 SDK 对象和字典形式的 usage。"""
    usage = getattr(response, "usage", None)

    def value(name: str) -> int:
        if usage is None:
            return 0
        raw = (
            usage.get(name, 0)
            if isinstance(usage, dict)
            else getattr(usage, name, 0)
        )
        return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0

    return {
        "input": value("input_tokens"),
        "output": value("output_tokens"),
        "cache_read": value("cache_read_input_tokens"),
        "cache_create": value("cache_creation_input_tokens"),
    }


def record_model_response(component: str, response) -> dict[str, int]:
    """记录一次模型响应，并返回本次标准化 usage。"""
    usage = response_usage(response)
    metrics = _turn_model_metrics.get()
    if metrics is not None:
        bucket = metrics.setdefault(
            component,
            {
                "calls": 0,
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_create": 0,
            },
        )
        bucket["calls"] += 1
        for key in ("input", "output", "cache_read", "cache_create"):
            bucket[key] += usage[key]
    return usage


def turn_model_metrics() -> dict:
    """返回当前轮次模型统计的副本及总计。"""
    metrics = _turn_model_metrics.get() or {}
    components = {
        name: dict(values)
        for name, values in metrics.items()
    }
    total = {
        "calls": 0,
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_create": 0,
    }
    for values in components.values():
        for key in total:
            total[key] += values[key]
    return {"components": components, "total": total}
