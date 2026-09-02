"""В какие ветки доехал коммит — единственное место, где jwu зовёт сам ``git``.

``core.gitinfo`` сознательно обходится без подпроцессов: «это репозиторий, ветка такая-то»
целиком лежит в паре файлов внутри ``.git``. Здесь так нельзя: «содержит ли ветка коммит» —
вопрос о графе истории, а не о файле, и правильно отвечает на него только git
(``git branch --contains``). Поэтому модуль изолирован: git зовётся коротко, с таймаутом,
а любая его ошибка превращается в пустой результат с пояснением в ``error``, а не в падение
команды — на дежурстве важнее получить ответ по остальным репозиториям, чем трассировку.

Ничего не тянет из сети: ответ строится по тому, что уже есть в локальном клоне. Если
клон давно не обновлялся, свежая релизная ветка в нём может просто отсутствовать —
поэтому в отчёте всегда видно, какие ветки-кандидаты вообще нашлись.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

# Ветки, до которых «доезжает» фикс: релизные, поддерживающие и основные.
# Перекрывается флагом --branch-pattern: у каждого проекта свои соглашения.
DEFAULT_BRANCH_PATTERNS = (
    "release/*", "release-*", "releases/*", "hotfix/*", "support/*",
    "master", "main", "develop",
)

_TIMEOUT = 20.0


@dataclass(frozen=True)
class CommitReach:
    """Один коммит задачи в одном репозитории."""

    sha: str
    found: bool                                  # есть ли такой коммит в этом клоне
    branches: list[str] = field(default_factory=list)  # ветки, которые его содержат


@dataclass
class RepoReach:
    """Куда доехали коммиты задачи в одном репозитории."""

    name: str
    root: str
    commits: list[CommitReach] = field(default_factory=list)
    # ветка-кандидат → sha, которые в неё доехали (пустой список = не доехало ничего)
    branches: dict[str, list[str]] = field(default_factory=dict)
    error: str = ""

    @property
    def found_shas(self) -> list[str]:
        return [c.sha for c in self.commits if c.found]

    # Во всех трёх списках порядок один — от свежих веток к старым (так их отдаёт git
    # с сортировкой по дате коммита). Для дежурного это и есть нужный порядок: вопрос
    # «есть ли фикс в версии клиента» почти всегда про свежие релизы.

    @property
    def reached(self) -> list[str]:
        """Ветки, куда доехали ВСЕ найденные коммиты задачи."""
        total = len(self.found_shas)
        return [b for b, shas in self.branches.items() if total and len(shas) == total]

    @property
    def partial(self) -> list[str]:
        """Ветки, куда доехала только часть коммитов — самый опасный случай."""
        total = len(self.found_shas)
        return [b for b, shas in self.branches.items() if shas and len(shas) < total]

    @property
    def missing(self) -> list[str]:
        """Ветки-кандидаты, в которых нет ни одного коммита задачи."""
        return [b for b, shas in self.branches.items() if not shas]


def _git(root: str, args: list[str]) -> tuple[bool, str]:
    """Позвать git в каталоге ``root``. Ошибка/таймаут/нет git — (False, текст)."""
    try:
        proc = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True, text=True, timeout=_TIMEOUT, check=False,
        )
    except FileNotFoundError:
        return False, "git не найден в PATH"
    except subprocess.TimeoutExpired:
        return False, f"git {' '.join(args[:2])} не ответил за {int(_TIMEOUT)}с"
    except OSError as exc:  # noqa: BLE001 — каталог исчез, нет прав и т.п.
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()[:200]
    return True, proc.stdout


def _short_name(ref: str) -> str:
    """``origin/release/12.5`` → ``release/12.5``: локальная и удалённая ветка — одна ветка."""
    for prefix in ("remotes/",):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
    parts = ref.split("/", 1)
    # Отрезаем имя remote только у известных префиксов: ветка «release/12.5» не должна
    # превратиться в «12.5».
    if len(parts) == 2 and parts[0] in {"origin", "upstream"}:
        return parts[1]
    return ref


def _branch_names(root: str) -> tuple[list[str], str]:
    """Ветки репозитория, свежие первыми (по дате последнего коммита).

    Порядок здесь не косметика: релизных веток в живом репозитории десятки, а спрашивают
    почти всегда про свежие — сортировка по дате ставит нужные наверх сама.
    """
    ok, out = _git(root, ["branch", "-a", "--sort=-committerdate",
                          "--format=%(refname:short)"])
    if not ok:
        return [], out
    names = []
    for line in out.splitlines():
        ref = line.strip()
        if not ref or ref.endswith("/HEAD") or "->" in ref:
            continue
        names.append(_short_name(ref))
    return list(dict.fromkeys(names)), ""


def _origin_url(root: str) -> str:
    """URL origin — по нему разные клоны и worktree одного репозитория узнаются как один."""
    ok, out = _git(root, ["config", "--get", "remote.origin.url"])
    return out.strip() if ok else ""


def _matches(name: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(fnmatch(name, p) for p in patterns)


def repo_reach(
    root: str,
    shas: list[str],
    *,
    name: str = "",
    patterns: tuple[str, ...] | list[str] = DEFAULT_BRANCH_PATTERNS,
) -> RepoReach:
    """Куда доехали ``shas`` в репозитории ``root``.

    Коммитов задачи в конкретном репозитории может не быть вовсе (задача правила другой
    сервис) — тогда ``found_shas`` пуст, и репозиторий из отчёта отфильтровывается выше.
    """
    result = RepoReach(name=name or Path(root).name, root=root)
    candidates, error = _branch_names(root)
    if error:
        result.error = error
        return result
    candidates = [b for b in candidates if _matches(b, patterns)]
    result.branches = {b: [] for b in candidates}

    for sha in dict.fromkeys(s for s in shas if s):
        ok, _ = _git(root, ["cat-file", "-e", f"{sha}^{{commit}}"])
        if not ok:
            result.commits.append(CommitReach(sha=sha, found=False))
            continue
        ok, out = _git(root, ["branch", "-a", "--contains", sha, "--format=%(refname:short)"])
        contains = sorted(dict.fromkeys(
            _short_name(line.strip()) for line in out.splitlines()
            if line.strip() and "->" not in line
        )) if ok else []
        result.commits.append(CommitReach(sha=sha, found=True, branches=contains))
        for branch in contains:
            if branch in result.branches:
                result.branches[branch].append(sha)
    return result


def reach(
    roots: list[str],
    shas: list[str],
    *,
    names: dict[str, str] | None = None,
    patterns: tuple[str, ...] | list[str] = DEFAULT_BRANCH_PATTERNS,
) -> list[RepoReach]:
    """Пройтись по репозиториям и оставить те, где коммиты задачи реально нашлись.

    ``names`` — необязательное отображение «путь → имя репозитория» (из gitinfo), чтобы
    в отчёте стояло имя из origin, а не имя каталога.
    """
    names = names or {}
    out: list[RepoReach] = []
    # Один и тот же репозиторий часто лежит в нескольких клонах и worktree (стенды,
    # параллельные задачи). Ответ про ветки у них одинаковый, поэтому в отчёт идёт
    # по одному представителю на origin — тот, где нашлось больше коммитов задачи.
    seen: dict[str, int] = {}
    for root in dict.fromkeys(roots):
        item = repo_reach(root, shas, name=names.get(root, ""), patterns=patterns)
        if not (item.found_shas or item.error):
            continue
        origin = _origin_url(root)
        if origin and origin in seen:
            kept = out[seen[origin]]
            better = (len(item.found_shas), len(item.branches)) > \
                (len(kept.found_shas), len(kept.branches))
            if better:
                out[seen[origin]] = item
            continue
        if origin:
            seen[origin] = len(out)
        out.append(item)
    # Репозиторий, где фикс реально доехал до релизных веток, — главный ответ на вопрос,
    # поэтому он идёт первым; шумные клоны с случайно найденным коммитом уезжают вниз.
    out.sort(key=lambda r: (-len(r.reached), -len(r.partial), r.name))
    return out
