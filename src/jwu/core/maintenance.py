"""Обслуживание БД для синка через файловое облако (iCloud).

Две задачи:
- ``ensure_db_available`` — не дать открыть БД, которую iCloud выгрузил в плейсхолдер
  (иначе sqlite создал бы поверх пустую базу, и облако затёрло бы реальную).
- ``run_daily_maintenance`` — раз в день проверять целостность и делать ЛОКАЛЬНЫЙ бэкап
  (не в облаке — чтобы пережить порчу синхронизации).
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import date
from pathlib import Path

from .config import ConfigError, data_dir


def ensure_db_available(db_file: Path) -> None:
    """Бросить ConfigError, если БД отсутствует, но рядом лежит iCloud-плейсхолдер."""
    if db_file.exists():
        return
    placeholder = db_file.parent / f".{db_file.name}.icloud"
    if placeholder.exists():
        raise ConfigError(
            f"БД выгружена из iCloud (плейсхолдер {placeholder.name}). "
            "Открой файл в Finder, чтобы iCloud скачал его, и повтори команду — "
            "иначе будет создана пустая база поверх реальной."
        )
    # файла нет вовсе — это первый запуск; Store создаст новую БД (это ок)


def run_daily_maintenance(
    db_file: Path, *, backups_dir: Path | None = None, keep: int = 7
) -> list[str]:
    """Раз в день: quick_check + локальный бэкап БД, чистка старше ``keep`` копий.

    Бэкапы кладутся в ЛОКАЛЬНЫЙ каталог (по умолчанию ``data_dir()/backups``), а не рядом
    с БД — чтобы они не уезжали в iCloud и пережили порчу синка. Возвращает короткие
    сообщения для вывода (или []). Битую БД не бэкапит (чтобы не плодить мусор).
    """
    if not db_file.exists():
        return []
    bdir = backups_dir or (data_dir() / "backups")
    bdir.mkdir(parents=True, exist_ok=True)
    marker = bdir / f"{db_file.name}.bak-{date.today().isoformat()}"
    if marker.exists():
        return []  # сегодня уже делали

    try:
        con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            row = con.execute("PRAGMA quick_check").fetchone()
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        return [f"⚠ БД повреждена ({exc}); бэкап не делаю — проверь iCloud-синк"]
    if not row or row[0] != "ok":
        return [f"⚠ integrity_check: {row[0] if row else '?'}; бэкап пропущен"]

    shutil.copy2(db_file, marker)
    for old in sorted(bdir.glob(f"{db_file.name}.bak-*"))[:-keep]:
        old.unlink()
    return [f"бэкап БД: {marker.name}"]
