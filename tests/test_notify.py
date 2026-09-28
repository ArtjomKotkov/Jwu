"""Уведомления после синка: отбор дельт, формат, отправка в Telegram, хук в Service.sync."""

import json

import httpx
import pytest
import respx

from jwu.core import notify
from jwu.core.bitbucket import BitbucketClient
from jwu.core.config import Config
from jwu.core.jira import JiraClient
from jwu.core.models import Delta, Mention, PR, Reviewer
from jwu.core.service import Service
from jwu.core.store import Store

from .fixtures import bitbucket_dashboard_raw, bitbucket_merge_raw, bitbucket_pr_raw

BB = "https://git.test"
JIRA = "https://jira.test"
TG = "https://api.telegram.org"


def test_select_notable_filters_and_dedupes():
    deltas = [
        Delta(key="P/r#1", kind="new_conflict"),
        Delta(key="P/r#1", kind="new_conflict"),      # дубль
        Delta(key="P/r#2", kind="reviewer_approved"),  # не важное
        Delta(key="P/r#3", kind="build_failed"),
        Delta(key="X-1", kind="gone"),
    ]
    picked = notify.select_notable(deltas)
    assert [(d.key, d.kind) for d in picked] == [("P/r#1", "new_conflict"), ("P/r#3", "build_failed")]
    # свой набор видов
    assert [d.kind for d in notify.select_notable(deltas, kinds=["gone"])] == ["gone"]


def test_format_message_sections_links_and_escapes():
    note = notify.Notification(
        workspace="work",
        deltas=[
            Delta(key="P/r#1", kind="reviewer_needs_work", summary="<fix> & go", detail="bob: needs work"),
            Delta(key="P/r#2", kind="new_conflict", summary="second", detail="появился merge-конфликт"),
            Delta(key="P/r#3", kind="new_conflict", summary="third"),
            Delta(key="TS-5", kind="qa_comment", summary="задача", detail="+2 комм. от Anna QA"),
        ],
        mentions=[Mention(task_key="PROJ-9", author="Ann", text="[~me] глянь <это>")],
    )
    links = notify.Links(jira="https://jira.x", bitbucket="https://git.x")
    text = notify.format_message(note, links=links)
    head, _, rest = text.partition("\n\n")
    assert head.startswith("🔔 <b>jwu · work</b>\n")            # контур, дата отдельной строкой
    blocks = rest.split("\n\n")
    assert blocks[0] == "<b>⚠️ КОНФЛИКТ</b>"                     # секции в порядке блокеров
    assert blocks[1] == '<a href="https://git.x/projects/P/repos/r/pull-requests/2">P/r#2</a>\n<i>second</i>'
    assert blocks[2] == '<a href="https://git.x/projects/P/repos/r/pull-requests/3">P/r#3</a>\n<i>third</i>'
    assert blocks[3] == "<b>✍️ NEEDS WORK</b>"
    assert blocks[4] == ('<a href="https://git.x/projects/P/repos/r/pull-requests/1">P/r#1</a> · bob'
                         "\n<i>&lt;fix&gt; &amp; go</i>")
    assert blocks[5] == "<b>🧪 КОММЕНТАРИЙ QA</b>"
    assert blocks[6].startswith('<a href="https://jira.x/browse/TS-5">TS-5</a> · Anna QA · +2')
    assert blocks[7] == "<b>📣 УПОМИНАНИЯ</b>"
    assert blocks[8] == '<a href="https://jira.x/browse/PROJ-9">PROJ-9</a> · Ann\n<i>«глянь &lt;это&gt;»</i>'
    assert "•" not in text and "появился" not in text
    assert "<b>P/r#1</b>" in notify.format_message(note)          # без хостов — жирный ключ
    assert notify.key_url("dndeck/ui#5", notify.Links(github="https://github.com")) == "https://github.com/dndeck/ui/pull/5"
    assert notify.strip_mention_tags("[~akotkov] [~asmirnov] - как дела?") == "как дела?"
    assert not notify.Notification(workspace="w")


def test_split_message_by_lines():
    text = "\n".join(f"line {i:03d}" for i in range(100))
    chunks = notify.split_message(text, limit=100)
    assert len(chunks) > 1
    assert "\n".join(chunks) == text
    assert all(len(c) <= 100 for c in chunks)


@respx.mock
def test_telegram_notifier_sends_and_reports_errors():
    route = respx.post(f"{TG}/bottok/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    )
    sender = notify.TelegramNotifier("tok", "42")
    try:
        assert sender.send("<b>hi</b>") == [7]
        import json as _json

        body = _json.loads(route.calls.last.request.content)
        assert body["chat_id"] == "42" and body["parse_mode"] == "HTML"

        route.mock(return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))
        with pytest.raises(notify.NotifyError) as exc:
            sender.send("x")
        assert "401" in str(exc.value) and "tok" not in str(exc.value)
    finally:
        sender.close()


def test_notifier_requires_token_and_chat():
    with pytest.raises(notify.NotifyError):
        notify.TelegramNotifier("", "1")
    cfg = Config()
    assert notify.notifier_from_config(cfg) is None          # chat_id пуст
    cfg.telegram.chat_id = "1"
    assert notify.notifier_from_config(cfg) is None          # токена нет


def _service(tmp_path, *, chat_id=""):
    cfg = Config()
    cfg.jira.base_url = JIRA
    cfg.bitbucket.base_url = BB
    cfg.telegram.chat_id = chat_id
    svc = Service(cfg, JiraClient(JIRA, "tok"), BitbucketClient(BB, "tok"),
                  Store(tmp_path / "state.db"))
    return svc


def _mock_pr(reviewer_status: str):
    pr = bitbucket_pr_raw(pr_id=42)
    pr["reviewers"] = [{"user": {"name": "bob", "displayName": "Bob"},
                        "approved": reviewer_status == "APPROVED", "status": reviewer_status}]
    return pr


@respx.mock
def test_sync_sends_notification_when_configured(tmp_path, monkeypatch):
    """Переход ревьювера в NEEDS_WORK между синками → дельта и одно сообщение в Telegram."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    dash = respx.get(f"{BB}/rest/api/1.0/dashboard/pull-requests")
    dash.side_effect = [
        httpx.Response(200, json=bitbucket_dashboard_raw([_mock_pr("UNAPPROVED")])),
        httpx.Response(200, json=bitbucket_dashboard_raw([_mock_pr("NEEDS_WORK")])),
    ]
    respx.get(f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42/merge").mock(
        return_value=httpx.Response(200, json=bitbucket_merge_raw())
    )
    respx.get(f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42/commits").mock(
        return_value=httpx.Response(200, json={"values": []})
    )
    tg = respx.post(f"{TG}/bottok/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )

    svc = _service(tmp_path, chat_id="42")
    try:
        r1 = svc.sync_section("prs_mine")
        assert not r1.notified and tg.call_count == 0     # первый раз PR виден — тихо
        r2 = svc.sync_section("prs_mine")
        assert [d.kind for d in r2.deltas] == ["reviewer_needs_work"]
        assert r2.notified and tg.call_count == 1
        assert "NEEDS WORK" in tg.calls.last.request.content.decode()
        assert "PROJ/repo#42" in tg.calls.last.request.content.decode()
        # роль вычислена из памяти: PR из вкладки «мои» — блок «МОЁ» с меткой «мой PR»
        sent = json.loads(tg.calls.last.request.content)["text"]
        assert "━━ МОЁ ━━" in sent and "🔧 мой PR" in sent
    finally:
        svc.close()


@respx.mock
def test_sync_is_silent_without_chat_and_survives_telegram_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    dash = respx.get(f"{BB}/rest/api/1.0/dashboard/pull-requests")
    dash.side_effect = [
        httpx.Response(200, json=bitbucket_dashboard_raw([_mock_pr("UNAPPROVED")])),
        httpx.Response(200, json=bitbucket_dashboard_raw([_mock_pr("NEEDS_WORK")])),
    ] * 2
    respx.get(f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42/merge").mock(
        return_value=httpx.Response(200, json=bitbucket_merge_raw())
    )
    respx.get(f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42/commits").mock(
        return_value=httpx.Response(200, json={"values": []})
    )
    tg = respx.post(f"{TG}/bottok/sendMessage").mock(return_value=httpx.Response(500, text="boom"))

    quiet = _service(tmp_path)  # chat_id пуст → отправщика нет вовсе
    try:
        quiet.sync_section("prs_mine")
        r = quiet.sync_section("prs_mine")
        assert [d.kind for d in r.deltas] == ["reviewer_needs_work"] and not r.notified
        assert tg.call_count == 0
    finally:
        quiet.close()

    (tmp_path / "b").mkdir(exist_ok=True)
    loud = _service(tmp_path / "b", chat_id="42")
    try:
        loud.sync_section("prs_mine")
        r = loud.sync_section("prs_mine")  # Telegram отвечает 500 — синк всё равно успешен
        assert [d.kind for d in r.deltas] == ["reviewer_needs_work"] and not r.notified
        assert tg.call_count == 1
    finally:
        loud.close()


def test_pr_signature_needs_work_delta_only_on_transition(tmp_path):
    store = Store(tmp_path / "s.db")
    try:
        def pr(status):
            return PR(id=1, project="P", repository="r", reviewers=[Reviewer(name="bob", status=status)])

        r1 = store.start_sync_run(["prs:mine"]); store.save_pr_snapshot(r1, pr("NEEDS_WORK"), ["mine"])
        store.compute_changes(r1)
        r2 = store.start_sync_run(["prs:mine"]); store.save_pr_snapshot(r2, pr("NEEDS_WORK"), ["mine"])
        assert "reviewer_needs_work" not in [d.kind for d in store.compute_changes(r2)]
        r3 = store.start_sync_run(["prs:mine"]); store.save_pr_snapshot(r3, pr("APPROVED"), ["mine"])
        store.compute_changes(r3)
        r4 = store.start_sync_run(["prs:mine"]); store.save_pr_snapshot(r4, pr("NEEDS_WORK"), ["mine"])
        assert "reviewer_needs_work" in [d.kind for d in store.compute_changes(r4)]
    finally:
        store.close()
