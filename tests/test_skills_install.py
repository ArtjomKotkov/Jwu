from jwu.skills_install import (
    EXPECTED_AGENTS,
    EXPECTED_SKILLS,
    install_agents,
    install_skills,
)


def test_installs_all_bundled_skills(tmp_path):
    results = install_skills(tmp_path)
    names = {name for name, _ in results}
    # все ожидаемые jwu-скиллы развёрнуты
    assert EXPECTED_SKILLS <= names
    for name in EXPECTED_SKILLS:
        md = tmp_path / name / "SKILL.md"
        assert md.is_file()
        assert md.read_text(encoding="utf-8").lstrip().startswith("---")  # есть frontmatter
    # на чистый каталог — все "добавлен"
    assert all(action == "добавлен" for _, action in results)


def test_replaces_existing(tmp_path):
    install_skills(tmp_path)
    # подменим один скилл локально — повторная установка должна перезаписать
    target = tmp_path / "jwu-resume-job" / "SKILL.md"
    target.write_text("СТАРОЕ", encoding="utf-8")

    results = dict(install_skills(tmp_path))
    assert results["jwu-resume-job"] == "обновлён"
    assert "СТАРОЕ" not in target.read_text(encoding="utf-8")


def test_installs_all_bundled_agents(tmp_path):
    results = install_agents(tmp_path)
    names = {name for name, _ in results}
    # все ожидаемые субагенты развёрнуты
    assert EXPECTED_AGENTS <= names
    for name in EXPECTED_AGENTS:
        md = tmp_path / f"{name}.md"
        assert md.is_file()
        assert md.read_text(encoding="utf-8").lstrip().startswith("---")  # есть frontmatter
    # на чистый каталог — все "добавлен"
    assert all(action == "добавлен" for _, action in results)


def test_replaces_existing_agent(tmp_path):
    install_agents(tmp_path)
    target = tmp_path / "reviewer-jwu-sample.md"
    target.write_text("СТАРОЕ", encoding="utf-8")

    results = dict(install_agents(tmp_path))
    assert results["reviewer-jwu-sample"] == "обновлён"
    assert "СТАРОЕ" not in target.read_text(encoding="utf-8")


def test_install_removes_retired_skills(tmp_path):
    """Переименованный скилл нельзя оставлять у пользователя: он сработает по своим
    триггерам со старыми инструкциями."""
    from jwu.skills_install import RETIRED_SKILLS, install_skills

    stale = tmp_path / "jwu-create-issue"
    stale.mkdir()
    (stale / "SKILL.md").write_text("старое", encoding="utf-8")
    assert "jwu-create-issue" in RETIRED_SKILLS

    results = install_skills(tmp_path)

    assert not stale.exists()
    assert ("jwu-create-issue", "удалён (устарел)") in results
    assert (tmp_path / "jwu-task-create" / "SKILL.md").is_file()


def test_install_does_not_touch_foreign_skills(tmp_path):
    """Чужие скиллы пользователя не трогаем — удаляем только перечисленные явно."""
    from jwu.skills_install import install_skills

    mine = tmp_path / "jwu-my-own"
    mine.mkdir()
    (mine / "SKILL.md").write_text("моё", encoding="utf-8")

    install_skills(tmp_path)

    assert (mine / "SKILL.md").read_text(encoding="utf-8") == "моё"
