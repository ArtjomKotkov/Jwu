"""Фоновый синк без дашборда: цикл по воркспейсам + установка как службы (launchd/systemd).

До этого сетевой синк случался только тремя способами: руками (`jwu sync`), из TUI в
режиме `-a` и внутри `action day-analyze`. Закрыл дашборд — дельты перестали копиться,
уведомления некуда слать. Демон делает синк регулярным и независимым от открытого окна:
раз в ``interval`` секунд проходит по всем контурам с внешним провайдером, синкает и
даёт сработать хукам после синка (уведомления в мессенджер — см. ``core.notify``).

Устройство намеренно простое:

- один процесс на машину — файловый лок (``fcntl.flock``) в каталоге данных; второй
  экземпляр честно выходит, а не синкает параллельно;
- воркспейсы синкаются последовательно, ошибка одного не валит остальных (запись в лог
  и дальше); падение сети — обычная ситуация для ноутбука, а не повод умереть;
- сам процесс ничего не знает о TUI: дашборд с `-a` может работать параллельно, дельты
  считаются по снапшотам и от двойного синка не задваиваются.

Служба — launchd на macOS (``~/Library/LaunchAgents``), systemd --user на Linux. И то и
другое умеет перезапускать процесс и стартовать при входе; логи — в файл в каталоге данных.
"""

from __future__ import annotations

import fcntl
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

from .config import data_dir

if TYPE_CHECKING:  # только для аннотаций: не тянуть тяжёлые импорты при старте CLI
    from .models import Workspace
    from .service import Service, SyncResult
    from .store import Store

LAUNCHD_LABEL = "dev.jwu.daemon"
SYSTEMD_UNIT = "jwu-daemon.service"
DEFAULT_INTERVAL = 600  # секунд между проходами; отсчёт — от ОКОНЧАНИЯ прохода
MIN_INTERVAL = 60


class DaemonError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    """Строка лога с меткой времени в stderr (launchd/systemd перенаправят в файл)."""
    print(f"{_now()} {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Лок: один демон на машину
# --------------------------------------------------------------------------- #


def lock_path() -> Path:
    return data_dir() / "daemon.lock"


class SingleInstance:
    """Файловый лок на время жизни процесса. ``acquire()`` → False, если уже занят."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or lock_path()
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")  # noqa: SIM115 — держим открытым до release()
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def holder_pid(self) -> int | None:
        """PID процесса, записанный в лок (если файл есть и лок кем-то удерживается)."""
        try:
            text = self.path.read_text().strip()
        except OSError:
            return None
        if not text.isdigit():
            return None
        probe = open(self.path, "a+")  # noqa: SIM115
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return int(text)  # занят — демон жив
        else:
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            return None  # свободен — файл остался от умершего процесса
        finally:
            probe.close()

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None

    def __enter__(self) -> "SingleInstance":
        if not self.acquire():
            raise DaemonError(
                f"Демон уже запущен (pid {self.holder_pid() or '?'}, лок {self.path})."
            )
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# --------------------------------------------------------------------------- #
# Проход по воркспейсам
# --------------------------------------------------------------------------- #


@dataclass
class PassReport:
    """Итог одного прохода: что синкнули, что упало."""

    started_at: str
    finished_at: str = ""
    synced: list[str] = field(default_factory=list)          # slug'и с успешным синком
    skipped: list[str] = field(default_factory=list)         # локальные контуры
    failed: dict[str, str] = field(default_factory=dict)     # slug → текст ошибки
    deltas: int = 0

    def summary(self) -> str:
        parts = [f"синк: {len(self.synced)} ок"]
        if self.failed:
            parts.append(f"{len(self.failed)} с ошибкой ({', '.join(self.failed)})")
        if self.skipped:
            parts.append(f"{len(self.skipped)} пропущено")
        parts.append(f"дельт: {self.deltas}")
        return ", ".join(parts)


ServiceFactory = Callable[["Workspace"], "Service"]


def _default_factory(ws: "Workspace") -> "Service":
    from .service import Service

    return Service.for_workspace(ws)


def default_after_sync(svc: "Service", result: "SyncResult") -> None:
    """Хук по умолчанию: забрать ответы боту в Telegram и превратить их в заметки."""
    written = svc.poll_telegram_replies()
    if written:
        log(f"[{svc.workspace.slug if svc.workspace else '?'}] заметок из Telegram: {len(written)}")


def syncable(workspaces: Iterable["Workspace"]) -> tuple[list["Workspace"], list["Workspace"]]:
    """Разделить контуры на те, что есть чем синкать, и локальные."""
    todo: list["Workspace"] = []
    skipped: list["Workspace"] = []
    for ws in workspaces:
        if ws.archived:
            continue
        if ws.provider in ("jira", "github"):
            todo.append(ws)
        else:
            skipped.append(ws)
    return todo, skipped


def run_pass(
    store: "Store",
    *,
    factory: ServiceFactory | None = None,
    after_sync: Callable[["Service", "SyncResult"], None] | None = None,
) -> PassReport:
    """Один проход: синкнуть каждый внешний контур, вызвать ``after_sync`` по каждому.

    ``store`` — реестр воркспейсов (без скоупа). Сервис каждого контура открывает своё
    соединение с БД и закрывается сразу после синка: демон живёт часами, держать
    десяток открытых клиентов ради экономии логина не стоит — сессия Jira всё равно
    протухает быстрее.
    """
    factory = factory or _default_factory
    if after_sync is None:
        after_sync = default_after_sync
    report = PassReport(started_at=_now())
    todo, skipped = syncable(store.list_workspaces())
    report.skipped = [w.slug for w in skipped]
    for ws in todo:
        try:
            svc = factory(ws)
        except Exception as exc:  # noqa: BLE001 — нет кредов/сети: лог и дальше
            report.failed[ws.slug] = f"{type(exc).__name__}: {exc}"
            log(f"[{ws.slug}] сервис не создан: {exc}")
            continue
        try:
            result = svc.sync()
            report.synced.append(ws.slug)
            report.deltas += len(result.deltas)
            log(f"[{ws.slug}] синк #{result.run_id}: дельт {len(result.deltas)}")
            if after_sync is not None:
                try:
                    after_sync(svc, result)
                except Exception as exc:  # noqa: BLE001 — хук не должен ронять синк
                    log(f"[{ws.slug}] хук после синка упал: {exc}")
        except Exception as exc:  # noqa: BLE001
            report.failed[ws.slug] = f"{type(exc).__name__}: {exc}"
            log(f"[{ws.slug}] синк упал: {exc}")
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
    report.finished_at = _now()
    store.set_meta(LAST_PASS_META, report.finished_at)
    store.set_meta(LAST_PASS_SUMMARY_META, report.summary())
    return report


LAST_PASS_META = "daemon:last_pass"
LAST_PASS_SUMMARY_META = "daemon:last_summary"


class Waiter:
    """Пауза между проходами, которую можно прервать: SIGUSR1 → внеплановый проход."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def kick(self, *_args) -> None:
        self._event.set()

    def wait(self, seconds: float) -> bool:
        """True — разбудили раньше срока (kick), False — пауза вышла целиком."""
        kicked = self._event.wait(seconds)
        self._event.clear()
        return kicked


def announce_start(store: "Store", interval: int) -> int:
    """Сказать в каждый настроенный чат, что демон поднялся. Вернуть число сообщений.

    Один чат может быть у нескольких контуров — шлём в него один раз. Ошибки Telegram
    не мешают старту: демон нужен ради синка, а не ради приветствия.
    """
    from . import notify
    from .workspaces import config_for_workspace

    todo, _ = syncable(store.list_workspaces())
    seen_chats: set[str] = set()
    sent = 0
    for ws in todo:
        try:
            sender = notify.notifier_from_config(config_for_workspace(store, ws))
        except Exception:  # noqa: BLE001
            sender = None
        if sender is None or sender.chat_id in seen_chats:
            continue
        seen_chats.add(sender.chat_id)
        text = (f"🟢 <b>jwu-демон запущен</b> на {socket.gethostname()}\n"
                f"интервал {interval // 60} мин · контуры: {', '.join(w.slug for w in todo)}\n"
                f"первый проход — сейчас; внеплановый — <code>jwu daemon kick</code>")
        try:
            sender.send(text)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            log(f"[{ws.slug}] стартовое сообщение не ушло: {exc}")
        finally:
            sender.close()
    return sent


def run_loop(
    open_store: Callable[[], "Store"],
    *,
    interval: int = DEFAULT_INTERVAL,
    once: bool = False,
    factory: ServiceFactory | None = None,
    after_sync: Callable[["Service", "SyncResult"], None] | None = None,
    sleep: Callable[[float], None] | None = None,
    announce: bool = True,
) -> None:
    """Главный цикл демона: проход → пауза ``interval`` → проход… (``once`` — один проход).

    Пауза считается от конца прохода, а не от начала: медленная сеть не должна
    приводить к тому, что следующий проход стартует сразу за предыдущим. Сигнал SIGUSR1
    (`jwu daemon kick`) прерывает паузу и запускает проход немедленно.
    """
    interval = max(MIN_INTERVAL, int(interval))
    waiter = Waiter()
    if sleep is None:
        try:
            signal.signal(signal.SIGUSR1, waiter.kick)
        except (ValueError, OSError):
            pass  # не главный поток (тесты) — без сигналов
    with SingleInstance():
        log(f"демон запущен (pid {os.getpid()}, интервал {interval}с)")
        if announce and not once:
            store = open_store()
            try:
                n = announce_start(store, interval)
            finally:
                store.close()
            if n:
                log(f"стартовое сообщение отправлено в {n} чат(а)")
        while True:
            store = open_store()
            try:
                report = run_pass(store, factory=factory, after_sync=after_sync)
            finally:
                store.close()
            log(f"проход завершён: {report.summary()}")
            if once:
                return
            if sleep is not None:
                sleep(interval)
            elif waiter.wait(interval):
                log("внеплановый проход по kick")


def kick() -> int | None:
    """Разбудить работающий демон (SIGUSR1). Вернуть его pid либо None, если не запущен."""
    pid = SingleInstance().holder_pid()
    if pid is None:
        return None
    try:
        os.kill(pid, signal.SIGUSR1)
    except OSError as exc:
        raise DaemonError(f"Не удалось послать сигнал демону (pid {pid}): {exc}") from exc
    return pid


# --------------------------------------------------------------------------- #
# Установка как службы
# --------------------------------------------------------------------------- #


def jwu_executable() -> str:
    """Путь до бинаря ``jwu``: тот, которым нас запустили, иначе первый в PATH."""
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.name == "jwu" and argv0.exists():
        return str(argv0.resolve())
    found = shutil.which("jwu")
    if found:
        return found
    raise DaemonError("Не нашёл исполняемый файл jwu в PATH — укажи его флагом --jwu-bin.")


def log_path() -> Path:
    return data_dir() / "daemon.log"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def render_plist(jwu_bin: str, interval: int, *, log_file: Path | None = None,
                 env: dict[str, str] | None = None) -> str:
    """Текст LaunchAgent: KeepAlive — launchd сам перезапустит упавший процесс."""
    log_file = log_file or log_path()
    env_xml = ""
    if env:
        items = "".join(
            f"\n        <key>{k}</key>\n        <string>{v}</string>" for k, v in sorted(env.items())
        )
        env_xml = f"\n    <key>EnvironmentVariables</key>\n    <dict>{items}\n    </dict>"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{jwu_bin}</string>
        <string>daemon</string>
        <string>run</string>
        <string>--interval</string>
        <string>{interval}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>{log_file}</string>
    <key>StandardErrorPath</key>
    <string>{log_file}</string>{env_xml}
</dict>
</plist>
"""


def systemd_unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / SYSTEMD_UNIT


def render_systemd_unit(jwu_bin: str, interval: int, *, log_file: Path | None = None,
                        env: dict[str, str] | None = None) -> str:
    log_file = log_file or log_path()
    env_lines = "".join(f"Environment={k}={v}\n" for k, v in sorted((env or {}).items()))
    return f"""[Unit]
Description=jwu background sync

[Service]
ExecStart={jwu_bin} daemon run --interval {interval}
Restart=always
RestartSec=30
StandardOutput=append:{log_file}
StandardError=append:{log_file}
{env_lines}
[Install]
WantedBy=default.target
"""


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return 127, f"{cmd[0]}: команда не найдена"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def install(interval: int = DEFAULT_INTERVAL, *, jwu_bin: str | None = None,
            runner: Callable[[list[str]], tuple[int, str]] | None = None) -> list[str]:
    """Записать файл службы и включить её. Возвращает строки для вывода."""
    runner = runner or _run
    interval = max(MIN_INTERVAL, int(interval))
    jwu_bin = jwu_bin or jwu_executable()
    # Демон стартует вне шелла: PATH и переменные окружения пользователя ему не достаются.
    # Путь до БД пробрасываем явно, если он переопределён окружением.
    env = {k: v for k, v in os.environ.items() if k in ("JWU_DB_PATH", "JWU_WORKSPACE")}
    messages: list[str] = []
    if is_macos():
        path = plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # уже стоит — перезагрузим, чтобы подхватить новый интервал/путь
        if path.exists():
            runner(["launchctl", "bootout", f"{_launchd_domain()}/{LAUNCHD_LABEL}"])
        path.write_text(render_plist(jwu_bin, interval, env=env), encoding="utf-8")
        code, out = runner(["launchctl", "bootstrap", _launchd_domain(), str(path)])
        if code != 0:
            code, out = runner(["launchctl", "load", "-w", str(path)])
        if code != 0:
            raise DaemonError(f"launchctl не смог загрузить службу: {out}")
        messages.append(f"LaunchAgent записан: {path}")
        messages.append(f"служба {LAUNCHD_LABEL} загружена, интервал {interval}с, лог {log_path()}")
    else:
        path = systemd_unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_systemd_unit(jwu_bin, interval, env=env), encoding="utf-8")
        for cmd in (["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT]):
            code, out = runner(cmd)
            if code != 0:
                raise DaemonError(f"{' '.join(cmd)}: {out}")
        messages.append(f"unit записан: {path}")
        messages.append(f"служба {SYSTEMD_UNIT} включена, интервал {interval}с, лог {log_path()}")
    return messages


def uninstall(*, runner: Callable[[list[str]], tuple[int, str]] | None = None) -> list[str]:
    runner = runner or _run
    messages: list[str] = []
    if is_macos():
        path = plist_path()
        runner(["launchctl", "bootout", f"{_launchd_domain()}/{LAUNCHD_LABEL}"])
        if path.exists():
            path.unlink()
            messages.append(f"LaunchAgent удалён: {path}")
        else:
            messages.append("LaunchAgent не был установлен")
    else:
        path = systemd_unit_path()
        runner(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
        if path.exists():
            path.unlink()
            runner(["systemctl", "--user", "daemon-reload"])
            messages.append(f"unit удалён: {path}")
        else:
            messages.append("unit не был установлен")
    return messages


def status(store: "Store", *, runner: Callable[[list[str]], tuple[int, str]] | None = None) -> dict:
    """Состояние: установлена ли служба, жив ли процесс, когда был последний проход."""
    runner = runner or _run
    installed = plist_path().exists() if is_macos() else systemd_unit_path().exists()
    if is_macos():
        code, _ = runner(["launchctl", "print", f"{_launchd_domain()}/{LAUNCHD_LABEL}"])
    else:
        code, _ = runner(["systemctl", "--user", "is-active", "--quiet", SYSTEMD_UNIT])
    pid = SingleInstance().holder_pid()
    return {
        "installed": installed,
        "service_loaded": code == 0,
        "running": pid is not None,
        "pid": pid,
        "last_pass": store.get_meta(LAST_PASS_META),
        "last_summary": store.get_meta(LAST_PASS_SUMMARY_META),
        "log": str(log_path()),
        "service_file": str(plist_path() if is_macos() else systemd_unit_path()),
    }
