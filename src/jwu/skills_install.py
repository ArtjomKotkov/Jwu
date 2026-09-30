"""Установка jwu-скиллов и субагентов (Claude Code) из пакета.

Скиллы лежат внутри пакета (``jwu/skills/<name>/SKILL.md``), субагенты —
``jwu/agents/<name>.md``. Едут вместе с wheel/pipx. ``install_skills`` и
``install_agents`` копируют их в целевые каталоги (по умолчанию
``~/.claude/skills`` и ``~/.claude/agents``), перезаписывая существующие.
"""

from __future__ import annotations

import importlib.resources as resources
import json
import shutil
import subprocess
import sys
from pathlib import Path

# Ожидаемые скиллы (для проверок/тестов; фактически ставится всё, что лежит в пакете).
EXPECTED_SKILLS = {
    "jwu-start-job",
    "jwu-resume-job",
    "jwu-track-job",
    "jwu-job-review",
    "jwu-commit-message",
    "jwu-task-create",
    "jwu-task-comment",
    "jwu-task-status",
    "jwu-task-attach",
    "jwu-task-branches",
    "jwu-analyze-day",
    "jwu-post-analyze-day",
    "jwu-track-time",
    "jwu-4test-message",
    "build-failure",
    "jwu-workspace-setup",
    "jwu-update",
    "jwu-prompt-refine",
    "duty-support",
    "jwu-pr-task",
    "jwu-pr-comment",
    "jwu-context",
    "jwu-review-pr",
    "jwu-qa-triage",
    "jwu-wrap-up",
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
}

# Ожидаемые субагенты — дефолтные ревьюеры/исполнители jwu.
EXPECTED_AGENTS = {
    "reviewer-jwu-sample",
    "jenkins-build-analyst",
    "duty-support-sample",
    "qa-triage-sample",
    "voice-writer-sample",
    "review-filter-sample",
}


def default_dest() -> Path:
    """Каталог скиллов Claude Code по умолчанию."""
    return Path.home() / ".claude" / "skills"


def default_agents_dest() -> Path:
    """Каталог субагентов Claude Code по умолчанию."""
    return Path.home() / ".claude" / "agents"


def install_skills(dest: Path) -> list[tuple[str, str]]:
    """Развернуть забандленные скиллы в ``dest``. Перезаписывает существующие.

    Возвращает список (имя_скилла, действие), где действие — "добавлен" | "обновлён",
    отсортированный по имени.
    """
    root = resources.files("jwu") / "skills"
    results: list[tuple[str, str]] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        name = entry.name
        content = skill_md.read_text(encoding="utf-8")
        target_dir = dest / name
        action = "обновлён" if (target_dir / "SKILL.md").exists() else "добавлен"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "SKILL.md").write_text(content, encoding="utf-8")
        results.append((name, action))
    results.extend(_remove_retired(dest))
    return sorted(results)


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
TARGETS = ("claude", "cursor")


def cursor_skills_dest() -> Path:
    return Path.home() / ".cursor" / "skills"


def cursor_agents_dest() -> Path:
    return Path.home() / ".cursor" / "agents"


def cursor_mcp_path() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


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


def register_mcp_claude(command: str | None = None) -> str:
    """Зарегистрировать сервер jwu в Claude Code (scope user) через CLI claude."""
    if not shutil.which("claude"):
        return "пропущен: CLI claude не найден (добавь сам: claude mcp add --scope user jwu -- jwu-mcp)"
    cmd = command or mcp_command()
    listed = subprocess.run(["claude", "mcp", "get", "jwu"], capture_output=True, text=True)
    if listed.returncode == 0 and cmd in (listed.stdout or ""):
        return "без изменений"
    subprocess.run(["claude", "mcp", "remove", "--scope", "user", "jwu"], capture_output=True, text=True)
    proc = subprocess.run(["claude", "mcp", "add", "--scope", "user", "jwu", "--", cmd],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return f"ошибка: {(proc.stderr or proc.stdout).strip()[:200]}"
    return "добавлен"


def install_agents(dest: Path, *, cursor: bool = False) -> list[tuple[str, str]]:
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
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / entry.name
        action = "обновлён" if target.exists() else "добавлен"
        target.write_text(content, encoding="utf-8")
        results.append((name, action))
    return sorted(results)
