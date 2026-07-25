"""Git-информация о папках воркспейса — чтением файлов, без запуска ``git``.

jwu сознательно не порождает подпроцессы: всё, что нужно для маркера «это репозиторий,
ветка такая-то», лежит в самом каталоге ``.git``:

- ``.git/HEAD`` → ``ref: refs/heads/<branch>`` (или голый sha в detached-состоянии);
- ``.git/config`` → ``url`` у ``[remote "origin"]``, из него берётся имя репозитория.

Поддерживается и ``.git``-ФАЙЛ (``gitdir: …``), который git создаёт для worktree и submodule.

Индексация ленивая и дешёвая: пара маленьких файлов на каталог. Глубина ограничена
(``MAX_DEPTH``), потому что типичный случай — либо сама папка репозиторий, либо папка
с репозиториями внутри; рекурсивно обходить всё дерево диска незачем.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# На сколько уровней вглубь искать репозитории внутри папки воркспейса.
MAX_DEPTH = 2
# Каталоги, в которые заведомо не стоит спускаться при поиске репозиториев.
SKIP_DIRS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__", ".idea", ".vscode",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache", "target", ".gradle",
}

_HEAD_REF_RE = re.compile(r"^ref:\s*refs/heads/(?P<branch>.+)$")
_GITDIR_RE = re.compile(r"^gitdir:\s*(?P<path>.+)$")
_ORIGIN_URL_RE = re.compile(
    r'\[remote\s+"origin"\][^\[]*?url\s*=\s*(?P<url>\S+)', re.DOTALL
)


@dataclass(frozen=True)
class GitInfo:
    """Что показываем рядом с папкой: имя репозитория и текущая ветка."""

    root: str            # каталог с .git
    name: str            # имя репозитория (из origin либо по имени каталога)
    branch: str = ""     # имя ветки; пусто при detached HEAD
    detached: str = ""   # короткий sha, если HEAD отвязан от ветки

    @property
    def label(self) -> str:
        """Компактная подпись «имя/ветка» для таблицы."""
        where = self.branch or (f"@{self.detached}" if self.detached else "?")
        return f"{self.name}/{where}"


def _git_dir(path: Path) -> Path | None:
    """Каталог .git для ``path`` (умеет .git-файл worktree/submodule)."""
    marker = path / ".git"
    try:
        if marker.is_dir():
            return marker
        if marker.is_file():
            match = _GITDIR_RE.match(marker.read_text(encoding="utf-8", errors="replace").strip())
            if match:
                target = Path(match.group("path"))
                if not target.is_absolute():
                    target = (path / target).resolve()
                return target if target.exists() else None
    except OSError:
        return None
    return None


def _read_branch(git_dir: Path) -> tuple[str, str]:
    """(branch, detached_sha) из HEAD. Ошибки чтения — не повод падать."""
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "", ""
    match = _HEAD_REF_RE.match(head)
    if match:
        return match.group("branch").strip(), ""
    return ("", head[:7]) if head else ("", "")


def _read_name(git_dir: Path, fallback: Path) -> str:
    """Имя репозитория: из URL origin, иначе — имя каталога."""
    try:
        config = (git_dir / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        config = ""
    match = _ORIGIN_URL_RE.search(config)
    if match:
        url = match.group("url").rstrip("/")
        if url.endswith(".git"):
            url = url[:-4]
        tail = url.replace("\\", "/").split("/")[-1]
        if tail:
            return tail
    return fallback.name or str(fallback)


def git_info(path: str | Path) -> GitInfo | None:
    """Git-инфо, если ``path`` — корень репозитория; иначе None (вверх НЕ поднимаемся)."""
    folder = Path(path)
    git_dir = _git_dir(folder)
    if git_dir is None:
        return None
    branch, detached = _read_branch(git_dir)
    return GitInfo(root=str(folder), name=_read_name(git_dir, folder),
                   branch=branch, detached=detached)


def find_repos(path: str | Path, *, max_depth: int = MAX_DEPTH) -> list[GitInfo]:
    """Репозитории в папке и её подпапках (не глубже ``max_depth``).

    Сама папка проверяется первой: если она репозиторий, внутрь не спускаемся —
    вложенные подмодули нас на этом уровне не интересуют.
    """
    root = Path(path)
    own = git_info(root)
    if own is not None:
        return [own]
    found: list[GitInfo] = []
    _walk(root, depth=max_depth, out=found)
    return found


def _walk(folder: Path, *, depth: int, out: list[GitInfo]) -> None:
    if depth <= 0:
        return
    try:
        entries = sorted(folder.iterdir())
    except OSError:  # нет прав / папка исчезла — не наша забота
        return
    for entry in entries:
        # Скрытые каталоги пропускаем: дерево структуры их тоже не показывает,
        # иначе счётчик «N репо» обещал бы то, что нельзя открыть.
        if (not entry.is_dir() or entry.is_symlink()
                or entry.name.startswith(".") or entry.name in SKIP_DIRS):
            continue
        info = git_info(entry)
        if info is not None:
            out.append(info)
            continue  # внутрь репозитория не идём
        _walk(entry, depth=depth - 1, out=out)


def index(paths: list[str]) -> dict[str, list[GitInfo]]:
    """Проиндексировать папки воркспейса: путь → найденные в нём репозитории."""
    return {p: find_repos(p) for p in paths}
