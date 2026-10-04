import json
import re

from ..config import WORKDIR
from ..telemetry import emit
from ..tools.tools import (
    READ_ONLY_TOOLS,
    WRITE_TOOLS,
    get_tool_permission,
    is_known_tool,
)
from ..tools import definitions

# 每一种事件对应一个 Hook 函数列表
HOOKS = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}


# 禁止直接执行的危险命令
DENY_LIST = [
    "rm -rf /",
    "sudo",
    "shutdown",
    "reboot",
    "mkfs",
    "dd if=",
    "> /dev/sda",
    "format",
]


# 需要询问用户的命令
RISKY_BASH = [
    "python ",
    "pip ",
    "npm ",
    "git ",
    "curl ",
]


DESTRUCTIVE_BASH = [
    "rm ",
    "del ",
    "rmdir",
    "rd /s",
    "> /etc/",
    "chmod 777",
]


# Shell syntax is too broad to infer safety from the first word. In
# particular, a command that begins with `echo` can run more commands via
# chaining, pipes, substitutions, or redirection. Require review of the
# entire command instead of maintaining a partial shell parser.
MAX_APPROVAL_COMMAND_CHARS = 32_000


# 写入文件时不允许出现这些敏感内容
SECRET_PATTERNS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "sk-",
    "api_key",
    "secret",
]

def register_hook(event: str, callback):
    """Register a callback for a Hook event."""
    if event not in HOOKS:
        raise ValueError(f"Unknown Hook event: {event}")

    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    """Run callbacks until one returns a non-None result."""
    if event not in HOOKS:
        raise ValueError(f"Unknown Hook event: {event}")

    for callback in HOOKS[event]:
        result = callback(*args)

        if result is not None:
            return result

    return None


def ask_user(
    tool_name: str,
    args: dict,
    reason: str,
) -> str | None:
    """
    询问用户是否允许工具执行。

    返回 None 表示允许；
    返回字符串表示拒绝，并作为 tool_result 返回模型。
    """
    emit(
        "HOOK", "PermissionRequest", color="\033[33m",
        name=tool_name, reason=reason,
        args=_safe_args_preview(args),
    )

    if tool_name == "bash":
        # The telemetry preview deliberately omits arguments. The person
        # granting approval must still see the *whole* command; JSON quoting
        # makes newlines and terminal control characters visible as escapes.
        command = str(args.get("command", ""))
        print(f"   待执行命令: {json.dumps(command, ensure_ascii=False)}")

    if tool_name == "mcp__google-calendar__create_calendar_event":
        # The generic telemetry preview is intentionally short. Show the
        # complete event details locally so approval covers the actual date,
        # time zone, calendar and destination of this external write.
        for label, key in (
            ("日历", "calendar_id"),
            ("标题", "title"),
            ("开始", "start_time"),
            ("结束", "end_time"),
            ("时区", "time_zone"),
            ("地点", "location"),
            ("备注", "description"),
        ):
            value = args.get(key, "primary" if key == "calendar_id" else "")
            print(f"   {label}: {json.dumps(str(value), ensure_ascii=False)}")

    if tool_name in {
        "mcp__dingtalk-calendar__create_calendar_event",
        "mcp__dingtalk-calendar__update_calendar_event",
        "mcp__dingtalk-calendar__delete_calendar_event",
    }:
        print("   目标: 钉钉中已配置用户的 primary 主日历")
        for label, key in (
            ("日程 ID", "event_id"),
            ("原标题", "expected_title"),
            ("原开始时间", "expected_start_time"),
            ("新标题", "title"),
            ("新开始", "start_time"),
            ("新结束", "end_time"),
            ("时区", "time_zone"),
            ("地点", "location"),
            ("备注", "description"),
        ):
            if key in args:
                print(f"   {label}: {json.dumps(str(args[key]), ensure_ascii=False)}")
        if tool_name.endswith("delete_calendar_event"):
            print("   警告: 确认后将永久删除这条日程。")

    try:
        choice = input("   Allow? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        emit(
            "HOOK", "PermissionDecision",
            name=tool_name, allowed="no", reason="input-interrupted",
        )
        return "Permission denied: approval input interrupted"

    if choice in ("y", "yes"):
        emit("HOOK", "PermissionDecision", name=tool_name, allowed="yes")
        return None

    emit("HOOK", "PermissionDecision", name=tool_name, allowed="no")
    return "Permission denied by user"


def first_command(command: str) -> str:
    """Return the first word of a shell command."""
    command = command.strip()

    if not command:
        return ""

    return command.split(maxsplit=1)[0].casefold()


def _safe_args_preview(args: dict) -> str:
    """生成适合终端和持久化日志的脱敏参数摘要。"""
    safe_args = {}
    for key, value in args.items():
        normalized_key = str(key).casefold()
        if any(
            marker in normalized_key
            for marker in ("secret", "token", "password", "api_key")
        ):
            safe_args[key] = "<redacted>"
        elif normalized_key in {"content", "old_text", "new_text"}:
            safe_args[key] = f"<{len(str(value))} chars>"
        elif normalized_key == "command":
            safe_args[key] = (
                f"<command={first_command(str(value)) or '(empty)'} "
                f"chars={len(str(value))}>"
            )
        elif normalized_key == "url":
            safe_args[key] = str(value).partition("?")[0]
        else:
            safe_args[key] = value

    return " ".join(str(safe_args).split())[:200]


def log_hook(block):
    """PreToolUse: log every tool call."""
    args_preview = _safe_args_preview(block.input)
    tool_id = getattr(block, "id", "-")

    emit(
        "HOOK", "ToolStart", color="\033[90m",
        id=tool_id, name=block.name, args=args_preview,
    )

    return None


def secret_scan_hook(block):
    """PreToolUse: prevent secrets from being written to files."""
    if block.name not in WRITE_TOOLS:
        return None

    content = str(block.input.get("content", ""))
    new_text = str(block.input.get("new_text", ""))
    text = (content + new_text).casefold()

    for pattern in SECRET_PATTERNS:
        if pattern.casefold() in text:
            return (
                "Blocked by secret_scan_hook: "
                f"possible secret found ({pattern})"
            )

    return None


def permission_hook(block):
    """PreToolUse: check whether a tool call may execute."""
    tool_name = block.name
    args = block.input

    # 工具不存在
    if not is_known_tool(tool_name):
        return f"Permission denied: unknown tool {tool_name}"

    # MCP tools are namespaced and default to approval. Only a server tool
    # explicitly annotated read-only and non-destructive may run automatically.
    mcp_permission = get_tool_permission(tool_name)
    if mcp_permission == "read":
        return None
    if mcp_permission == "write":
        return ask_user(
            tool_name,
            args,
            "MCP tool may change external state",
        )

    # 普通安全工具直接放行
    if tool_name in READ_ONLY_TOOLS:
        return None

    # 文件写入工具
    if tool_name in WRITE_TOOLS:
        path = args.get("path", "")

        try:
            target = (WORKDIR / path).resolve()
            outside_workspace = not target.is_relative_to(WORKDIR)
        except (OSError, TypeError):
            outside_workspace = True

        if outside_workspace:
            reason = "Writing outside workspace"
        else:
            reason = "This tool will modify workspace files"

        return ask_user(tool_name, args, reason)

    # Shell 命令
    if tool_name == "bash":
        command = str(args.get("command", ""))
        lowered = command.casefold()

        if len(command) > MAX_APPROVAL_COMMAND_CHARS:
            return (
                "Permission denied: shell command too long to review "
                f"({len(command)} > {MAX_APPROVAL_COMMAND_CHARS} characters)"
            )

        for pattern in DENY_LIST:
            if pattern.casefold() in lowered:
                return f"Permission denied by deny list: {pattern}"

        if any(pattern in lowered for pattern in DESTRUCTIVE_BASH):
            return ask_user(
                tool_name,
                args,
                "Potentially destructive command",
            )

        if any(pattern in lowered for pattern in RISKY_BASH):
            return ask_user(
                tool_name,
                args,
                "Command may change project or environment",
            )

        return ask_user(
            tool_name,
            args,
            "Bash command may change system state",
        )

    return f"Permission denied: unclassified tool {tool_name}"


def large_output_hook(
    block,
    output,
    elapsed_seconds: float = 0.0,
    status: str = "ok",
):
    """PostToolUse: inspect tool output size."""
    output_length = len(str(output))
    tool_id = getattr(block, "id", "-")
    error_kind = None
    if status == "error":
        match = re.match(
            r"^(?:Tool execution failed|Error):\s*([A-Za-z_][\w.]*)",
            str(output).strip(),
        )
        error_kind = match.group(1) if match else "reported-error"

    emit(
        "HOOK", "ToolEnd", color="\033[90m",
        id=tool_id, name=block.name, status=status,
        duration=f"{elapsed_seconds:.2f}s",
        output_chars=output_length, error=error_kind,
    )

    if output_length > 100_000:
        emit(
            "HOOK", "LargeOutput", color="\033[33m",
            name=block.name, output_chars=output_length,
        )

    return None


def context_inject_hook(query: str):
    """UserPromptSubmit: log the current working directory."""
    emit(
        "HOOK", "UserPromptSubmit", color="\033[90m",
        working_in=WORKDIR, query_chars=len(str(query)),
    )

    return None


def summary_hook(messages: list):
    """Stop: print tool-result and todo state counts for this user turn."""
    tool_count = 0

    for message in messages:
        content = message.get("content")

        if not isinstance(content, list):
            continue

        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "tool_result":
                    tool_count += 1

    status_counts = {
        "pending": 0,
        "in_progress": 0,
        "waiting_for_user": 0,
        "completed": 0,
    }
    for todo in definitions.CURRENT_TODOS:
        status = todo.get("status")
        if status in status_counts:
            status_counts[status] += 1

    emit(
        "HOOK", "Stop", color="\033[90m",
        tool_results=tool_count,
        todos=(f"(completed={status_counts['completed']},"
               f"waiting={status_counts['waiting_for_user']},"
               f"active={status_counts['pending'] + status_counts['in_progress']})"),
    )

    return None

def unfinished_todo_hook(message: list):
    todos = definitions.CURRENT_TODOS

    waiting = [todo for todo in todos
               if todo["status"] == "waiting_for_user"]

    if waiting:
        return None
    
    unfinished = [
        todo
        for todo in definitions.CURRENT_TODOS
        if todo["status"] in {"pending", "in_progress",}
    ]

    if unfinished:
        return (
            "<reminder>"
            "There are unfinished todos. Continue working. "
            "If progress requires user input, call todo_write "
            "and set the blocking todo to waiting_for_user, "
            "then ask one concise clarification question and "
            "end the turn. Never mark an unconfirmed task as "
            "completed."
            "</reminder>"
        )
    return None


# 注册顺序就是执行顺序
register_hook("UserPromptSubmit", context_inject_hook)

register_hook("PreToolUse", log_hook)
register_hook("PreToolUse", secret_scan_hook)
register_hook("PreToolUse", permission_hook)

register_hook("PostToolUse", large_output_hook)

#register_hook("Stop", unfinished_todo_hook)
register_hook("Stop", summary_hook)
