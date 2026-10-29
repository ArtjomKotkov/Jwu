"""Строка статуса Claude Code (``statusLine``): что jwu знает о текущей папке.

Claude Code на каждое обновление запускает команду из ``settings.json`` → ``statusLine``,
подаёт ей на stdin JSON о сессии (модель, папка, занятый контекст, стоимость) и печатает
вывод под строкой ввода. ``jwu statusline`` читает этот JSON и дописывает своё: воркспейс,
активную работу с последней фазой, очередь задач и живость демона; в сессии-оркестраторе
(``jwu worker orchestrator``) — ещё блок воркеров с их состоянием.

Команда дёргается часто, поэтому здесь только локальная БД и файл лока демона — без сети.
Любая ошибка глушится: сломанная строка статуса хуже пустой.

Интеграция с уже стоящей строкой (например, плагином claude-hud): ``install`` запоминает
прежнюю команду в глобальном meta и ставит ``jwu statusline``; та при запуске сначала
прогоняет прежнюю команду с тем же stdin и печатает её вывод, а своё — строкой ниже.
Без прежней команды jwu сам показывает модель, контекст и стоимость.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from . import workspaces
from .daemon import SingleInstance

if TYPE_CHECKING:
    from .store import Store

PREV_META_KEY = "statusline.prev"      # прежняя команда statusLine (глобальный meta)
OUR_COMMAND = "jwu statusline"      # по этому хвосту узнаём свою запись в settings.json
CHAIN_TIMEOUT = 3.0                    # сек на прежнюю команду; дольше — печатаем без неё
TITLE_MAX = 40                         # символов названия работы/фичи
REFRESH_SECONDS = 15                   # statusLine.refreshInterval: перерисовка без событий


# --------------------------------------------------------------------------- #
# Вход
# --------------------------------------------------------------------------- #


def parse_stdin(raw: str) -> dict:
    """JSON от Claude Code; мусор или пусто → {}. Строка статуса не должна падать."""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def session_cwd(stdin: dict) -> str:
    """Папка сессии из JSON (``cwd`` либо ``workspace.current_dir``), иначе cwd процесса."""
    ws = stdin.get("workspace") if isinstance(stdin.get("workspace"), dict) else {}
    return str(stdin.get("cwd") or ws.get("current_dir") or ws.get("project_dir") or Path.cwd())


# --------------------------------------------------------------------------- #
# Рендер
# --------------------------------------------------------------------------- #

_RESET = "\033[0m"


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}{_RESET}"


def _dim(text: str) -> str:
    return _c("2", text)


def _shorten(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fmt_duration(ms: Optional[float]) -> str:
    if not ms or ms <= 0:
        return ""
    minutes = int(ms // 60000)
    if minutes < 60:
        return f"{minutes}м"
    return f"{minutes // 60}ч{minutes % 60:02d}"


def claude_parts(stdin: dict) -> list[str]:
    """Что отдал сам Claude Code: модель, занятый контекст, стоимость, длительность."""
    parts: list[str] = []
    model = stdin.get("model") if isinstance(stdin.get("model"), dict) else {}
    name = (model.get("display_name") or model.get("id") or "").strip()
    if name:
        parts.append(_c("1;36", name))
    ctx = stdin.get("context_window") if isinstance(stdin.get("context_window"), dict) else {}
    pct = ctx.get("used_percentage")
    if isinstance(pct, (int, float)):
        color = "32" if pct < 60 else ("33" if pct < 85 else "31")
        parts.append(f"ctx {_c(color, f'{pct:.0f}%')}")
    cost = stdin.get("cost") if isinstance(stdin.get("cost"), dict) else {}
    usd = cost.get("total_cost_usd")
    if isinstance(usd, (int, float)) and usd > 0:
        parts.append(f"${usd:.2f}")
    dur = _fmt_duration(cost.get("total_duration_ms"))
    if dur:
        parts.append(dur)
    return parts


def _job_line(store: "Store") -> str:
    """Активная работа: якорь, название (фичи или работы) и значок последней фазы.

    Текст фазы не показываем: он длинный и технический («core/statusline.py…»),
    в строке статуса он читается как мусор. Значок — ✓ фаза закрыта, … в процессе.
    """
    label = _dim("работа   ")
    jobs = store.list_jobs(status="active")
    if not jobs:
        return label + _dim("нет активной")
    job = jobs[0]  # list_jobs отдаёт по updated_at DESC — берём ту, что трогали последней
    head = _c("1;33", f"#{job.id} {job.anchor}")
    title = job.title
    if job.feature_id:
        feature = store.get_feature(job.feature_id)
        if feature is not None and feature.title:
            title = feature.title
    phase = next((r for r in reversed(job.records) if r.kind == "phase"), None)
    mark = "" if phase is None else (_c("32", " ✓") if phase.status == "done" else _c("33", " …"))
    body = f"{head}{mark}  {_shorten(title, TITLE_MAX)}" if title else f"{head}{mark}"
    return label + body


def _queue_line(store: "Store") -> str:
    """Очередь: счётчики и текст пункта, который сейчас в основном потоке."""
    label = _dim("очередь  ")
    summary = store.queue_counts()
    c = summary["counts"]
    if not c["queued"] and not c["running"]:
        return label + _dim("пусто")
    bits = [f"ждёт {c['queued']}", f"идёт {c['running']}"]
    if summary["slots_busy"]:
        bits.append(f"слоты {summary['slots_busy']}/{summary['slots']}")
    line = label + _c("35", "  ·  ".join(bits))
    current = summary.get("main_running")
    if current:
        item = store.queue_get(current)
        if item is not None:
            line += _dim("   ▶ ") + _shorten(item.text, TITLE_MAX)
    return line


def _daemon_part(store: "Store | None" = None) -> str:
    try:
        alive = SingleInstance().holder_pid() is not None
    except Exception:  # noqa: BLE001 — лок может быть недоступен; это не повод падать
        alive = False
    out = f"демон {_c('32', '✓') if alive else _c('31', '✗')}"
    if alive and store is not None:
        try:
            from .daemon import pass_in_progress

            if pass_in_progress(store):
                out += " " + _c("33", "⏳ синк идёт")
        except Exception:  # noqa: BLE001
            pass
    return out


def _disk_part(store: "Store", workspace_id: int) -> str:
    """Места меньше порога — красным; иначе ничего (не занимаем строку лишним)."""
    try:
        from . import jobspace

        report = jobspace.check_space(store, workspace_id, with_docker=False)
    except Exception:  # noqa: BLE001
        return ""
    if report.ok:
        return ""
    return _dim("   ·   ") + _c("1;31", f"диск {report.free_gb:.0f} ГБ свободно!")


def jwu_lines(store: "Store", cwd: str) -> list[str]:
    """Что знает jwu о папке: воркспейс и демон, работа, очередь — по строке на каждое."""
    match = workspaces.workspace_for_path(store, cwd)
    if match is None:
        return [_dim("jwu      папка не привязана")]
    ws, _ = match
    store.use_workspace(ws.id)
    head = (_dim("jwu      ") + _c("1;34", ws.name or ws.slug) + _dim(f" ({ws.provider})")
            + _dim("   ·   ") + _daemon_part(store) + _disk_part(store, ws.id))
    return [head, _job_line(store), _queue_line(store)]


WORKERS_SHOWN = 5
_WORKER_COLORS = {"waiting": "1;33", "dead": "1;31", "stuck": "31", "silent": "2", "working": "36",
                  "starting": "2", "done": "32"}
_SUMMARY_WORDS = (("waiting", "ждут тебя"), ("dead", "упали"), ("stuck", "застряли"),
                  ("silent", "молчат"), ("working", "работают"), ("starting", "запускаются"),
                  ("done", "готовы"))


def workers_lines(store: "Store", session_id: str) -> list[str]:
    """Блок воркеров — только в сессии-оркестраторе (её id запомнил ``jwu worker orchestrator``)."""
    from . import workers

    boss = workers.orchestrator(store)
    if not session_id or boss.get("session_id") != session_id:
        return []
    items = workers.list_workers(store)
    label = _dim("воркеры  ")
    if not items:
        return [label + _dim("нет — «возьми … воркеру»")]
    counts = workers.summary(items)
    bits = [_c(_WORKER_COLORS[k], f"{workers.ICONS[k]} {counts[k]} {word}") for k, word in _SUMMARY_WORDS if counts.get(k)]
    lines = [label + f"{len(items)}" + _dim("  ·  ") + _dim("  ·  ").join(bits)]
    width = min(32, max(len(w["name"]) for w in items[:WORKERS_SHOWN]))
    for w in items[:WORKERS_SHOWN]:
        what = w.get("note") or w["state_text"]
        if w["state"] == "waiting":
            what = "ждёт: " + what if w.get("note") else what
        lines.append("  " + _c(_WORKER_COLORS.get(w["state"], "0"), w["icon"]) + " "
                     + _shorten(w["name"], width).ljust(width) + "  " + _shorten(what, TITLE_MAX)
                     + (_dim(f"  {w['ago']}") if w.get("ago") and w["state"] != "done" else ""))
    if len(items) > WORKERS_SHOWN:
        lines.append(_dim(f"  … ещё {len(items) - WORKERS_SHOWN}"))
    return lines


def render(stdin: dict, store: "Store", *, with_claude: bool) -> str:
    """Готовый блок строки статуса. ``with_claude=False`` — поверх стоит другая строка."""
    lines: list[str] = []
    if with_claude:
        claude = claude_parts(stdin)
        if claude:
            lines.append(_dim("   ·   ").join(claude))
    lines.extend(jwu_lines(store, session_cwd(stdin)))
    try:
        lines.extend(workers_lines(store, str(stdin.get("session_id") or "")))
    except Exception:  # noqa: BLE001 — строка статуса не падает
        pass
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Цепочка с прежней командой
# --------------------------------------------------------------------------- #


def previous_command(store: "Store") -> str:
    return (store.get_meta(PREV_META_KEY) or "").strip()


def run_previous(command: str, raw_stdin: str, cwd: str | None = None) -> str:
    """Вывод прежней строки статуса с тем же stdin; таймаут или падение → пусто."""
    if not command:
        return ""
    # Без TTY node/chalk и rich гасят цвета — просим их явно, как это сделал бы терминал.
    env = {**os.environ, "FORCE_COLOR": "1", "CLICOLOR_FORCE": "1"}
    try:
        proc = subprocess.run(
            command, shell=True, input=raw_stdin, capture_output=True, text=True,
            timeout=CHAIN_TIMEOUT, cwd=cwd or None, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.rstrip("\n")


# --------------------------------------------------------------------------- #
# Установка в settings.json Claude Code
# --------------------------------------------------------------------------- #


def settings_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "settings.json"


def _read_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise ValueError(f"{path}: не JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: ожидался объект JSON")
    return data


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def current_command(path: Path | None = None) -> str:
    entry = _read_settings(path or settings_path()).get("statusLine")
    if isinstance(entry, dict) and entry.get("type", "command") == "command":
        return str(entry.get("command") or "")
    return ""


def is_ours(command: str) -> bool:
    return command.strip().endswith(OUR_COMMAND)


def is_installed(path: Path | None = None) -> bool:
    return is_ours(current_command(path))


def our_command() -> str:
    """Команда для settings.json: абсолютный путь к ``jwu`` — PATH у Claude Code может
    быть урезан (claude-hud по той же причине пишет полный путь к node)."""
    exe = shutil.which("jwu")
    return f"{exe} statusline" if exe else OUR_COMMAND


def install(store: "Store", path: Path | None = None, *, chain: bool = True,
            command: str | None = None) -> str:
    """Поставить ``jwu statusline`` в statusLine. Прежняя команда — в цепочку (chain).

    Повторный вызов лишь обновляет команду (например, путь к jwu), не трогая запомненную.
    """
    path = path or settings_path()
    data = _read_settings(path)
    prev = current_command(path)
    if prev and not is_ours(prev):
        store.set_meta(PREV_META_KEY, prev if chain else "")
    elif not chain:
        store.set_meta(PREV_META_KEY, "")
    data["statusLine"] = {"type": "command", "command": command or our_command(),
                          # по таймеру, а не только по событиям: оркестратор видит воркеров,
                          # даже когда сам простаивает
                          "refreshInterval": REFRESH_SECONDS}
    _write_settings(path, data)
    kept = previous_command(store)
    return (f"statusLine → {OUR_COMMAND}; прежняя команда сохранена и печатается первой строкой"
            if kept else f"statusLine → {OUR_COMMAND}")


def uninstall(store: "Store", path: Path | None = None) -> str:
    """Вернуть прежнюю команду (если была) или убрать statusLine совсем."""
    path = path or settings_path()
    data = _read_settings(path)
    prev = previous_command(store)
    if prev:
        data["statusLine"] = {"type": "command", "command": prev}
        note = "statusLine возвращена к прежней команде"
    else:
        data.pop("statusLine", None)
        note = "statusLine убрана"
    _write_settings(path, data)
    store.set_meta(PREV_META_KEY, "")
    return note
