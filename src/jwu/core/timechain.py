"""Цепочка ворклогов: время дня одним сплошным отрезком.

Пользователь говорит «с 10:00 по МСК» и список «задача — сколько». Каждый ворклог
начинается ровно там, где закончился предыдущий: затреканное за день получается
последовательным, без дыр и наложений — так его и видит таймшит.

Часовой пояс — настройка контура (``worklog.timezone``, IANA-имя вроде Europe/Moscow):
спрашивается один раз, дальше скилл спрашивает только начало. Время ``started`` уходит
в Jira в её формате ``2026-09-30T10:00:00.000+0300`` — со смещением пояса, поэтому
Jira покажет его правильно независимо от настроек профиля.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from .store import Store

TZ_SETTING = "worklog.timezone"
# Короткие имена, которыми пользователь называет пояс, → IANA
TZ_ALIASES = {
    "мск": "Europe/Moscow", "msk": "Europe/Moscow", "москва": "Europe/Moscow",
    "екб": "Asia/Yekaterinburg", "екатеринбург": "Asia/Yekaterinburg",
    "нск": "Asia/Novosibirsk", "новосибирск": "Asia/Novosibirsk",
    "калининград": "Europe/Kaliningrad", "utc": "UTC",
}
HOURS_PER_DAY = 8  # «1d» в Jira по умолчанию — рабочий день из 8 часов

_DUR_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([dhm])", re.I)


class ChainError(ValueError):
    pass


def resolve_tz(name: str) -> ZoneInfo:
    key = (name or "").strip()
    key = TZ_ALIASES.get(key.casefold(), key)
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ChainError(f"Не знаю часовой пояс «{name}» — нужен вида Europe/Moscow или МСК") from exc


def get_timezone(store: "Store", workspace_id: int) -> str:
    return (store.workspace_settings(workspace_id).get(TZ_SETTING) or "").strip()


def set_timezone(store: "Store", workspace_id: int, name: str) -> str:
    tz = resolve_tz(name)
    store.set_workspace_settings(workspace_id, {TZ_SETTING: tz.key})
    return tz.key


def parse_duration(text: str) -> int:
    """«1h 30m», «45m», «1.5h», «1d 2h» → секунды. Пусто или мусор — ошибка."""
    s = (text or "").strip()
    parts = _DUR_RE.findall(s)
    if not parts or _DUR_RE.sub("", s).strip():
        raise ChainError(f"Не понял длительность «{text}» — нужно вида «1h 30m», «45m»")
    unit = {"d": HOURS_PER_DAY * 3600, "h": 3600, "m": 60}
    total = sum(float(n.replace(",", ".")) * unit[u.lower()] for n, u in parts)
    if total <= 0:
        raise ChainError(f"Нулевая длительность «{text}»")
    return int(round(total))


def fmt_duration(seconds: int) -> str:
    """Секунды → формат Jira: «1h 30m», «45m»."""
    h, m = divmod(int(seconds) // 60, 60)
    return " ".join(p for p in (f"{h}h" if h else "", f"{m}m" if m else "") if p) or "0m"


def jira_started(dt: datetime) -> str:
    """Aware datetime → формат ``started`` Jira: 2026-09-30T10:00:00.000+0300."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def to_jira_started(value: str, tz_name: str) -> str:
    """Начало ворклога → формат Jira. ISO со смещением берётся как есть; «YYYY-MM-DD HH:MM»
    (или с «T») — в поясе ``tz_name`` (пусто — ошибка: без пояса время уедет не туда)."""
    raw = (value or "").strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%f%z")
        except ValueError as exc:
            raise ChainError(f"Не понял начало «{value}» — нужно «2026-09-30 10:00»") from exc
    if dt.tzinfo is None:
        if not tz_name:
            raise ChainError("Часовой пояс для трекинга не задан (jwu worklog-tz МСК)")
        dt = dt.replace(tzinfo=resolve_tz(tz_name))
    return jira_started(dt)


def parse_start(start: str, day: date, tz: ZoneInfo) -> datetime:
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*", start or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        raise ChainError(f"Не понял время начала «{start}» — нужно вида 10:00")
    return datetime.combine(day, time(int(m.group(1)), int(m.group(2) or 0)), tzinfo=tz)


@dataclass
class ChainItem:
    key: str
    time: str            # как ушло в Jira: «1h 30m»
    seconds: int
    comment: str
    started: str         # формат Jira со смещением
    start_local: str     # «10:00»
    end_local: str       # «11:30»

    def as_dict(self) -> dict:
        return asdict(self)


def plan(items: Iterable[dict], *, start: str, day: date, tz_name: str) -> list[ChainItem]:
    """Разложить задачи подряд от ``start``. ``items``: [{key, time, comment}]."""
    tz = resolve_tz(tz_name)
    cur = parse_start(start, day, tz)
    out: list[ChainItem] = []
    for raw in items:
        key = (raw.get("key") or "").strip()
        if not key:
            raise ChainError("У строки плана нет ключа задачи")
        sec = parse_duration(raw.get("time", ""))
        end = cur + timedelta(seconds=sec)
        out.append(ChainItem(key=key, time=fmt_duration(sec), seconds=sec,
                             comment=(raw.get("comment") or "").strip(),
                             started=jira_started(cur), start_local=cur.strftime("%H:%M"),
                             end_local=end.strftime("%H:%M") + ("" if end.date() == day else " (+1д)")))
        cur = end
    if not out:
        raise ChainError("План пустой")
    return out


def _parse_jira_dt(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    except (TypeError, ValueError):
        return None


def overlaps(chain: list[ChainItem], existing: dict[str, list[dict]]) -> list[dict]:
    """Уже затреканные записи дня, которые пересекаются с цепочкой по времени."""
    if not chain:
        return []
    c_start = _parse_jira_dt(chain[0].started)
    c_end = c_start + timedelta(seconds=sum(i.seconds for i in chain)) if c_start else None
    hits: list[dict] = []
    for key, entries in existing.items():
        for e in entries:
            s = _parse_jira_dt(e.get("started", ""))
            if s is None or c_start is None:
                continue
            f = s + timedelta(seconds=int(e.get("seconds") or 0))
            if s < c_end and f > c_start:
                tz = c_start.tzinfo
                hits.append({"key": key, "time": e.get("time", ""),
                             "from": s.astimezone(tz).strftime("%H:%M"),
                             "to": f.astimezone(tz).strftime("%H:%M"),
                             "comment": e.get("comment", "")})
    return hits


def latest_end(existing: dict[str, list[dict]], tz_name: str) -> str:
    """Когда закончилась последняя уже затреканная запись дня («HH:MM») — подсказка для начала."""
    tz = resolve_tz(tz_name)
    ends = []
    for entries in existing.values():
        for e in entries:
            s = _parse_jira_dt(e.get("started", ""))
            if s is not None:
                ends.append(s + timedelta(seconds=int(e.get("seconds") or 0)))
    return max(ends).astimezone(tz).strftime("%H:%M") if ends else ""
