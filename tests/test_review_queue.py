"""Очередь ревью: отбор PR, ревьюер по репозиторию, каталог сводки (JWU-48)."""

import asyncio
import json
from datetime import date

import pytest
from typer.testing import CliRunner

from jwu import mcp_server as srv
from jwu.cli import main as cli
from jwu.core import reviewq
from jwu.core.models import PR, Reviewer
from jwu.core.store import Store
from jwu.skills_install import EXPECTED_AGENTS, EXPECTED_SKILLS

runner = CliRunner()


def _pr(pid, repo="chat", status="UNAPPROVED", **kw):
    return PR(id=pid, project="P", repository=repo, title=kw.pop("title", f"PR {pid}"),
              source_branch=kw.pop("source", f"feature/PROJ-{pid}-x"), target_branch="develop",
              reviewers=[Reviewer(name="me", display_name="Я", status=status, approved=status == "APPROVED"),
                         Reviewer(name="other", status="APPROVED", approved=True)], **kw)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.db")
    yield s
    s.close()


def test_my_status():
    assert reviewq.my_status(_pr(1, status="APPROVED"), "me") == "APPROVED"
    assert reviewq.my_status(_pr(1, status="NEEDS_WORK"), "Я") == "NEEDS_WORK"
    assert reviewq.my_status(_pr(1), "stranger") == ""


def test_select_skips_my_approved_and_filters():
    prs = [_pr(1), _pr(2, status="APPROVED"), _pr(3, repo="docs"), _pr(4, status="NEEDS_WORK")]
    queue, skipped = reviewq.select(prs, "me")
    assert [p.id for p in queue] == [1, 3, 4]
    assert skipped == [{"pr": 2, "repo": "chat", "title": "PR 2", "reason": "мой статус уже APPROVED"}]
    queue, _ = reviewq.select(prs, "me", include_approved=True)
    assert [p.id for p in queue] == [1, 2, 3, 4]
    queue, skipped = reviewq.select(prs, "me", repos=["DOCS"])
    assert [p.id for p in queue] == [3] and {s["reason"] for s in skipped} == {"не в списке репозиториев"}
    queue, _ = reviewq.select(prs, "me", ids=[4, 2])
    assert [p.id for p in queue] == [4]  # #2 — в списке, но уже APPROVED


def test_agents_settings(store):
    wid = store.get_workspace_by_slug("work").id
    assert reviewq.reviewer_for(store, wid, "chat") == "reviewer-jwu-sample"
    info = reviewq.set_agents(store, wid, repo="chat", reviewer="reviewer-chat", filter_name="my-filter")
    assert info == {"filter_agent": "my-filter", "default_reviewer": "reviewer-jwu-sample",
                    "reviewers": {"chat": "reviewer-chat"}}
    reviewq.set_agents(store, wid, repo="chat", reviewer="-", filter_name="-")
    assert reviewq.agents(store, wid)["reviewers"] == {}
    assert reviewq.filter_agent(store, wid) == "review-filter-sample"


def test_plan_paths_and_fields(store):
    wid = store.get_workspace_by_slug("work").id
    reviewq.set_agents(store, wid, repo="chat", reviewer="reviewer-chat")
    plan = reviewq.plan(store, wid, "work", [_pr(7), _pr(8, repo="docs", source="docs-update",
                                                           title="PROJ-9: правка доки")], "me")
    day = date.today().isoformat()
    assert plan["dir"].endswith(f"/reviews/work/{day}") and plan["summary"].endswith("summary.md")
    first, second = plan["queue"]
    assert first["reviewer_agent"] == "reviewer-chat" and first["task_key"] == "PROJ-7"
    assert first["review_file"].endswith("pr-chat-7.review.md") and first["facts_file"].endswith("pr-chat-7.facts.md")
    assert second["reviewer_agent"] == "reviewer-jwu-sample" and second["task_key"] == "PROJ-9"
    assert plan["filter_agent"] == "review-filter-sample"


def test_shipped():
    assert "jwu-review-queue" in EXPECTED_SKILLS and "review-filter-sample" in EXPECTED_AGENTS


def test_mcp_review_queue(tmp_path, monkeypatch):
    class Svc:
        def __init__(self, store):
            self.store = store

        def prs(self, view, **kw):
            assert view == "review" and kw == {"with_conflicts": False, "with_builds": False}
            return [_pr(1), _pr(2, status="APPROVED")]

        def _resolve_username(self):
            return "me"

    db = tmp_path / "state.db"
    monkeypatch.setenv("JWU_DB_PATH", str(db))
    monkeypatch.setattr(srv, "_base_store", None)
    monkeypatch.setattr(srv, "_stores", {})
    Store(db).close()
    monkeypatch.setattr(srv, "_full_svc", lambda workspace=None: Svc(srv._store_only(workspace)))
    try:
        out = asyncio.run(srv.jwu_review_queue(workspace="work"))
        assert [q["pr"] for q in out["queue"]] == [1] and out["skipped"][0]["pr"] == 2
        from pathlib import Path
        assert Path(out["dir"]).is_dir() and out["workspace"] == "work"
    finally:
        for s in list(srv._stores.values()):
            s.close()
        if srv._base_store is not None:
            srv._base_store.close()


def test_cli_review_agents():
    r = runner.invoke(cli.app, ["-W", "work", "review", "agents", "--repo", "chat", "--reviewer", "rv-chat", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output[r.output.index("{"):])["reviewers"] == {"chat": "rv-chat"}
    r = runner.invoke(cli.app, ["-W", "work", "review", "agents", "--reviewer", "x"])
    assert r.exit_code == 1 and "--repo" in r.output
