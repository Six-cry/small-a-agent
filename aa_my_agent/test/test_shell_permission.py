"""Shell approval must cover the complete command, not its first word."""

import json
from types import SimpleNamespace

import pytest

from aa_my_agent.hooks import hooks
from aa_my_agent.subagents.runner import execute_subagent_tool


@pytest.mark.parametrize(
    "command",
    [
        "echo harmless",
        "dir",
        "echo harmless & python side_effect.py",
        "echo harmless && python side_effect.py",
        "echo harmless | python side_effect.py",
        "echo harmless > changed.txt",
        "echo harmless\npython side_effect.py",
        "echo $(python side_effect.py)",
        "echo %COMSPEC%",
        "find . -exec python side_effect.py ;",
    ],
)
def test_unapproved_shell_command_never_reaches_handler(
    command, monkeypatch,
):
    calls = []
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    block = SimpleNamespace(
        name="bash", input={"command": command}, id="test-bash",
    )

    output = execute_subagent_tool(
        block,
        {"bash": lambda **kwargs: calls.append(kwargs)},
    )

    assert output == "Permission denied by user"
    assert calls == []


def test_shell_approval_shows_entire_command_with_controls_escaped(
    monkeypatch, capsys,
):
    command = "echo safe; python side_effect.py\nprint('x')\x1b[31m"
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    assert hooks.ask_user("bash", {"command": command}, "Review command") == (
        "Permission denied by user"
    )

    display = capsys.readouterr().out
    command_line = next(
        line for line in display.splitlines()
        if line.startswith("   待执行命令:")
    )
    assert json.dumps(command, ensure_ascii=False) in command_line
    assert "side_effect.py" in command_line
    assert "\nprint('x')" not in command_line
    assert "\x1b" not in command_line


def test_approved_shell_command_reaches_handler_once(monkeypatch):
    calls = []
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    command = "echo harmless & echo still harmless"
    block = SimpleNamespace(
        name="bash", input={"command": command}, id="test-bash",
    )

    output = execute_subagent_tool(
        block,
        {"bash": lambda **kwargs: calls.append(kwargs) or "mocked"},
    )

    assert output == "mocked"
    assert calls == [{"command": command}]


def test_oversized_shell_command_is_denied_before_approval(monkeypatch):
    def fail_if_prompted(_prompt):
        raise AssertionError("oversized command must not be presented")

    monkeypatch.setattr("builtins.input", fail_if_prompted)
    command = "echo " + "x" * hooks.MAX_APPROVAL_COMMAND_CHARS
    block = SimpleNamespace(name="bash", input={"command": command})

    assert hooks.permission_hook(block).startswith(
        "Permission denied: shell command too long to review"
    )


def test_hard_denied_shell_command_never_requests_approval(monkeypatch):
    def fail_if_prompted(_prompt):
        raise AssertionError("deny-list command must not prompt")

    monkeypatch.setattr("builtins.input", fail_if_prompted)
    block = SimpleNamespace(
        name="bash", input={"command": "echo hello & shutdown"},
    )

    assert hooks.permission_hook(block).startswith(
        "Permission denied by deny list"
    )
