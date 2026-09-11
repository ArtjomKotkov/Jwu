"""Пороги «давно» и раздел «Застряло» (JWU-29), упоминания из CLI/MCP (JWU-28)."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from jwu import mcp_server as srv
from jwu.cli import main as cli
from jwu.core import thresholds as th_mod
from jwu.core.dates import age_days
from jwu.core.models import Issue, Mention, PR, Reviewer
from jwu.core.service import DayContext
from jwu.core.store import Store

runner = CliRunner()


def _ms(days_ago: float) -> int:
    return int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp() * 1000)


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_age_days():
    assert age_days(_ms(3.4)) == 3 and age_days(_iso(0.2)) == 0 and age_days("") is None


def test_thresholds_load_save_reset(tmp_path):
    store = Store(tmp_path / "s.db")
    try:
        wid = store.get_workspace_by_slug("work").id
        assert th_mod.load(store, wid) == th_mod.Thresholds()
        th = th_mod.save(store, wid, stale_pr_days=30, mention_days=None, testing_days=-2)
        assert th.stale_pr_days == 30 and th.testing_days == 0 and th.mention_days == 14
        store.set_workspace_settings(wid, {"thresholds.review_wait_days": "мусор"})
        assert th_mod.load(store, wid).review_wait_days == th_mod.Thresholds().review_wait_days
        assert th_mod.reset(store, wid).stale_pr_days == 14
    finally:
        store.close()


def test_collect_stuck_names_reasons():
    th = th_mod.Thresholds(stale_pr_days=14, approval_wait_days=7, review_wait_days=3, testing_days=5)
    approved = [Reviewer(name="bob", approved=True, status="APPROVED")]
    waiting = [Reviewer(name="bob", status="UNAPPROVED")]
    prs_mine = [
        PR(id=1, project="P", repository="r", title="старый", updated=_ms(20), reviewers=approved),
        PR(id=2, project="P", repository="r", title="ждёт", updated=_ms(9), reviewers=waiting),
        PR(id=3, project="P", repository="r", title="свежий", updated=_ms(1), reviewers=waiting),
        PR(id=4, project="P", repository="r", title="needs work", updated=_ms(9),
           reviewers=[Reviewer(name="bob", status="NEEDS_WORK")]),
    ]
    prs_review = [
        PR(id=5, project="P", repository="r", title="ждёт меня", updated=_ms(4), reviewers=[Reviewer(name="me", status="UNAPPROVED")]),
        PR(id=6, project="P", repository="r", title="я апрувнул", updated=_ms(40), reviewers=[Reviewer(name="me", approved=True, status="APPROVED")]),
    ]
    mine = [
        Issue(key="T-1", summary="на тестах", status="READY FOR TESTING", updated=_iso(8)),
        Issue(key="T-2", summary="в работе", status="In Progress", updated=_iso(8)),
        Issue(key="T-3", summary="только на тесты", status="Testing", updated=_iso(1)),
    ]
    stuck = th_mod.collect_stuck(prs_mine=prs_mine, prs_review=prs_review, mine=mine, th=th, login="me")
    by_key = {x["key"]: x for x in stuck}
    assert set(by_key) == {"P/r#1", "P/r#2", "P/r#5", "T-1"}
    assert "апрувы собраны" in by_key["P/r#1"]["reason"] and by_key["P/r#1"]["days"] == 20
    assert "пнуть ревьюверов" in by_key["P/r#2"]["reason"]
    assert "моего ревью" in by_key["P/r#5"]["reason"]
    assert "пнуть тестирование" in by_key["T-1"]["reason"]
    assert [x["days"] for x in stuck] == sorted((x["days"] for x in stuck), reverse=True)


def test_day_context_renders_stuck_section():
    ctx = DayContext(user="me", thresholds=th_mod.Thresholds(stale_pr_days=10).as_dict(),
                     stuck=[{"kind": "pr", "key": "P/r#1", "title": "T", "days": 12, "reason": "без движения 12 дн"}])
    text = cli._render_day_context_md(ctx)
    assert "## Застряло (1)" in text and "PR без движения 10 дн" in text
    assert "- ⏳ P/r#1 — 12 дн: без движения 12 дн — T" in text
    assert "«Застряло»" in cli._DAY_PROMPT


def test_cli_workspace_thresholds(monkeypatch, tmp_path):
    db = tmp_path / "s.db"
    monkeypatch.setattr(cli, "_store", lambda: _scoped(db))
    res = runner.invoke(cli.app, ["workspace", "thresholds", "--stale-pr", "21", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["stale_pr_days"] == 21
    res = runner.invoke(cli.app, ["workspace", "thresholds"])
    assert "21 дн" in res.output
    res = runner.invoke(cli.app, ["workspace", "thresholds", "--reset", "--json"])
    assert json.loads(res.stdout)["stale_pr_days"] == 14


def _scoped(db):
    store = Store(db)
    store.use_workspace(store.get_workspace_by_slug("work").id)
    return store


def _seed_mentions(store):
    return store.add_mentions([
        Mention(task_key="A-1", comment_id="1", author="Ann", text="[~me] глянь", created=_iso(1), summary="s1"),
        Mention(task_key="A-2", comment_id="2", author="Bob", text="[~me] старое", created=_iso(60), summary="s2"),
        Mention(task_key="A-3", comment_id="3", author="Cid", text="[~me] старое непрочитанное", created=_iso(90), summary="s3"),
    ])


def test_archive_mentions_respects_seen(tmp_path):
    store = _scoped(tmp_path / "s.db")
    try:
        added = _seed_mentions(store)
        store.mark_mentions_seen([added[1].id])            # только A-2 прочитано
        assert store.archive_mentions(older_than_days=30) == 1  # A-2 удалено, A-3 (непрочитанное) осталось
        assert {m.task_key for m in store.list_mentions()} == {"A-1", "A-3"}
        assert store.archive_mentions(older_than_days=30, seen_only=False) == 1
        assert [m.task_key for m in store.list_mentions()] == ["A-1"]
    finally:
        store.close()


def test_cli_mentions_list_read_archive(monkeypatch, tmp_path):
    db = tmp_path / "s.db"
    store = _scoped(db)
    _seed_mentions(store)
    store.close()
    monkeypatch.setattr(cli, "_store", lambda: _scoped(db))
    res = runner.invoke(cli.app, ["mentions", "list", "--json"])
    assert res.exit_code == 0 and [m["task_key"] for m in json.loads(res.stdout)] == ["A-1", "A-2", "A-3"]
    res = runner.invoke(cli.app, ["mentions", "read"])
    assert res.exit_code == 1
    ids = [m["id"] for m in json.loads(runner.invoke(cli.app, ["mentions", "list", "--json"]).stdout)]
    assert runner.invoke(cli.app, ["mentions", "read", str(ids[0])]).exit_code == 0
    res = runner.invoke(cli.app, ["mentions", "list", "--unseen", "--json"])
    assert [m["task_key"] for m in json.loads(res.stdout)] == ["A-2", "A-3"]
    assert runner.invoke(cli.app, ["mentions", "read", "--all"]).exit_code == 0
    res = runner.invoke(cli.app, ["mentions", "archive", "--older-than", "30"])
    assert "Удалено упоминаний: 2" in res.output


def test_mcp_mentions_and_thresholds(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.setenv("JWU_DB_PATH", str(db))
    monkeypatch.setattr(srv, "_base_store", None)
    monkeypatch.setattr(srv, "_stores", {})
    store = _scoped(db)
    _seed_mentions(store)
    store.set_workspace_settings(store.workspace_id, {"thresholds.testing_days": "9"})
    store.close()
    try:
        items = asyncio.run(srv.jwu_mentions(workspace="work"))
        assert len(items) == 3 and not items[0]["seen"]
        asyncio.run(srv.jwu_mentions_seen(ids=[items[0]["id"]], workspace="work"))
        assert len(asyncio.run(srv.jwu_mentions(unseen=True, workspace="work"))) == 2
        assert asyncio.run(srv.jwu_mentions_archive(older_than_days=30, include_unseen=True, workspace="work"))["removed"] == 2
        assert asyncio.run(srv.jwu_thresholds(workspace="work"))["testing_days"] == 9
    finally:
        for s in list(srv._stores.values()):
            s.close()
        if srv._base_store is not None:
            srv._base_store.close()
