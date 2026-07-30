"""Обслуживание БД: защита от облака, ежедневный бэкап и чистка снапшотов.

Три задачи:
- ``ensure_db_available`` — не дать открыть БД, которую iCloud выгрузил в плейсхолдер
  (иначе sqlite создал бы поверх пустую базу, и облако затёрло бы реальную).
- ``run_daily_maintenance`` — раз в день проверять целостность и делать ЛОКАЛЬНЫЙ бэкап
  (не в облаке — чтобы пережить порчу синхронизации).
- ``run_daily_prune`` — раз в день сносить старые снапшоты, если база разрослась.
  Идёт СТРОГО после бэкапа: чистка необратима, и откатываться должно быть куда.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from .config import ConfigError, data_dir

if TYPE_CHECKING:  # только для аннотаций: core.store импортирует config, не наоборот
    from .store import Store


def _restrict(path: Path, mode: int) -> None:
    """Ужать права (700 на каталог бэкапов, 600 на копии). Не вышло — не критично."""
    try:
        path.chmod(mode)
    except OSError:
        pass


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


# Папки файловых облаков: БД с секретами в открытом виде им отдавать не стоит.
_CLOUD_MARKERS = (
    "Mobile Documents", "iCloud Drive", "iCloudDrive",
    "Dropbox", "Google Drive", "GoogleDrive", "OneDrive", "YandexDisk", "Яндекс.Диск",
)


def warn_if_cloud_path(db_file: Path) -> list[str]:
    """Предупреждения, если БД (а в ней — секреты плайнтекстом) лежит в облачной папке."""
    parts = set(Path(db_file).expanduser().parts)
    hit = next((m for m in _CLOUD_MARKERS if m in parts), None)
    if not hit:
        return []
    return [
        f"БД лежит в облачной папке ({hit}): {db_file}. В ней хранятся токены и пароли "
        "в открытом виде — они уедут в облако. Перенеси БД локально "
        "(jwu configure --db-path ~/.local/share/jwu/state.db) либо держи секреты "
        "в переменных окружения."
    ]


def backup_before_migration(
    db_file: Path, *, to_version: int, backups_dir: Path | None = None
) -> Path | None:
    """Снять копию БД перед структурной миграцией схемы; вернуть путь копии или None.

    Копия делается через sqlite backup API (консистентно даже при активном WAL) и НЕ попадает
    под ротацию ежедневных бэкапов (другое имя) — она должна пережить любые последующие
    проблемы. Пустую только что созданную БД не копируем.
    """
    if not db_file.exists() or db_file.stat().st_size == 0:
        return None
    bdir = backups_dir or (data_dir() / "backups")
    bdir.mkdir(parents=True, exist_ok=True)
    dest = bdir / f"{db_file.name}.pre-v{to_version}-{date.today().isoformat()}"
    if dest.exists():
        return dest
    src = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    _restrict(bdir, 0o700)
    _restrict(dest, 0o600)
    return dest


def run_daily_maintenance(
    db_file: Path, *, backups_dir: Path | None = None, keep: int = 7
) -> list[str]:
    """Раз в день: quick_check + локальный бэкап БД, чистка старше ``keep`` копий.

    Бэкапы кладутся в ЛОКАЛЬНЫЙ каталог (по умолчанию ``data_dir()/backups``), а не рядом
    с БД — чтобы они не уезжали в iCloud и пережили порчу синка. Возвращает короткие
    сообщения для вывода (или []). Битую БД не бэкапит (чтобы не плодить мусор).

    ВНИМАНИЕ: копия содержит секреты воркспейсов в открытом виде (они хранятся в БД),
    поэтому каталог и файлы закрываются правами 700/600.
    """
    if not db_file.exists():
        return []
    bdir = backups_dir or (data_dir() / "backups")
    bdir.mkdir(parents=True, exist_ok=True)
    _restrict(bdir, 0o700)
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
    _restrict(marker, 0o600)
    for old in sorted(bdir.glob(f"{db_file.name}.bak-*"))[:-keep]:
        old.unlink()
    return [f"бэкап БД: {marker.name}"]


# Порог, ниже которого чистку не запускаем вовсе: на маленькой базе снапшоты никому
# не мешают, а необратимая операция без нужды — плохой размен.
AUTO_PRUNE_MIN_BYTES = 256 * 1024 * 1024
AUTO_PRUNE_DAYS = 30
# VACUUM пересобирает файл целиком (для гигабайта — десятки секунд и двойной объём
# на диске), поэтому только когда освободилось действительно много.
VACUUM_MIN_FREE_RATIO = 0.25
_PRUNE_META_KEY = "last_prune"


def _human_bytes(n: int) -> str:
    size = float(max(0, n))
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{int(size)} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} Б"


def run_daily_prune(
    store: "Store",
    db_file: Path,
    *,
    backups_dir: Path | None = None,
    min_bytes: int = AUTO_PRUNE_MIN_BYTES,
    days: int = AUTO_PRUNE_DAYS,
) -> list[str]:
    """Раз в день снести старые снапшоты, если база переросла порог. Сообщения — наружу.

    Порядок здесь важнее самой чистки:

    1. база меньше порога — не трогаем ничего (обычный случай, выходим молча);
    2. сегодня уже чистили — выходим;
    3. **сегодняшнего бэкапа нет — не чистим**. Удаление снапшотов необратимо, а бэкап
       делает ``run_daily_maintenance``; его же он пропускает на битой БД — значит
       отсутствие копии это ещё и сигнал «с базой что-то не так, не усугубляй».

    VACUUM зовём только если после чистки освободилась заметная доля файла.
    """
    if store.db_size() < min_bytes:
        return []
    today = date.today().isoformat()
    if store.get_meta(_PRUNE_META_KEY) == today:
        return []
    bdir = backups_dir or (data_dir() / "backups")
    if not (bdir / f"{db_file.name}.bak-{today}").exists():
        return ["чистку снапшотов пропустил: сегодняшнего бэкапа БД нет"]

    reports = store.prune_all_workspaces(days=days, dry_run=False)
    store.set_meta(_PRUNE_META_KEY, today)
    removed = sum(r.total for r in reports.values())
    if not removed:
        return []
    msgs = [f"чистка снапшотов старше {days} дн.: удалено {removed} записей"]
    if store.free_ratio() >= VACUUM_MIN_FREE_RATIO:
        freed = store.vacuum()
        msgs.append(f"VACUUM: файл БД похудел на {_human_bytes(freed)}")
    return msgs
