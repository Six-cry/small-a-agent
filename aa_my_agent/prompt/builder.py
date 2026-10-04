import json
from pathlib import Path
from collections.abc import Iterable

from ..config import KNOWLEDGE_DIR, PROMPT_SECTIONS, allow_knowledge_file_type
from ..memory.store import read_memory_index
from ..skills.registry import list_skills, scan_skills
from ..telemetry import emit


_last_context_key: str | None = None
_last_prompt: str | None = None


def list_knowledge_files() -> str:
    """List source filenames without loading documents or accessing embeddings."""
    root = Path(KNOWLEDGE_DIR).resolve()
    if not root.exists():
        return "(no local documents available)"
    try:
        paths = sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in allow_knowledge_file_type
            and not path.is_symlink()
            and path.resolve().is_relative_to(root)
        )
    except OSError:
        return "(local document catalog unavailable; use search_knowledge)"
    # JSON quoting preserves unusual filenames as data on a single line.
    return "\n".join(
        "- " + json.dumps(path, ensure_ascii=False) for path in paths
    ) or "(no local documents available)"


def get_system_prompt(
    enabled_tool_names: Iterable[str],
) -> str:
    """Build the system prompt from the capabilities currently available."""
    global _last_context_key, _last_prompt

    enabled_tools = set(enabled_tool_names)

    # Skills may be created or changed while the process is running.
    scan_skills()
    skill_catalog = list_skills()
    if skill_catalog == "(no skills available)":
        skill_catalog = ""

    memory_index = read_memory_index()
    knowledge_catalog = (
        list_knowledge_files() if "search_knowledge" in enabled_tools else ""
    )
    context = {
        "enabled_tools": sorted(enabled_tools),
        "skill_catalog": skill_catalog,
        "memory_index": memory_index,
        "knowledge_catalog": knowledge_catalog,
    }
    context_key = json.dumps(
        context,
        ensure_ascii=False,
        sort_keys=True,
    )

    if context_key == _last_context_key and _last_prompt is not None:
        emit(
            "PROMPT", "build",
            cache="hit", system_prompt_unchanged="yes",
        )
        return _last_prompt

    sections = [PROMPT_SECTIONS["identity"]]
    loaded_sections = ["identity"]

    conditional_sections = (
        ("todo_write", "todo"),
        ("search_knowledge", "rag"),
        ("subagent_task", "subagent"),
        ("web_search", "web"),
        ("get_current_time", "time"),
    )
    for tool_name, section_name in conditional_sections:
        if tool_name in enabled_tools:
            section = PROMPT_SECTIONS[section_name]
            if section_name == "rag":
                section = section.format(catalog=knowledge_catalog)
            sections.append(section)
            loaded_sections.append(section_name)

    if skill_catalog:
        sections.append(
            PROMPT_SECTIONS["skills"].format(
                catalog=skill_catalog,
            )
        )
        loaded_sections.append("skills")

    if memory_index:
        sections.append(
            PROMPT_SECTIONS["memory"].format(
                index=memory_index,
            )
        )
        loaded_sections.append("memory")

    prompt = "\n\n".join(sections)
    _last_context_key = context_key
    _last_prompt = prompt

    memory_entries = sum(
        1
        for line in memory_index.splitlines()
        if line.strip().startswith("- [")
    )
    emit(
        "PROMPT", "build", cache="miss",
        sections=",".join(loaded_sections), chars=len(prompt),
        enabled_tools=len(enabled_tools), memory_entries=memory_entries,
    )
    return prompt
