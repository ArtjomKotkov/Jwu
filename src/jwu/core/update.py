"""Обновление jwu: проверить релиз и переставить jwu вместе с аддонами.

Проверка — последний релиз GitHub-репозитория jwu против исполняемой версии. Обновление —
теми же руками, что и скилл jwu-update, но одной командой:

1. копия БД (``state.db.pre-update-<версия>-<дата>`` в каталоге бэкапов);
2. ``pipx install --force`` — из тега релиза на GitHub, а если jwu стоит из папки (разработка),
   то из той же папки;
3. аддоны, вколотые в venv jwu (``pipx inject``), — обратно, с теми же источниками и ``-e``:
   переустановка venv их теряет;
4. ``jwu install`` (скиллы, субагенты, MCP) и перезапуск демона с прежним интервалом.

Запущенные MCP-серверы в сессиях остаются на старом коде до ``/mcp``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from .. import __version__

REPO = "ArtjomKotkov/Jwu"
CHECK_META = "update:checked"         # когда последний раз смотрели релиз (дата) — глобальный meta
NOTIFIED_META = "update:notified"     # о какой версии уже сказали в бот
SNOOZE_META = "update:snooze_until"   # «отложить на 3 дня» — до какой даты молчим
SNOOZE_DAYS = 3
CALLBACK_PREFIX = "upd:"

Runner = Callable[[list[str]], tuple[int, str]]


class UpdateError(RuntimeError):
    pass


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def repo() -> str:
    return os.environ.get("JWU_UPDATE_REPO") or REPO


# --------------------------------------------------------------------------- #
# Версии: 3.0.0 > 3.0.0.dev2 > 3.0.0.dev1 > 2.15.0
# --------------------------------------------------------------------------- #

_VER = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.?(dev|a|b|rc)(\d*))?")
_PRE = {"dev": 0, "a": 1, "b": 2, "rc": 3}


def version_key(text: str) -> tuple:
    m = _VER.match((text or "").strip())
    if not m:
        return (0, 0, 0, 9, 0)
    major, minor, patch, pre, num = m.groups()
    return (int(major), int(minor or 0), int(patch or 0), _PRE.get(pre, 9), int(num or 0))


def is_newer(candidate: str, current: str) -> bool:
    return version_key(candidate) > version_key(current)


# --------------------------------------------------------------------------- #
# Проверка
# --------------------------------------------------------------------------- #


@dataclass
class Release:
    version: str
    tag: str
    url: str = ""
    notes: str = ""


def latest_release(client=None) -> Release:
    """Последний релиз на GitHub (без черновиков и пре-релизов)."""
    from .http import new_client

    own = client is None
    client = client or new_client(timeout=15)
    try:
        resp = client.get(f"https://api.github.com/repos/{repo()}/releases/latest",
                          headers={"Accept": "application/vnd.github+json"})
    except Exception as exc:  # noqa: BLE001
        raise UpdateError(f"GitHub недоступен: {exc}") from exc
    finally:
        if own:
            client.close()
    if resp.status_code >= 400:
        raise UpdateError(f"GitHub ответил {resp.status_code} на запрос релиза {repo()}")
    data = resp.json()
    tag = str(data.get("tag_name") or "")
    return Release(version=tag.lstrip("v"), tag=tag, url=str(data.get("html_url") or ""),
                   notes=str(data.get("body") or "").strip())


def check(client=None, *, current: str = __version__) -> dict:
    rel = latest_release(client)
    return {"current": current, "latest": rel.version, "tag": rel.tag, "newer": is_newer(rel.version, current),
            "url": rel.url, "notes": rel.notes}


# --------------------------------------------------------------------------- #
# Как стоит jwu (pipx)
# --------------------------------------------------------------------------- #


@dataclass
class Install:
    source: str                      # путь до папки или URL, откуда ставили jwu
    injected: list[dict] = field(default_factory=list)   # [{name, source, editable}]

    @property
    def from_folder(self) -> bool:
        return bool(self.source) and Path(self.source).expanduser().is_dir()


def current_install(runner: Runner = _run) -> Install:
    code, out = runner(["pipx", "list", "--json"])
    if code != 0:
        raise UpdateError(f"pipx не ответил: {out[-300:]}")
    try:
        meta = json.loads(out)["venvs"]["jwu"]["metadata"]
    except (ValueError, KeyError) as exc:
        raise UpdateError("jwu не найден среди пакетов pipx — ставили не через pipx?") from exc
    main = meta.get("main_package") or {}
    injected = []
    for name, pkg in (meta.get("injected_packages") or {}).items():
        injected.append({"name": name, "source": str(pkg.get("package_or_url") or name),
                         "editable": "--editable" in (pkg.get("pip_args") or []) or "-e" in (pkg.get("pip_args") or [])})
    return Install(source=str(main.get("package_or_url") or ""), injected=injected)


# --------------------------------------------------------------------------- #
# Обновление
# --------------------------------------------------------------------------- #


def _backup_db(version: str) -> Optional[Path]:
    from .config import data_dir, db_path
    from .maintenance import _sqlite_copy

    db = db_path()
    if not db.exists():
        return None
    bdir = data_dir() / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    dest = bdir / f"{db.name}.pre-update-{version}-{date.today().isoformat()}"
    if not dest.exists():
        _sqlite_copy(db, dest)
    return dest


def daemon_interval() -> Optional[int]:
    """Интервал установленной службы демона (из plist/unit) или None — демона нет."""
    from . import daemon

    path = daemon.plist_path() if daemon.is_macos() else daemon.systemd_unit_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"--interval(?:</string>\s*<string>|\s+)(\d+)", text)
    return int(m.group(1)) if m else daemon.DEFAULT_INTERVAL


def plan(target: Optional[Release], inst: Install) -> list[list[str]]:
    """Команды обновления по порядку (без бэкапа — его делает run)."""
    if inst.from_folder:
        spec = inst.source
    elif target is not None:
        spec = f"git+https://github.com/{repo()}.git@{target.tag}"
    else:
        raise UpdateError("Нет ни папки, откуда стоит jwu, ни релиза — ставить нечего")
    cmds = [["pipx", "install", "--force", spec]]
    for pkg in inst.injected:
        cmds.append(["pipx", "inject", "--force", "jwu", *(["-e"] if pkg["editable"] else []), pkg["source"]])
    jwu = shutil.which("jwu") or "jwu"
    cmds.append([jwu, "install"])
    interval = daemon_interval()
    if interval is not None:
        cmds.append([jwu, "daemon", "install", "--interval", str(interval)])
    return cmds


def run(target: Optional[Release], *, runner: Runner = _run, log: Callable[[str], None] = print,
        inst: Optional[Install] = None) -> dict:
    """Обновить: бэкап → pipx → аддоны → jwu install → демон. Ошибка шага — стоп с UpdateError."""
    inst = inst or current_install(runner)
    cmds = plan(target, inst)
    backup = _backup_db(__version__)
    if backup:
        log(f"копия БД: {backup}")
    for cmd in cmds:
        log("$ " + " ".join(cmd))
        code, out = runner(cmd)
        if code != 0:
            raise UpdateError(f"{' '.join(cmd[:3])} упал: {out[-600:]}")
    code, out = runner([shutil.which("jwu") or "jwu", "--help"])
    if code != 0:
        raise UpdateError(f"jwu после обновления не запускается: {out[-600:]}")
    return {"ok": True, "from": __version__, "source": inst.source if inst.from_folder else (target.tag if target else ""),
            "addons": [p["name"] for p in inst.injected], "backup": str(backup or ""), "steps": len(cmds)}


# --------------------------------------------------------------------------- #
# Демон: раз в день проверить и спросить в боте
# --------------------------------------------------------------------------- #


def keyboard() -> dict:
    return {"inline_keyboard": [[{"text": "⬆️ Обновить", "callback_data": CALLBACK_PREFIX + "go"},
                                 {"text": f"⏰ Отложить на {SNOOZE_DAYS} дн.", "callback_data": CALLBACK_PREFIX + "snooze"}]]}


def _notes_short(notes: str, limit: int = 1500) -> str:
    text = notes.strip()
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n…"


def offer_text(info: dict) -> str:
    import html

    lines = [f"⬆️ <b>Вышел jwu {html.escape(info['latest'])}</b> · у тебя {html.escape(info['current'])}"]
    if info.get("notes"):
        lines += ["", html.escape(_notes_short(info["notes"]))]
    if info.get("url"):
        lines += ["", f'<a href="{html.escape(info["url"])}">Релиз на GitHub</a>']
    return "\n".join(lines)


def due_offer(store, *, today: Optional[date] = None, client=None) -> Optional[dict]:
    """Пора ли предложить обновление: раз в день смотрим релиз; о версии говорим один раз,
    «отложить» — молчим SNOOZE_DAYS дней. → info для offer_text или None."""
    today = today or date.today()
    if (store.get_meta(CHECK_META) or "") == today.isoformat():
        return None
    snooze = store.get_meta(SNOOZE_META) or ""
    if snooze and today.isoformat() < snooze:
        return None
    info = check(client)
    store.set_meta(CHECK_META, today.isoformat())
    if not info["newer"]:
        return None
    if (store.get_meta(NOTIFIED_META) or "") == info["latest"] and not snooze:
        return None                     # уже сказали и не откладывали — второй раз не надоедаем
    store.set_meta(NOTIFIED_META, info["latest"])
    store.set_meta(SNOOZE_META, "")
    return info


def snooze(store, *, today: Optional[date] = None) -> str:
    from datetime import timedelta

    until = (today or date.today()) + timedelta(days=SNOOZE_DAYS)
    store.set_meta(SNOOZE_META, until.isoformat())
    store.set_meta(CHECK_META, "")
    return until.isoformat()


def start_detached(*, notify: bool = True) -> int:
    """Запустить ``jwu update --yes`` отдельным процессом: он перезапустит и сам демон."""
    from .config import data_dir

    jwu = shutil.which("jwu") or "jwu"
    log_file = open(data_dir() / "update.log", "a", encoding="utf-8")   # noqa: SIM115 — живёт с процессом
    proc = subprocess.Popen([jwu, "update", "--yes", *(["--notify"] if notify else [])],
                            stdout=log_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=True)
    return proc.pid


def handle_callback(store, data: str) -> tuple[str, dict]:
    action = data[len(CALLBACK_PREFIX):]
    if action == "snooze":
        until = snooze(store)
        return f"⏰ Ок, напомню не раньше {date.fromisoformat(until).strftime('%d.%m')}.", {}
    if action == "go":
        pid = start_detached()
        return (f"⬆️ Обновляю (процесс {pid}). Демон перезапустится — итог пришлю сюда. "
                "В сессиях агента потом переподключи jwu (/mcp)."), {}
    return "Эта кнопка устарела.", {}


def announce(store, text: str, *, markup: Optional[dict] = None) -> int:
    """Сообщение во все чаты ботов (каждый чат — один раз, даже если он у нескольких контуров)."""
    from .daemon import _bot_senders

    seen: set[str] = set()
    for _ws, _cfg, sender, _login in _bot_senders(store):
        try:
            if str(sender.chat_id) in seen:
                continue
            seen.add(str(sender.chat_id))
            sender.send(text, markup=markup)
        except Exception:  # noqa: BLE001 — один чат лёг, остальным всё равно скажем
            pass
        finally:
            sender.close()
    return len(seen)


def offer_if_due(store) -> bool:
    """Задача демона: вышла новая версия — спросить в боте кнопками."""
    info = due_offer(store)
    if info is None:
        return False
    announce(store, offer_text(info), markup=keyboard())
    return True
