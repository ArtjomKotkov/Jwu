"""Настройки уведомлений: какие события слать (по воркспейсу) и когда не беспокоить.

Что слать — по воркспейсу: группы (:data:`GROUPS`), внутри них отдельные виды событий, плюс
тумблеры «Упоминания» и «Участвую» (события по чужим задачам и PR, где я участник). Хранится в
настройках воркспейса: ``notify.off`` (выключенные группы и тумблеры), ``notify.kinds_off``.

Не беспокоить — несколько диапазонов («10:00-16:00,22:00-08:00») и дни целиком («sat,sun»).
Общие (для всех воркспейсов) лежат в meta базы (``notify.quiet`` / ``notify.quiet_days``),
у воркспейса — свои в настройках с теми же именами: пусто — как в общих, ``off`` — без тишины.
Время — местное, по часам компьютера (пояс ворклогов воркспейса на тишину не влияет).

Всё, что пришлось на тишину, копится (:func:`deliver`) и уходит одной сводкой, когда тишина
кончилась (:func:`flush`). Тишину пробивают только запросы допуска git и ответы бота на команды
пользователя — они идут мимо :func:`deliver`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .store import Store

OFF_SETTING = "notify.off"
KINDS_OFF_SETTING = "notify.kinds_off"
QUIET_SETTING = "notify.quiet"
QUIET_DAYS_SETTING = "notify.quiet_days"
HELD_META = "notify:held"          # [{"ts", "text"}] — накопленное за тихие часы (meta воркспейса)
AWAIT_META = "notify:await"        # ждём текстом диапазоны тишины: {"scope", "at"} (общая meta)
AWAIT_MINUTES = 10
HELD_MAX = 200
INHERIT, NONE = "", "off"

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
_RANGE_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*[-–—]\s*(\d{1,2})(?::(\d{2}))?$")

Range = tuple[time, time]


class PrefsError(ValueError):
    pass


def _csv(raw: str) -> set[str]:
    return {x.strip() for x in (raw or "").split(",") if x.strip()}


def parse_ranges(raw: str) -> list[Range]:
    """«10-16, 22-08» / «22:30-08:00» → [(10:00,16:00), (22:00,08:00)]; пусто/«off» → []."""
    raw = (raw or "").strip().lower()
    if raw in ("", "off", "-", "выкл", "нет"):
        return []
    raw = re.sub(r"\s*[-–—]\s*", "-", raw)
    out: list[Range] = []
    for part in re.split(r"[,;\s]+(?=\d)", raw):
        part = part.strip().strip(",;")
        if not part:
            continue
        m = _RANGE_RE.match(part.replace(" ", ""))
        if not m:
            raise PrefsError(f"«{part}»: нужен вид 22-08 или 22:30-08:00, несколько — через запятую")
        h1, m1, h2, m2 = int(m.group(1)), int(m.group(2) or 0), int(m.group(3)), int(m.group(4) or 0)
        if h1 == 24:
            h1 = 0
        if h2 == 24:
            h2 = 0
        if not (0 <= h1 < 24 and 0 <= h2 < 24 and m1 < 60 and m2 < 60) or (h1, m1) == (h2, m2):
            raise PrefsError(f"«{part}»: неверное время")
        out.append((time(h1, m1), time(h2, m2)))
    return out


def format_ranges(ranges: list[Range]) -> str:
    return ",".join(f"{a:%H:%M}-{b:%H:%M}" for a, b in ranges)


def _in_range(t: time, r: Range) -> bool:
    start, end = r
    return start <= t < end if start < end else (t >= start or t < end)


@dataclass
class Quiet:
    ranges: list[Range] = field(default_factory=list)
    days: set[str] = field(default_factory=set)

    def label(self) -> str:
        parts = [f"с {a:%H:%M} до {b:%H:%M}" for a, b in self.ranges]
        if self.days:
            parts.append(", ".join(DAY_TITLES[d] for d in DAYS if d in self.days) + " — весь день")
        return "; ".join(parts) or "выключено"

    def active(self, now: datetime) -> bool:
        if DAYS[now.weekday()] in self.days:
            return True
        return any(_in_range(now.time(), r) for r in self.ranges)

    @property
    def empty(self) -> bool:
        return not self.ranges and not self.days


@dataclass
class Prefs:
    off: set[str] = field(default_factory=set)
    kinds_off: set[str] = field(default_factory=set)
    quiet: Quiet = field(default_factory=Quiet)
    own_quiet: bool = False     # у воркспейса свои тихие часы (иначе — общие)

    def on(self, name: str) -> bool:
        return name not in self.off

    def kinds(self) -> list[str]:
        """Виды событий синка, которые слать."""
        from .notify import DEFAULT_KINDS

        muted = {k for g in GROUPS if g in self.off for k in GROUPS[g][1]} | self.kinds_off
        return [k for k in DEFAULT_KINDS if k not in muted]

    def kind_on(self, kind: str) -> bool:
        group = next((g for g, (_, ks) in GROUPS.items() if kind in ks), "")
        return kind not in self.kinds_off and (not group or self.on(group))

    def quiet_label(self) -> str:
        return self.quiet.label()

    def is_quiet(self, now: Optional[datetime] = None) -> bool:
        # местное время компьютера: пояс ворклогов воркспейса к тишине отношения не имеет
        return self.quiet.active(now or datetime.now())


# --------------------------------------------------------------------------- #
# Загрузка и запись
# --------------------------------------------------------------------------- #


def _read_quiet(ranges_raw: str, days_raw: str) -> Quiet:
    try:
        ranges = parse_ranges(ranges_raw)
    except PrefsError:
        ranges = []
    return Quiet(ranges=ranges, days={d for d in _csv(days_raw) if d in DAYS})


def load_global(store: "Store") -> Quiet:
    return _read_quiet(store.get_meta(QUIET_SETTING) or "", store.get_meta(QUIET_DAYS_SETTING) or "")


def save_global(store: "Store", quiet: Quiet) -> Quiet:
    store.set_meta(QUIET_SETTING, format_ranges(quiet.ranges))
    store.set_meta(QUIET_DAYS_SETTING, ",".join(d for d in DAYS if d in quiet.days))
    return load_global(store)


def load(store: "Store", workspace_id: Optional[int] = None) -> Prefs:
    ws = workspace_id or store.workspace_id
    s = store.workspace_settings(ws)
    raw_q, raw_d = s.get(QUIET_SETTING, INHERIT), s.get(QUIET_DAYS_SETTING, INHERIT)
    own = raw_q != INHERIT or raw_d != INHERIT
    quiet = _read_quiet("" if raw_q == NONE else raw_q, "" if raw_d == NONE else raw_d) if own else load_global(store)
    return Prefs(off=_csv(s.get(OFF_SETTING, "")), kinds_off=_csv(s.get(KINDS_OFF_SETTING, "")),
                 quiet=quiet, own_quiet=own)


def save(store: "Store", prefs: Prefs, workspace_id: Optional[int] = None) -> Prefs:
    ws = workspace_id or store.workspace_id
    values = {OFF_SETTING: ",".join(sorted(prefs.off)), KINDS_OFF_SETTING: ",".join(sorted(prefs.kinds_off))}
    if prefs.own_quiet:
        values[QUIET_SETTING] = format_ranges(prefs.quiet.ranges) or NONE
        values[QUIET_DAYS_SETTING] = ",".join(d for d in DAYS if d in prefs.quiet.days) or NONE
    else:
        values[QUIET_SETTING] = values[QUIET_DAYS_SETTING] = INHERIT
    store.set_workspace_settings(ws, values)
    return load(store, ws)


def toggle(prefs: Prefs, name: str) -> Prefs:
    if name not in GROUPS and name not in TOGGLES:
        raise PrefsError(f"Нет группы «{name}». Есть: {', '.join(list(GROUPS) + list(TOGGLES))}")
    prefs.off ^= {name}
    return prefs


def toggle_kind(prefs: Prefs, kind: str) -> Prefs:
    group = next((g for g, (_, ks) in GROUPS.items() if kind in ks), "")
    if not group:
        raise PrefsError(f"Нет такого события «{kind}»")
    if not prefs.on(group):                  # группа выключена — включаем её, остальные гасим
        prefs.off.discard(group)
        prefs.kinds_off |= set(GROUPS[group][1]) - {kind}
        prefs.kinds_off.discard(kind)
    else:
        prefs.kinds_off ^= {kind}
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
    n = len(items)
    word = "уведомление" if n % 10 == 1 and n % 100 != 11 else (
        "уведомления" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "уведомлений")
    head = f"🌙 <b>Пока было тихо</b> — {n} {word}"
    body = "\n\n".join(f"<i>{i['ts']}</i>\n{i['text']}" for i in items)
    sender.send(head + "\n\n" + body, silent=True)
    store.set_workspace_meta(HELD_META, "[]")
    return n


# --------------------------------------------------------------------------- #
# Меню в боте. Область тихих часов (scope): «g» — общие, иначе id воркспейса.
# --------------------------------------------------------------------------- #

CALLBACK_PREFIX = "np:"
PRESETS = (("22:00-08:00", "с 22 до 08"), ("23:00-09:00", "с 23 до 09"), ("00:00-09:00", "с 00 до 09"),
           ("13:00-14:00", "обед 13–14"))


def _cb(*parts) -> str:
    return CALLBACK_PREFIX + ":".join(str(p) for p in parts)


def _btn(text: str, *parts) -> dict:
    return {"text": text, "callback_data": _cb(*parts)}


def _summary(store: "Store", ws) -> str:
    p = load(store, ws.id)
    total = len(GROUPS) + len(TOGGLES)
    on = sum(p.on(g) for g in list(GROUPS) + list(TOGGLES))
    events = "всё включено" if on == total and not p.kinds_off else f"{on} из {total} групп" + (
        f", без {len(p.kinds_off)} событий" if p.kinds_off else "")
    return events + ("" if not p.own_quiet else f" · 🌙 свои: {p.quiet_label()}")


def home(store: "Store") -> tuple[str, dict]:
    g = load_global(store)
    lines = ["🔔 <b>Уведомления</b>", "", f"🌐 <b>Общие</b> — 🌙 не беспокоить: {g.label()}"]
    rows = [[_btn("🌐 Общие: не беспокоить", "g", "quiet")]]
    for w in store.list_workspaces():
        if w.archived:
            continue
        lines.append(f"🗂 <b>{w.name or w.slug}</b> — {_summary(store, w)}")
        rows.append([_btn(f"🗂 {w.name or w.slug}", w.id, "show")])
    return "\n".join(lines), {"inline_keyboard": rows}


def workspace_menu(store: "Store", ws_id: int) -> tuple[str, dict]:
    ws = store.get_workspace(ws_id)
    p = load(store, ws_id)
    lines = [f"🔔 <b>Уведомления · {ws.name or ws.slug}</b>", "",
             "Нажми группу — внутри видно каждое событие, его можно выключить.",
             f"🌙 Не беспокоить: {p.quiet_label()}" + ("" if p.own_quiet else " (как в общих)")]
    rows = []
    for g, (title, kinds) in GROUPS.items():
        on = sum(p.kind_on(k) for k in kinds)
        state = "выкл" if on == 0 else ("все" if on == len(kinds) else f"{on}/{len(kinds)}")
        rows.append([_btn(f"{'✅' if on else '🔕'} {title} · {state}", ws_id, "grp", g)])
    rows += [[_btn(("✅ " if p.on(t) else "🔕 ") + title, ws_id, "t", t)] for t, title in TOGGLES.items()]
    rows.append([_btn("🌙 Не беспокоить: " + ("свои" if p.own_quiet else "как в общих"), ws_id, "quiet")])
    rows.append([_btn("← все уведомления", "home")])
    return "\n".join(lines), {"inline_keyboard": rows}


def group_menu(store: "Store", ws_id: int, group: str) -> tuple[str, dict]:
    from .notify import NOTABLE_KINDS

    ws = store.get_workspace(ws_id)
    p = load(store, ws_id)
    title, kinds = GROUPS[group]
    lines = [f"{title} · <b>{ws.name or ws.slug}</b>", "", "Нажми событие, чтобы включить или выключить."]
    buttons = [_btn(("✅ " if p.kind_on(k) else "🔕 ") + NOTABLE_KINDS.get(k, k).split(" ", 1)[-1], ws_id, "k", k)
               for k in kinds]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([_btn("🔕 Выключить всю группу" if p.on(group) else "✅ Включить всю группу", ws_id, "ga", group)])
    rows.append([_btn("← " + (ws.name or ws.slug), ws_id, "show")])
    return "\n".join(lines), {"inline_keyboard": rows}


def quiet_menu(store: "Store", scope: str) -> tuple[str, dict]:
    if scope == "g":
        q, title, own = load_global(store), "общие — для всех воркспейсов", True
        back = _btn("← все уведомления", "home")
    else:
        ws = store.get_workspace(int(scope))
        p = load(store, int(scope))
        q, title, own = p.quiet, (ws.name or ws.slug), p.own_quiet
        back = _btn("← " + (ws.name or ws.slug), scope, "show")
    lines = [f"🌙 <b>Не беспокоить · {title}</b>", "",
             f"Сейчас: {q.label()}" + ("" if own else " (как в общих)"), "",
             "В эти часы уведомления не приходят, а копятся — и приходят одним сообщением, когда "
             "тишина кончится. Запросы допуска git и ответы на твои команды — всегда.",
             "Свои часы — «✏️ Ввести свои»: например <code>10-16, 22-08</code>."]
    cur = format_ranges(q.ranges)
    presets = [_btn(("● " if cur == val else "") + label, scope, "qp", val.replace(":", "")) for val, label in PRESETS]
    rows = [presets[:2], presets[2:]]
    rows.append([_btn("✏️ Ввести свои", scope, "qt")])
    weekend = {"sat", "sun"} <= q.days
    rows.append([_btn(("✅ " if weekend else "") + "Выходные целиком", scope, "qw")])
    rows.append([_btn("Выключить", scope, "qoff")] + ([] if scope == "g" or not own else
                                                     [_btn("Как в общих", scope, "qinh")]))
    rows.append([back])
    return "\n".join(lines), {"inline_keyboard": rows}


def _set_quiet(store: "Store", scope: str, quiet: Quiet) -> None:
    if scope == "g":
        save_global(store, quiet)
        return
    p = load(store, int(scope))
    p.quiet, p.own_quiet = quiet, True
    save(store, p, int(scope))


def _get_quiet(store: "Store", scope: str) -> Quiet:
    return load_global(store) if scope == "g" else load(store, int(scope)).quiet


def handle_callback(store: "Store", data: str) -> tuple[str, dict]:
    parts = data[len(CALLBACK_PREFIX):].split(":")
    if parts == ["home"] or parts == ["list"]:
        return home(store)
    if len(parts) < 2:
        return "Кнопка устарела.", {}
    scope, action, arg = parts[0], parts[1], ":".join(parts[2:])
    if scope != "g":
        if not scope.isdigit() or store.get_workspace(int(scope)) is None:
            return "Кнопка устарела.", {}
    try:
        if action == "show" and scope != "g":
            return workspace_menu(store, int(scope))
        if action == "grp" and arg in GROUPS:
            return group_menu(store, int(scope), arg)
        if action in ("k", "ga", "t") and scope != "g":
            ws_id = int(scope)
            p = load(store, ws_id)
            if action == "k":
                save(store, toggle_kind(p, arg), ws_id)
                group = next(g for g, (_, ks) in GROUPS.items() if arg in ks)
                return group_menu(store, ws_id, group)
            if action == "ga":
                if p.on(arg):
                    p.off.add(arg)
                else:
                    p.off.discard(arg)
                    p.kinds_off -= set(GROUPS[arg][1])
                save(store, p, ws_id)
                return group_menu(store, ws_id, arg)
            save(store, toggle(p, arg), ws_id)
            return workspace_menu(store, ws_id)
        if action == "quiet":
            return quiet_menu(store, scope)
        q = _get_quiet(store, scope)
        if action == "qp":
            q.ranges = parse_ranges(arg[:2] + ":" + arg[2:4] + "-" + arg[5:7] + ":" + arg[7:9]) if arg else []
            _set_quiet(store, scope, q)
        elif action == "qw":
            q.days = q.days - {"sat", "sun"} if {"sat", "sun"} <= q.days else q.days | {"sat", "sun"}
            _set_quiet(store, scope, q)
        elif action == "qoff":
            _set_quiet(store, scope, Quiet())
        elif action == "qinh" and scope != "g":
            p = load(store, int(scope))
            p.own_quiet = False
            save(store, p, int(scope))
        elif action == "qt":
            store.set_meta(AWAIT_META, json.dumps({"scope": scope, "at": datetime.now(timezone.utc).isoformat()}))
            return ("✏️ Пришли часы тишины одним сообщением, например:\n<code>10-16, 22-08</code>\n"
                    "или <code>22:30-07:45</code>. «off» — выключить. Жду 10 минут.",
                    {"inline_keyboard": [[_btn("Отмена", scope, "quiet")]]})
        return quiet_menu(store, scope)
    except PrefsError as exc:
        return f"⚠️ {exc}", {"inline_keyboard": [[_btn("← назад", scope, "quiet")]]}


def take_text_input(store: "Store", text: str) -> Optional[tuple[str, dict]]:
    """Сообщение боту, пока ждём часы тишины текстом → применить и вернуть меню; не ждём — None."""
    raw = store.get_meta(AWAIT_META) or ""
    if not raw or text.startswith("/"):
        return None
    try:
        pending = json.loads(raw)
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(pending["at"])).total_seconds() / 60
    except (ValueError, KeyError, TypeError):
        store.set_meta(AWAIT_META, "")
        return None
    if age > AWAIT_MINUTES:
        store.set_meta(AWAIT_META, "")
        return None
    scope = str(pending.get("scope", "g"))
    try:
        ranges = parse_ranges(text)
    except PrefsError as exc:
        return f"⚠️ {exc}. Попробуй ещё раз или нажми «Отмена».", {"inline_keyboard": [[_btn("Отмена", scope, "quiet")]]}
    store.set_meta(AWAIT_META, "")
    q = _get_quiet(store, scope)
    q.ranges = ranges
    _set_quiet(store, scope, q)
    text_out, markup = quiet_menu(store, scope)
    return "✅ Сохранил.\n\n" + text_out, markup
