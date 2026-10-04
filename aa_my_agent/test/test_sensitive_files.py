from pathlib import PurePosixPath, PureWindowsPath

import pytest

from aa_my_agent.tools.definitions import (
    run_bash,
    run_edit,
    run_glob,
    run_read,
    run_write,
)
from aa_my_agent.tools.sensitive_files import (
    sensitive_command_reason,
    sensitive_path_reason,
)


def test_sensitive_path_policy_blocks_secrets_and_allows_templates():
    blocked = (
        ".env",
        ".env.local",
        "aa_my_agent/.env.production",
        "keys/private.pem",
        "keys/client.p12",
        ".ssh/id_ed25519",
        ".aws/credentials",
        ".git/config",
        "config/credentials.json",
    )
    for path in blocked:
        assert sensitive_path_reason(path), path

    allowed = (
        ".env.example",
        ".env.local.example",
        ".env.sample",
        ".env.template",
        "docs/credentials-guide.md",
        "certificates/public.crt",
    )
    for path in allowed:
        assert sensitive_path_reason(path) is None, path


@pytest.mark.parametrize("path_type", [str, PurePosixPath, PureWindowsPath])
def test_sensitive_path_policy_recognizes_windows_paths_on_any_host(path_type):
    blocked = (
        r"aa_my_agent\.env",
        r"C:\project\.env.local",
        r".ssh\id_rsa",
        r".git\config",
        r"config\credentials.json",
        r"keys\private.pem",
        r"config/.aws\credentials",
    )
    for path in blocked:
        assert sensitive_path_reason(path_type(path)), path

    allowed = (
        r"aa_my_agent\.env.example",
        r"C:\project\.env.local.template",
        r"docs\credentials-guide.md",
        r"certificates\public.crt",
    )
    for path in allowed:
        assert sensitive_path_reason(path_type(path)) is None, path


def test_file_tools_block_sensitive_paths_before_access():
    assert run_read("aa_my_agent/.env").startswith(
        "Error: Access to sensitive files is blocked"
    )
    assert run_write("private.pem", "not-a-real-key").startswith(
        "Error: Access to sensitive files is blocked"
    )
    assert run_edit(".env.local", "old", "new").startswith(
        "Error: Access to sensitive files is blocked"
    )
    assert run_glob("aa_my_agent/.env").startswith(
        "Error: Access to sensitive files is blocked"
    )


def test_recursive_glob_hides_sensitive_matches(tmp_path, monkeypatch):
    visible = tmp_path / "visible.txt"
    hidden = tmp_path / ".env"
    key = tmp_path / "private.key"
    visible.write_text("visible", encoding="utf-8")
    hidden.write_text("secret", encoding="utf-8")
    key.write_text("secret", encoding="utf-8")

    import aa_my_agent.tools.definitions as definitions

    monkeypatch.setattr(definitions, "WORKDIR", tmp_path)
    matches = definitions.run_glob("*").splitlines()

    assert visible.name in matches
    assert hidden.name not in matches
    assert key.name not in matches


def test_shell_blocks_direct_sensitive_file_references_without_execution(monkeypatch):
    import aa_my_agent.tools.definitions as definitions

    def reject_execution(*args, **kwargs):
        raise AssertionError("Sensitive command reached the subprocess executor")

    monkeypatch.setattr(definitions.subprocess, "run", reject_execution)
    commands = (
        r"type aa_my_agent\.env",
        r"Get-Content .env.local",
        r"cat .ssh/id_rsa",
        r"python upload.py credentials.json",
        r"git config --file .git/config --list",
    )
    for command in commands:
        assert sensitive_command_reason(command), command
        assert run_bash(command).startswith(
            "Error: Access to sensitive files is blocked"
        )


def test_env_example_remains_readable():
    result = run_read("aa_my_agent/.env.example", limit=1)
    assert not result.startswith("Error: Access to sensitive files is blocked")
    assert result != ""
