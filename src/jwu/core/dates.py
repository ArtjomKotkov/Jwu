"""Единые хелперы форматирования времени для CLI и TUI.

Везде показываем время как `ДД.ММ.ГГГГ ЧЧ:ММ` в локальной таймзоне. Никаких
ISO-строк наружу. Принимаем все ходовые входные форматы: ISO-строка, миллисекунды
(Bitbucket) или ``datetime``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Union

TimeLike = Union[str, int, float, datetime, None]

DT_FMT = "%d.%m.%Y %H:%M"


def _to_dt(value: TimeLike) -> "datetime | None":
    if value is None or value == "" or value == 0:
        return None
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, (int, float)):
        # Bitbucket отдаёт миллисекунды; обычные unix-таймстампы — секунды.
        seconds = float(value) / 1000.0 if float(value) > 1e12 else float(value)
        ts = datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        try:
            ts = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if ts.tzinfo is not None:
        ts = ts.astimezone()
    return ts


def fmt_dt(value: TimeLike, *, fallback: str = "—") -> str:
    """Время → `ДД.ММ.ГГГГ ЧЧ:ММ` в локальной TZ; пусто/невалидно → ``fallback``."""
    ts = _to_dt(value)
    return ts.strftime(DT_FMT) if ts is not None else fallback


def fmt_ago(value: TimeLike, *, fallback: str = "не синкано — нажми R") -> str:
    """Время → «N мин/ч/дн назад» (или «только что»). Пусто/невалидно → ``fallback``."""
    ts = _to_dt(value)
    if ts is None:
        return fallback
    now = datetime.now(ts.tzinfo or timezone.utc)
    mins = int((now - ts).total_seconds() // 60)
    if mins < 1:
        return "только что"
    if mins < 60:
        return f"{mins} мин назад"
    if mins < 60 * 24:
        return f"{mins // 60} ч назад"
    return f"{mins // 1440} дн назад"
