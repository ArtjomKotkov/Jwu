"""Реестр локальных веток по задаче: где лежит код, без работ и без записи в git.

Вопрос «на какой ветке я это делал» раньше отвечался по записям работы. Но работы
может не быть, а ветка есть. Здесь ответ собирается прямо из клонов воркспейса:
локальные ветки каждого репозитория с последним коммитом, апстримом, ahead/behind,
отметкой «сейчас checkout'нута» (в клоне или в worktree) и грязным деревом.

Только чтение: `git for-each-ref`, `git worktree list`, `git status --porcelain`.
Ничего не фетчится — ahead/behind считаются от последнего известного origin.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

from . import gitinfo
from .gitbranches import _git

if TYPE_CHECKING:
    from .store import Store

TASK_KEY_RE = re.compile(r"([A-Z][A-Z0-9]+-\d+)")
_TRACK_RE = re.compile(r"ahead (\d+)|behind (\d+)")
# Ветки, которые «про задачу» не бывают: их не показываем без --all.
_SKIP_BRANCHES = {"main", "master", "develop", "dev", "HEAD"}


@dataclass
class BranchInfo:
    repo: str
    root: str
    branch: str
    sha: str = ""
    date: str = ""
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    gone: bool = False          # апстрим удалён (ветка уже влита/закрыта на сервере)
    current: bool = False       # checkout'нута в этом клоне
    worktree: str = ""          # путь worktree, где она checkout'нута (если не в клоне)
    dirty: int = -1             # незакоммиченных файлов там, где checkout'нута; -1 — неизвестно
    task_key: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def task_key_of(branch: str) -> str:
    match = TASK_KEY_RE.search(branch.upper())
    return match.group(1) if match else ""


def _parse_track(track: str) -> tuple[int, int, bool]:
    if "gone" in track:
        return 0, 0, True
    ahead = behind = 0
    for m in _TRACK_RE.finditer(track):
        if m.group(1):
            ahead = int(m.group(1))
        if m.group(2):
            behind = int(m.group(2))
    return ahead, behind, False


def worktrees(root: str) -> dict[str, str]:
    """ветка → путь worktree (кроме основного клона)."""
    ok, out = _git(root, ["worktree", "list", "--porcelain"])
    if not ok:
        return {}
    result: dict[str, str] = {}
    path = ""
    main = str(Path(root).resolve())
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.startswith("branch ") and path:
            branch = line[len("branch "):].strip().replace("refs/heads/", "")
            if str(Path(path).resolve()) != main:
                result[branch] = path
    return result


def dirty_count(path: str) -> int:
    ok, out = _git(path, ["status", "--porcelain", "--untracked-files=normal"])
    if not ok:
        return -1
    return sum(1 for line in out.splitlines() if line.strip())


def repo_branches(root: str, name: str) -> list[BranchInfo]:
    """Локальные ветки репозитория, свежие первыми."""
    ok, out = _git(root, [
        "for-each-ref", "--sort=-committerdate",
        "--format=%(refname:short)%09%(objectname:short)%09%(committerdate:iso-strict)"
        "%09%(upstream:short)%09%(upstream:track)%09%(HEAD)",
        "refs/heads",
    ])
    if not ok:
        return []
    wts = worktrees(root)
    dirty_here: Optional[int] = None
    items: list[BranchInfo] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        branch, sha, date, upstream, track, head = parts[:6]
        ahead, behind, gone = _parse_track(track)
        info = BranchInfo(repo=name, root=root, branch=branch, sha=sha, date=date,
                          upstream=upstream, ahead=ahead, behind=behind, gone=gone,
                          current=head.strip() == "*", worktree=wts.get(branch, ""),
                          task_key=task_key_of(branch))
        if info.current:
            if dirty_here is None:
                dirty_here = dirty_count(root)
            info.dirty = dirty_here
        elif info.worktree:
            info.dirty = dirty_count(info.worktree)
        items.append(info)
    return items


def workspace_roots(store: "Store") -> dict[str, str]:
    """Каталоги git-репозиториев воркспейса → имя (как в Service._workspace_repo_roots)."""
    roots: dict[str, str] = {}
    for path in store.workspace_paths(store.workspace_id):
        for info in gitinfo.find_repos(path.path):
            roots[info.root] = info.name
    return roots


def collect(roots: dict[str, str], *, key: Optional[str] = None, all_branches: bool = False,
            limit_per_repo: int = 200) -> list[BranchInfo]:
    """Ветки по всем репозиториям: по ключу задачи, все «задачные» либо вообще все."""
    wanted = (key or "").strip().upper()
    out: list[BranchInfo] = []
    for root, name in sorted(roots.items()):
        for info in repo_branches(root, name)[:limit_per_repo]:
            if wanted:
                if wanted not in info.branch.upper():
                    continue
            elif not all_branches:
                if info.branch in _SKIP_BRANCHES or not info.task_key:
                    continue
            out.append(info)
    out.sort(key=lambda b: (b.date or ""), reverse=True)
    return out


def summary_line(b: BranchInfo) -> str:
    """Одна строка про ветку — для CLI и контекста."""
    where = "текущая" if b.current else (f"worktree {b.worktree}" if b.worktree else "")
    sync = []
    if b.gone:
        sync.append("апстрим удалён")
    else:
        if b.ahead:
            sync.append(f"↑{b.ahead}")
        if b.behind:
            sync.append(f"↓{b.behind}")
        if b.upstream and not sync:
            sync.append("в синке")
        if not b.upstream:
            sync.append("без апстрима")
    if b.dirty > 0:
        sync.append(f"незакоммичено {b.dirty}")
    bits = [b.repo, b.branch, b.sha, b.date[:10] if b.date else ""]
    if where:
        bits.append(where)
    return " · ".join(x for x in bits if x) + (f" · {', '.join(sync)}" if sync else "")
