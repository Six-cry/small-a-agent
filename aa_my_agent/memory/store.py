import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import MEMORY_DIR, MEMORY_INDEX_PATH
from ..telemetry import emit


MEMORY_TYPES = {
    "user",
    "feedback",
    "project",
    "reference",
}


@dataclass(frozen=True)
class MemoryRecord:
    filename: str
    name: str
    memory_type: str
    description: str
    body: str


def _slugify(name: str) -> str:
    slug = name.strip().lower()
    slug = re.sub(
        r"[^a-z0-9\u4e00-\u9fff_-]+",
        "-",
        slug,
    )
    slug = slug.strip("-_")

    return slug[:80] or "memory"


def _parse_memory(path: Path) -> MemoryRecord | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None

    if not raw.startswith("---"):
        return None

    parts = raw.split("---", 2)

    if len(parts) != 3:
        return None

    try:
        metadata = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None

    return MemoryRecord(
        filename=path.name,
        name=str(metadata.get("name", path.stem)),
        memory_type=str(metadata.get("type", "user")),
        description=str(metadata.get("description", "")),
        body=parts[2].strip(),
    )


def list_memories() -> list[MemoryRecord]:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    records = []

    for path in sorted(MEMORY_DIR.glob("*.md")):
        if path.name == "MEMORY.md":
            continue

        record = _parse_memory(path)

        if record is not None:
            records.append(record)

    return records


def read_memory(filename: str) -> str | None:
    # 防止模型构造 ../ 等路径
    safe_name = Path(filename).name

    if safe_name != filename:
        return None

    path = MEMORY_DIR / safe_name

    if not path.exists():
        return None

    return path.read_text(encoding="utf-8")


def read_memory_index() -> str:
    if not MEMORY_INDEX_PATH.exists():
        return ""

    return MEMORY_INDEX_PATH.read_text(
        encoding="utf-8"
    ).strip()


def rebuild_memory_index() -> None:
    records = list_memories()
    lines = []

    for record in records:
        lines.append(
            f"- [{record.name}]({record.filename})"
            f" — {record.description}"
        )

    content = "\n".join(lines)

    if content:
        content += "\n"

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    temporary = MEMORY_INDEX_PATH.with_suffix(".tmp")

    temporary.write_text(
        content,
        encoding="utf-8",
    )

    temporary.replace(MEMORY_INDEX_PATH)
    emit(
        "MEMORY", "index",
        action="rebuilt", entries=len(records),
    )


def write_memory(
    name: str,
    memory_type: str,
    description: str,
    body: str,
) -> Path:
    if memory_type not in MEMORY_TYPES:
        raise ValueError(
            f"Unsupported memory type: {memory_type}"
        )

    description = " ".join(description.split())

    if not description or not body.strip():
        raise ValueError(
            "Memory description and body cannot be empty"
        )

    metadata = {
        "name": name,
        "description": description[:300],
        "type": memory_type,
    }

    content = (
        "---\n"
        + yaml.safe_dump(
            metadata,
            allow_unicode=True,
            sort_keys=False,
        )
        + "---\n\n"
        + body.strip()[:8000]
        + "\n"
    )

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    path = MEMORY_DIR / f"{_slugify(name)}.md"
    action = "updated" if path.exists() else "created"
    temporary = path.with_suffix(".tmp")

    temporary.write_text(
        content,
        encoding="utf-8",
    )

    temporary.replace(path)
    emit(
        "MEMORY", "write",
        action=action, type=memory_type, file=path.name,
    )
    rebuild_memory_index()

    return path
