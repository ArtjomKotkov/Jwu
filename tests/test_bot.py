"""Команды боту и ответы-заметки (core.bot), опрос между проходами демона."""

import json

import httpx
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
    assert len(replies) == 7 and all(r["reply_to_message_id"] for r in replies)
    texts = [r["text"] for r in replies]
    assert "<b>Команды</b>" in texts[0]
    assert "демон: не запущен" in texts[1] and "синк: 1 ок" in texts[1]
    assert texts[2].startswith("🔄 Синк запущен")
    assert "Застряло (1)" in texts[3] and 'pull-requests/7">P/r#7</a>' in texts[3]
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
