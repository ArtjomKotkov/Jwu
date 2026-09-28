"""`jwu doctor`: что не так с окружением и доступами — одной командой.

Безголовая установка (облачная сессия, новая машина) ломается в десятке мест, и каждое
раньше выяснялось отдельной командой: БД не там, папка воркспейса не склонирована,
токен не задан, MCP не прописан, скиллы отстали, демон не стоит. Здесь всё это — список
проверок с одним из четырёх статусов и подсказкой, что делать.

Сетевые проверки (доступы к сервисам, Telegram) включаются флагом ``network``; всё
остальное читает только диск и БД.
"""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass
from importlib import resources
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from . import daemon, memory, workspaces as ws_mod
from .config import db_path, telegram_token
from .maintenance import warn_if_cloud_path
from .. import __version__
from ..skills_install import EXPECTED_AGENTS, EXPECTED_SKILLS, default_agents_dest, default_dest

if TYPE_CHECKING:
    from .store import Store


@dataclass
class Check:
    name: str
    status: str      # ok | warn | fail | skip
    detail: str
    hint: str = ""


def _human(n: int) -> str:
    size = float(max(0, n))
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{int(size)} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} Б"


def _installed_version() -> str:
    try:
        return _pkg_version("jwu")
    except PackageNotFoundError:
        return "dev"


def check_db(store: "Store") -> list[Check]:
    out: list[Check] = []
    path = db_path()
    if not path.exists():
        out.append(Check("БД", "fail", f"{path} не существует", "jwu init / jwu configure создадут её"))
        return out
    size = path.stat().st_size
    out.append(Check("БД", "ok", f"{path} · {_human(size)} · схема v{store.get_meta('schema_version') or '?'}"))
    cloud = warn_if_cloud_path(path)
    if cloud:
        out.append(Check("БД в облаке", "fail", cloud[0],
                         "jwu configure --db-path ~/.local/share/jwu/state.db"))
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        out.append(Check("Права на БД", "warn", f"{oct(mode)} — читаема другими пользователями",
                         f"chmod 600 {path}"))
    if size > 512 * 1024 * 1024:
        out.append(Check("Размер БД", "warn", f"{_human(size)} — снапшоты разрослись",
                         "jwu db prune (или подожди ежедневной чистки)"))
    return out


def check_workspace(store: "Store", explicit: Optional[str]) -> tuple[list[Check], Optional[object]]:
    out: list[Check] = []
    try:
        res = ws_mod.resolve(store, explicit=explicit)
    except ws_mod.WorkspaceError as exc:
        out.append(Check("Воркспейс", "fail", str(exc), "jwu init . --provider jira|github|local"))
        return out, None
    ws = res.workspace
    out.append(Check("Воркспейс", "ok", f"{ws.label} · провайдер {ws.provider} · выбран по {res.source}"))
    missing = [p.path for p in ws.paths if not Path(p.path).exists()]
    if missing:
        out.append(Check("Папки воркспейса", "warn",
                         f"нет на диске: {', '.join(missing[:5])}" + (" …" if len(missing) > 5 else ""),
                         "склонируй репозитории туда же или перепривяжи: jwu workspace add-path/remove-path"))
    elif ws.paths:
        out.append(Check("Папки воркспейса", "ok", f"{len(ws.paths)} на месте"))
    else:
        out.append(Check("Папки воркспейса", "warn", "ни одной папки не привязано",
                         "jwu workspace add-path <DIR> --tag <тег>"))
    return out, ws


def check_config(store: "Store", ws) -> tuple[list[Check], object]:
    """Полнота конфига под провайдера контура — без сети."""
    out: list[Check] = []
    cfg = ws_mod.config_for_workspace(store, ws)
    from .config import get_slot

    def has(slot: str) -> bool:
        try:
            return bool(get_slot(cfg, slot))
        except Exception:  # noqa: BLE001
            return False

    if ws.provider == "jira":
        need = [("jira.base_url", cfg.jira.base_url), ("jira.username", cfg.jira.username)]
        missing = [k for k, v in need if not v]
        token_ok = has("jira.token") or has("jira.password")
        if missing or not token_ok:
            out.append(Check("Конфиг Jira", "fail",
                             "нет: " + ", ".join(missing + ([] if token_ok else ["jira.token/password"])),
                             "jwu configure … или переменные JWU_JIRA_URL / JWU_JIRA_USER / JIRA_TOKEN"))
        else:
            out.append(Check("Конфиг Jira", "ok", f"{cfg.jira.base_url} как {cfg.jira.username}"))
        if ws.bitbucket_enabled:
            if not cfg.bitbucket.base_url or not has("bitbucket.token"):
                out.append(Check("Конфиг Bitbucket", "fail", "нет хоста или токена",
                                 "jwu configure --bitbucket-host … --bitbucket-token … / BITBUCKET_TOKEN"))
            else:
                out.append(Check("Конфиг Bitbucket", "ok", cfg.bitbucket.base_url))
        if cfg.jenkins.username and not has("jenkins.token"):
            out.append(Check("Конфиг Jenkins", "warn", "логин есть, токена нет — разбор сборок недоступен",
                             "jwu configure --jenkins-token … / JENKINS_TOKEN"))
    elif ws.provider == "github":
        if not has("github.token"):
            out.append(Check("Конфиг GitHub", "fail", "нет токена", "GITHUB_TOKEN или jwu configure --github-token …"))
        elif not cfg.github.owner:
            out.append(Check("Конфиг GitHub", "warn", "owner не задан — выборка задач уедет на весь GitHub",
                             "jwu configure --github-owner …"))
        else:
            out.append(Check("Конфиг GitHub", "ok", f"{cfg.github.owner} · {cfg.github.repos or 'все репозитории'}"))
    else:
        out.append(Check("Конфиг", "ok", "локальный контур — внешних доступов не требуется"))
    if cfg.telegram.chat_id:
        if has("telegram.token"):
            out.append(Check("Telegram", "ok", f"чат {cfg.telegram.chat_id}"))
        else:
            out.append(Check("Telegram", "warn", "chat_id задан, токена бота нет", "jwu configure --telegram-token … / TELEGRAM_BOT_TOKEN"))
    else:
        out.append(Check("Telegram", "skip", "уведомления не настроены", "jwu configure --telegram-chat <id> --telegram-token …"))
    return out, cfg


def check_access(ws, cfg) -> list[Check]:
    """Доступы к сервисам контура — сеть."""
    from .service import Service

    out: list[Check] = []
    if ws.provider == "local":
        return out
    try:
        svc = Service.for_workspace(ws, cfg)
    except Exception as exc:  # noqa: BLE001
        return [Check("Доступы", "fail", f"клиенты не создались: {exc}", "см. проверки конфига выше")]
    try:
        for name, res in svc.auth_check().items():
            if res.get("ok"):
                who = f" ({res.get('name') or res.get('user')})" if res.get("name") or res.get("user") else ""
                out.append(Check(f"Доступ {name}", "ok", f"есть{who}"))
            else:
                out.append(Check(f"Доступ {name}", "fail", res.get("error", "?"),
                                 "проверь токен/пароль и сеть (VPN?); 401 — креды, таймаут — сеть"))
        if cfg.telegram.chat_id and telegram_token(cfg):
            try:
                from .notify import TelegramNotifier

                sender = TelegramNotifier(telegram_token(cfg), cfg.telegram.chat_id)
                try:
                    me = sender.get_me()
                finally:
                    sender.close()
                out.append(Check("Доступ telegram", "ok", f"бот @{me.get('username', '?')}"))
            except Exception as exc:  # noqa: BLE001
                out.append(Check("Доступ telegram", "fail", str(exc), "токен от @BotFather, chat_id — id чата с ботом"))
    finally:
        svc.close()
    return out


def check_daemon(store: "Store") -> list[Check]:
    try:
        info = daemon.status(store)
    except Exception as exc:  # noqa: BLE001
        return [Check("Демон", "warn", f"не удалось проверить: {exc}")]
    if info["running"]:
        return [Check("Демон", "ok", f"работает (pid {info['pid']}), последний проход {info['last_pass'] or '—'}")]
    if info["installed"]:
        return [Check("Демон", "warn", "служба установлена, но процесс не работает",
                      f"launchctl/systemctl: см. {info['log']}; либо jwu daemon install заново")]
    return [Check("Демон", "skip", "фоновый синк не установлен", "jwu daemon install")]


def check_ssh(store: "Store", ws) -> list[Check]:
    """Стенды контура выданы Claude Code: ssh-mcp есть, конфиг собран, MCP в папках воркспейса."""
    from . import ssh as ssh_mod

    servers = ssh_mod.list_servers(store, ws.id)
    if not servers:
        return [Check("SSH-стенды", "skip", "стендов нет", "jwu ssh add <имя> --host … --user … --key …")]
    out: list[Check] = []
    if not ssh_mod.find_binary():
        out.append(Check("SSH-стенды", "fail", f"{len(servers)} шт., но ssh-mcp не найден",
                         "go install github.com/overklassniy/ssh-mcp/cmd/ssh-mcp@latest"))
        return out
    if not ssh_mod.config_path(ws.slug).exists():
        out.append(Check("SSH-стенды", "warn", f"{len(servers)} шт., конфиг ssh-mcp не выдан", "jwu ssh install"))
        return out
    try:
        projects = json.loads(_claude_json().read_text(encoding="utf-8")).get("projects") or {}
    except (OSError, ValueError):
        projects = {}
    name = ssh_mod.mcp_name(ws.slug)
    missing = [p.path for p in ws.paths
               if name not in ((projects.get(p.path) or {}).get("mcpServers") or {})]
    if missing:
        out.append(Check("SSH-стенды", "warn", f"MCP {name} не зарегистрирован в: {', '.join(missing)}",
                         "jwu ssh install"))
    else:
        out.append(Check("SSH-стенды", "ok", f"{len(servers)} шт. · MCP {name}"))
    return out


def _claude_json() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_PATH") or (Path.home() / ".claude.json"))


def check_mcp() -> list[Check]:
    path = _claude_json()
    if not path.exists():
        return [Check("MCP в Claude Code", "skip", f"{path} нет — Claude Code на этой машине не настроен")]
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [Check("MCP в Claude Code", "warn", f"{path} не читается: {exc}")]
    entry = (cfg.get("mcpServers") or {}).get("jwu")
    if not entry:
        return [Check("MCP в Claude Code", "fail", "сервер jwu не зарегистрирован",
                      "claude mcp add --scope user jwu -- ~/.local/bin/jwu-mcp")]
    cmd = entry.get("command", "")
    if cmd and not Path(cmd).expanduser().exists():
        return [Check("MCP в Claude Code", "fail", f"команда {cmd} не существует",
                      "pipx install --force … и claude mcp add … заново")]
    return [Check("MCP в Claude Code", "ok", cmd or "зарегистрирован")]


def check_skills() -> list[Check]:
    out: list[Check] = []
    dest = default_dest()
    root = resources.files("jwu") / "skills"
    missing: list[str] = []
    stale: list[str] = []
    for name in sorted(EXPECTED_SKILLS):
        installed = dest / name / "SKILL.md"
        if not installed.exists():
            missing.append(name)
            continue
        try:
            if installed.read_text(encoding="utf-8") != (root / name / "SKILL.md").read_text(encoding="utf-8"):
                stale.append(name)
        except (OSError, FileNotFoundError):
            stale.append(name)
    if missing or stale:
        parts = []
        if missing:
            parts.append(f"нет: {', '.join(missing[:6])}" + (" …" if len(missing) > 6 else ""))
        if stale:
            parts.append(f"устарели: {', '.join(stale[:6])}" + (" …" if len(stale) > 6 else ""))
        out.append(Check("Скиллы Claude", "warn", "; ".join(parts), "jwu install-claude-skills"))
    else:
        out.append(Check("Скиллы Claude", "ok", f"{len(EXPECTED_SKILLS)} на месте и актуальны ({dest})"))
    agents = default_agents_dest()
    lost = [a for a in sorted(EXPECTED_AGENTS) if not (agents / f"{a}.md").exists()]
    if lost:
        out.append(Check("Субагенты Claude", "warn", f"нет: {', '.join(lost)}", "jwu install-claude-skills"))
    return out


def check_version() -> list[Check]:
    installed = _installed_version()
    if installed not in ("dev", __version__):
        return [Check("Версия", "warn", f"код {__version__}, метаданные пакета {installed}",
                      "после обновления перезапусти сессию Claude Code (MCP-сервер живёт на старом "
                      "коде); при editable-установке метаданные обновляет `pipx install --force -e .`")]
    return [Check("Версия", "ok", f"jwu {__version__} · {platform.system()} · Python {platform.python_version()}")]


def check_memory(store: "Store") -> list[Check]:
    repo = store.get_meta(memory.MEMORY_REPO_META)
    if not repo:
        return [Check("Память (git)", "skip", "синк памяти не настроен", "jwu memory sync --repo <приватный git-каталог>")]
    if not (Path(repo) / ".git").exists():
        return [Check("Память (git)", "warn", f"{repo} больше не git-репозиторий", "jwu memory sync --repo …")]
    return [Check("Память (git)", "ok", f"{repo} · последний синк {store.get_meta(memory.LAST_SYNC_META) or '—'}")]


def run(store: "Store", *, explicit_workspace: Optional[str] = None, network: bool = True) -> list[Check]:
    checks: list[Check] = []
    checks += check_version()
    checks += check_db(store)
    ws_checks, ws = check_workspace(store, explicit_workspace)
    checks += ws_checks
    if ws is not None:
        cfg_checks, cfg = check_config(store, ws)
        checks += cfg_checks
        if network:
            checks += check_access(ws, cfg)
        else:
            checks.append(Check("Доступы", "skip", "--offline: сеть не проверялась"))
        checks += check_ssh(store, ws)
    checks += check_daemon(store)
    checks += check_mcp()
    checks += check_skills()
    checks += check_memory(store)
    return checks
