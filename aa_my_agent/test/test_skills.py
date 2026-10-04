from pathlib import Path

from aa_my_agent.skills import registry


def write_skill(
    skills_dir: Path,
    name: str,
    description: str = "Test skill",
) -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            "---\n\n"
            f"# {name}\n"
        ),
        encoding="utf-8",
    )
    return skill_dir


def test_load_skill_exposes_bundled_resources(
    tmp_path,
    monkeypatch,
):
    skills_dir = tmp_path / "skills"
    skill_dir = write_skill(skills_dir, "resource-skill")

    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "guide.md").write_text(
        "# Guide\n",
        encoding="utf-8",
    )
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "build.py").write_text(
        "print('ok')\n",
        encoding="utf-8",
    )
    (skill_dir / "__pycache__").mkdir()
    (skill_dir / "__pycache__" / "build.pyc").write_bytes(b"cache")

    monkeypatch.setattr(registry, "SKILLS_DIR", skills_dir)

    loaded = registry.load_skill("resource-skill")

    assert "references/guide.md" in loaded
    assert "scripts/build.py" in loaded
    assert "__pycache__" not in loaded
    assert f"Skill directory: {skill_dir}" in loaded


def test_build_system_discovers_skill_created_in_same_process(
    tmp_path,
    monkeypatch,
):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(registry, "SKILLS_DIR", skills_dir)

    first_system = registry.build_system_with_skills("base")
    assert "(no skills available)" in first_system

    write_skill(
        skills_dir,
        "new-skill",
        "Created after the first catalog build",
    )

    refreshed_system = registry.build_system_with_skills("base")

    assert "new-skill" in refreshed_system
    assert "Created after the first catalog build" in refreshed_system

