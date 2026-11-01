"""Настройки уведомлений воркспейса: какие события слать и когда не беспокоить.

Что слать — группами (:data:`GROUPS`), плюс тумблеры «Упоминания» и «Участвую» (события по
чужим задачам и PR, где я участник); точечно — отдельные виды событий. Хранится в настройках
воркспейса: ``notify.off`` (выключенные группы и тумблеры), ``notify.kinds_off`` (виды).

Тихие часы (``notify.quiet`` = «22:00-09:00», ``notify.quiet_days`` = «sat,sun») — в часовом
поясе воркспейса (тот же, что у утренней сводки). Всё, что пришлось на тишину, копится
(:func:`deliver`) и уходит одной сводкой, когда тишина кончилась (:func:`flush`). Тихие часы
пробивают только запросы допуска git и ответы бота на команды пользователя — они идут мимо
:func:`deliver`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .store import Store

OFF_SETTING = "notify.off"
KINDS_OFF_SETTING = "notify.kinds_off"
QUIET_SETTING = "notify.quiet"
QUIET_DAYS_SETTING = "notify.quiet_days"
HELD_META = "notify:held"          # [{"ts", "text"}] — накопленное за тихие часы
HELD_MAX = 200

GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "builds": ("🔧 Сборки и конфликты", ("new_conflict", "build_failed", "build_fixed")),
    "my_prs": ("✍️ Мои PR", ("reviewer_needs_work", "reviewer_approved", "pr_all_approved", "new_pr_task",
                            "pr_task_resolved", "new_pr_comment", "new_pr_commit", "pr_gone")),
    "review": ("👀 PR на ревью мне", ("new_review_pr",)),
    "tasks": ("📋 Задачи", ("new_issue", "status_change", "new_comment", "resolved", "new_pr", "gone")),
    "qa": ("🧪 QA", ("returned_from_testing", "qa_comment")),
}
TOGGLES: dict[str, str] = {
    "mentions": "📣 Упоминания",
    "participate": "👥 «Участвую» — чужие задачи и PR",
}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_TITLES = dict(zip(DAYS, ("пн", "вт", "ср", "чт", "пт", "сб", "вс")))
_QUIET_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


class PrefsError(ValueError):
    pass


def _csv(raw: str) -> set[str]:
    return {x.strip() for x in (raw or "").split(",") if x.strip()}


def parse_quiet(raw: str) -> Optional[tuple[time, time]]:
    """«22:00-09:00» → (22:00, 09:00); пусто/«off» → None."""
    raw = (raw or "").strip().lower()
    if raw in ("", "off", "-", "выкл"):
        return None
    m = _QUIET_RE.match(raw)
    if not m:
        raise PrefsError(f"Тихие часы «{raw}»: нужен вид ЧЧ:ММ-ЧЧ:ММ, например 22:00-09:00")
    h1, m1, h2, m2 = map(int, m.groups())
    if not (0 <= h1 < 24 and 0 <= h2 < 24 and 0 <= m1 < 60 and 0 <= m2 < 60) or (h1, m1) == (h2, m2):
        raise PrefsError(f"Тихие часы «{raw}»: неверное время")
    return time(h1, m1), time(h2, m2)


@dataclass
class Prefs:
    off: set[str] = field(default_factory=set)
    kinds_off: set[str] = field(default_factory=set)
    quiet: Optional[tuple[time, time]] = None
    quiet_days: set[str] = field(default_factory=set)
    tz: object = None

    def on(self, name: str) -> bool:
        return name not in self.off

    def kinds(self) -> list[str]:
        """Виды событий синка, которые слать."""
        from .notify import DEFAULT_KINDS

        off_groups = {g for g in GROUPS if g in self.off}
        muted = {k for g in off_groups for k in GROUPS[g][1]} | self.kinds_off
        return [k for k in DEFAULT_KINDS if k not in muted]

    def quiet_label(self) -> str:
        parts = []
        if self.quiet:
            parts.append(f"{self.quiet[0]:%H:%M}–{self.quiet[1]:%H:%M}")
        if self.quiet_days:
            parts.append(", ".join(DAY_TITLES[d] for d in DAYS if d in self.quiet_days) + " — весь день")
        return "; ".join(parts) or "выключено"

    def is_quiet(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(self.tz) if self.tz else (now or datetime.now())
        if DAYS[now.weekday()] in self.quiet_days:
            return True
        if not self.quiet:
            return False
        start, end = self.quiet
        t = now.time()
        return start <= t < end if start < end else (t >= start or t < end)


def load(store: "Store", workspace_id: Optional[int] = None) -> Prefs:
    from .brief import _tz

    ws = workspace_id or store.workspace_id
    s = store.workspace_settings(ws)
    try:
        quiet = parse_quiet(s.get(QUIET_SETTING, ""))
    except PrefsError:
        quiet = None
    try:
        tz = _tz(store, ws)
    except Exception:  # noqa: BLE001
        tz = None
    return Prefs(off=_csv(s.get(OFF_SETTING, "")), kinds_off=_csv(s.get(KINDS_OFF_SETTING, "")), quiet=quiet,
                 quiet_days={d for d in _csv(s.get(QUIET_DAYS_SETTING, "")) if d in DAYS}, tz=tz)


def save(store: "Store", prefs: Prefs, workspace_id: Optional[int] = None) -> Prefs:
    ws = workspace_id or store.workspace_id
    store.set_workspace_settings(ws, {
        OFF_SETTING: ",".join(sorted(prefs.off)),
        KINDS_OFF_SETTING: ",".join(sorted(prefs.kinds_off)),
        QUIET_SETTING: f"{prefs.quiet[0]:%H:%M}-{prefs.quiet[1]:%H:%M}" if prefs.quiet else "",
        QUIET_DAYS_SETTING: ",".join(d for d in DAYS if d in prefs.quiet_days),
    })
    return load(store, ws)


def toggle(prefs: Prefs, name: str) -> Prefs:
    if name not in GROUPS and name not in TOGGLES:
        raise PrefsError(f"Нет группы «{name}». Есть: {', '.join(list(GROUPS) + list(TOGGLES))}")
    prefs.off ^= {name}
    return prefs


def filter_note(note, prefs: Prefs):
    """Убрать из уведомления синка выключенное: упоминания и «Участвую» (по ролям)."""
    if not prefs.on("mentions"):
        note.mentions = []
    if not prefs.on("participate") and note.roles:
        mine = lambda key: bool(note.roles.get(key) and note.roles[key].mine)  # noqa: E731
        note.deltas = [d for d in note.deltas if mine(d.key)]
        note.mentions = [m for m in note.mentions if mine(m.task_key)]
    return note


# --------------------------------------------------------------------------- #
# Доставка с тихими часами
# --------------------------------------------------------------------------- #


def held(store: "Store") -> list[dict]:
    try:
        return list(json.loads(store.get_workspace_meta(HELD_META) or "[]"))
    except ValueError:
        return []


def deliver(store: "Store", sender, text: str, *, prefs: Optional[Prefs] = None,
            now: Optional[datetime] = None) -> str:
    """Отправить или, в тихие часы, отложить. → «sent» | «held»."""
    prefs = prefs or load(store)
    if prefs.is_quiet(now):
        items = held(store)
        items.append({"ts": (now or datetime.now()).strftime("%d.%m %H:%M"), "text": text})
        store.set_workspace_meta(HELD_META, json.dumps(items[-HELD_MAX:], ensure_ascii=False))
        return "held"
    flush(store, sender, prefs=prefs, now=now)      # сначала накопленное — порядок событий
    sender.send(text)
    return "sent"


def flush(store: "Store", sender, *, prefs: Optional[Prefs] = None, now: Optional[datetime] = None) -> int:
    """Тишина кончилась — накопленное одной сводкой. → сколько было отложено."""
    prefs = prefs or load(store)
    items = held(store)
    if not items or prefs.is_quiet(now):
        return 0
    head = (f"🌙 <b>Пока было тихо</b> ({prefs.quiet_label()}) — {len(items)} "
            f"{'уведомление' if len(items) == 1 else 'уведомлений' if len(items) >= 5 else 'уведомления'}")
    body = "\n\n".join(f"<i>{i['ts']}</i>\n{i['text']}" for i in items)
    sender.send(head + "\n\n" + body, silent=True)
    store.set_workspace_meta(HELD_META, "[]")
    return len(items)


# --------------------------------------------------------------------------- #
# Меню в боте
# --------------------------------------------------------------------------- #

CALLBACK_PREFIX = "np:"
QUIET_PRESETS = (("22:00-09:00", "22–09"), ("23:00-08:00", "23–08"), ("00:00-09:00", "00–09"), ("", "выкл"))


def _btn(text: str, ws_id: int, action: str) -> dict:
    return {"text": text, "callback_data": f"{CALLBACK_PREFIX}{ws_id}:{action}"}


def workspaces_menu(store: "Store") -> tuple[str, dict]:
    rows = [[{"text": f"🔔 {w.name or w.slug}", "callback_data": f"{CALLBACK_PREFIX}{w.id}:show"}]
            for w in store.list_workspaces() if not w.archived]
    return "🔔 <b>Уведомления</b> — выбери воркспейс:", {"inline_keyboard": rows}


def menu(store: "Store", ws_id: int) -> tuple[str, dict]:
    ws = store.get_workspace(ws_id)
    p = load(store, ws_id)
    lines = [f"🔔 <b>Уведомления · {ws.name or ws.slug if ws else ws_id}</b>", "",
             f"🌙 Не беспокоить: {p.quiet_label()}" + (" · сейчас тихо" if p.is_quiet() else "")]
    if held(store) and ws_id == store.workspace_id:
        lines.append(f"Отложено: {len(held(store))}")
    lines += ["", "Нажми группу, чтобы включить или выключить. Запросы допуска git и ответы на твои "
                  "команды приходят всегда."]
    rows = [[_btn(("✅ " if p.on(g) else "🔕 ") + title, ws_id, f"t:{g}")] for g, (title, _) in GROUPS.items()]
    rows += [[_btn(("✅ " if p.on(t) else "🔕 ") + title, ws_id, f"t:{t}")] for t, title in TOGGLES.items()]
    cur = f"{p.quiet[0]:%H:%M}-{p.quiet[1]:%H:%M}" if p.quiet else ""
    rows.append([_btn(("● " if cur == val else "") + "🌙 " + label, ws_id, f"q:{val or 'off'}")
                 for val, label in QUIET_PRESETS])
    weekend = {"sat", "sun"} <= p.quiet_days
    rows.append([_btn(("✅ " if weekend else "") + "🌙 Выходные целиком", ws_id, "w")])
    rows.append([{"text": "← воркспейсы", "callback_data": f"{CALLBACK_PREFIX}list"}])
    return "\n".join(lines), {"inline_keyboard": rows}


def handle_callback(store: "Store", data: str) -> tuple[str, dict]:
    rest = data[len(CALLBACK_PREFIX):]
    if rest == "list":
        return workspaces_menu(store)
    try:
        ws_part, action = rest.split(":", 1)
        ws_id = int(ws_part)
    except ValueError:
        return "Кнопка устарела.", {}
    if store.get_workspace(ws_id) is None:
        return "Такого воркспейса нет.", {}
    p = load(store, ws_id)
    if action.startswith("t:"):
        save(store, toggle(p, action[2:]), ws_id)
    elif action.startswith("q:"):
        val = action[2:]
        p.quiet = None if val == "off" else parse_quiet(val)
        save(store, p, ws_id)
    elif action == "w":
        p.quiet_days = set() if {"sat", "sun"} <= p.quiet_days else (p.quiet_days | {"sat", "sun"})
        save(store, p, ws_id)
    return menu(store, ws_id)
