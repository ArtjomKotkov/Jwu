"""Безголовый бутстрап из окружения, jwu doctor и предупреждение о версии MCP (JWU-20/30/31)."""

import asyncio
import json

from typer.testing import CliRunner

from jwu import mcp_server as srv
from jwu.cli import main as cli
from jwu.core import doctor, workspaces
from jwu.core.config import Config, apply_env_overrides
from jwu.core.store import Store

runner = CliRunner()


def test_env_overrides_apply_to_workspace_config(monkeypatch, tmp_path):
    monkeypatch.setenv("JWU_JIRA_URL", "https://jira.env/")
    monkeypatch.setenv("JWU_JIRA_USER", "alice")
    monkeypatch.setenv("JWU_BITBUCKET_REPO", "repo-env")
    monkeypatch.setenv("JWU_TELEGRAM_CHAT", "777")
    monkeypatch.setenv("JIRA_PASSWORD", "pw")
    cfg = Config()
    applied = apply_env_overrides(cfg)
    assert set(applied) == {"JWU_JIRA_URL", "JWU_JIRA_USER", "JWU_BITBUCKET_REPO", "JWU_TELEGRAM_CHAT"}
    assert cfg.jira.base_url == "https://jira.env" and cfg.telegram.chat_id == "777"

    store = Store(tmp_path / "s.db")
    try:
        ws = store.get_workspace_by_slug("work")
        store.set_workspace_settings(ws.id, {"jira.base_url": "https://jira.db", "jira.project": "DB"})
        wcfg = workspaces.config_for_workspace(store, ws)
        assert wcfg.jira.base_url == "https://jira.env"      # env перекрывает БД
        assert wcfg.jira.project == "DB"                     # незатронутое остаётся
        from jwu.core.config import get_slot
        assert get_slot(wcfg, "jira.password") == "pw"        # пароль из окружения
    finally:
        store.close()


def _quiet_doctor(monkeypatch, tmp_path):
    """Доктор без внешнего мира: чужой ~/.claude.json, свои каталоги скиллов, лок демона."""
    monkeypatch.setenv("CLAUDE_CONFIG_PATH", str(tmp_path / "claude.json"))
    monkeypatch.setattr(doctor, "default_dest", lambda: tmp_path / "skills")
    monkeypatch.setattr(doctor, "default_agents_dest", lambda: tmp_path / "agents")
    monkeypatch.setattr(doctor.daemon, "lock_path", lambda: tmp_path / "d.lock")
    monkeypatch.setattr(doctor.daemon, "plist_path", lambda: tmp_path / "none.plist")
    monkeypatch.setattr(doctor.daemon, "systemd_unit_path", lambda: tmp_path / "none.service")
    monkeypatch.setattr(doctor.daemon, "_run", lambda cmd: (1, ""))
    # keyring машины разработчика — не часть теста: секреты только из env/БД
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda s, a: None)


def test_doctor_offline_reports_missing_pieces(monkeypatch, tmp_path):
    _quiet_doctor(monkeypatch, tmp_path)
    db = tmp_path / "state.db"
    monkeypatch.setenv("JWU_DB_PATH", str(db))
    store = Store(db)
    try:
        ws = store.get_workspace_by_slug("work")
        workspaces.add_path(store, ws, tmp_path / "missing-repo")
        checks = {c.name: c for c in doctor.run(store, explicit_workspace="work", network=False)}
    finally:
        store.close()
    assert checks["БД"].status == "ok" and "schema" not in checks["БД"].detail
    assert checks["Папки воркспейса"].status == "warn" and "missing-repo" in checks["Папки воркспейса"].detail
    assert checks["Конфиг Jira"].status == "fail" and "jira.token/password" in checks["Конфиг Jira"].detail
    assert checks["Конфиг Bitbucket"].status == "fail"
    assert checks["Доступы"].status == "skip"
    assert checks["Демон"].status == "skip"
    assert checks["MCP в Claude Code"].status == "skip"          # claude.json нет
    assert checks["Скиллы Claude"].status == "warn" and "нет:" in checks["Скиллы Claude"].detail
    assert checks["Память (git)"].status == "skip"
    assert checks["Telegram"].status == "skip"


def test_doctor_happy_config_and_mcp(monkeypatch, tmp_path):
    _quiet_doctor(monkeypatch, tmp_path)
    (tmp_path / "claude.json").write_text(json.dumps({"mcpServers": {"jwu": {"command": str(tmp_path / "jwu-mcp")}}}))
    (tmp_path / "jwu-mcp").write_text("#!/bin/sh\n")
    monkeypatch.setenv("JWU_JIRA_URL", "https://jira.x")
    monkeypatch.setenv("JWU_JIRA_USER", "alice")
    monkeypatch.setenv("JIRA_TOKEN", "t")
    monkeypatch.setenv("JWU_BITBUCKET_URL", "https://git.x")
    monkeypatch.setenv("BITBUCKET_TOKEN", "b")
    db = tmp_path / "state.db"
    monkeypatch.setenv("JWU_DB_PATH", str(db))
    store = Store(db)
    try:
        ws = store.get_workspace_by_slug("work")
        repo = tmp_path / "repo"
        repo.mkdir()
        workspaces.add_path(store, ws, repo)
        checks = {c.name: c for c in doctor.run(store, explicit_workspace="work", network=False)}
    finally:
        store.close()
    assert checks["Конфиг Jira"].status == "ok" and "alice" in checks["Конфиг Jira"].detail
    assert checks["Конфиг Bitbucket"].status == "ok"
    assert checks["Папки воркспейса"].status == "ok"
    assert checks["MCP в Claude Code"].status == "ok"
    # битая регистрация MCP — команда не существует
    (tmp_path / "claude.json").write_text(json.dumps({"mcpServers": {"jwu": {"command": "/nope/jwu-mcp"}}}))
    assert doctor.check_mcp()[0].status == "fail"


def test_doctor_flags_cloud_db_and_version_mismatch(monkeypatch, tmp_path):
    cloud = tmp_path / "Library" / "Mobile Documents" / "x"
    cloud.mkdir(parents=True)
    db = cloud / "state.db"
    monkeypatch.setenv("JWU_DB_PATH", str(db))
    store = Store(db)
    try:
        names = {c.name: c for c in doctor.check_db(store)}
    finally:
        store.close()
    assert names["БД в облаке"].status == "fail"
    monkeypatch.setattr(doctor, "_installed_version", lambda: "0.0.1")
    assert doctor.check_version()[0].status == "warn"
    monkeypatch.setattr(doctor, "_installed_version", lambda: "dev")
    assert doctor.check_version()[0].status == "ok"


def test_cli_doctor_json_and_exit_code(monkeypatch, tmp_path):
    _quiet_doctor(monkeypatch, tmp_path)
    db = tmp_path / "state.db"
    monkeypatch.setenv("JWU_DB_PATH", str(db))
    Store(db).close()
    monkeypatch.setattr(cli, "_WORKSPACE_ARG", "work")
    res = runner.invoke(cli.app, ["doctor", "--offline", "--json"])
    assert res.exit_code == 1  # конфиг Jira не заполнен → fail
    payload = json.loads(res.stdout)
    assert any(c["name"] == "Конфиг Jira" and c["status"] == "fail" for c in payload)


def test_configure_refuses_cloud_db_path_without_force(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_open_store", lambda: (_ for _ in ()).throw(AssertionError("не должен дойти")))
    res = runner.invoke(cli.app, ["configure", "--non-interactive",
                                  "--db-path", str(tmp_path / "Dropbox" / "state.db")])
    assert res.exit_code == 1 and "Отказ" in res.output


def test_mcp_version_warning(monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    monkeypatch.setenv("JWU_DB_PATH", str(db))
    monkeypatch.setattr(srv, "_base_store", None)
    monkeypatch.setattr(srv, "_stores", {})
    monkeypatch.setattr(srv, "_installed_version", lambda: "9.9.9")
    try:
        payload = asyncio.run(srv.jwu_version())
        assert payload["installed"] == "9.9.9" and "перезапусти" in payload["version_warning"]
        monkeypatch.setattr(srv, "_installed_version", lambda: srv._version())
        assert "version_warning" not in asyncio.run(srv.jwu_version())
    finally:
        if srv._base_store is not None:
            srv._base_store.close()
