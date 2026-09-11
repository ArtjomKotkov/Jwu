"""Контекст сущностей (JWU-33): типизированные заметки, status, автозаметка при job done."""

import asyncio
import json

import pytest
from typer.testing import CliRunner

from jwu import mcp_server as srv
from jwu.cli import main as cli
from jwu.core import handoff
from jwu.core.models import Job, JobPRLink, JobRecord, PR, pr_note_key
from jwu.core.service import DayContext
from jwu.core.store import Store

runner = CliRunner()


def _scoped(db):
    store = Store(db)
    store.use_workspace(store.get_workspace_by_slug("work").id)
    return store


def test_notes_kinds_status_pinning_and_bulk(tmp_path):
    store = _scoped(tmp_path / "s.db")
    try:
        assert store.get_meta("schema_version") == "9"
        a = store.add_note("PROJ-1", "решили порт в 10.7", kind="decision")
        s1 = store.add_note("PROJ-1", "ждём ответа Eugeny", kind="status")
        s2 = store.add_note("PROJ-1", "  ждём  QA  ", kind="status")
        store.add_note("PROJ/repo#42", "грабли с таймаутом", kind="gotcha", pinned=True)
        with pytest.raises(ValueError):
            store.add_note("PROJ-1", "x", kind="weird")
        with pytest.raises(ValueError):
            store.add_note("PROJ-1", "   ")
        notes = store.get_notes("PROJ-1")
        assert [(n.kind, n.pinned) for n in notes] == [("decision", False), ("status", False), ("status", True)]
        assert notes[-1].text == "ждём QA" and s2.pinned and s2.id > s1.id > a.id
        assert store.status_notes(["PROJ-1", "PROJ/repo#42", "NOPE-1"]) == {"PROJ-1": "ждём QA", "PROJ/repo#42": ""}
        both = store.notes_for_keys(["PROJ-1", "PROJ/repo#42"])
        assert len(both["PROJ-1"]) == 3 and both["PROJ/repo#42"][0].pinned
        assert store.delete_note(a.id) and not store.delete_note(a.id)
        assert pr_note_key("PROJ", "repo", 42) == "PROJ/repo#42" and pr_note_key("", "", 7) == "#7"
    finally:
        store.close()


def test_cli_note_notes_and_rm(monkeypatch, tmp_path):
    db = tmp_path / "s.db"
    monkeypatch.setattr(cli, "_store", lambda: _scoped(db))
    res = runner.invoke(cli.app, ["note", "PROJ/repo#42", "ждём ревью", "--kind", "status", "--json"])
    assert res.exit_code == 0, res.output
    nid = json.loads(res.stdout)["id"]
    res = runner.invoke(cli.app, ["notes", "PROJ/repo#42"])
    assert "СТАТУС" in res.output and "ждём ревью" in res.output
    assert runner.invoke(cli.app, ["note", "PROJ-1", "x", "--kind", "bogus"]).exit_code != 0
    res = runner.invoke(cli.app, ["notes", "PROJ/repo#42", "--rm", str(nid)])
    assert "Удалено" in res.output
    assert json.loads(runner.invoke(cli.app, ["notes", "PROJ/repo#42", "--json"]).stdout) == []


def test_job_done_writes_context_note_to_task_and_pr(monkeypatch, tmp_path):
    db = tmp_path / "s.db"
    monkeypatch.setattr(cli, "_store", lambda: _scoped(db))
    res = runner.invoke(cli.app, ["job", "start", "PROJ-1", "--title", "фикс", "--json"])
    job_id = json.loads(res.stdout)["id"]
    runner.invoke(cli.app, ["job", "add", str(job_id), "разбор", "--kind", "phase", "--status", "done", "--no-git"])
    runner.invoke(cli.app, ["job", "add", str(job_id), "тесты", "--kind", "phase", "--no-git"])
    runner.invoke(cli.app, ["job", "link", str(job_id), "--pr", "42", "--project", "PROJ", "--repo", "repo"])
    res = runner.invoke(cli.app, ["job", "done", str(job_id)])
    assert res.exit_code == 0 and "PROJ-1, PROJ/repo#42" in res.output
    store = _scoped(db)
    try:
        for key in ("PROJ-1", "PROJ/repo#42"):
            notes = store.get_notes(key)
            assert len(notes) == 1 and notes[0].kind == "context" and notes[0].author == "jwu"
            assert f"Работа #{job_id} (done): фикс" in notes[0].text
            assert "сделано: разбор" in notes[0].text and "осталось: тесты" in notes[0].text
    finally:
        store.close()
    # --no-note — без заметки
    res = runner.invoke(cli.app, ["job", "start", "PROJ-2", "--title", "тихо", "--json"])
    jid = json.loads(res.stdout)["id"]
    runner.invoke(cli.app, ["job", "done", str(jid), "--no-note"])
    store = _scoped(db)
    try:
        assert store.get_notes("PROJ-2") == []
    finally:
        store.close()


def test_handoff_includes_notes_and_summary_note(tmp_path):
    store = _scoped(tmp_path / "s.db")
    try:
        store.add_note("PROJ-1", "порт в 10.7", kind="decision")
        store.add_note("PROJ/repo#42", "ждём ревью", kind="status")
        job = Job(id=5, task_key="PROJ-1", title="t", status="active",
                  prs=[JobPRLink(pr_id=42, project="PROJ", repo="repo")],
                  records=[JobRecord(kind="phase", text="а", status="done", branch="b", commit="c")])
        data = handoff.collect(store, job, offline=True)
        text = handoff.render(data)
        assert "## Заметки-контекст" in text and "🧭 [PROJ-1] порт в 10.7" in text and "📍 [PROJ/repo#42] ждём ревью" in text
        summary = handoff.summary_note(data)
        assert summary.startswith("Работа #5 (active): t · git: b@c · сделано: а")
        keys = handoff.save_context_notes(store, data)
        assert keys == ["PROJ-1", "PROJ/repo#42"]
        assert any(n.text == summary for n in store.get_notes("PROJ/repo#42"))
    finally:
        store.close()


def test_day_context_shows_status_notes():
    pr = PR(id=42, project="PROJ", repository="repo", title="T")
    ctx = DayContext(prs_mine=[pr], status_notes={"PROJ/repo#42": "ждём Eugeny"})
    text = cli._render_day_context_md(ctx)
    assert 'заметка: «ждём Eugeny»' in text


def test_mcp_context_and_note(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.setenv("JWU_DB_PATH", str(db))
    monkeypatch.setattr(srv, "_base_store", None)
    monkeypatch.setattr(srv, "_stores", {})
    store = _scoped(db)
    job = store.create_job("PROJ-1", "фикс")
    store.add_job_record(job.id, "начал", kind="phase", branch="PROJ-1-fix", commit="abc")
    store.link_job_pr(job.id, 42, "PROJ", "repo")
    run = store.start_sync_run(["prs:mine"])
    store.save_pr_snapshot(run, PR(id=42, project="PROJ", repository="repo", title="PR", tasks_open=2), ["mine"])
    store.close()
    try:
        asyncio.run(srv.jwu_note("PROJ/repo#42", "ждём ревью", kind="status", workspace="work"))
        asyncio.run(srv.jwu_note("PROJ-1", "решение", kind="decision", workspace="work"))
        ctx = asyncio.run(srv.jwu_context("PROJ/repo#42", workspace="work"))
        assert ctx["status"] == "ждём ревью" and ctx["pr"]["tasks_open"] == 2
        assert ctx["last_job"]["id"] == job.id and ctx["last_job"]["branch"] == "PROJ-1-fix"
        tctx = asyncio.run(srv.jwu_context("PROJ-1", workspace="work"))
        assert tctx["status"] == "" and [n["kind"] for n in tctx["notes"]] == ["decision"]
        assert tctx["last_job"]["prs"][0]["pr_id"] == 42
        # закрытие через MCP тоже пишет контекст
        done = asyncio.run(srv.jwu_job_status(job.id, "done", workspace="work"))
        assert done["context_notes"] == ["PROJ-1", "PROJ/repo#42"]
        assert len(asyncio.run(srv.jwu_notes("PROJ-1", workspace="work"))) == 2
    finally:
        for s in list(srv._stores.values()):
            s.close()
        if srv._base_store is not None:
            srv._base_store.close()
