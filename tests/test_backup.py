"""jwu backup / restore (JWU-21): архив с БД, конфигом и проектными скиллами; восстановление."""

import json
import sqlite3
import tarfile

import pytest
from typer.testing import CliRunner

from jwu.cli import main as cli
from jwu.core import backup as bk
from jwu.core import config as cfgmod
from jwu.core.store import Store

runner = CliRunner()


def _env(monkeypatch, tmp_path):
    """Своя БД, свой config.toml, свои ~/.claude/agents и skills."""
    db = tmp_path / "data" / "state.db"
    db.parent.mkdir(parents=True)
    monkeypatch.setenv("JWU_DB_PATH", str(db))
    cfg = tmp_path / "cfg" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[storage]\ndb_path = "/Users/someone-else/.local/share/jwu/state.db"\n')
    monkeypatch.setattr(cfgmod, "config_path", lambda: cfg)
    monkeypatch.setattr(bk, "config_path", lambda: cfg)
    agents = tmp_path / "claude" / "agents"
    skills = tmp_path / "claude" / "skills"
    agents.mkdir(parents=True)
    skills.mkdir(parents=True)
    (agents / "reviewer-myproj.md").write_text("Ревьювер, используется скиллом /jwu-job-review")
    (agents / "unrelated.md").write_text("Про кошек")
    (agents / "reviewer-jwu-sample.md").write_text("из поставки jwu")
    (skills / "myproj-duty").mkdir()
    (skills / "myproj-duty" / "SKILL.md").write_text("---\nname: myproj-duty\n---\nзовёт jwu_task")
    (skills / "jwu-track-job").mkdir()
    (skills / "jwu-track-job" / "SKILL.md").write_text("из поставки jwu")
    monkeypatch.setattr(bk, "default_agents_dest", lambda: agents)
    monkeypatch.setattr(bk, "default_dest", lambda: skills)
    store = Store(db)
    ws = store.get_workspace_by_slug("work")
    store.set_workspace_secret(ws.id, "jira.token", "SECRET-TOKEN")
    store.use_workspace(ws.id)
    store.create_job("PROJ-1", "работа")
    store.close()
    return db, cfg, agents, skills


def test_collect_extras_picks_only_project_files_about_jwu(monkeypatch, tmp_path):
    _, _, agents, skills = _env(monkeypatch, tmp_path)
    names = sorted(arc for _, arc in bk.collect_extras(agents, skills))
    assert names == ["claude-extras/agents/reviewer-myproj.md", "claude-extras/skills/myproj-duty/SKILL.md"]


def test_backup_archive_contents_and_no_secrets(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    report = bk.create_backup(tmp_path / "out" / "b.tar.gz", user="alice")
    with tarfile.open(report.archive) as tar:
        names = sorted(m.name for m in tar.getmembers() if m.isfile())
    assert names == sorted([
        "b/state.db", "b/config.toml", "b/claude-extras/agents/reviewer-myproj.md",
        "b/claude-extras/skills/myproj-duty/SKILL.md", "b/manifest.json", "b/RESTORE.md", "b/SHA256SUMS",
    ])
    with tarfile.open(report.archive) as tar:
        manifest = json.loads(tar.extractfile("b/manifest.json").read())
        restore_md = tar.extractfile("b/RESTORE.md").read().decode()
        db_bytes = tar.extractfile("b/state.db").read()
    assert manifest["with_secrets"] and manifest["user"] == "alice" and "reviewer-myproj.md" in str(manifest["extras"])
    assert "jwu restore b.tar.gz" in restore_md and "СЕКРЕТЫ" in restore_md
    assert b"SECRET-TOKEN" in db_bytes

    clean = bk.create_backup(tmp_path / "out" / "clean.tar.gz", with_secrets=False, extras=False)
    with tarfile.open(clean.archive) as tar:
        assert not any("claude-extras" in m.name for m in tar.getmembers())
        assert b"SECRET-TOKEN" not in tar.extractfile("clean/state.db").read()


def test_restore_into_fresh_home_rewrites_db_path_and_extras(monkeypatch, tmp_path):
    db, _, _, _ = _env(monkeypatch, tmp_path)
    archive = bk.create_backup(tmp_path / "b.tar.gz").archive
    new_db = tmp_path / "new" / "state.db"
    new_cfg = tmp_path / "new" / "config.toml"
    agents = tmp_path / "new" / "agents"
    skills = tmp_path / "new" / "skills"
    dry = bk.restore_backup(archive, dry_run=True, target_db=new_db, target_config=new_cfg,
                            agents_dir=agents, skills_dir=skills)
    assert dry.dry_run and not new_db.exists() and dry.extras == ["agents/reviewer-myproj.md", "skills/myproj-duty"]

    rep = bk.restore_backup(archive, target_db=new_db, target_config=new_cfg, agents_dir=agents, skills_dir=skills)
    assert new_db.exists() and (new_db.stat().st_mode & 0o777) == 0o600
    assert f'db_path = "{new_db}"' in new_cfg.read_text()          # путь переписан под эту машину
    assert (agents / "reviewer-myproj.md").exists() and (skills / "myproj-duty" / "SKILL.md").exists()
    restored = Store(new_db)
    try:
        ws = restored.get_workspace_by_slug("work")
        assert restored.get_workspace_secret(ws.id, "jira.token") == "SECRET-TOKEN"
        restored.use_workspace(ws.id)
        assert restored.jobs_for_task("PROJ-1")
    finally:
        restored.close()
    assert not rep.db_backup

    # повторно поверх существующей — только с force, старая копия остаётся рядом
    with pytest.raises(bk.BackupError, match="force"):
        bk.restore_backup(archive, target_db=new_db, target_config=new_cfg, agents_dir=agents, skills_dir=skills)
    rep2 = bk.restore_backup(archive, force=True, db_only=True, target_db=new_db, target_config=new_cfg,
                             agents_dir=agents, skills_dir=skills)
    assert rep2.db_backup and (tmp_path / "new" / rep2.db_backup.split("/")[-1]).exists()


def test_restore_rejects_tampered_archive(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    archive = bk.create_backup(tmp_path / "t.tar.gz").archive
    # подменим state.db внутри архива
    work = tmp_path / "unpack"
    with tarfile.open(archive) as tar:
        tar.extractall(work)
    con = sqlite3.connect(str(work / "t" / "state.db"))
    con.execute("INSERT INTO meta (key, value) VALUES ('x', 'y')")
    con.commit()
    con.close()
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        tar.add(work / "t", arcname="t")
    with pytest.raises(bk.BackupError, match="Контрольные суммы"):
        bk.restore_backup(bad, target_db=tmp_path / "x.db", target_config=tmp_path / "x.toml",
                          agents_dir=tmp_path / "a", skills_dir=tmp_path / "s")
    with pytest.raises(bk.BackupError, match="не найден"):
        bk.restore_backup(tmp_path / "nope.tar.gz")


def test_cli_backup_and_restore(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_prepare_db", lambda: None)
    res = runner.invoke(cli.app, ["backup", "--out", str(tmp_path / "arc"), "--json"])
    assert res.exit_code == 0, res.output
    archive = json.loads(res.stdout)["archive"]
    assert archive.endswith(".tar.gz") and "state.db" in json.loads(res.stdout)["files"]
    res = runner.invoke(cli.app, ["restore", archive, "--dry-run"])
    assert res.exit_code == 1 and "force" in res.output   # БД уже есть (JWU_DB_PATH указывает на неё)
    res = runner.invoke(cli.app, ["restore", archive, "--dry-run", "--force"])
    assert res.exit_code == 0 and "Сухой прогон" in res.output
