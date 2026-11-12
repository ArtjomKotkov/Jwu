"""Метрики инструментов: запись без аргументов, отчёт, экономия jwu_query, упущенные цепочки."""

from datetime import datetime, timedelta, timezone

import pytest

from jwu.core import toolstats
from jwu.core.store import Store


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("JWU_TOOLSTATS", "1")
    monkeypatch.setattr(toolstats, "start_flusher", lambda open_store: None)   # без фонового сброса
    yield
    toolstats._buffer.clear()


@pytest.fixture
def store(tmp_path):
    toolstats._buffer.clear()
    st = Store(tmp_path / "s.db")
    yield st
    st.close()


def _rec(tool, session="S", chars=100, ok=True, **kw):
    toolstats.record(tool, session=session, workspace="work", ms=10, chars=chars, ok=ok, **kw)


def test_report_top_query_savings_and_sessions(store):
    _rec("jwu_task", chars=3000)
    _rec("jwu_pr", chars=1000, ok=False)
    _rec("jwu_query", chars=500, inner_calls=4, raw_chars=20000)
    assert toolstats.flush(store) == 3 and toolstats.flush(store) == 0
    rep = toolstats.report(store, days=1)
    assert rep["calls"] == 3 and rep["tools"][0]["tool"] == "jwu_task"
    assert rep["query"] == {"calls": 1, "errors": 0, "inner_calls": 4, "raw_chars": 20000, "chars": 500,
                            "saved_chars": 19500}
    assert next(t for t in rep["tools"] if t["tool"] == "jwu_pr")["errors"] == 1
    assert rep["sessions"][0]["query"] == 1
    text = toolstats.render(rep)
    assert "jwu_query:" in text and "сэкономлено" in text


def test_missed_chains(store):
    for tool in ("jwu_prs", "jwu_builds", "jwu_builds", "jwu_builds"):
        _rec(tool)
    _rec("jwu_job_add")                      # запись рвёт цепочку
    _rec("jwu_task")
    _rec("jwu_query")
    for tool in ("jwu_task", "jwu_pr"):      # две — ещё не цепочка
        _rec(tool, session="T")
    toolstats.flush(store)
    rep = toolstats.report(store, days=1)
    assert rep["missed_chains"] == [{"tools": "jwu_prs → jwu_builds", "times": 1}]
    assert rep["missed_examples"][0]["calls"] == 4


def test_prune_and_workspace_filter(store):
    _rec("jwu_task")
    toolstats.flush(store)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
    store.conn.execute("UPDATE tool_usage SET ts = ?", (old,))
    assert toolstats.prune(store) == 1
    _rec("jwu_task")
    toolstats.flush(store)
    assert toolstats.report(store, workspace="home")["calls"] == 0
    assert toolstats.report(store, workspace="work")["calls"] == 1


def test_wrapper_records_without_arguments(tmp_path):
    import asyncio

    from jwu import mcp_server as srv

    toolstats._buffer.clear()
    asyncio.run(srv.jwu_jobs(task="SECRET-42", workspace="work"))
    row = toolstats._buffer[-1]
    assert row[3] == "jwu_jobs" and row[2] == "work"
    assert "SECRET-42" not in repr(row)


def test_render_empty(store):
    assert "Записей нет" in toolstats.render(toolstats.report(store))


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("JWU_TOOLSTATS", "0")
    toolstats._buffer.clear()
    _rec("jwu_task")
    assert toolstats._buffer == []


def test_clear(store):
    _rec("jwu_task")
    toolstats.flush(store)
    assert toolstats.clear(store) == 1 and toolstats.report(store)["calls"] == 0
