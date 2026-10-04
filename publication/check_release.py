"""Check a public source tree without loading models or contacting services."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import tokenize
import tomllib


ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv"}
FORBIDDEN_PARTS = {"node_modules", ".workbuddy", ".memory", ".worktrees", "tmp"}
PUBLIC_REPORTS = {
    "README.md",
    "baseline_before_upgrade.md",
    "after_hybrid_bm25_rrf.md",
    "after_image_parent_child_production.md",
    "after_reranker_production.md",
    "after_adjacent_context_production.md",
}
PUBLIC_FIXTURES = {
    "aa_my_agent/eval/rag/fixtures/scan_01/scan_01_image_only.pdf",
    "aa_my_agent/eval/rag/fixtures/scan_01/scan_01_source.png",
}
TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".html", ".example"
}
SECRET_NAME = re.compile(
    r"(?:API_KEY|AUTH_TOKEN|ACCESS_TOKEN|REFRESH_TOKEN|CLIENT_SECRET|PASSWORD)$"
)
SECRET_SHAPE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{24,}|tvly-[A-Za-z0-9_-]{24,}"
    r"|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}"
    r"|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,})"
)


def credentials_in(path: Path) -> list[str]:
    values = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*)", line)
        if not match or not SECRET_NAME.search(match[1]):
            continue
        value = match[2].strip().strip("\"'")
        if len(value) >= 12 and not value.lower().startswith(
            ("your", "replace", "example", "changeme", "<")
        ):
            values.append(value)
    return list(set(values))


def check(credential_files: list[Path]) -> dict:
    errors: list[str] = []
    credentials: list[str] = []
    for credential_file in credential_files:
        credentials.extend(credentials_in(credential_file))

    required = (
        "README.md", "LICENSE", "AGENTS.md", "THIRD_PARTY_NOTICES.md",
        "requirements.txt", ".gitignore", "pyproject.toml", "conftest.py",
        ".github/workflows/tests.yml", "aa_my_agent/main.py",
        "aa_my_agent/.env.example", "aa_my_agent/mcp/servers.example.json",
        "aa_my_agent/rag/RAG_UPGRADE_LOG.md", "publication/UPGRADE_LOG.md",
        "aa_my_agent/eval/rag/reports/README.md",
    )
    for name in required:
        if not (ROOT / name).is_file():
            errors.append(f"Missing required file: {name}")

    files = []
    python_count = 0
    total_bytes = 0
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if any(part in SKIP_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            errors.append(f"Symlink is not allowed in release inputs: {relative.as_posix()}")
            continue
        if not path.is_file():
            continue
        name = relative.as_posix()
        files.append(name)
        size = path.stat().st_size
        total_bytes += size
        if size > 100 * 1024 * 1024:
            errors.append(f"File exceeds 100 MiB: {name}")
        if any(part in FORBIDDEN_PARTS or part.endswith("-workspace") for part in relative.parts):
            errors.append(f"Local runtime/experiment file: {name}")
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            errors.append(f"Local environment file: {name}")
        if name.startswith("aa_my_agent/storage/") and name != "aa_my_agent/storage/README.md":
            errors.append(f"Agent runtime state: {name}")
        if name.startswith(("aa_my_agent/rag/data/knowledge/", "aa_my_agent/eval/rag/storage/")):
            errors.append(f"Private knowledge or database: {name}")
        if name == "aa_my_agent/mcp/servers.json":
            errors.append(f"Personal MCP configuration: {name}")
        if name.startswith("aa_my_agent/eval/rag/reports/") and relative.name not in PUBLIC_REPORTS:
            errors.append(f"Unreviewed generated report: {name}")
        if path.suffix.lower() in {".pdf", ".png", ".docx", ".sqlite", ".sqlite3", ".db", ".pt", ".pth", ".log", ".pem", ".key"} and name not in PUBLIC_FIXTURES:
            errors.append(f"Unexpected data/weight/key file: {name}")

        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {"LICENSE", ".gitignore"}:
            text = path.read_text(encoding="utf-8-sig")
            if SECRET_SHAPE.search(text):
                errors.append(f"Credential-shaped text: {name}")
            if any(value in text for value in credentials):
                errors.append(f"Configured local credential appears in: {name}")
            if text.lstrip().startswith("-----BEGIN PRIVATE KEY-----"):
                errors.append(f"Private key material: {name}")
            if path.name == ".env.example":
                for line in text.splitlines():
                    match = re.match(r"\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*)", line)
                    if match and SECRET_NAME.search(match[1]) and match[2].strip():
                        errors.append(f"Secret field must be empty in template: {name}")
                        break

        try:
            if path.suffix == ".py":
                with tokenize.open(path) as handle:
                    ast.parse(handle.read(), filename=name)
                python_count += 1
            elif path.suffix == ".json":
                json.loads(path.read_text(encoding="utf-8-sig"))
            elif path.suffix == ".toml":
                tomllib.loads(path.read_text(encoding="utf-8-sig"))
            if path.name.startswith("requirements") and path.suffix == ".txt":
                for line in path.read_text(encoding="utf-8-sig").splitlines():
                    if line.strip().startswith("-r "):
                        dependency = (path.parent / line.strip()[3:].strip()).resolve()
                        if not dependency.is_relative_to(ROOT) or not dependency.is_file():
                            errors.append(f"Dependency include missing or outside release: {name}")
        except (SyntaxError, ValueError) as exc:
            errors.append(f"Invalid syntax/data: {name} ({type(exc).__name__})")

    return {
        "status": "passed" if not errors else "failed",
        "files": len(files),
        "python_files": python_count,
        "bytes": total_bytes,
        "local_credential_values_checked": len(set(credentials)),
        "errors": sorted(set(errors)),
        "scope": "Source inputs and configured credential matches; no real model, OCR or retrieval evaluation",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-file", action="append", type=Path, default=[],
                        help="Optional local .env file to compare in memory; values are never printed")
    parser.add_argument("--report", type=Path, help="Write a value-free JSON verification report")
    args = parser.parse_args()
    result = check(args.credential_file)
    if args.report:
        output = args.report.resolve()
        if not output.is_relative_to(ROOT):
            parser.error("The report must stay inside this release tree")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
