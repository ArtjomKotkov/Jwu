"""Утренняя сводка в Telegram: доска «что делать» коротко, раз в день в выбранное время.

Время — настройка контура ``brief.time`` (ЧЧ:ММ, в поясе ворклогов контура или системном),
выбирается кнопками в меню бота. Пусто — выключено. Демон проверяет раз в ~25 с и шлёт,
если время наступило, а сегодня ещё не слал (``brief:last`` в meta воркспейса).
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from . import notify, todo

if TYPE_CHECKING:
    from .store import Store

TIME_SETTING = "brief.time"
LAST_META = "brief:last"
CHOICES = ("08:00", "08:30", "09:00", "09:30", "10:00", "10:30", "11:00")
TOP = 5
CB = "brief:"


def get_time(store: "Store", workspace_id: int) -> str:
    return (store.workspace_settings(workspace_id).get(TIME_SETTING) or "").strip()


def set_time(store: "Store", workspace_id: int, value: str) -> str:
    store.set_workspace_settings(workspace_id, {TIME_SETTING: value if value in CHOICES else ""})
    return get_time(store, workspace_id)


def _tz(store: "Store", workspace_id: int):
    from . import timechain

    name = timechain.get_timezone(store, workspace_id) or ""
    if name:
        try:
            return timechain.resolve_tz(name)
        except Exception:  # noqa: BLE001
            pass
    return datetime.now().astimezone().tzinfo


def due(store: "Store", workspace_id: int, now: Optional[datetime] = None) -> bool:
    at = get_time(store, workspace_id)
    if not at:
        return False
    now = now or datetime.now(_tz(store, workspace_id))
    if now.strftime("%H:%M") < at:
        return False
    return (store.get_workspace_meta(LAST_META) or "") != now.date().isoformat()


def render(board: "todo.Board", slug: str, links: Optional[notify.Links]) -> str:
    lines = [f"☀️ <b>Что делать · {html.escape(slug)}</b> · {datetime.now().strftime('%d.%m')}"]
    for group in todo.GROUPS:
        part = [i for i in board.items if i.group == group]
        if not part:
            continue
        free = [i for i in part if not i.taken]
        lines += ["", f"<b>{todo.GROUP_TITLES[group]}</b> — {len(part)}, свободно {len(free)}"]
        for i in free[:TOP]:
            prio = todo.PRIORITY_ICON.get(todo._prio(i.priority), "") if i.priority else ""
            key = i.issue_key or i.key
            what = ", ".join(p for p in i.problems if not p.startswith("автор ")) or i.status
            title = i.title[len(i.issue_key):].lstrip(" :—-") if i.issue_key and i.title.startswith(i.issue_key) else i.title
            lines.append(f"{prio} {notify._key_html(key, links)} — {html.escape(notify._short(title, 60))}"
                         + (f" · <i>{html.escape(what)}</i>" if what else ""))
        if len(free) > TOP:
            lines.append(f"… и ещё {len(free) - TOP}")
    if not board.items:
        lines += ["", "Ничего не горит."]
    lines += ["", "Раздать по сессиям — /jwu-todo в Claude Code."]
    return "\n".join(lines)


def send_if_due(store: "Store", workspace, cfg, sender, login: str) -> bool:
    if not due(store, workspace.id):
        return False
    board = todo.build(store, workspace.id, login, session="brief")
    sender.send(render(board, workspace.slug, notify.links_from_config(cfg)))
    store.set_workspace_meta(LAST_META, datetime.now(_tz(store, workspace.id)).date().isoformat())
    return True


def menu(store: "Store", workspace_id: int) -> tuple[str, dict]:
    at = get_time(store, workspace_id)
    text = ("⏰ <b>Утренняя сводка</b>\n" + (f"Приходит в {at}." if at else "Выключена.")
            + "\nВо сколько присылать «что делать»?")
    rows, row = [], []
    for c in CHOICES:
        row.append({"text": ("✅ " if c == at else "") + c, "callback_data": CB + c})
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "🔕 Выключить", "callback_data": CB + "off"}])
    return text, {"inline_keyboard": rows}


def handle_callback(store: "Store", workspace_id: int, data: str) -> tuple[str, dict]:
    value = data[len(CB):]
    set_time(store, workspace_id, "" if value == "off" else value)
    return menu(store, workspace_id)
