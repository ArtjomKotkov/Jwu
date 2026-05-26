from jwu.skills_install import EXPECTED_SKILLS, install_skills


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
