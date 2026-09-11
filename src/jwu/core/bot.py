"""Входящие сообщения Telegram-боту: команды и ответы-заметки.

Бот не только шлёт уведомления — ему можно ответить. Два вида входящих:

- **ответ на уведомление** или сообщение вида ``PROJ-1 текст`` → заметка-контекст по ключу
  (см. ``notify.parse_incoming``), в чат уходит подтверждение «записал»;
- **команда**: ``/sync`` — внеплановый проход демона, ``/status`` — когда был последний
  проход и что накопилось, ``/stuck`` — что застряло по порогам контура, ``/mentions`` —
  непрочитанные упоминания, ``/help``.

Всё считается из памяти jwu (без сети и без логина в трекер), поэтому демон может
опрашивать бота часто — раз в минуту — независимо от интервала синка. Сообщения не из
настроенного чата игнорируются; смещение обработанных апдейтов лежит в meta воркспейса.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Callable, Optional

from . import notify, thresholds as th_mod
from .dates import fmt_ago

if TYPE_CHECKING:
    from .config import Config
    from .models import Workspace
    from .store import Store

HELP = (
    "<b>Команды</b>\n"
    "/sync — синк всех контуров прямо сейчас\n"
    "/status — последний проход и что накопилось\n"
    "/stuck — что застряло по порогам контура\n"
    "/mentions — непрочитанные упоминания\n"
    "/help — это сообщение\n\n"
    "Ответ на уведомление → заметка по его ключу. "
    "Сообщение вида <code>PROJ-1 текст</code> → заметка по PROJ-1."
)


def _cmd_status(store: "Store", ws: "Workspace") -> str:
    from .daemon import LAST_PASS_META, LAST_PASS_SUMMARY_META, SingleInstance

    pid = SingleInstance().holder_pid()
    last = store.get_meta(LAST_PASS_META)
    pending = store.pending_changes()
    notable = notify.select_notable(pending)
    unseen = len(store.unseen_mentions())
    lines = [
        f"<b>jwu · {html.escape(ws.slug)}</b>",
        f"демон: {'работает (pid ' + str(pid) + ')' if pid else 'не запущен'}",
        f"последний проход: {fmt_ago(last, fallback='ещё не было') if last else 'ещё не было'}",
        f"  {html.escape(store.get_meta(LAST_PASS_SUMMARY_META) or '')}".rstrip(),
        f"накоплено дельт: {len(pending)}, из них важных: {len(notable)}; "
        f"непрочитанных упоминаний: {unseen}",
    ]
    return "\n".join(line for line in lines if line.strip())


def _cmd_stuck(store: "Store", ws: "Workspace", login: str,
               links: Optional[notify.Links]) -> str:
    th = th_mod.load(store, ws.id)
    items = th_mod.collect_stuck(
        prs_mine=store.latest_prs("mine"), prs_review=store.latest_prs("review"),
        mine=store.latest_issues("mine"), th=th, login=login,
    )
    if not items:
        return "⏳ Застрявшего нет — по порогам контура всё движется."
    lines = [f"⏳ <b>Застряло ({len(items)})</b>"]
    for x in items[:15]:
        key = notify._key_html(x["key"], links)
        lines.append(f"• {key} — {x['days']} дн: {html.escape(x['reason'])}")
        if x.get("title"):
            lines.append(f"   <i>{html.escape(notify._short(x['title'], 90))}</i>")
    if len(items) > 15:
        lines.append(f"… и ещё {len(items) - 15}")
    return "\n".join(lines)


def _cmd_mentions(store: "Store", links: Optional[notify.Links]) -> str:
    items = store.unseen_mentions()
    if not items:
        return "📣 Непрочитанных упоминаний нет."
    lines = [f"📣 <b>Непрочитанные упоминания ({len(items)})</b>"]
    for m in items[:10]:
        lines.append(f"• {notify._key_html(m.task_key, links)} — {html.escape(m.author or 'кто-то')}")
        lines.append(f"   <i>{html.escape(notify._short(m.text, 160))}</i>")
    return "\n".join(lines)


def handle_command(text: str, *, store: "Store", ws: "Workspace", login: str,
                   links: Optional[notify.Links], on_sync: Optional[Callable[[], bool]]) -> str:
    """Ответ на команду (HTML). Неизвестная команда → подсказка."""
    cmd = text.split()[0].lower().split("@")[0]
    if cmd == "/sync":
        if on_sync is None:
            return "Демон не запущен — синк запустить некому. Руками: <code>jwu sync</code>."
        return "🔄 Синк запущен, результат придёт уведомлением." if on_sync() else "🔄 Синк уже идёт."
    if cmd == "/status":
        return _cmd_status(store, ws)
    if cmd == "/stuck":
        return _cmd_stuck(store, ws, login, links)
    if cmd == "/mentions":
        return _cmd_mentions(store, links)
    if cmd in ("/help", "/start"):
        return HELP
    return f"Не знаю команду {html.escape(cmd)}.\n\n{HELP}"


def process_updates(
    store: "Store", ws: "Workspace", cfg: "Config", sender: notify.TelegramNotifier, *,
    login: str = "", on_sync: Optional[Callable[[], bool]] = None, long_poll: int = 0,
) -> list[dict]:
    """Забрать новые сообщения боту, выполнить команды, записать заметки, ответить.

    ``long_poll`` — сколько секунд Telegram может держать запрос в ожидании сообщения.
    Возвращает список обработанного: ``{"kind": "note"|"command", ...}``. Смещение
    сдвигается даже для пропущенных сообщений, чтобы чужие или пустые не крутились вечно.
    """
    store.use_workspace(ws.id)
    offset = int(store.get_workspace_meta(notify.OFFSET_META) or 0)
    updates = sender.get_updates(offset + 1 if offset else None, timeout=long_poll)
    links = notify.links_from_config(cfg)
    handled: list[dict] = []
    last = offset
    for upd in updates:
        last = max(last, int(upd.get("update_id", 0) or 0))
        msg = upd.get("message") or {}
        if str((msg.get("chat") or {}).get("id", "")) != str(sender.chat_id):
            continue
        text = " ".join((msg.get("text") or "").split())
        reply_to = msg.get("message_id")
        if text.startswith("/"):
            reply = handle_command(text, store=store, ws=ws, login=login, links=links, on_sync=on_sync)
            handled.append({"kind": "command", "text": text, "reply": reply})
        else:
            parsed = notify.parse_incoming(upd, chat_id=sender.chat_id)
            if parsed is None:
                reply = ("Не понял, к чему это. Ответь на уведомление или начни с ключа: "
                         "<code>PROJ-1 текст</code>. Команды — /help")
                handled.append({"kind": "ignored", "text": text})
            else:
                key, note_text = parsed
                note = store.add_note(key, note_text, author="telegram", kind="context")
                reply = f"📝 записал в {notify._key_html(key, links)}: <i>{html.escape(notify._short(note_text, 120))}</i>"
                handled.append({"kind": "note", "key": key, "note_id": note.id, "text": note_text})
        try:
            sender.send(reply, reply_to=reply_to, keyboard=text.lower().startswith(("/help", "/start")))
        except notify.NotifyError:
            pass  # ответ не критичен: заметка уже записана / команда выполнена
    if last != offset:
        store.set_workspace_meta(notify.OFFSET_META, str(last))
    return handled
