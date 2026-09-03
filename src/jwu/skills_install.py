"""Установка jwu-скиллов и субагентов (Claude Code) из пакета.

Скиллы лежат внутри пакета (``jwu/skills/<name>/SKILL.md``), субагенты —
``jwu/agents/<name>.md``. Едут вместе с wheel/pipx. ``install_skills`` и
``install_agents`` копируют их в целевые каталоги (по умолчанию
``~/.claude/skills`` и ``~/.claude/agents``), перезаписывая существующие.
"""

from __future__ import annotations

import importlib.resources as resources
import shutil
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


def install_agents(dest: Path) -> list[tuple[str, str]]:
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
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / entry.name
        action = "обновлён" if target.exists() else "добавлен"
        target.write_text(content, encoding="utf-8")
        results.append((name, action))
    return sorted(results)
