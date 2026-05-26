"""Установка jwu-скиллов (Claude Code) из пакета в каталог скиллов.

Скиллы лежат внутри пакета (``jwu/skills/<name>/SKILL.md``) и едут вместе
с wheel/pipx. ``install_skills`` копирует их в целевой каталог (по умолчанию
``~/.claude/skills``), перезаписывая существующие.
"""

from __future__ import annotations

import importlib.resources as resources
from pathlib import Path

# Ожидаемые скиллы (для проверок/тестов; фактически ставится всё, что лежит в пакете).
EXPECTED_SKILLS = {
    "jwu-start-job",
    "jwu-resume-job",
    "jwu-track-job",
    "jwu-analyze-day",
    "jwu-post-analyze-day",
}


def default_dest() -> Path:
    """Каталог скиллов Claude Code по умолчанию."""
    return Path.home() / ".claude" / "skills"


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
    return sorted(results)
