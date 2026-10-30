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
    target = tmp_path / "jwu-continue-job" / "SKILL.md"
    target.write_text("СТАРОЕ", encoding="utf-8")

    results = dict(install_skills(tmp_path))
    assert results["jwu-continue-job"] == "обновлён"
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


# --- установка в Cursor и регистрация MCP ---------------------------------------- #

import json as _json

from jwu import skills_install as _si


def test_cursor_agents_drop_claude_tools(tmp_path):
    _si.install_agents(tmp_path, cursor=True)
    text = (tmp_path / "voice-writer-sample.md").read_text()
    assert text.startswith("---\n") and "\ntools:" not in text and "name: voice-writer-sample" in text
    _si.install_agents(tmp_path / "claude")
    assert "\ntools:" in (tmp_path / "claude" / "voice-writer-sample.md").read_text()


def test_register_mcp_cursor_merges(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(_json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1}))
    assert _si.register_mcp_cursor(path, command="/bin/jwu-mcp") == "добавлен"
    data = _json.loads(path.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"} and data["keep"] == 1
    assert data["mcpServers"]["jwu"] == {"command": "/bin/jwu-mcp", "args": []}
    assert _si.register_mcp_cursor(path, command="/bin/jwu-mcp") == "без изменений"
    assert _si.register_mcp_cursor(path, command="/new/jwu-mcp") == "обновлён"
    path.write_text("{битый")
    import pytest
    with pytest.raises(ValueError, match="не JSON"):
        _si.register_mcp_cursor(path, command="/bin/jwu-mcp")


def test_register_mcp_claude_without_cli(monkeypatch):
    monkeypatch.setattr(_si.shutil, "which", lambda name: None)
    assert _si.register_mcp_claude(command="/bin/jwu-mcp").startswith("пропущен")


def test_cli_install_for_cursor(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from jwu.cli import main as cli

    monkeypatch.setattr(_si, "cursor_skills_dest", lambda: tmp_path / "skills")
    monkeypatch.setattr(_si, "cursor_agents_dest", lambda: tmp_path / "agents")
    monkeypatch.setattr(_si, "cursor_mcp_path", lambda: tmp_path / "mcp.json")
    r = CliRunner().invoke(cli.app, ["install", "--for", "cursor"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "skills" / "jwu-session-init" / "SKILL.md").exists()
    assert (tmp_path / "agents" / "reviewer-jwu-sample.md").exists()
    assert "jwu" in _json.loads((tmp_path / "mcp.json").read_text())["mcpServers"]


def test_register_mcp_claude_checks_user_scope(tmp_path, monkeypatch):
    calls = []
    cfg = tmp_path / "claude.json"
    cfg.write_text(_json.dumps({"mcpServers": {"jwu": {"command": "/bin/jwu-mcp"}}}))
    monkeypatch.setenv("CLAUDE_CONFIG_PATH", str(cfg))
    monkeypatch.setattr(_si.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(_si.subprocess, "run", lambda args, **kw: calls.append(args))
    assert _si.register_mcp_claude(command="/bin/jwu-mcp") == "без изменений" and calls == []


def test_copyable_texts_in_code_block_and_no_promises():
    """Текст для копирования — в блоке кода; обещаний от имени пользователя голос не пишет."""
    from importlib.resources import files

    root = files("jwu")
    voice = (root / "agents" / "voice-writer-sample.md").read_text(encoding="utf-8")
    assert "```text" in voice and "Никаких обещаний от имени пользователя" in voice
    duty = (root / "agents" / "duty-support-sample.md").read_text(encoding="utf-8")
    assert "Предлагаю сделать дежурному:" in duty and "каждый в своём блоке кода" in duty
    skill = (root / "skills" / "jwu-duty" / "SKILL.md").read_text(encoding="utf-8")
    assert "Обещания — только по решению дежурного" in skill and 'kind="todo"' in skill
    for d in (root / "skills").iterdir():
        md = d / "SKILL.md"
        text = md.read_text(encoding="utf-8") if md.is_file() else ""
        if "## Текст от имени пользователя (голос)" in text:
            assert "текст целиком **в блоке кода**" in text, d.name


def test_voice_agent_rewrites_draft_not_writes_from_facts():
    """Голос переписывает форму технического черновика; содержание пишет тот, кто разбирался."""
    from importlib.resources import files

    root = files("jwu")
    voice = (root / "agents" / "voice-writer-sample.md").read_text(encoding="utf-8")
    assert "`draft`" in voice and "Что ушло при сжатии" in voice and "Содержание — из черновика" in voice
    filt = (root / "agents" / "review-filter-sample.md").read_text(encoding="utf-8")
    assert "Черновик:" in filt
    for d in (root / "skills").iterdir():
        md = d / "SKILL.md"
        text = md.read_text(encoding="utf-8") if md.is_file() else ""
        if "## Текст от имени пользователя (голос)" in text:
            assert "технический черновик" in text and "`draft`" in text, d.name
            assert "`facts`, `context`" not in text, d.name
