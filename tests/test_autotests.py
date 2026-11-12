"""Автотесты PR: общая модель, сборки «не тесты», доска и хуки аддонов."""

from types import SimpleNamespace

import pytest

from jwu.core import addons, todo
from jwu.core.models import PR, Autotests, BuildStatus
from jwu.core.service import Service
from jwu.core.store import Store

H = 3600_000


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "s.db")
    st.use_workspace(st.get_workspace_by_slug("work").id)
    yield st
    st.close()


def test_summary_texts():
    now = 100 * H
    assert Autotests(state="queued", queued_since=now - 10 * H - 18 * 60_000, typical_ms=2 * H + 52 * 60_000,
                     server_time=now).summary() == "тесты в очереди 10 ч 18 мин · обычно ~2 ч 52 мин"
    assert Autotests(state="not_started").summary() == "тесты не запускали"
    assert Autotests(state="running", build_number=7, remaining_ms=600_000).summary() == "тесты идут #7, осталось ~10 мин"
    assert Autotests(state="failed", finished_at=now - H, server_time=now).summary() == "тесты упали 1 ч назад"
    assert Autotests(state="queued").pending and not Autotests(state="failed").pending


def test_not_tests_builds_do_not_color_pr(store):
    store.set_workspace_settings(store.workspace_id, {Service.NOT_TESTS_SETTING: "*ai-review*"})
    svc = Service.__new__(Service)
    svc.store = store
    pr = PR(id=1, builds=[BuildStatus(state="FAILED", key="x-ai-review"), BuildStatus(state="SUCCESSFUL", key="tests")])
    Service._mark_build_kinds(svc, pr)
    assert [b.kind for b in pr.builds] == ["other", "tests"]
    assert pr.build_state == "SUCCESSFUL"
    only_other = PR(id=2, builds=[BuildStatus(state="FAILED", key="x-ai-review", kind="other")])
    assert only_other.build_state == ""


def _addon(monkeypatch, **hooks):
    fake = addons.Addon(name="fake", module=SimpleNamespace(**hooks))
    monkeypatch.setattr(addons, "active", lambda: [fake])
    return fake


def test_autotests_from_addon_and_run(monkeypatch):
    seen = {}
    _addon(monkeypatch, pr_autotests=lambda cfg, pr: {"state": "queued", "queued_since": 1},
           pr_autotests_run=lambda cfg, pr, opts: seen.update(opts) or {"message": "ок"})
    svc = Service.__new__(Service)
    svc.cfg = None
    got = Service._autotests(svc, PR(id=5))
    assert got.state == "queued" and got.source == "fake"
    svc.pr_client = SimpleNamespace(pr=lambda p, r, i: PR(id=i, project=p, repository=r))
    svc.default_pr_ref = lambda: ("P", "r")
    res = Service.run_autotests(svc, None, None, 5, {"maxfail": 3})
    assert res["ok"] and res["source"] == "fake" and seen == {"maxfail": 3}


def test_run_without_capable_addon_or_with_error(monkeypatch):
    svc = Service.__new__(Service)
    svc.cfg = None
    svc.pr_client = SimpleNamespace(pr=lambda p, r, i: PR(id=i))
    svc.default_pr_ref = lambda: ("P", "r")
    _addon(monkeypatch)
    with pytest.raises(ValueError, match="некому"):
        Service.run_autotests(svc, None, None, 1)

    def boom(cfg, pr, opts):
        raise RuntimeError("плагин ответил 403")

    _addon(monkeypatch, pr_autotests_run=boom)
    with pytest.raises(ValueError, match="403"):
        Service.run_autotests(svc, None, None, 1)


def test_board_waits_for_queued_tests_and_flags_failed(store):
    store.set_workspace_settings(store.workspace_id, {todo.BUILD_REQUIRED_SETTING: "*"})
    base = dict(project="P", repository="app", state="OPEN", title="t", source_branch="b")
    queued = PR(id=1, **base, autotests=Autotests(state="queued", queued_since=1, server_time=2 * H))
    idle = PR(id=2, **base, autotests=Autotests(state="not_started"))
    red = PR(id=3, **base, builds=[BuildStatus(state="SUCCESSFUL", key="t")], autotests=Autotests(state="failed"))
    run = store.start_sync_run(["prs:mine"])
    for pr in (queued, idle, red):
        store.save_pr_snapshot(run, pr, ["mine"])
    store.finish_sync_run(run, {"prs:mine": 3})
    board = todo.build(store, store.workspace_id, "me", remember=False)
    by_id = {it.pr["id"]: it for it in board.items if it.pr}
    assert 1 not in by_id                                    # в очереди — ждём, не «запустить»
    assert by_id[2].group == "build" and "тесты не запускали" in by_id[2].problems
    assert todo.TESTS_RED in by_id[3].problems
    assert "тесты в очереди" in todo.build_cell(queued, True)
