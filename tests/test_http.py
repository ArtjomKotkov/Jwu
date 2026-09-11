"""HTTP с повторами (JWU-27), сигналы QA в дельтах (JWU-22), краткий дневной контекст (JWU-26)."""

import httpx
import pytest
import respx

from jwu.core import http as jhttp
from jwu.core.bitbucket import BitbucketClient, BitbucketError
from jwu.core.models import Comment, Delta, Issue, Mention, PR, Reviewer
from jwu.core.service import DashboardData, _brief_dashboard
from jwu.core.store import Store, is_testing_status


# --- повторы ------------------------------------------------------------------ #

@respx.mock
def test_get_retries_on_timeout_and_5xx():
    route = respx.get("https://x.test/a")
    route.side_effect = [httpx.ReadTimeout("slow"), httpx.Response(503), httpx.Response(200, json={"ok": 1})]
    slept = []
    client = jhttp.RetryingClient(retries=2, sleep=slept.append)
    try:
        resp = client.get("https://x.test/a")
        assert resp.status_code == 200 and route.call_count == 3
        assert slept == [0.5, 1.5]
    finally:
        client.close()


@respx.mock
def test_last_5xx_is_returned_and_exhausted_timeout_raises():
    route = respx.get("https://x.test/b").mock(return_value=httpx.Response(502))
    client = jhttp.RetryingClient(retries=1, sleep=lambda _s: None)
    try:
        assert client.get("https://x.test/b").status_code == 502 and route.call_count == 2
        bad = respx.get("https://x.test/c")
        bad.side_effect = httpx.ConnectError("down")
        with pytest.raises(httpx.ConnectError):
            client.get("https://x.test/c")
        assert bad.call_count == 2
    finally:
        client.close()


@respx.mock
def test_post_is_never_retried():
    route = respx.post("https://x.test/w")
    route.side_effect = [httpx.Response(503), httpx.Response(200)]
    client = jhttp.RetryingClient(retries=3, sleep=lambda _s: None)
    try:
        assert client.post("https://x.test/w", json={}).status_code == 503
        assert route.call_count == 1
    finally:
        client.close()


def test_env_controls_timeout_and_retries(monkeypatch):
    monkeypatch.setenv("JWU_HTTP_TIMEOUT", "7.5")
    monkeypatch.setenv("JWU_HTTP_RETRIES", "0")
    client = jhttp.new_client()
    try:
        assert client.timeout.read == 7.5
        assert client._retries == 0
    finally:
        client.close()
    monkeypatch.setenv("JWU_HTTP_RETRIES", "мусор")
    assert jhttp.default_retries() == jhttp.DEFAULT_RETRIES


@respx.mock
def test_bitbucket_client_survives_flaky_network():
    route = respx.get("https://git.test/rest/api/1.0/dashboard/pull-requests")
    route.side_effect = [httpx.ReadTimeout("t"), httpx.Response(200, json={"values": [], "isLastPage": True})]
    bb = BitbucketClient("https://git.test", "tok")
    bb._client._sleep = lambda _s: None
    try:
        assert bb.dashboard_prs("mine") == []
        assert route.call_count == 2
    finally:
        bb.close()


# --- сигналы QA ---------------------------------------------------------------- #

def test_is_testing_status():
    assert is_testing_status("READY FOR TESTING") and is_testing_status("На тестах") and is_testing_status("QA")
    assert not is_testing_status("In Review") and not is_testing_status("")


def _issue(status, comments=(), resolution=""):
    return Issue(key="PROJ-1", summary="S", status=status, resolution=resolution,
                 comments=[Comment(id=str(i), author=a, author_key=k, body="x") for i, a, k in comments])


def test_returned_from_testing_and_qa_comment(tmp_path):
    store = Store(tmp_path / "s.db")
    try:
        r1 = store.start_sync_run(["mine"]); store.save_issue_snapshot(r1, _issue("READY FOR TESTING", [(1, "Me", "me")]))
        store.compute_changes(r1, me=("me", "Me"))
        # тестировщик написал в задаче на тестах
        r2 = store.start_sync_run(["mine"])
        store.save_issue_snapshot(r2, _issue("READY FOR TESTING", [(1, "Me", "me"), (2, "Anna QA", "aqa")]))
        d2 = store.compute_changes(r2, me=("me", "Me"))
        assert [(d.kind, d.detail) for d in d2] == [("qa_comment", "+1 комм. от Anna QA")]
        # мой собственный ответ — обычный new_comment
        r3 = store.start_sync_run(["mine"])
        store.save_issue_snapshot(r3, _issue("READY FOR TESTING", [(1, "Me", "me"), (2, "Anna QA", "aqa"), (3, "Me", "me")]))
        assert [d.kind for d in store.compute_changes(r3, me=("me",))] == ["new_comment"]
        # вернули с тестов
        r4 = store.start_sync_run(["mine"])
        store.save_issue_snapshot(r4, _issue("In Progress", [(1, "Me", "me"), (2, "Anna QA", "aqa"), (3, "Me", "me")]))
        d4 = store.compute_changes(r4, me=("me",))
        assert [d.kind for d in d4] == ["returned_from_testing"] and "READY FOR TESTING → In Progress" in d4[0].detail
        # обычный переход и закрытие — не «возврат»
        r5 = store.start_sync_run(["mine"])
        store.save_issue_snapshot(r5, _issue("READY FOR TESTING", [(1, "Me", "me"), (2, "Anna QA", "aqa"), (3, "Me", "me")]))
        assert [d.kind for d in store.compute_changes(r5, me=("me",))] == ["status_change"]
        r6 = store.start_sync_run(["mine"])
        store.save_issue_snapshot(r6, _issue("Closed", [(1, "Me", "me"), (2, "Anna QA", "aqa"), (3, "Me", "me")], resolution="Done"))
        kinds = [d.kind for d in store.compute_changes(r6, me=("me",))]
        assert "returned_from_testing" not in kinds and "status_change" in kinds and "resolved" in kinds
    finally:
        store.close()


# --- краткий контекст ------------------------------------------------------------- #

def test_brief_dashboard_filters_noise():
    mine_pr = PR(id=1, project="P", repository="r", reviewers=[Reviewer(name="bob", status="APPROVED", approved=True)])
    waits = PR(id=2, project="P", repository="r", reviewers=[Reviewer(name="me", status="UNAPPROVED")])
    done = PR(id=3, project="P", repository="r", reviewers=[Reviewer(name="me", status="APPROVED", approved=True)])
    d = DashboardData(
        user="me",
        deltas=[
            Delta(key="X-1", kind="gone"), Delta(key="P/r#9", kind="pr_gone"),
            Delta(key="P/r#1", kind="reviewer_approved"),      # мой PR — оставить
            Delta(key="P/r#3", kind="reviewer_approved"),      # чужой PR — шум
            Delta(key="P/r#3", kind="new_conflict"),           # чужой, но конфликт — оставить
            Delta(key="X-2", kind="qa_comment"),
        ],
        prs_mine=[mine_pr], prs_review=[waits, done],
        mentions=[
            Mention(id=1, task_key="A", seen=False, created="2026-09-10T10:00:00+03:00"),
            Mention(id=2, task_key="B", seen=True, created="2026-09-10T10:00:00+03:00"),
            Mention(id=3, task_key="C", seen=False, created="2026-01-01T10:00:00+03:00"),
            Mention(id=4, task_key="D", seen=False, created=""),
        ],
    )
    from datetime import datetime, timezone
    import jwu.core.service as svc_mod

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 11, tzinfo=timezone.utc)

    original = svc_mod.datetime
    svc_mod.datetime = _Now
    try:
        b = _brief_dashboard(d, "me", mention_days=14)
    finally:
        svc_mod.datetime = original
    assert [(x.key, x.kind) for x in b.deltas] == [
        ("P/r#1", "reviewer_approved"), ("P/r#3", "new_conflict"), ("X-2", "qa_comment")]
    assert [p.id for p in b.prs_review] == [2]
    assert [m.id for m in b.mentions] == [1, 4]
    assert b.prs_mine == [mine_pr] and b.user == "me"
