"""Команды боту и ответы-заметки (core.bot), опрос между проходами демона."""

import json

import httpx
import pytest
import respx

from jwu.core import bot, daemon, notify, workspaces
from jwu.core.config import Config
from jwu.core.models import Issue, PR, Reviewer
from jwu.core.store import Store

TG = "https://api.telegram.org"


def _msg(update_id, text, chat=42, reply_to_text=None, message_id=None):
    m = {"chat": {"id": chat}, "text": text, "message_id": message_id or update_id * 10}
    if reply_to_text:
        m["reply_to_message"] = {"text": reply_to_text}
    return {"update_id": update_id, "message": m}


def _env(tmp_path):
    store = Store(tmp_path / "s.db")
    ws = store.get_workspace_by_slug("work")
    store.use_workspace(ws.id)
    cfg = Config()
    cfg.jira.base_url = "https://jira.x"
    cfg.bitbucket.base_url = "https://git.x"
    cfg.telegram.chat_id = "42"
    return store, ws, cfg


@respx.mock
def test_process_updates_commands_notes_and_replies(tmp_path):
    store, ws, cfg = _env(tmp_path)
    run = store.start_sync_run(["prs:mine"])
    store.save_pr_snapshot(run, PR(id=7, project="P", repository="r", title="старый", updated=1,
                                   reviewers=[Reviewer(name="bob", approved=True, status="APPROVED")]), ["mine"])
    store.finish_sync_run(run, {"prs:mine": 1})
    store.set_meta(daemon.LAST_PASS_SUMMARY_META, "синк: 1 ок")
    respx.get(f"{TG}/bottok/getUpdates").mock(return_value=httpx.Response(200, json={"ok": True, "result": [
        _msg(1, "/help"),
        _msg(2, "/status"),
        _msg(3, "/sync"),
        _msg(4, "/stuck"),
        _msg(5, "поправил, жду ревью", reply_to_text="⚠️ конфликт\n• P/r#7 — x"),
        _msg(6, "просто текст"),
        _msg(7, "/mentions"),
        _msg(8, "PROJ-9 чужой чат", chat=1),
    ]}))
    sent = respx.post(f"{TG}/bottok/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}))
    sender = notify.TelegramNotifier("tok", "42")
    kicks = []
    try:
        handled = bot.process_updates(store, ws, cfg, sender, login="me", on_sync=lambda: kicks.append(1) or True)
    finally:
        sender.close()
    kinds = [h["kind"] for h in handled]
    assert kinds == ["command", "command", "command", "command", "note", "ignored", "command"]
    assert kicks == [1]
    replies = [json.loads(c.request.content) for c in sent.calls]
    assert len(replies) == 7
    # цитатой — только заметка и «не понял»; ответы на команды — обычными сообщениями
    assert [bool(r.get("reply_to_message_id")) for r in replies] == [False, False, False, False, True, True, False]
    texts = [r["text"] for r in replies]
    assert "<b>Команды</b>" in texts[0]
    assert "демон не запущен" in texts[1] and "1 ок" in texts[1]
    assert texts[2].startswith("🔄 Синк запущен")
    assert texts[3].startswith("Не знаю команду /stuck")   # кнопку и команду убрали
    assert texts[4].startswith("📝 записал в") and "P/r#7" in texts[4]
    assert texts[5].startswith("Не понял")
    assert "Непрочитанных упоминаний нет" in texts[6]
    assert [n.text for n in store.get_notes("P/r#7")] == ["поправил, жду ревью"]
    assert store.get_workspace_meta(notify.OFFSET_META) == "8"   # чужое сообщение тоже сдвигает offset
    store.close()


def test_sync_without_daemon_explains(tmp_path):
    store, ws, cfg = _env(tmp_path)
    reply = bot.handle_command("/sync@jwu_bot", store=store, ws=ws, login="", links=None, on_sync=None)
    assert "не запущен" in reply
    assert "Не знаю команду /foo" in bot.handle_command("/foo", store=store, ws=ws, login="", links=None, on_sync=None)
    from jwu.core.models import Mention

    store.add_mentions([Mention(task_key="A-1", comment_id="1", author="x", text="[~me]", created="2026-09-01T10:00:00+03:00")])
    assert "Помечено прочитанными: 1" in bot.handle_command("/seen", store=store, ws=ws, login="", links=None, on_sync=None)
    assert store.unseen_mentions() == []
    assert "не было" in bot.handle_command("/seen", store=store, ws=ws, login="", links=None, on_sync=None)
    store.close()


@respx.mock
def test_daemon_poll_bots_iterates_configured_workspaces(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    store = Store(tmp_path / "s.db")
    work = store.get_workspace_by_slug("work")
    store.set_workspace_settings(work.id, {"telegram.chat_id": "42"})
    workspaces.create(store, "gh", provider="github")   # без Telegram — пропускается
    respx.get(f"{TG}/bottok/getUpdates").mock(return_value=httpx.Response(200, json={"ok": True, "result": [
        _msg(1, "PROJ-1 ждём QA")]}))
    respx.post(f"{TG}/bottok/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    handled = daemon.poll_bots(store)
    assert [(h["workspace"], h["kind"], h["key"]) for h in handled] == [("work", "note", "PROJ-1")]
    store.use_workspace(work.id)
    assert store.get_notes("PROJ-1")[0].author == "telegram"
    store.close()


@respx.mock
def test_shared_bot_is_polled_once(tmp_path, monkeypatch):
    """Один бот у нескольких контуров — один опрос: нажатие не приходит дважды."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    store = Store(tmp_path / "s.db")
    work = store.get_workspace_by_slug("work")
    store.set_workspace_settings(work.id, {"telegram.chat_id": "42"})
    gh = workspaces.create(store, "gh", provider="github")
    store.set_workspace_settings(gh.id, {"telegram.chat_id": "42"})
    route = respx.get(f"{TG}/bottok/getUpdates").mock(return_value=httpx.Response(200, json={"ok": True, "result": [
        _msg(1, "PROJ-1 ждём QA")]}))
    respx.post(f"{TG}/bottok/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    handled = daemon.poll_bots(store)
    assert route.call_count == 1 and [h["workspace"] for h in handled] == ["work"]
    store.close()


def test_daemon_sync_hook_does_not_touch_telegram():
    assert daemon.no_telegram_after_sync(None, None) is None


@respx.mock
def test_get_updates_long_poll_passes_timeout():
    route = respx.get(f"{TG}/bottok/getUpdates").mock(return_value=httpx.Response(200, json={"ok": True, "result": []}))
    sender = notify.TelegramNotifier("tok", "42")
    try:
        assert sender.get_updates(5, timeout=25) == []
        params = route.calls.last.request.url.params
        assert params["timeout"] == "25" and params["offset"] == "5"
        sender.get_updates()
        assert route.calls.last.request.url.params["timeout"] == "0"
    finally:
        sender.close()


def test_cmd_tasks_groups_by_status_meaning(tmp_path):
    store, ws, _ = _env(tmp_path)
    run = store.start_sync_run(["mine"])
    for issue in (Issue(key="AB-1", summary="Ревью", status="In Review", priority="Major"),
                  Issue(key="AB-2", summary="Новая", status="To Do", priority="Critical"),
                  Issue(key="AB-3", summary="Делаю", status="In Progress", priority="Minor")):
        store.save_issue_snapshot(run, issue, ["mine"])
    store.finish_sync_run(run, {"tasks:mine": 3})
    text = bot._cmd_tasks(store, ws, None)
    assert "В работе" in text and "Не начаты" in text and "На ревью" in text
    assert text.index("В работе") < text.index("Не начаты") < text.index("На ревью")
    store.close()


def test_today_shows_only_latest_summary_of_a_job(tmp_path):
    """Работу закрыли дважды — в «Сегодня» один, последний итог, а не оба."""
    store, ws, cfg = _env(tmp_path)
    job = store.create_job("PROJ-1", "задача")
    store.add_job_record(job.id, "убрал поле: бэкенд и PR", kind="summary")
    store.add_job_record(job.id, "убрал поле: бэкенд, PR и 4test", kind="summary")
    other = store.create_job("PROJ-1", "возврат")
    store.add_job_record(other.id, "починил возврат с тестов", kind="summary")
    text = bot._cmd_today(store, ws, None)
    assert "бэкенд и PR" not in text
    assert text.count("✍️") == 2
    assert "PR и 4test" in text and "починил возврат" in text


def test_poll_replies_refused_while_daemon_reads_bot(tmp_path, monkeypatch):
    """Пока демон жив, другой процесс бота не читает — иначе Telegram 409 рвёт опрос демона."""
    from jwu.core.service import Service

    monkeypatch.setattr(daemon.SingleInstance, "holder_pid", lambda self: 999999)
    store = Store(tmp_path / "s.db")
    svc = Service.__new__(Service)
    svc.store = store
    with pytest.raises(notify.NotifyError, match="демон jwu"):
        Service.poll_telegram_replies(svc)
    store.close()


@respx.mock
def test_backup_bot_polled_separately_and_replies_itself(tmp_path, monkeypatch):
    """Резервный бот: свой опрос и своё смещение; ответ на сообщение в нём — через него же."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_BACKUP_BOT_TOKEN", "spare")
    store = Store(tmp_path / "s.db")
    work = store.get_workspace_by_slug("work")
    store.set_workspace_settings(work.id, {"telegram.chat_id": "42"})
    main_poll = respx.get(f"{TG}/bottok/getUpdates").mock(return_value=httpx.Response(200, json={"ok": True, "result": []}))
    respx.get(f"{TG}/botspare/getUpdates").mock(return_value=httpx.Response(200, json={"ok": True, "result": [
        _msg(5, "PROJ-2 из резерва")]}))
    main_send = respx.post(f"{TG}/bottok/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    spare_send = respx.post(f"{TG}/botspare/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 3}}))
    assert daemon.has_backup_bot(store)
    got: list = []
    daemon.poll_bots(store, role="backup", dispatch=lambda wid, upd: got.append((wid, upd)))
    assert main_poll.call_count == 0 and len(got) == 1 and got[0][1][daemon.BOT_ROLE_KEY] == "backup"
    store.use_workspace(work.id)
    assert store.get_workspace_meta(f"{notify.OFFSET_META}:backup") == "5" and not store.get_workspace_meta(notify.OFFSET_META)
    daemon.handle_bot_update(lambda: Store(tmp_path / "s.db"), work.id, got[0][1])
    assert spare_send.call_count == 1 and main_send.call_count == 0
    store.close()


@respx.mock
def test_backup_menu_marked_and_checks_main(monkeypatch):
    """Меню резервного бота помечено «резервный» и умеет проверить основной."""
    notify._DOWN.clear()
    pair = notify.FallbackNotifier(notify.TelegramNotifier("tok", "42"),
                                   notify.TelegramNotifier("spare", "42", role="backup"))
    title, kb = bot.main_menu(pair.swapped())
    assert "резервный бот" in title and "menu:checkmain" in str(kb)
    assert "резервный" not in bot.main_menu(pair)[0] and "checkmain" not in str(bot.main_menu(pair)[1])
    respx.get(f"{TG}/bottok/getMe").mock(side_effect=httpx.ReadTimeout("timed out"))
    assert bot.check_main_bot(pair.swapped()).startswith("❌") and notify.is_down("tok")
    respx.get(f"{TG}/bottok/getMe").mock(return_value=httpx.Response(200, json={"ok": True, "result": {"username": "main_bot"}}))
    assert "@main_bot отвечает" in bot.check_main_bot(pair.swapped()) and not notify.is_down("tok")
