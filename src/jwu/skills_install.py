"""Установка клиентских скиллов и субагентов из ресурсов jwu."""

from __future__ import annotations

import importlib.resources as resources
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Штатные скиллы Claude Code (проверки doctor и исключения из backup).
EXPECTED_SKILLS = {
    "jwu-start-job",
    "jwu-continue-job",
    "jwu-close-job",
    "jwu-todo",
    "jwu-zephyr-testcase",
    "jwu-cleanup",
    "jwu-orchestrator",
    "jwu-job-review",
    "jwu-commit-message",
    "jwu-task-create",
    "jwu-task-comment",
    "jwu-task-status",
    "jwu-task-attach",
    "jwu-task-branches",
    "jwu-track-time",
    "jwu-build-failure",
    "jwu-workspace-setup",
    "jwu-update",
    "jwu-prompt-refine",
    "jwu-pr-task",
    "jwu-pr-comment",
    "jwu-context",
    "jwu-review-pr",
    "jwu-setup-remote",
    "jwu-session-init",
    "jwu-review-queue",
    "jwu-voice-profile",
    "jwu-voice-rewrite",
    "jwu-confluence",
}

# Скиллы, которые jwu раздавал раньше и больше не раздаёт. Установка их УДАЛЯЕТ:
# иначе переименованный скилл остаётся у пользователя второй копией и продолжает
# срабатывать по своим триггерам — с устаревшими инструкциями. Список именно
# перечислением, а не «всё лишнее с префиксом jwu-»: свои скиллы пользователя
# удалять нельзя.
RETIRED_SKILLS = {
    "jwu-create-issue",  # 1.8.0: разъехался на jwu-task-create/comment/status/attach/branches
    # 2.0.0: флоу работ — три скилла start/continue/close
    "jwu-track-job",     # журнал → раздел «Журнал работы» в jwu-start-job
    "jwu-resume-job",    # → jwu-continue-job (плюс переоткрытие закрытой)
    "jwu-wrap-up",       # → jwu-close-job (итог, уборка) / jwu-continue-job (пауза)
    # 2.2.0: у всех скиллов jwu — префикс jwu-
    "build-failure",     # → jwu-build-failure
    "jwu-analyze-day",       # → доска /jwu-todo (двигать дальше, буксует, ответить, закрыть работу)
    "jwu-post-analyze-day",  # → jwu-track-time (план без записи — «покажи, что затрекал бы»)
    "jwu-queue",         # 3.3.0: очередь убрана — её заменили планы (jwu_plan*)
}

# Ожидаемые субагенты — дефолтные ревьюеры/исполнители jwu.
EXPECTED_AGENTS = {
    "reviewer-jwu-sample",
    "jenkins-build-analyst",
    "voice-writer-sample",
    "review-filter-sample",
    "jwu-testing-agent",
}


def default_dest() -> Path:
    """Каталог скиллов Claude Code по умолчанию."""
    return Path.home() / ".claude" / "skills"


def default_agents_dest() -> Path:
    """Каталог субагентов Claude Code по умолчанию."""
    return Path.home() / ".claude" / "agents"


TARGETS = ("claude", "cursor", "codex", "jwu-hermes")


def skill_bundle(target: str):
    """Ресурсы ровно одного клиента; отсутствующий комплект — ошибка."""
    if target not in TARGETS:
        raise ValueError(f"Неизвестный клиент: {target}")
    root = resources.files("jwu") / "skill_bundles" / target
    if not root.is_dir():
        raise FileNotFoundError(f"Нет пакета скиллов {target}")
    return root


def hermes_skills_dest() -> Path:
    profile = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()
    return profile / "skills" / "jwu-hermes"


def _skill_files(source, target: Path):
    for child in source.iterdir():
        if child.name == "__pycache__":
            continue
        dest = target / child.name
        if child.is_dir():
            yield from _skill_files(child, dest)
        elif child.is_file():
            yield child, dest


def install_skills(dest: Path, *, target: str = "claude") -> list[tuple[str, str]]:
    """Скопировать скиллы одного клиента, включая их ресурсы."""
    root = skill_bundle(target)
    entries = [entry for entry in root.iterdir() if (entry / "SKILL.md").is_file()]
    if not entries:
        raise FileNotFoundError(f"Пакет скиллов {target} пуст")
    files = [(source, path) for entry in entries
             for source, path in _skill_files(entry, dest / entry.name)]
    # Hermes не пишет через ссылки в старый профиль, private skills или checkout.
    if target == "jwu-hermes":
        for _, path in files:
            for candidate in (path, *path.parents):
                if candidate.is_symlink():
                    raise ValueError(f"Символическая ссылка в каталоге установки: {candidate}")
                if candidate == dest.parent:
                    break
    results = [(entry.name, "обновлён" if (dest / entry.name / "SKILL.md").exists()
                else "добавлен") for entry in entries]
    for source, path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        with resources.as_file(source) as source_path:
            shutil.copy2(source_path, path)
        if path.suffix == ".sh" and path.parent.name == "scripts":
            path.chmod(path.stat().st_mode | 0o100)
    if target != "jwu-hermes":
        results.extend(_install_addon_skills(dest))
        results.extend(_remove_retired(dest))
    return sorted(results)


def _install_addon_skills(dest: Path) -> list[tuple[str, str]]:
    """Скиллы установленных аддонов (``skills_dir()`` модуля аддона) — рядом со своими."""
    from .core import addons

    out: list[tuple[str, str]] = []
    for addon in addons.active():
        folder = addon.call("skills_dir")
        if not folder:
            continue
        for entry in Path(folder).iterdir():
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            target = dest / entry.name
            action = "обновлён" if (target / "SKILL.md").exists() else "добавлен"
            target.mkdir(parents=True, exist_ok=True)
            (target / "SKILL.md").write_text(skill_md.read_text(encoding="utf-8"), encoding="utf-8")
            out.append((entry.name, action))
    return out


def _remove_retired(dest: Path) -> list[tuple[str, str]]:
    """Снести скиллы, которые jwu больше не раздаёт (см. RETIRED_SKILLS)."""
    removed: list[tuple[str, str]] = []
    for name in RETIRED_SKILLS:
        target = dest / name
        if not (target / "SKILL.md").is_file():
            continue
        shutil.rmtree(target, ignore_errors=True)
        if not target.exists():
            removed.append((name, "удалён (устарел)"))
    return removed


# Куда ставить под каждого клиента. Cursor (2.4+) понимает те же SKILL.md и те же
# markdown-субагенты, что Claude Code, но ищет их в своих каталогах.
def cursor_skills_dest() -> Path:
    return Path.home() / ".cursor" / "skills"


def cursor_agents_dest() -> Path:
    return Path.home() / ".cursor" / "agents"


def cursor_mcp_path() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10 — бэкпорт tomli (зависимость jwu)
    import tomli as tomllib  # type: ignore[no-redef]


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def codex_skills_dest() -> Path:
    return codex_home() / "skills"


def codex_agents_dest() -> Path:
    return codex_home() / "agents"


def _frontmatter(text: str) -> tuple[dict, str]:
    """Простой frontmatter агента (``key: value`` построчно) и тело."""
    if not text.startswith("---\n"):
        return {}, text
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        return {}, text
    meta = {}
    for line in head.splitlines():
        key, colon, value = line.partition(":")
        if colon and key.strip():
            meta[key.strip()] = value.strip().strip('"')
    return meta, body.lstrip("\n")


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # базовая строка JSON — валидная базовая строка TOML


_TRIPLE = "'" * 3


def _for_codex(text: str) -> str:
    """Субагент для Codex: TOML с name, description, developer_instructions (тело агента).
    Поля Claude Code (tools, color) Codex не знает — их нет."""
    meta, body = _frontmatter(text)
    name, description = meta.get("name", ""), meta.get("description", "")
    if not (name and description):
        raise ValueError("у агента нет name или description во frontmatter")
    instructions = body.rstrip() + "\n"
    value = (_TRIPLE + "\n" + instructions + _TRIPLE) if _TRIPLE not in instructions else _toml_str(instructions)
    out = ("# Поставлено jwu install --for codex — перезаписывается при обновлении.\n"
           f"name = {_toml_str(name)}\ndescription = {_toml_str(description)}\n"
           f"developer_instructions = {value}\n")
    tomllib.loads(out)  # сломанный TOML не пишем
    return out


def codex_config_path() -> Path:
    return codex_home() / "config.toml"


def codex_mcp_entry(path: Path | None = None) -> dict:
    """Секция [mcp_servers.jwu] из config.toml Codex (пустой dict — нет)."""
    path = path or codex_config_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return (data.get("mcp_servers") or {}).get("jwu") or {}


def register_mcp_codex(path: Path | None = None, command: str | None = None) -> str:
    """Добавить/обновить [mcp_servers.jwu] в config.toml Codex, НЕ трогая остальной файл.

    Файл правится текстом, а не пересборкой TOML: чужие секции, комментарии и порядок (плагины,
    доверенные проекты, другие серверы) остаются байт в байт. Заменяется только своя секция —
    от заголовка [mcp_servers.jwu] до следующего заголовка.
    """
    import re

    path = path or codex_config_path()
    cmd = command or mcp_command()
    block = f'[mcp_servers.jwu]\ncommand = {json.dumps(cmd)}\nargs = []\n'
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if text:
        try:
            tomllib.loads(text)
        except ValueError as exc:
            raise ValueError(f"{path} — не TOML, правь руками: {exc}") from exc
    if codex_mcp_entry(path) == {"command": cmd, "args": []}:
        return "без изменений"
    pattern = re.compile(r"^\[mcp_servers\.jwu\][^\n]*\n(?:(?!^\[).*\n?)*", re.M)
    if pattern.search(text):
        new = pattern.sub(block + "\n", text, count=1).rstrip("\n") + "\n"
        action = "обновлён"
    else:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
        action = "добавлен"
    tomllib.loads(new)   # не записываем то, что сами же не прочитаем
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    return action


def _for_cursor(text: str) -> str:
    """Субагент для Cursor: без строки ``tools:`` — там имена инструментов Claude Code,
    которых в Cursor нет (у Cursor свой набор; ограничение прав — поле readonly)."""
    head, sep, body = text.partition("\n---\n") if text.startswith("---\n") else ("", "", text)
    if not sep:
        return text
    head = "\n".join(ln for ln in head.splitlines() if not ln.startswith("tools:"))
    return head + sep + body


def mcp_command() -> str:
    """Путь к jwu-mcp: рядом с текущим интерпретатором (pipx-venv) или в PATH."""
    near = Path(sys.executable).parent / "jwu-mcp"
    if near.exists():
        return str(near)
    return shutil.which("jwu-mcp") or str(Path.home() / ".local" / "bin" / "jwu-mcp")


def register_mcp_cursor(path: Path | None = None, command: str | None = None) -> str:
    """Добавить/обновить сервер jwu в mcp.json Cursor. Чужие записи не трогаем."""
    path = path or cursor_mcp_path()
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            raise ValueError(f"{path} — не JSON, правь руками: {exc}") from exc
    servers = data.setdefault("mcpServers", {})
    entry = {"command": command or mcp_command(), "args": []}
    action = "без изменений" if servers.get("jwu") == entry else ("обновлён" if "jwu" in servers else "добавлен")
    servers["jwu"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return action


def _claude_user_mcp_command() -> str:
    path = Path(os.environ.get("CLAUDE_CONFIG_PATH") or (Path.home() / ".claude.json"))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return ((data.get("mcpServers") or {}).get("jwu") or {}).get("command", "") or ""


def register_mcp_claude(command: str | None = None) -> str:
    """Зарегистрировать сервер jwu в Claude Code (scope user) через CLI claude."""
    if not shutil.which("claude"):
        return "пропущен: CLI claude не найден (добавь сам: claude mcp add --scope user jwu -- jwu-mcp)"
    cmd = command or mcp_command()
    # Смотрим именно user-scope в ~/.claude.json: `claude mcp get` отдаёт запись с наивысшим
    # приоритетом, и проектный .mcp.json (например, в клоне jwu) маскировал бы user-запись.
    if _claude_user_mcp_command() == cmd:
        return "без изменений"
    subprocess.run(["claude", "mcp", "remove", "--scope", "user", "jwu"], capture_output=True, text=True)
    proc = subprocess.run(["claude", "mcp", "add", "--scope", "user", "jwu", "--", cmd],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return f"ошибка: {(proc.stderr or proc.stdout).strip()[:200]}"
    return "добавлен"


def install_agents(dest: Path, *, cursor: bool = False, codex: bool = False) -> list[tuple[str, str]]:
    """Развернуть забандленные субагенты в ``dest``. Перезаписывает существующие.

    Структура источника: ``jwu/agents/<name>.md``. Каждый агент — один markdown-файл
    с frontmatter (см. формат субагентов Claude Code).

    Возвращает список (имя_агента, действие), действие — "добавлен" | "обновлён",
    отсортированный по имени.
    """
    root = resources.files("jwu") / "agents"
    results: list[tuple[str, str]] = []
    if not root.is_dir():
        return results
    for entry in root.iterdir():
        if not entry.is_file() or not entry.name.endswith(".md"):
            continue
        name = entry.name[: -len(".md")]
        content = entry.read_text(encoding="utf-8")
        if cursor:
            content = _for_cursor(content)
        target_name = entry.name
        if codex:
            content = _for_codex(content)
            target_name = f"{name}.toml"
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / target_name
        action = "обновлён" if target.exists() else "добавлен"
        target.write_text(content, encoding="utf-8")
        results.append((name, action))
    results.extend(_install_addon_agents(dest, cursor=cursor, codex=codex))
    return sorted(results)


def _install_addon_agents(dest: Path, *, cursor: bool, codex: bool) -> list[tuple[str, str]]:
    """Субагенты аддонов (``agents_dir()`` модуля аддона) — рядом со своими, в том же виде."""
    from .core import addons

    out: list[tuple[str, str]] = []
    for addon in addons.active():
        folder = addon.call("agents_dir")
        if not folder:
            continue
        for entry in sorted(Path(folder).glob("*.md")):
            name, content = entry.stem, entry.read_text(encoding="utf-8")
            if cursor:
                content = _for_cursor(content)
            target_name = entry.name
            if codex:
                content, target_name = _for_codex(content), f"{name}.toml"
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / target_name
            action = "обновлён" if target.exists() else "добавлен"
            target.write_text(content, encoding="utf-8")
            out.append((name, action))
    return out
