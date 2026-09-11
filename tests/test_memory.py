"""Память отдельно от кэша: экспорт, импорт с дедупликацией, синк через git (JWU-19)."""

import json
import subprocess

import pytest
from typer.testing import CliRunner

from jwu.cli import main as cli
from jwu.core import memory, workspaces
from jwu.core.store import Store

runner = CliRunner()


def _populated(tmp_path):
    store = Store(tmp_path / "src.db")
    work = store.get_workspace_by_slug("work")
    home = workspaces.create(store, "home", name="Личное", provider="local")
    workspaces.add_path(store, home, tmp_path / "pet", tags=["фронт"])
    store.use_workspace(home.id)
    store.add_rule("Не пушить в main", kind="constraint")
    store.add_rule("Стенд", text="docker compose up", kind="howto", tag="фронт")
    feat = store.create_feature("Тёмная тема", priority="высокий")
    job = store.create_job("", "тема", feature_id=feat.id)
    store.add_job_record(job.id, "начал", kind="phase", status="done", branch="b", commit="c1")
    store.link_job_pr(job.id, 5, "o", "r")
    store.add_note("HOMEJWU-1", "заметка")
    store.use_workspace(work.id)
    store.set_workspace_settings(work.id, {"jira.base_url": "https://jira.x"})
    store.set_workspace_secret(work.id, "jira.token", "SECRET")
    job2 = store.create_job("PROJ-1", "фикс")
    store.add_job_record(job2.id, "разбор", kind="note")
    return store


def test_export_writes_json_without_secrets_or_snapshots(tmp_path):
    store = _populated(tmp_path)
    try:
        report = memory.export_memory(store, tmp_path / "mem")
    finally:
        store.close()
    assert (report.workspaces, report.rules, report.features, report.jobs, report.notes) == (2, 2, 1, 2, 1)
    index = json.loads((tmp_path / "mem" / "workspaces.json").read_text())
    slugs = {w["slug"]: w for w in index["workspaces"]}
    assert slugs["home"]["paths"][0]["tags"] == ["фронт"]
    assert slugs["work"]["settings"] == {"jira.base_url": "https://jira.x"}
    dump = "".join(p.read_text() for p in (tmp_path / "mem").rglob("*.json"))
    assert "SECRET" not in dump and "features.seq" not in dump
    jobs = json.loads((tmp_path / "mem" / "home" / "jobs.json").read_text())
    features = json.loads((tmp_path / "mem" / "home" / "features.json").read_text())
    assert jobs[0]["feature_key"] == features[0]["key"] and features[0]["key"].endswith("-1")
    assert jobs[0]["records"][0]["commit"] == "c1" and jobs[0]["prs"][0]["pr_id"] == 5


def test_import_into_fresh_db_and_repeat_is_idempotent(tmp_path):
    src = _populated(tmp_path)
    memory.export_memory(src, tmp_path / "mem")
    src.close()

    dst = Store(tmp_path / "dst.db")
    try:
        dry = memory.import_memory(dst, tmp_path / "mem", dry_run=True)
        assert dry.dry_run and dry.added["workspaces"] == 1  # home новый, work уже есть
        assert dst.get_workspace_by_slug("home") is None       # сухой прогон ничего не создал

        first = memory.import_memory(dst, tmp_path / "mem")
        assert first.added["workspaces"] == 1 and first.added["rules"] == 2
        assert first.added["features"] == 1 and first.added["jobs"] == 2 and first.added["notes"] == 1
        home = dst.get_workspace_by_slug("home")
        assert home.name == "Личное" and home.paths[0].tags == ["фронт"]
        dst.use_workspace(home.id)
        feat = dst.list_features()[0]
        assert feat.key.endswith("-1") and feat.priority == "высокий"
        job = dst.list_jobs()[0]
        assert job.feature_key == feat.key and job.records[0].commit == "c1" and job.prs[0].pr_id == 5
        assert dst.get_notes("HOMEJWU-1")[0].text == "заметка"
        # новая фича после импорта получает следующий номер, а не «-1» повторно
        assert dst.create_feature("ещё").key == feat.key.rsplit("-", 1)[0] + "-2"

        second = memory.import_memory(dst, tmp_path / "mem")
        assert not second.added and second.skipped["jobs"] == 2 and second.skipped["rules"] == 2
    finally:
        dst.close()


def test_import_merges_new_records_and_newer_status(tmp_path):
    src = _populated(tmp_path)
    memory.export_memory(src, tmp_path / "mem")
    dst = Store(tmp_path / "dst.db")
    try:
        memory.import_memory(dst, tmp_path / "mem")
        # на исходной машине работа продолжилась: новая запись + закрыли
        src.use_workspace(src.get_workspace_by_slug("work").id)
        job = src.jobs_for_task("PROJ-1")[0]
        src.add_job_record(job.id, "готово", kind="phase", status="done")
        src.set_job_status(job.id, "done")
        memory.export_memory(src, tmp_path / "mem")

        rep = memory.import_memory(dst, tmp_path / "mem")
        assert rep.updated["jobs"] == 1
        dst.use_workspace(dst.get_workspace_by_slug("work").id)
        merged = dst.jobs_for_task("PROJ-1")[0]
        assert merged.status == "done" and [r.text for r in merged.records] == ["разбор", "готово"]
    finally:
        src.close()
        dst.close()


def test_import_rejects_non_memory_dir(tmp_path):
    store = Store(tmp_path / "s.db")
    try:
        with pytest.raises(memory.MemoryError):
            memory.import_memory(store, tmp_path)
    finally:
        store.close()


def _git_ok():
    try:
        return subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


@pytest.mark.skipif(not _git_ok(), reason="нужен git")
def test_sync_commits_into_local_repo_and_remembers_path(tmp_path):
    repo = tmp_path / "memrepo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    store = _populated(tmp_path)
    try:
        result = memory.sync_memory(store, repo)
        assert result["committed"] and not result["pulled"] and not result["pushed"]
        log = subprocess.run(["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True).stdout
        assert "jwu memory" in log
        assert store.get_meta(memory.MEMORY_REPO_META) == str(repo)
        # без изменений — коммита нет
        again = memory.sync_memory(store)
        assert not again["committed"]
        with pytest.raises(memory.MemoryError):
            memory.sync_memory(store, tmp_path / "not-a-repo")
    finally:
        store.close()


def test_cli_export_import_roundtrip(monkeypatch, tmp_path):
    src = _populated(tmp_path)
    src.close()
    monkeypatch.setattr(cli, "_open_store", lambda: Store(tmp_path / "src.db"))
    res = runner.invoke(cli.app, ["memory", "export", "--dir", str(tmp_path / "m"), "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["jobs"] == 2
    monkeypatch.setattr(cli, "_open_store", lambda: Store(tmp_path / "other.db"))
    res = runner.invoke(cli.app, ["memory", "import", str(tmp_path / "m")])
    assert res.exit_code == 0, res.output and "добавлено" in res.output
    res = runner.invoke(cli.app, ["memory", "import", str(tmp_path / "nope")])
    assert res.exit_code == 1
