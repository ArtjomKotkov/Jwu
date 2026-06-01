import httpx
import respx

from jwu.core.bitbucket import BitbucketClient
from jwu.core.config import Config
from jwu.core.jira import JiraClient
from jwu.core.service import Service
from jwu.core.store import Store

from .fixtures import (
    bitbucket_commits_raw,
    bitbucket_dashboard_raw,
    bitbucket_merge_raw,
    bitbucket_pr_raw,
    dev_status_pr_raw,
    jira_issue_raw,
    jira_search_raw,
)

JIRA = "https://jira.test"
BB = "https://git.test"


def _service(tmp_path):
    cfg = Config()
    cfg.jira.base_url = JIRA
    cfg.bitbucket.base_url = BB
    return Service(
        cfg,
        JiraClient(JIRA, "tok"),
        BitbucketClient(BB, "tok"),
        Store(tmp_path / "state.db"),
    )


@respx.mock
def test_download_attachments_filters_kinds_and_writes(tmp_path):
    atts = [
        {"id": 1, "filename": "bug.png", "mime": "image/png", "size": 3},
        {"id": 2, "filename": "app.log", "size": 4},
        {"id": 3, "filename": "demo.mp4", "mime": "video/mp4"},  # видео — не качаем
    ]
    respx.get(f"{JIRA}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(200, json=jira_issue_raw(attachments=atts))
    )
    respx.get(f"{JIRA}/secure/attachment/1/bug.png").mock(
        return_value=httpx.Response(200, content=b"img"))
    respx.get(f"{JIRA}/secure/attachment/2/app.log").mock(
        return_value=httpx.Response(200, content=b"logs"))

    svc = _service(tmp_path)
    dest = tmp_path / "dl"
    got = svc.download_attachments("PROJ-1", kinds=["image", "log"], dest=dest)
    svc.close()

    assert sorted(p.name for _, p in got) == ["app.log", "bug.png"]  # mp4 отфильтрован
    assert (dest / "bug.png").read_bytes() == b"img"
    assert (dest / "app.log").read_bytes() == b"logs"


@respx.mock
def test_download_attachments_default_dir_under_tmp(tmp_path):
    respx.get(f"{JIRA}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(200, json=jira_issue_raw())  # без вложений
    )
    svc = _service(tmp_path)
    assert svc.attachments_dir("PROJ-1").name == "PROJ-1"
    assert svc.download_attachments("PROJ-1") == []  # нечего качать
    svc.close()


@respx.mock
def test_sync_detects_new_comment_across_runs(tmp_path):
    respx.get(f"{JIRA}/rest/api/2/search").mock(
        return_value=httpx.Response(200, json=jira_search_raw([jira_issue_raw()]))
    )
    issue_route = respx.get(f"{JIRA}/rest/api/2/issue/PROJ-1")
    issue_route.side_effect = [
        httpx.Response(200, json=jira_issue_raw(comments=[{"id": 1, "body": "первый"}])),
        httpx.Response(200, json=jira_issue_raw(comments=[{"id": 1, "body": "первый"}, {"id": 2, "body": "новый"}])),
    ]
    respx.get(f"{JIRA}/rest/dev-status/1.0/issue/detail").mock(
        return_value=httpx.Response(200, json={"detail": []})
    )
    respx.get(f"{BB}/rest/api/1.0/dashboard/pull-requests").mock(
        return_value=httpx.Response(200, json=bitbucket_dashboard_raw([]))
    )

    svc = _service(tmp_path)
    try:
        r1 = svc.sync_section("mine")
        assert any(d.kind == "new_issue" for d in r1.deltas)
        assert r1.counts["tasks:mine"] == 1

        r2 = svc.sync_section("mine")
        assert any(d.kind == "new_comment" for d in r2.deltas)
        assert not any(d.kind == "new_issue" for d in r2.deltas)
        # посекционный синк не теряет вкладку mine в памяти
        assert [i.key for i in svc.store.latest_issues("mine")] == ["PROJ-1"]
    finally:
        svc.close()


@respx.mock
def test_full_sync_idempotent_no_phantom_new_pr(tmp_path):
    """Задача из mine, чей PR ссылается на неё веткой, не должна на КАЖДОМ синке
    давать ложный new_pr. Раньше _snapshot_pr_tasks досохранял обеднённый дубль
    (with_dev=False, pr_ids=[]) того же ключа в том же прогоне, и сравнение в
    compute_changes сравнивало богатый снапшот с пустым → new_pr заново всякий раз.
    """
    # mine/mentions отдают одну и ту же задачу PROJ-1
    respx.get(f"{JIRA}/rest/api/2/search").mock(
        return_value=httpx.Response(200, json=jira_search_raw([jira_issue_raw()]))
    )
    respx.get(f"{JIRA}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(200, json=jira_issue_raw())
    )
    # dev-панель: у задачи есть PR #42
    respx.get(f"{JIRA}/rest/dev-status/1.0/issue/detail").mock(
        return_value=httpx.Response(200, json=dev_status_pr_raw())
    )
    # PR, чья ветка ссылается на PROJ-1 (триггерит _snapshot_pr_tasks по ключу из ветки)
    pr = bitbucket_pr_raw(pr_id=42)
    pr["fromRef"]["displayId"] = "PROJ-1-fix"
    respx.get(f"{BB}/rest/api/1.0/dashboard/pull-requests").mock(
        return_value=httpx.Response(200, json=bitbucket_dashboard_raw([pr]))
    )
    respx.get(
        f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42/merge"
    ).mock(return_value=httpx.Response(200, json=bitbucket_merge_raw()))
    respx.get(
        f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42/commits"
    ).mock(return_value=httpx.Response(200, json=bitbucket_commits_raw()))

    svc = _service(tmp_path)
    try:
        svc.sync()  # первый синк — здесь new_issue/new_pr ожидаемы
        # ровно один снапшот PROJ-1 в прогоне — без обеднённого pr_link-дубля
        run1 = svc.store.latest_run_id()
        n = svc.store.conn.execute(
            "SELECT COUNT(*) c FROM issue_snapshots WHERE sync_run_id=? AND key='PROJ-1'",
            (run1,),
        ).fetchone()["c"]
        assert n == 1

        r2 = svc.sync()  # второй синк без реальных изменений — должен быть тихим
        assert not any(d.kind == "new_pr" for d in r2.deltas), \
            [(d.kind, d.key) for d in r2.deltas]
        assert r2.deltas == []
    finally:
        svc.close()


@respx.mock
def test_auth_check_reports_both(tmp_path):
    respx.get(f"{JIRA}/rest/api/2/myself").mock(
        return_value=httpx.Response(200, json={"name": "alice", "displayName": "Alice"})
    )
    respx.get(f"{BB}/rest/api/1.0/dashboard/pull-requests").mock(
        return_value=httpx.Response(200, json=bitbucket_dashboard_raw([]))
    )
    svc = _service(tmp_path)
    try:
        result = svc.auth_check()
    finally:
        svc.close()
    assert result["jira"]["ok"] is True
    assert result["jira"]["user"] == "alice"
    assert result["bitbucket"]["ok"] is True


def _stub_creds(monkeypatch, token="tok"):
    """Детерминированные креды без обращения к keychain."""
    import jwu.core.service as svc_mod

    monkeypatch.setattr(svc_mod, "jira_token", lambda cfg: token)
    monkeypatch.setattr(svc_mod, "jira_login", lambda cfg: None)
    monkeypatch.setattr(svc_mod, "jira_proxy_basic", lambda cfg: None)


@respx.mock
def test_identity_cached_across_restart_without_refetch(tmp_path, monkeypatch):
    _stub_creds(monkeypatch)
    route = respx.get(f"{JIRA}/rest/api/2/myself").mock(
        return_value=httpx.Response(200, json={
            "name": "alice", "displayName": "Alice", "emailAddress": "a@example.com"})
    )

    svc = _service(tmp_path)
    try:
        d = svc.dashboard()
    finally:
        svc.close()
    assert (d.user, d.display_name, d.email) == ("alice", "Alice", "a@example.com")
    assert route.call_count == 1

    # «перезапуск»: новый сервис на той же БД, креды те же → сеть не трогаем
    svc2 = _service(tmp_path)
    try:
        d2 = svc2.dashboard()
    finally:
        svc2.close()
    assert (d2.user, d2.display_name, d2.email) == ("alice", "Alice", "a@example.com")
    assert route.call_count == 1  # /myself повторно не дёрнули


@respx.mock
def test_identity_refetched_when_creds_change(tmp_path, monkeypatch):
    _stub_creds(monkeypatch, token="tok-A")
    route = respx.get(f"{JIRA}/rest/api/2/myself").mock(
        return_value=httpx.Response(200, json={"name": "alice", "displayName": "Alice"})
    )
    svc = _service(tmp_path)
    try:
        svc.dashboard()
    finally:
        svc.close()
    assert route.call_count == 1

    _stub_creds(monkeypatch, token="tok-B")  # креды сменились → другой отпечаток
    svc2 = _service(tmp_path)
    try:
        svc2.dashboard()
    finally:
        svc2.close()
    assert route.call_count == 2


def test_dashboard_from_memory_reads_cached_identity(tmp_path):
    import json

    from jwu.core.service import _IDENTITY_META, dashboard_from_memory

    store = Store(tmp_path / "state.db")
    store.set_meta(_IDENTITY_META, json.dumps({
        "fp": "x", "user": "alice", "display_name": "Alice", "email": "a@example.com"}))
    d = dashboard_from_memory(store)
    store.close()
    assert (d.user, d.display_name, d.email) == ("alice", "Alice", "a@example.com")
