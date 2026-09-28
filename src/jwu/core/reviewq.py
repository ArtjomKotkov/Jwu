"""Очередь ревью: какие PR на мне ревьюить, кем, и куда складывать сводку.

Скилл jwu-review-queue гоняет пакетное ревью чужих PR: ревьюеры-субагенты пулом, затем
фильтр замечаний, затем голос. Решения, которые можно проверить тестом, живут здесь, а не
в тексте скилла:

- **отбор** — PR на моём ревью, кроме тех, где мой статус уже APPROVED (если не попросили
  включить), с фильтром по номерам и репозиториям; у каждого пропущенного — причина;
- **кем** — ревьювер по репозиторию из настроек контура (``review.reviewer.<repo>``),
  иначе дефолт jwu; фильтр замечаний — ``review.filter_agent``;
- **куда** — сводка и разделы по PR лежат в каталоге данных jwu
  (``reviews/<slug>/<дата>/``), а не в репозитории проекта: следующая сессия находит их
  по стабильному пути.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .branches import task_key_of
from .config import data_dir
from .models import PR

if TYPE_CHECKING:
    from .store import Store

DEFAULT_REVIEWER = "reviewer-jwu-sample"
DEFAULT_FILTER = "review-filter-sample"
REVIEWER_PREFIX = "review.reviewer."
FILTER_SETTING = "review.filter_agent"


def reviews_dir(slug: str, day: str | None = None) -> Path:
    """Каталог сводки за день: ``<data>/reviews/<slug>/<YYYY-MM-DD>``."""
    return data_dir() / "reviews" / slug / (day or date.today().isoformat())


def reviewer_for(store: "Store", workspace_id: int, repo: str) -> str:
    settings = store.workspace_settings(workspace_id)
    return (settings.get(REVIEWER_PREFIX + repo) or "").strip() or DEFAULT_REVIEWER


def filter_agent(store: "Store", workspace_id: int) -> str:
    return (store.workspace_settings(workspace_id).get(FILTER_SETTING) or "").strip() or DEFAULT_FILTER


def set_agents(store: "Store", workspace_id: int, *, repo: str | None = None,
               reviewer: str | None = None, filter_name: str | None = None) -> dict:
    """Задать ревьювера репозитория и/или фильтр замечаний. «-» или пусто — вернуть дефолт."""
    updates: dict[str, str] = {}
    drops: list[str] = []
    if repo and reviewer is not None:
        key = REVIEWER_PREFIX + repo
        if reviewer.strip() in ("", "-", DEFAULT_REVIEWER):
            drops.append(key)
        else:
            updates[key] = reviewer.strip()
    if filter_name is not None:
        if filter_name.strip() in ("", "-", DEFAULT_FILTER):
            drops.append(FILTER_SETTING)
        else:
            updates[FILTER_SETTING] = filter_name.strip()
    if updates:
        store.set_workspace_settings(workspace_id, updates)
    if drops:
        store.delete_workspace_settings(workspace_id, drops)
    return agents(store, workspace_id)


def agents(store: "Store", workspace_id: int) -> dict:
    settings = store.workspace_settings(workspace_id)
    return {
        "filter_agent": filter_agent(store, workspace_id),
        "default_reviewer": DEFAULT_REVIEWER,
        "reviewers": {k[len(REVIEWER_PREFIX):]: v for k, v in sorted(settings.items())
                      if k.startswith(REVIEWER_PREFIX) and v},
    }


def my_status(pr: PR, login: str) -> str:
    """Мой статус ревью на PR: APPROVED | NEEDS_WORK | UNAPPROVED | "" (я не ревьювер)."""
    me = (login or "").casefold()
    for r in pr.reviewers:
        if me and me in ((r.name or "").casefold(), (r.display_name or "").casefold()):
            return (r.status or ("APPROVED" if r.approved else "UNAPPROVED")).upper()
    return pr.my_review_status or ""


def select(prs: Iterable[PR], login: str, *, include_approved: bool = False,
           ids: Iterable[int] | None = None, repos: Iterable[str] | None = None
           ) -> tuple[list[PR], list[dict]]:
    """Очередь ревью и пропущенные (с причиной). Порядок — как пришли (свежие сверху)."""
    id_set = {int(i) for i in ids} if ids else set()
    repo_set = {r.casefold() for r in repos} if repos else set()
    queue: list[PR] = []
    skipped: list[dict] = []
    for pr in prs:
        status = my_status(pr, login)
        reason = ""
        if id_set and pr.id not in id_set:
            reason = "не в списке номеров"
        elif repo_set and pr.repository.casefold() not in repo_set:
            reason = "не в списке репозиториев"
        elif status == "APPROVED" and not include_approved:
            reason = "мой статус уже APPROVED"
        if reason:
            skipped.append({"pr": pr.id, "repo": pr.repository, "title": pr.title, "reason": reason})
        else:
            queue.append(pr)
    return queue, skipped


def plan(store: "Store", workspace_id: int, slug: str, prs: Iterable[PR], login: str, *,
         include_approved: bool = False, ids: Iterable[int] | None = None,
         repos: Iterable[str] | None = None, day: str | None = None) -> dict:
    """Всё, что нужно скиллу для фазы A: очередь с ревьюером и файлом раздела, пропуски, пути."""
    queue, skipped = select(prs, login, include_approved=include_approved, ids=ids, repos=repos)
    out_dir = reviews_dir(slug, day)
    return {
        "dir": str(out_dir),
        "summary": str(out_dir / "summary.md"),
        "filter_agent": filter_agent(store, workspace_id),
        "queue": [{
            "pr": pr.id, "project": pr.project, "repo": pr.repository, "title": pr.title,
            "author": pr.author, "source": pr.source_branch, "target": pr.target_branch,
            "task_key": task_key_of(pr.source_branch) or task_key_of(pr.title),
            "my_status": my_status(pr, login) or "UNAPPROVED",
            "reviewer_agent": reviewer_for(store, workspace_id, pr.repository),
            "review_file": str(out_dir / f"pr-{pr.repository}-{pr.id}.review.md"),
            "facts_file": str(out_dir / f"pr-{pr.repository}-{pr.id}.facts.md"),
            "url": pr.url,
        } for pr in queue],
        "skipped": skipped,
    }
