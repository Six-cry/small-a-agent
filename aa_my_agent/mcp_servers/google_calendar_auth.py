"""One-time local OAuth setup for the Google Calendar MCP server.

Run ``python -m aa_my_agent.mcp_servers.google_calendar_auth --authorize``.
Only an explicit --authorize starts the browser flow or updates .env.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from dotenv import dotenv_values

from aa_my_agent.mcp_servers.google_calendar import CALENDAR_SCOPE, TOKEN_URL


ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_REFRESH_LINE = re.compile(r"^([ \t]*(?:export[ \t]+)?)GOOGLE_CALENDAR_REFRESH_TOKEN[ \t]*=")


def _client_credentials(env_path: Path) -> tuple[str, str]:
    if not env_path.is_file() or env_path.is_symlink():
        raise RuntimeError("aa_my_agent/.env is missing or is a link; create a regular .env file first.")
    try:
        stored = dotenv_values(env_path, encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise RuntimeError("Cannot read aa_my_agent/.env as UTF-8; check its encoding.") from None
    names = ("GOOGLE_CALENDAR_CLIENT_ID", "GOOGLE_CALENDAR_CLIENT_SECRET")
    values = tuple((os.getenv(name) or stored.get(name) or "").strip() for name in names)
    missing = [name for name, value in zip(names, values) if not value]
    if missing:
        raise RuntimeError("Set " + ", ".join(missing) + " in aa_my_agent/.env first.")
    return values  # type: ignore[return-value]


def _authorization_url(client_id: str, redirect_uri: str, state: str, verifier: str) -> str:
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return AUTHORIZE_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": CALENDAR_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )


def _await_authorization(server: HTTPServer, result: dict[str, str]) -> str:
    deadline = time.monotonic() + 180
    while "code" not in result and "error" not in result and time.monotonic() < deadline:
        server.timeout = min(5, max(0.1, deadline - time.monotonic()))
        server.handle_request()
    if result.get("error"):
        raise RuntimeError("Google Calendar authorization was denied or returned an invalid state.")
    if not result.get("code"):
        raise RuntimeError("Timed out waiting for Google Calendar authorization; run --authorize again.")
    return result["code"]


def _exchange_code(
    client_id: str, client_secret: str, code: str, redirect_uri: str, verifier: str
) -> str:
    try:
        response = requests.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
    except requests.RequestException:
        raise RuntimeError("Google Calendar authorization exchange failed; check the connection.") from None
    if response.status_code >= 400:
        try:
            error_code = response.json().get("error")
        except (ValueError, AttributeError):
            error_code = None
        if error_code == "invalid_grant":
            raise RuntimeError("Google rejected the authorization code; run --authorize again.")
        if error_code == "invalid_client":
            raise RuntimeError("Google rejected the OAuth client ID or secret.")
        raise RuntimeError(
            f"Google Calendar authorization exchange failed (HTTP {response.status_code})."
        )
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError("Google Calendar authorization returned invalid JSON.") from None
    token = payload.get("refresh_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError(
            "Google did not return a refresh token. Check the OAuth consent setup and try --authorize again."
        )
    return token


def _secure_temp_permissions(env_path: Path, temp_name: str, mode: int) -> None:
    os.chmod(temp_name, mode & 0o600 or 0o600)
    if os.name != "nt":
        return
    # chmod does not change NTFS ACLs. Copy and protect the source file's DACL
    # before the token is written so the temporary file is no more accessible.
    try:
        import win32security
    except ImportError:
        raise RuntimeError(
            "Cannot safely save the refresh token on Windows without pywin32."
        ) from None
    try:
        descriptor = win32security.GetFileSecurity(
            str(env_path), win32security.DACL_SECURITY_INFORMATION
        )
        descriptor.SetSecurityDescriptorControl(
            win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED
        )
        win32security.SetFileSecurity(
            temp_name,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            descriptor,
        )
    except Exception:
        raise RuntimeError("Could not preserve aa_my_agent/.env Windows file permissions.") from None


def _save_refresh_token(env_path: Path, token: str) -> None:
    if not env_path.is_file() or env_path.is_symlink():
        raise RuntimeError("aa_my_agent/.env must be an existing regular file.")
    original = env_path.read_bytes()
    has_bom = original.startswith(b"\xef\xbb\xbf")
    try:
        content = original.decode("utf-8-sig" if has_bom else "utf-8")
    except UnicodeDecodeError:
        raise RuntimeError("Cannot safely edit aa_my_agent/.env because it is not UTF-8.") from None
    newline = "\r\n" if b"\r\n" in original else "\n"
    new_line = "GOOGLE_CALENDAR_REFRESH_TOKEN=" + json.dumps(token, ensure_ascii=False)
    lines = content.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if _REFRESH_LINE.match(line)]
    if len(matches) > 1:
        raise RuntimeError("aa_my_agent/.env contains duplicate refresh-token keys; resolve them manually.")
    if matches:
        index = matches[0]
        line = lines[index]
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        prefix = _REFRESH_LINE.match(line).group(1)  # type: ignore[union-attr]
        lines[index] = prefix + new_line + ending
        updated = "".join(lines)
    else:
        updated = content + (newline if content and not content.endswith(("\r", "\n")) else "")
        updated += new_line + newline
    encoded = updated.encode("utf-8-sig" if has_bom else "utf-8")
    mode = stat.S_IMODE(env_path.stat().st_mode)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".env.", suffix=".tmp", dir=env_path.parent, delete=False
        ) as temp_file:
            temp_name = temp_file.name
            _secure_temp_permissions(env_path, temp_name, mode)
            temp_file.write(encoded)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, env_path)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def authorize(env_path: Path = ENV_PATH) -> None:
    client_id, client_secret = _client_credentials(env_path)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    result: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP handler API
            request_url = urlsplit(self.path)
            params = parse_qs(request_url.query)
            if request_url.path != "/":
                self.send_error(404, "Not found")
                return
            if params.get("state", [""])[0] != state:
                self.send_error(400, "Invalid OAuth callback")
                return
            if params.get("error"):
                result["error"] = "denied"
            else:
                result["code"] = params.get("code", [""])[0]
                if not result["code"]:
                    result["error"] = "missing_code"
            self.send_response(200 if result.get("code") else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body>Google Calendar authorization received. "
                b"You can close this window.</body></html>"
            )

        def log_message(self, _format: str, *_args: object) -> None:
            # Callback URL carries a one-time authorization code.
            return

    with HTTPServer(("127.0.0.1", 0), CallbackHandler) as server:
        redirect_uri = f"http://127.0.0.1:{server.server_port}"
        url = _authorization_url(client_id, redirect_uri, state, verifier)
        print("Opening Google authorization in your browser. If it does not open, visit:")
        print(url)
        try:
            webbrowser.open(url, new=1)
        except webbrowser.Error:
            pass  # The printed URL remains available for manual opening.
        code = _await_authorization(server, result)
    token = _exchange_code(client_id, client_secret, code, redirect_uri, verifier)
    _save_refresh_token(env_path, token)
    print("Google Calendar authorization completed. Refresh token saved to aa_my_agent/.env.")
    print("Restart small-a to make the calendar tools available.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Authorize small-a to read and create Google Calendar events."
    )
    parser.add_argument(
        "--authorize",
        action="store_true",
        help="start local OAuth flow and save the refresh token in aa_my_agent/.env",
    )
    args = parser.parse_args()
    if not args.authorize:
        print("Setup: enable Google Calendar API and create a Desktop OAuth client in Google Cloud.")
        print("Add GOOGLE_CALENDAR_CLIENT_ID and GOOGLE_CALENDAR_CLIENT_SECRET to aa_my_agent/.env.")
        print("Then run: python -m aa_my_agent.mcp_servers.google_calendar_auth --authorize")
        return 0
    try:
        authorize()
    except (OSError, RuntimeError) as exc:
        print(f"Authorization not completed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
