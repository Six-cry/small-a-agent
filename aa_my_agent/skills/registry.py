import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import SKILLS_DIR


SKILL_NAME_PATTERN = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$"
)


@dataclass(frozen=True)
class SkillRecord:
    """一个已经发现的 Skill。"""

    name: str
    description: str
    manifest_path: Path
    skill_dir: Path
    resources: tuple[str, ...]


SKILL_REGISTRY: dict[str, SkillRecord] = {}
SKILL_ERRORS: list[str] = []

IGNORED_RESOURCE_PARTS = {
    "__pycache__",
    ".git",
}


def _parse_frontmatter(text: str, ) -> tuple[dict, str]:
    """
    解析 SKILL.md 开头的 YAML Frontmatter。

    返回：
    - metadata
    - 正文
    """
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)

    if len(parts) < 3:
        return {}, text

    try:
        metadata = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}, text

    if not isinstance(metadata, dict):
        return {}, text

    return metadata, parts[2].strip()


def _fallback_description(text: str, default: str,) -> str:
    """没有 description 时，从第一个 Markdown 标题生成描述。"""
    for line in text.splitlines():
        stripped = line.strip()

        if stripped.startswith("#"):
            description = stripped.lstrip("#").strip()

            if description:
                return description

    return default


def _discover_resources(skill_dir: Path) -> tuple[str, ...]:
    """
    返回 Skill 文件夹内除 SKILL.md 外的资源路径。

    这里只建立轻量目录，不读取资源正文。模型加载 Skill 后，再按照
    SKILL.md 的指引按需读取 references、执行 scripts 或使用 assets。
    """
    resources: list[str] = []
    resolved_skill_dir = skill_dir.resolve()

    for resource_path in sorted(skill_dir.rglob("*")):
        if not resource_path.is_file():
            continue

        relative_path = resource_path.relative_to(skill_dir)

        if relative_path.as_posix() == "SKILL.md":
            continue

        if any(
            part in IGNORED_RESOURCE_PARTS
            or part.endswith((".pyc", ".pyo"))
            for part in relative_path.parts
        ):
            continue

        try:
            resolved_resource = resource_path.resolve()
        except OSError:
            continue

        if not resolved_resource.is_relative_to(resolved_skill_dir):
            continue

        resources.append(relative_path.as_posix())

    return tuple(resources)


def scan_skills() -> dict[str, SkillRecord]:
    """
    扫描 aa_my_agent/skills/*/SKILL.md。

    扫描时只建立目录，不把 Skill 正文放进模型上下文。
    """
    discovered: dict[str, SkillRecord] = {} # 本次扫描发现的新技能（局部变量）
    errors: list[str] = [] # 本次扫描的错误信息

    if not SKILLS_DIR.exists():
        SKILL_REGISTRY.clear()
        SKILL_ERRORS.clear()
        return SKILL_REGISTRY

    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue

        manifest_path = skill_dir / "SKILL.md"

        if not manifest_path.exists():
            continue

        try:
            raw = manifest_path.read_text(
                encoding="utf-8"
            )
        except OSError as exc:
            errors.append(
                f"{skill_dir.name}: 无法读取 SKILL.md - {exc}"
            )
            continue

        metadata, _ = _parse_frontmatter(raw)

        name = str(
            metadata.get("name", skill_dir.name)
        ).strip()

        description = str(
            metadata.get("description", "")
        ).strip()

        if not description:
            description = _fallback_description(
                raw,
                default=f"Skill: {name}",
            )

        if not SKILL_NAME_PATTERN.fullmatch(name):
            errors.append(
                f"{skill_dir.name}: 非法 Skill 名称 {name!r}"
            )
            continue

        if name in discovered:
            errors.append(
                f"{skill_dir.name}: Skill 名称重复 {name!r}"
            )
            continue

        discovered[name] = SkillRecord(
            name=name,
            description=description,
            manifest_path=manifest_path,
            skill_dir=skill_dir,
            resources=_discover_resources(skill_dir),
        )

    SKILL_REGISTRY.clear()
    SKILL_REGISTRY.update(discovered)

    SKILL_ERRORS.clear()
    SKILL_ERRORS.extend(errors)

    return SKILL_REGISTRY


def list_skills() -> str:
    """返回适合放入 System Prompt 的简短 Skill 目录。"""
    if not SKILL_REGISTRY:
        return "(no skills available)"

    return "\n".join(
        f"- {skill.name}: {skill.description}"
        for skill in SKILL_REGISTRY.values()
    )


def load_skill(name: str) -> str:
    """
    按准确名称加载 Skill 完整内容。

    通过注册表查找，不允许模型直接拼接任意路径。
    """
    if not isinstance(name, str) or not name.strip():
        return "Error: skill name cannot be empty"

    # Skill 可能在当前会话中刚刚创建或更新，因此每次加载前刷新。
    scan_skills()

    normalized_name = name.strip()
    skill = SKILL_REGISTRY.get(normalized_name)

    if skill is None:
        available = ", ".join(
            SKILL_REGISTRY.keys()
        ) or "(none)"

        return (
            f"Error: skill not found: {normalized_name}. "
            f"Available skills: {available}"
        )

    try:
        content = skill.manifest_path.read_text(
            encoding="utf-8"
        )
    except OSError as exc:
        return (
            f"Error: failed to load skill "
            f"{normalized_name}: {exc}"
        )

    if skill.resources:
        resource_catalog = "\n".join(
            f"- {resource_path}"
            for resource_path in skill.resources
        )
    else:
        resource_catalog = "(no bundled resources)"

    return (
        f"<loaded_skill name=\"{skill.name}\">\n"
        f"Skill directory: {skill.skill_dir}\n\n"
        "<resource_usage>\n"
        "Paths mentioned by this skill are relative to the Skill directory. "
        "Read only the referenced resources needed for the current task. "
        "Use read_file for references and text instructions, bash for scripts, "
        "and preserve assets for generated outputs. Do not claim to have used "
        "a bundled resource unless you actually read, executed, or copied it.\n"
        "</resource_usage>\n\n"
        "<bundled_resources>\n"
        f"{resource_catalog}\n"
        "</bundled_resources>\n\n"
        f"{content}\n"
        f"</loaded_skill>"
    )


def build_system_with_skills(base_system: str,) -> str:
    """在原有 System Prompt 后加入轻量 Skill 目录。"""
    # 允许当前进程立即发现刚创建或修改的 Skill，无需重启小 A。
    scan_skills()
    catalog = list_skills()

    return (
        f"{base_system}\n\n"
        "<available_skills>\n"
        f"{catalog}\n"
        "</available_skills>\n\n"
        "When a task clearly matches an available skill, "
        "call load_skill with the exact skill name before "
        "starting the task. Follow the loaded instructions. "
        "A loaded skill may contain bundled references, scripts, assets, "
        "templates, or specialized agent instructions. Resolve their paths "
        "relative to the Skill directory returned by load_skill, and use "
        "the relevant resources on demand. Do not read every resource "
        "up front. If a required resource or capability is unavailable, "
        "say so and use the skill's documented fallback when one exists. "
        "Do not load unrelated skills. Do not claim to have "
        "loaded a skill unless load_skill returned it."
    )


# 程序启动时扫描一次。
scan_skills()
