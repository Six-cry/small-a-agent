"""Central policy for files that Agent tools must never access directly."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath


_SENSITIVE_DIRECTORIES = {
    ".aws",
    ".azure",
    ".git",
    ".gnupg",
    ".kube",
    ".ssh",
}

_SENSITIVE_FILENAMES = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    "auth.json",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "service-account.json",
    "service_account.json",
    "secrets.json",
}

_SENSITIVE_SUFFIXES = {
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pfx",
    ".pem",
}

_ENV_TEMPLATE_MARKERS = (
    ".example",
    ".sample",
    ".template",
)

_COMMAND_PATH_TOKEN = re.compile(r"[A-Za-z0-9_.$~:/\\-]+")


def _is_safe_env_template(filename: str) -> bool:
    return filename.startswith(".env") and any(
        marker in filename for marker in _ENV_TEMPLATE_MARKERS
    )


def sensitive_path_reason(path: str | Path) -> str | None:
    """Return a stable policy reason when *path* names sensitive data."""
    # Policy checks must recognize both path styles, independent of the host OS.
    candidate = PurePosixPath(str(path).replace("\\", "/"))
    parts = tuple(part.casefold() for part in candidate.parts)

    if any(part in _SENSITIVE_DIRECTORIES for part in parts):
        return "protected credential or repository metadata directory"

    filename = candidate.name.casefold()
    if not filename:
        return None

    if filename == ".env" or (
        filename.startswith(".env.")
        and not _is_safe_env_template(filename)
    ):
        return "environment secrets file"

    if filename in _SENSITIVE_FILENAMES:
        return "credentials file"

    if candidate.suffix.casefold() in _SENSITIVE_SUFFIXES:
        return "private key or certificate container"

    return None


def sensitive_command_reason(command: str) -> str | None:
    """Detect direct sensitive-path references before invoking a shell.

    This is a hard stop for ordinary direct references. It complements, but
    does not replace, the separate shell permission policy.
    """
    for token in _COMMAND_PATH_TOKEN.findall(str(command)):
        reason = sensitive_path_reason(token.strip("\"'`"))
        if reason:
            return reason
    return None


def blocked_sensitive_message(reason: str) -> str:
    """Return a user-safe error without exposing file contents."""
    return f"Error: Access to sensitive files is blocked ({reason})"
