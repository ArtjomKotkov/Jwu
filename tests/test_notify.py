"""Уведомления после синка: отбор дельт, формат, отправка в Telegram, хук в Service.sync."""

import json

import httpx
import pytest
import respx

from jwu.builtin.telegram import transport as tg
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



def plain(text: str) -> str:
    """Текст уведомления без тегов и пробелов выравнивания — для проверок по смыслу."""
    import html as _h
    import re as _re
    return " ".join(_h.unescape(_re.sub(r"<[^>]+>", "", text)).replace("\u200a", " ").split())

def test_select_notable_filters_and_dedupes():
    deltas = [
        Delta(key="P/r#1", kind="new_conflict"),
        Delta(key="P/r#1", kind="new_conflict"),      # дубль
        Delta(key="P/r#2", kind="reviewer_approved"),
        Delta(key="P/r#3", kind="build_failed"),
        Delta(key="P/r#4", kind="pr_ready_to_merge"),  # «готов к мержу» не шлём
        Delta(key="X-1", kind="gone"),
    ]
    picked = notify.select_notable(deltas)
    assert [(d.key, d.kind) for d in picked] == [
        ("P/r#1", "new_conflict"), ("P/r#2", "reviewer_approved"), ("P/r#3", "build_failed"), ("X-1", "gone")]
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
    assert head.startswith("🔔 <b>work</b> · ")                  # контур и время в одной строке
    cards = rest.split("\n\n")                                   # одна карточка на ключ, тревожные сверху
    assert cards[0].startswith('<b>1.</b> <a href="https://git.x/projects/P/repos/r/pull-requests/2">P/r#2</a> · second')
    assert "<blockquote>⚠️ <b>конфликт</b></blockquote>" in cards[0]
    assert cards[1].startswith('<b>2.</b> <a href="https://git.x/projects/P/repos/r/pull-requests/3">P/r#3</a>')
    assert plain(cards[2]) == "3. P/r#1 · <fix> & go ✍️ needs work bob"
    assert "&lt;fix&gt; &amp; go" in cards[2]                    # экранирование
    assert cards[3].startswith('<b>4.</b> <a href="https://jira.x/browse/TS-5">TS-5</a>') and "Anna QA · +2" in plain(cards[3])
    assert plain(cards[4]) == "5. PROJ-9 📣 упоминание Ann «глянь <это>»"
    assert "•" not in text and "появился" not in text
    assert "<b>P/r#1</b>" in notify.format_message(note)          # без хостов — жирный ключ
    assert notify.key_url("dndeck/ui#5", notify.Links(github="https://github.com")) == "https://github.com/dndeck/ui/pull/5"
    assert notify.strip_mention_tags("[~alice] [~asmirnov] - как дела?") == "как дела?"
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
    sender = tg.TelegramNotifier("tok", "42")
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
        tg.TelegramNotifier("", "1")
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
        assert "needs work" in tg.calls.last.request.content.decode()
        assert "PROJ/repo#42" in tg.calls.last.request.content.decode()
        # роль вычислена из памяти: PR из вкладки «мои» — блок «МОЁ» с меткой «мой PR»
        sent = json.loads(tg.calls.last.request.content)["text"]
        assert "📌 Моё</b>" in sent and "<blockquote>" in sent
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


def test_ready_and_approved_are_notable_and_titled():
    deltas = [Delta(key="P/r#7", kind="pr_ready_to_merge", summary="PROJ-1: фича",
                    detail="апрувов 2/2 · сборка ок · конфликтов нет"),
              Delta(key="P/r#8", kind="pr_all_approved", summary="PROJ-2", detail="апрувов 1/1 · сборка красная")]
    note = notify.build_notification("work", deltas)
    assert [d.kind for d in note.deltas] == ["pr_all_approved"]        # «готов к мержу» не шлём
    note = notify.build_notification("work", deltas, kinds=["pr_ready_to_merge", "pr_all_approved"])
    text = plain(notify.format_message(note))
    assert "🚀 готов к мержу" in text and "👍 все апрувы" in text
    assert "апрувов 2/2 · сборка ок · конфликтов нет" in text and "сборка красная" in text


def test_all_sync_events_have_titles_and_default_set():
    must = {"new_issue", "status_change", "new_review_pr", "new_conflict", "build_failed", "new_comment",
            "new_pr_comment", "reviewer_needs_work", "gone", "pr_gone"}
    assert must <= set(notify.DEFAULT_KINDS) and "pr_ready_to_merge" not in notify.DEFAULT_KINDS
    assert all(k in notify.SECTION_TITLES for k in notify.DEFAULT_KINDS)
    note = notify.build_notification("work", [
        Delta(key="PROJ-5", kind="new_issue", summary="Новая"),
        Delta(key="P/r#9", kind="new_review_pr", summary="feat", detail="автор Иванов"),
        Delta(key="PROJ-6", kind="status_change", summary="Старая", detail="Open → In Progress"),
    ])
    text = plain(notify.format_message(note))
    assert "🆕 новая задача" in text and "👀 новый PR на ревью автор Иванов" in text
    assert text.index("PROJ-5") < text.index("P/r#9") < text.index("PROJ-6")   # по важности


@respx.mock
def test_backup_bot_takes_over_when_main_is_down(monkeypatch):
    """Основной бот не отвечает — сообщение уходит через резервный; лежащий не ждём заново."""
    tg._DOWN.clear()
    main = respx.post(f"{TG}/botmain/sendMessage").mock(side_effect=httpx.ReadTimeout("timed out"))
    backup = respx.post(f"{TG}/botspare/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 9}}))
    sender = tg.FallbackNotifier(tg.TelegramNotifier("main", "42"),
                                     tg.TelegramNotifier("spare", "42", role="backup"))
    assert sender.send("привет") == [9] and main.call_count == 1 and backup.call_count == 1
    assert sender.send("ещё") == [9] and main.call_count == 1           # основной помечен лежащим
    clock = [notify.time.monotonic() + tg.DOWN_FOR + 1]
    monkeypatch.setattr(notify.time, "monotonic", lambda: clock[0])
    main.mock(return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 5}}))
    assert sender.send("ожил") == [5]                                   # через 5 минут — снова основной
    # 4xx основного (не сбой связи) — ошибка как есть, без молчаливого ухода в резерв
    main.mock(return_value=httpx.Response(400, json={"ok": False, "description": "bad"}))
    with pytest.raises(notify.NotifyError):
        sender.send("x")
    # резервный — главный для ответа на нажатие в нём: answer_callback через него
    cb = respx.post(f"{TG}/botspare/answerCallbackQuery").mock(return_value=httpx.Response(200, json={"ok": True}))
    sender.swapped().answer_callback("c1")
    assert cb.call_count == 1 and sender.swapped().offset_meta != sender.offset_meta
    tg._DOWN.clear()


def test_notifier_from_config_with_backup(monkeypatch):
    from jwu.core import config as config_mod

    cfg = Config()
    cfg.telegram.chat_id = "42"
    monkeypatch.setattr(config_mod, "telegram_token", lambda c: "main")
    monkeypatch.setattr(config_mod, "telegram_backup_token", lambda c: "spare")
    sender = notify.notifier_from_config(cfg)
    assert isinstance(sender, tg.FallbackNotifier) and sender.second.token == "spare"
    monkeypatch.setattr(config_mod, "telegram_backup_token", lambda c: "")
    assert isinstance(notify.notifier_from_config(cfg), tg.TelegramNotifier)


def test_split_message_keeps_html_tags_balanced():
    """Разрез внутри цитаты: каждый кусок — валидный HTML для Telegram."""
    text = "🌙 <b>Пока было тихо</b>\n" + "\n".join(
        f"<i>02.11 {i:02d}:00</i>\n<blockquote>" + "\n".join(f"событие {i}.{j} " + "x" * 30 for j in range(6))
        + "</blockquote>" for i in range(12))
    chunks = notify.split_message(text, limit=400)
    assert len(chunks) > 3
    for chunk in chunks:
        stack: list = []
        notify._open_tags(chunk.replace("\n", " "), stack)
        assert stack == [], chunk
    assert "событие 11.5" in chunks[-1] and all(len(c) <= 400 + 40 for c in chunks)
