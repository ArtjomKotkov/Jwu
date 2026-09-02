import json

import httpx
import pytest
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
    jira_createmeta_raw,
    jira_issue_raw,
    jira_search_raw,
)

JIRA = "https://jira.test"
BB = "https://git.test"
SDESK = "https://sdesk.test"


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


def _service_with_sdesk(tmp_path):
    cfg = Config()
    cfg.jira.base_url = JIRA
    cfg.bitbucket.base_url = BB
    cfg.sdesk.base_url = SDESK
    cfg.sdesk.project = "SDESK"
    return Service(
        cfg,
        JiraClient(JIRA, "tok"),
        BitbucketClient(BB, "tok"),
        Store(tmp_path / "state.db"),
        sdesk=JiraClient(SDESK, "stok"),
    )


def test_client_for_key_routes_by_prefix(tmp_path):
    svc = _service_with_sdesk(tmp_path)
    try:
        assert svc._client_for_key("SDESK-39336") is svc.sdesk
        assert svc._client_for_key("sdesk-1") is svc.sdesk  # регистр не важен
        assert svc._client_for_key("WMCTASKS-5") is svc.jira
    finally:
        svc.close()


def test_client_for_key_without_sdesk_always_jira(tmp_path):
    svc = _service(tmp_path)
    try:
        assert svc._client_for_key("SDESK-1") is svc.jira  # SDESK не подключён
    finally:
        svc.close()


@respx.mock
def test_issue_and_worklog_route_to_sdesk_instance(tmp_path):
    """SDESK-ключ ходит на хост SDESK, обычный — на основную Jira."""
    jira_issue = respx.get(f"{JIRA}/rest/api/2/issue/WMCTASKS-1").mock(
        return_value=httpx.Response(200, json=jira_issue_raw(key="WMCTASKS-1")))
    sdesk_issue = respx.get(f"{SDESK}/rest/api/2/issue/SDESK-39336").mock(
        return_value=httpx.Response(200, json=jira_issue_raw(key="SDESK-39336")))
    for host in (JIRA, SDESK):
        respx.get(f"{host}/rest/dev-status/1.0/issue/detail").mock(
            return_value=httpx.Response(200, json={"detail": []}))
    sdesk_wl = respx.post(f"{SDESK}/rest/api/2/issue/SDESK-39336/worklog").mock(
        return_value=httpx.Response(201, json={"id": "1"}))

    svc = _service_with_sdesk(tmp_path)
    try:
        assert svc.issue("SDESK-39336").key == "SDESK-39336"
        assert svc.issue("WMCTASKS-1").key == "WMCTASKS-1"
        svc.add_worklog("SDESK-39336", "1h")
    finally:
        svc.close()

    assert sdesk_issue.called and jira_issue.called
    assert sdesk_wl.called  # worklog ушёл на SDESK, а не на Jira


@respx.mock
def test_broken_sdesk_does_not_break_jira(tmp_path):
    """Ленивое построение SDESK: недоступный/неверный SDESK не рушит команды по Jira.

    Фабрика SDESK кидает при логине; обращение к Jira-ключу этого даже не касается,
    а auth_check отдаёт jira ok и sdesk error, а не падает целиком.
    """
    from jwu.core.jira import JiraError

    cfg = Config()
    cfg.jira.base_url = JIRA
    cfg.bitbucket.base_url = BB
    cfg.sdesk.base_url = SDESK
    cfg.sdesk.project = "SDESK"

    def _boom():
        raise JiraError("401: не удалось залогиниться", 401)

    svc = Service(
        cfg,
        JiraClient(JIRA, "tok"),
        BitbucketClient(BB, "tok"),
        Store(tmp_path / "state.db"),
        sdesk_factory=_boom,
    )

    respx.get(f"{JIRA}/rest/api/2/issue/WMCTASKS-1").mock(
        return_value=httpx.Response(200, json=jira_issue_raw(key="WMCTASKS-1")))
    respx.get(f"{JIRA}/rest/dev-status/1.0/issue/detail").mock(
        return_value=httpx.Response(200, json={"detail": []}))
    respx.get(f"{JIRA}/rest/api/2/myself").mock(
        return_value=httpx.Response(200, json={"name": "akotkov", "displayName": "AK"}))
    respx.get(f"{BB}/rest/api/1.0/dashboard/pull-requests").mock(
        return_value=httpx.Response(200, json=bitbucket_dashboard_raw([])))

    try:
        # Jira-ключ работает как ни в чём не бывало — SDESK-фабрика не дёргается
        assert svc.issue("WMCTASKS-1").key == "WMCTASKS-1"
        res = svc.auth_check()
        assert res["jira"]["ok"] is True
        assert res["sdesk"]["ok"] is False and "401" in res["sdesk"]["error"]
    finally:
        svc.close()


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
    # личность кэшируется в пространстве воркспейса (зависит от Jira-инстанса)
    store.set_workspace_meta(_IDENTITY_META, json.dumps({
        "fp": "x", "user": "alice", "display_name": "Alice", "email": "a@example.com"}))
    d = dashboard_from_memory(store)
    store.close()
    assert (d.user, d.display_name, d.email) == ("alice", "Alice", "a@example.com")


@respx.mock
def test_my_reviews_filters_by_date_and_status(tmp_path):
    from datetime import datetime

    svc = _service(tmp_path)
    svc.cfg.jira.username = "me"

    def review_pr(pr_id, title, status):
        raw = bitbucket_pr_raw(pr_id=pr_id, title=title)
        raw["reviewers"] = [{
            "user": {"name": "me", "displayName": "Me"},
            "approved": status == "APPROVED",
            "status": status,
        }]
        return raw

    respx.get(f"{BB}/rest/api/1.0/dashboard/pull-requests").mock(
        return_value=httpx.Response(200, json=bitbucket_dashboard_raw([
            review_pr(1, "ABC-1 апрув сегодня", "APPROVED"),
            review_pr(2, "ABC-2 апрув давно", "APPROVED"),
            review_pr(3, "ABC-3 ещё не смотрел", "UNAPPROVED"),
        ]))
    )

    ts_today = 1_781_700_000_000
    ts_old = 1_700_000_000_000
    today = datetime.fromtimestamp(ts_today / 1000).date().isoformat()

    def acts(ts):
        return {"isLastPage": True, "values": [
            {"action": "APPROVED", "user": {"name": "me"}, "createdDate": ts}
        ]}

    for pid, ts in ((1, ts_today), (2, ts_old), (3, ts_today)):
        respx.get(
            f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/{pid}/activities"
        ).mock(return_value=httpx.Response(200, json=acts(ts)))

    # фильтр по сегодняшней дате: только PR #1 (UNAPPROVED #3 отброшен по статусу,
    # #2 — по дате)
    today_reviews = svc.my_reviews(on=today)
    assert [p.id for p in today_reviews] == [1]
    assert today_reviews[0].my_review_status == "APPROVED"
    assert today_reviews[0].my_review_at == ts_today

    # без фильтра по дате — оба апрувнутых (#1, #2), но не UNAPPROVED (#3)
    all_mine = svc.my_reviews()
    assert sorted(p.id for p in all_mine) == [1, 2]
    svc.close()


@respx.mock
def test_my_worklogs_on_filters_author_and_date(tmp_path):
    svc = _service(tmp_path)
    svc.cfg.jira.username = "me"

    respx.get(f"{JIRA}/rest/api/2/issue/PROJ-1/worklog").mock(
        return_value=httpx.Response(200, json={"worklogs": [
            {"author": {"name": "me"}, "timeSpent": "2h", "timeSpentSeconds": 7200,
             "comment": "Ревью", "started": "2026-06-17T10:00:00.000+0000"},
            {"author": {"name": "other"}, "timeSpent": "1h", "timeSpentSeconds": 3600,
             "comment": "чужое", "started": "2026-06-17T10:00:00.000+0000"},
            {"author": {"name": "me"}, "timeSpent": "30m", "timeSpentSeconds": 1800,
             "comment": "вчера", "started": "2026-06-16T10:00:00.000+0000"},
        ]})
    )
    respx.get(f"{JIRA}/rest/api/2/issue/PROJ-2/worklog").mock(
        return_value=httpx.Response(200, json={"worklogs": []})
    )

    data = svc.my_worklogs_on(["PROJ-1", "PROJ-2", "PROJ-1"], "2026-06-17")
    # только мои записи за дату; PROJ-2 без записей не попадает; дубль ключа схлопнут
    assert list(data.keys()) == ["PROJ-1"]
    assert len(data["PROJ-1"]) == 1
    assert data["PROJ-1"][0]["time"] == "2h"
    assert data["PROJ-1"][0]["comment"] == "Ревью"
    svc.close()


@respx.mock
def test_collect_mentions_records_event_once(tmp_path):
    """Упоминание — событие: запись создаётся один раз и не пересоздаётся на каждом синке."""
    issue = jira_issue_raw(comments=[
        {"id": 1, "author": "Боб", "body": "обычный коммент"},
        {"id": 2, "author": "Кэрол", "body": "глянь [~alice] плиз"},
    ])
    respx.get(f"{JIRA}/rest/api/2/search").mock(
        return_value=httpx.Response(200, json=jira_search_raw([issue]))
    )
    detail = respx.get(f"{JIRA}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(200, json=issue)
    )

    svc = _service(tmp_path)
    svc.cfg.jira.username = "alice"
    try:
        added = svc.collect_mentions()
        assert [(m.task_key, m.comment_id, m.author) for m in added] == [
            ("PROJ-1", "2", "Кэрол")
        ]
        assert svc.store.list_mentions()[0].text == "глянь [~alice] плиз"
        assert svc.store.list_mentions()[0].seen is False

        calls = detail.call_count
        # задача не менялась → карточку заново не тянем и дубля не создаём
        assert svc.collect_mentions() == []
        assert detail.call_count == calls
        assert len(svc.store.list_mentions()) == 1
    finally:
        svc.close()


@respx.mock
def test_collect_mentions_rescans_changed_issue(tmp_path):
    """Задача обновилась → карточку перечитываем и подхватываем новое упоминание."""
    first = jira_issue_raw(comments=[{"id": 1, "author": "Кэрол", "body": "[~alice] раз"}])
    second = jira_issue_raw(comments=[
        {"id": 1, "author": "Кэрол", "body": "[~alice] раз"},
        {"id": 2, "author": "Дэйв", "body": "[~alice] два"},
    ])
    second["fields"]["updated"] = "2026-05-21T10:00:00.000+0300"

    search = respx.get(f"{JIRA}/rest/api/2/search").mock(
        return_value=httpx.Response(200, json=jira_search_raw([first]))
    )
    detail = respx.get(f"{JIRA}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(200, json=first)
    )

    svc = _service(tmp_path)
    svc.cfg.jira.username = "alice"
    try:
        assert len(svc.collect_mentions()) == 1
        search.mock(return_value=httpx.Response(200, json=jira_search_raw([second])))
        detail.mock(return_value=httpx.Response(200, json=second))
        added = svc.collect_mentions()
        assert [m.comment_id for m in added] == ["2"]     # только новое
        assert len(svc.store.list_mentions()) == 2
    finally:
        svc.close()


@respx.mock
def test_create_issue_dry_run_does_not_post(tmp_path):
    """dry_run отдаёт готовый payload и проверку, но наружу ничего не пишет."""
    respx.get(f"{JIRA}/rest/api/2/issue/createmeta").mock(
        return_value=httpx.Response(200, json=jira_createmeta_raw()))
    post = respx.post(f"{JIRA}/rest/api/2/issue")
    svc = _service(tmp_path)
    try:
        res = svc.create_issue("PROJ", "Падает экспорт", description="шаги",
                               issuetype="Bug", dry_run=True)
    finally:
        svc.close()
    assert res["ok"] is False and res["dry_run"] is True and res["issue"] == {}
    assert res["check"]["ok"] is True
    assert res["fields"]["summary"] == "Падает экспорт"
    assert not post.called


@respx.mock
def test_create_issue_posts_and_returns_key(tmp_path):
    respx.get(f"{JIRA}/rest/api/2/issue/createmeta").mock(
        return_value=httpx.Response(200, json=jira_createmeta_raw()))
    respx.post(f"{JIRA}/rest/api/2/issue").mock(
        return_value=httpx.Response(201, json={"id": "1", "key": "PROJ-777"}))
    svc = _service(tmp_path)
    try:
        res = svc.create_issue("PROJ", "Падает экспорт", issuetype="Bug")
    finally:
        svc.close()
    assert res["ok"] is True
    assert res["issue"]["key"] == "PROJ-777"
    assert res["issue"]["url"] == f"{JIRA}/browse/PROJ-777"


@respx.mock
def test_create_issue_stops_before_post_when_required_field_missing(tmp_path):
    """Обязательное поле проекта не заполнено — POST не делаем вовсе."""
    respx.get(f"{JIRA}/rest/api/2/issue/createmeta").mock(return_value=httpx.Response(
        200, json=jira_createmeta_raw(required_extra={"customfield_10500": "Отдел"})))
    post = respx.post(f"{JIRA}/rest/api/2/issue")
    svc = _service(tmp_path)
    try:
        res = svc.create_issue("PROJ", "тема")
    finally:
        svc.close()
    assert res["ok"] is False and not post.called
    assert any("Отдел" in p for p in res["check"]["problems"])


@respx.mock
def test_create_issue_in_sdesk_project_goes_to_sdesk_instance(tmp_path):
    """Инстанс выбирается по префиксу ключа проекта — как и для карточки задачи."""
    respx.get(f"{SDESK}/rest/api/2/issue/createmeta").mock(
        return_value=httpx.Response(200, json=jira_createmeta_raw(project="SDESK")))
    post = respx.post(f"{SDESK}/rest/api/2/issue").mock(
        return_value=httpx.Response(201, json={"id": "2", "key": "SDESK-42"}))
    jira_post = respx.post(f"{JIRA}/rest/api/2/issue")
    svc = _service_with_sdesk(tmp_path)
    try:
        res = svc.create_issue("SDESK", "обращение клиента")
    finally:
        svc.close()
    assert res["issue"]["key"] == "SDESK-42"
    assert post.called and not jira_post.called


@respx.mock
def test_create_issue_propagates_jira_error(tmp_path):
    respx.get(f"{JIRA}/rest/api/2/issue/createmeta").mock(
        return_value=httpx.Response(200, json=jira_createmeta_raw()))
    respx.post(f"{JIRA}/rest/api/2/issue").mock(return_value=httpx.Response(
        400, json={"errors": {"summary": "Summary слишком длинный"}}))
    from jwu.core.jira import JiraError

    svc = _service(tmp_path)
    try:
        with pytest.raises(JiraError) as exc:
            svc.create_issue("PROJ", "тема")
    finally:
        svc.close()
    assert "summary: Summary слишком длинный" in str(exc.value)


@respx.mock
def test_link_issues_checks_type_and_instance(tmp_path):
    respx.get(f"{JIRA}/rest/api/2/issueLinkType").mock(return_value=httpx.Response(
        200, json={"issueLinkTypes": [{"name": "Relates"}, {"name": "Blocks"}]}))
    route = respx.post(f"{JIRA}/rest/api/2/issueLink").mock(
        return_value=httpx.Response(201, content=b""))
    svc = _service_with_sdesk(tmp_path)
    try:
        res = svc.link_issues("PROJ-2", "PROJ-1", "blocks")
        assert res["type"] == "Blocks"  # регистр берём из инстанса
        with pytest.raises(ValueError) as unknown:
            svc.link_issues("PROJ-2", "PROJ-1", "Дублирует")
        with pytest.raises(ValueError) as cross:
            svc.link_issues("PROJ-2", "SDESK-1", "Relates")
    finally:
        svc.close()
    assert route.call_count == 1
    assert "Relates, Blocks" in str(unknown.value)
    assert "разных инстансах" in str(cross.value)


def test_task_branch_reach_dedupes_commits_and_adds_pr_targets(tmp_path, monkeypatch):
    """Dev-панель отдаёт коммит дважды — считать его дважды нельзя («2 из 2» вместо «1»)."""
    from jwu.core import gitbranches
    from jwu.core.models import DevCommit, DevPullRequest, Issue, PR

    svc = _service(tmp_path)
    issue = Issue(key="PROJ-1", summary="S", status="Closed")
    issue.commits = [DevCommit(id="abc123", message="fix\nдетали"),
                     DevCommit(id="abc123", message="fix")]
    issue.pull_requests = [DevPullRequest(
        id="#42", name="fix", status="MERGED",
        url="https://git.test/projects/PROJ/repos/app/pull-requests/42")]
    monkeypatch.setattr(svc, "issue", lambda key: issue)
    monkeypatch.setattr(svc, "_workspace_repo_roots", lambda: {"/repo": "app"})
    monkeypatch.setattr(svc, "pr", lambda pr_id, project=None, repo=None: PR(
        id=pr_id, target_branch="release-10.5", repository=repo))
    seen: dict = {}

    def _reach(roots, shas, **kw):
        seen["shas"] = shas
        return [gitbranches.RepoReach(name="app", root="/repo")]

    monkeypatch.setattr(gitbranches, "reach", _reach)
    try:
        data = svc.task_branch_reach("PROJ-1")
    finally:
        svc.close()

    assert seen["shas"] == ["abc123"]                    # дубль коммита схлопнут
    assert data["commits"] == [{"sha": "abc123", "message": "fix"}]
    assert data["prs"][0]["target_branch"] == "release-10.5"
    assert data["repos"][0]["name"] == "app"


def test_task_branch_reach_survives_unavailable_pr_host(tmp_path, monkeypatch):
    """Хостинг недоступен — ветка PR просто неизвестна, отчёт всё равно строится."""
    from jwu.core import gitbranches
    from jwu.core.models import DevPullRequest, Issue

    svc = _service(tmp_path)
    issue = Issue(key="PROJ-1")
    issue.pull_requests = [DevPullRequest(
        id="#42", status="MERGED",
        url="https://git.test/projects/PROJ/repos/app/pull-requests/42")]
    monkeypatch.setattr(svc, "issue", lambda key: issue)
    monkeypatch.setattr(svc, "_workspace_repo_roots", lambda: {})
    monkeypatch.setattr(svc, "pr", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("нет сети")))
    monkeypatch.setattr(gitbranches, "reach", lambda *a, **kw: [])
    try:
        data = svc.task_branch_reach("PROJ-1")
    finally:
        svc.close()
    assert data["prs"][0]["target_branch"] == ""
    assert data["repos"] == []


@respx.mock
def test_add_comment_refuses_sdesk_without_client_consent(tmp_path):
    """Комментарий в SDESK читает клиент — без явного согласия он не уходит."""
    route = respx.post(f"{SDESK}/rest/api/2/issue/SDESK-1/comment").mock(
        return_value=httpx.Response(201, json={"id": "1"}))
    svc = _service_with_sdesk(tmp_path)
    try:
        with pytest.raises(ValueError) as exc:
            svc.add_comment("SDESK-1", "черновик ответа")
        assert not route.called
        assert svc.add_comment("SDESK-1", "готовый ответ", client_facing=True)["id"] == "1"
    finally:
        svc.close()
    assert "КЛИЕНТ" in str(exc.value)
    assert route.call_count == 1


@respx.mock
def test_add_comment_to_internal_task_needs_no_client_flag(tmp_path):
    route = respx.post(f"{JIRA}/rest/api/2/issue/PROJ-1/comment").mock(
        return_value=httpx.Response(201, json={"id": "7"}))
    svc = _service_with_sdesk(tmp_path)
    try:
        assert svc.add_comment("PROJ-1", "команде: перенёс фикс")["id"] == "7"
        with pytest.raises(ValueError, match="Пустой"):
            svc.add_comment("PROJ-1", "   ")
    finally:
        svc.close()
    assert json.loads(route.calls.last.request.content) == {"body": "команде: перенёс фикс"}


@respx.mock
def test_my_worklog_keys_on_searches_both_instances(tmp_path):
    """«Что вообще затрекано за день» ищется JQL-ом и в Jira, и в SDESK."""
    jira = respx.get(f"{JIRA}/rest/api/2/search").mock(return_value=httpx.Response(
        200, json=jira_search_raw([jira_issue_raw(key="PROJ-1")])))
    sdesk = respx.get(f"{SDESK}/rest/api/2/search").mock(return_value=httpx.Response(
        200, json=jira_search_raw([jira_issue_raw(key="SDESK-9")])))
    svc = _service_with_sdesk(tmp_path)
    try:
        keys = svc.my_worklog_keys_on("2026-08-31")
    finally:
        svc.close()
    assert keys == ["PROJ-1", "SDESK-9"]
    assert "worklogDate" in jira.calls.last.request.url.params["jql"]
    assert sdesk.called


@respx.mock
def test_my_worklog_keys_on_survives_dead_instance(tmp_path):
    """SDESK лёг — день всё равно показываем по основной Jira."""
    respx.get(f"{JIRA}/rest/api/2/search").mock(return_value=httpx.Response(
        200, json=jira_search_raw([jira_issue_raw(key="PROJ-1")])))
    respx.get(f"{SDESK}/rest/api/2/search").mock(return_value=httpx.Response(500, text="down"))
    svc = _service_with_sdesk(tmp_path)
    try:
        assert svc.my_worklog_keys_on("2026-08-31") == ["PROJ-1"]
    finally:
        svc.close()


@respx.mock
def test_transition_resolves_name_and_lists_available(tmp_path):
    """Перехода из текущего статуса нет — в ошибке список доступных, а не 400-я."""
    respx.get(f"{JIRA}/rest/api/2/issue/PROJ-1/transitions").mock(
        return_value=httpx.Response(200, json={"transitions": [
            {"id": "31", "name": "In Progress", "to": {"name": "In Progress"}}]}))
    route = respx.post(f"{JIRA}/rest/api/2/issue/PROJ-1/transitions").mock(
        return_value=httpx.Response(204, content=b""))
    svc = _service(tmp_path)
    try:
        result = svc.transition("PROJ-1", "in progress")   # регистр не важен
        with pytest.raises(ValueError) as exc:
            svc.transition("PROJ-1", "Closed")
    finally:
        svc.close()
    assert result == {"key": "PROJ-1", "transition": "In Progress", "status": "In Progress"}
    assert "In Progress" in str(exc.value)
    assert route.call_count == 1


@respx.mock
def test_add_attachment_checks_files_before_sending(tmp_path):
    """Половина загруженных файлов хуже внятного отказа — проверяем ДО отправки."""
    route = respx.post(f"{JIRA}/rest/api/2/issue/PROJ-1/attachments").mock(
        return_value=httpx.Response(200, json=[{"id": "9", "filename": "a.log", "size": 3}]))
    good = tmp_path / "a.log"
    good.write_text("abc", encoding="utf-8")
    svc = _service(tmp_path)
    try:
        with pytest.raises(ValueError, match="не найдены"):
            svc.add_attachment("PROJ-1", [str(good), str(tmp_path / "нет.log")])
        assert not route.called
        created = svc.add_attachment("PROJ-1", [str(good)])
    finally:
        svc.close()
    assert created[0]["filename"] == "a.log" and created[0]["path"] == str(good)


@respx.mock
def test_find_similar_issues_builds_and_query(tmp_path):
    """`text ~ "a b"` в Jira — поиск ФРАЗЫ и не находит ничего: слова соединяем через AND."""
    route = respx.get(f"{JIRA}/rest/api/2/search").mock(return_value=httpx.Response(
        200, json=jira_search_raw([jira_issue_raw(key="PROJ-9", summary="Отчёт врёт")])))
    svc = _service(tmp_path)
    try:
        found = svc.find_similar_issues("PROJ", "Отчёт «По часам» врёт PROJ-1", months=12)
    finally:
        svc.close()
    jql = route.calls.last.request.url.params["jql"]
    assert 'project = "PROJ"' in jql and "created >= -52w" in jql
    phrase = jql.split('text ~ "')[1].split('"')[0]
    assert " AND " in phrase and '"' not in phrase
    assert "PROJ-1" not in jql            # дефисы Lucene (ведущий дефис = NOT) не уедут
    assert found[0]["key"] == "PROJ-9"


@respx.mock
def test_find_similar_issues_relaxes_query_until_something_found(tmp_path):
    """Строгий набор слов ничего не дал — ищем по более коротким, а не сдаёмся."""
    route = respx.get(f"{JIRA}/rest/api/2/search")
    route.side_effect = [
        httpx.Response(200, json=jira_search_raw([])),                       # все слова
        httpx.Response(200, json=jira_search_raw([jira_issue_raw(key="PROJ-9")])),
    ]
    svc = _service(tmp_path)
    try:
        found = svc.find_similar_issues("PROJ", "Поддержка OpenSearch release")
    finally:
        svc.close()
    def _phrase(call):
        return call.request.url.params["jql"].split('text ~ "')[1].split('"')[0]

    first, second = (_phrase(c) for c in route.calls)
    assert first == "OpenSearch AND Поддержка AND release"   # сперва все значимые слова
    assert second == "OpenSearch AND Поддержка"              # потом без самого короткого
    assert found[0]["key"] == "PROJ-9"


@respx.mock
def test_find_similar_issues_gives_up_quietly(tmp_path):
    """Ничего не нашлось ни на одном уровне — пустой список, а не ошибка."""
    respx.get(f"{JIRA}/rest/api/2/search").mock(
        return_value=httpx.Response(200, json=jira_search_raw([])))
    svc = _service(tmp_path)
    try:
        assert svc.find_similar_issues("PROJ", "Совершенно уникальный заголовок") == []
    finally:
        svc.close()


@respx.mock
def test_create_issue_preview_carries_similar_issues(tmp_path):
    """Похожие задачи приходят вместе с превью — до создания, а не после."""
    respx.get(f"{JIRA}/rest/api/2/issue/createmeta").mock(
        return_value=httpx.Response(200, json=jira_createmeta_raw()))
    respx.get(f"{JIRA}/rest/api/2/search").mock(return_value=httpx.Response(
        200, json=jira_search_raw([jira_issue_raw(key="PROJ-9")])))
    svc = _service(tmp_path)
    try:
        preview = svc.create_issue("PROJ", "Отчёт показывает некорректные данные",
                                   dry_run=True)
        quiet = svc.create_issue("PROJ", "Отчёт показывает некорректные данные",
                                 dry_run=True, check_duplicates=False)
    finally:
        svc.close()
    assert preview["similar"][0]["key"] == "PROJ-9"
    assert quiet["similar"] == []


@respx.mock
def test_create_issue_sends_issuetype_by_id(tmp_path):
    """Регресс: по имени этот инстанс тип не находит («issue type selected is invalid»)."""
    respx.get(f"{JIRA}/rest/api/2/issue/createmeta").mock(
        return_value=httpx.Response(200, json=jira_createmeta_raw()))
    route = respx.post(f"{JIRA}/rest/api/2/issue").mock(
        return_value=httpx.Response(201, json={"id": "1", "key": "PROJ-777"}))
    svc = _service(tmp_path)
    try:
        preview = svc.create_issue("PROJ", "тема", issuetype="Task", priority="Major",
                                   dry_run=True, check_duplicates=False)
        result = svc.create_issue("PROJ", "тема", issuetype="Task", priority="Major")
    finally:
        svc.close()
    sent = json.loads(route.calls.last.request.content)["fields"]
    assert sent["issuetype"] == {"id": "10000"}
    assert sent["priority"] == {"id": "3"}
    # то, что показали в dry-run, и то, что отправили, — один и тот же payload
    assert preview["fields"] == result["fields"] == sent


@respx.mock
def test_create_issue_with_unknown_type_never_posts(tmp_path):
    respx.get(f"{JIRA}/rest/api/2/issue/createmeta").mock(
        return_value=httpx.Response(200, json=jira_createmeta_raw()))
    post = respx.post(f"{JIRA}/rest/api/2/issue")
    svc = _service(tmp_path)
    try:
        res = svc.create_issue("PROJ", "тема", issuetype="Эпик", check_duplicates=False)
    finally:
        svc.close()
    assert res["ok"] is False and not post.called
    assert "Task, Bug" in res["check"]["problems"][0]
