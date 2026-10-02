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
           include_needs_work: bool = False,
           ids: Iterable[int] | None = None, repos: Iterable[str] | None = None
           ) -> tuple[list[PR], list[dict], list[PR]]:
    """(очередь, пропущенные с причиной, мои NEEDS_WORK под вопрос). Порядок — как пришли.

    PR, где мой статус уже NEEDS_WORK, в очередь сразу не идут: смотреть их повторно
    имеет смысл, только если автор что-то поправил, — это решает пользователь. Явно
    названные номера (``ids``) и ``include_needs_work`` идут в очередь без вопроса.
    """
    id_set = {int(i) for i in ids} if ids else set()
    repo_set = {r.casefold() for r in repos} if repos else set()
    queue: list[PR] = []
    skipped: list[dict] = []
    ask: list[PR] = []
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
        elif status == "NEEDS_WORK" and not include_needs_work and not id_set:
            ask.append(pr)
        else:
            queue.append(pr)
    return queue, skipped, ask


def since_my_review(client, pr: PR, login: str) -> dict:
    """Что было в PR после моего последнего ревью-действия: когда оно, сколько коммитов после.

    Нужно, чтобы решить, смотреть ли PR с моим NEEDS_WORK повторно: нет новых коммитов —
    автор ещё не поправил. Ошибка сети — пустые поля, а не отказ всей очереди.
    """
    out: dict = {"my_review_at": None, "new_commits": None, "last_commit_at": None}
    try:
        at = client.my_review_at(pr.project, pr.repository, pr.id, login)
        commits = client.pr_commits(pr.project, pr.repository, pr.id, limit=50)
    except Exception:  # noqa: BLE001
        return out
    stamps = [int(c.get("ts") or 0) for c in commits if c.get("ts")]
    out["my_review_at"] = at
    out["last_commit_at"] = max(stamps) if stamps else None
    if at is not None and stamps:
        out["new_commits"] = sum(1 for t in stamps if t > at)
    return out


def _item(store: "Store", workspace_id: int, out_dir: Path, pr: PR, login: str) -> dict:
    return {
        "pr": pr.id, "project": pr.project, "repo": pr.repository, "title": pr.title,
        "author": pr.author, "source": pr.source_branch, "target": pr.target_branch,
        "task_key": task_key_of(pr.source_branch) or task_key_of(pr.title),
        "my_status": my_status(pr, login) or "UNAPPROVED",
        "reviewer_agent": reviewer_for(store, workspace_id, pr.repository),
        "review_file": str(out_dir / f"pr-{pr.repository}-{pr.id}.review.md"),
        "facts_file": str(out_dir / f"pr-{pr.repository}-{pr.id}.facts.md"),
        "url": pr.url,
    }


def plan(store: "Store", workspace_id: int, slug: str, prs: Iterable[PR], login: str, *,
         include_approved: bool = False, include_needs_work: bool = False,
         ids: Iterable[int] | None = None, repos: Iterable[str] | None = None,
         day: str | None = None, client=None) -> dict:
    """Всё, что нужно скиллу для фазы A: очередь с ревьюером и файлом раздела, пропуски,
    PR с моим NEEDS_WORK под вопрос (с тем, что было после моего ревью — если дан client)."""
    queue, skipped, ask = select(prs, login, include_approved=include_approved,
                                 include_needs_work=include_needs_work, ids=ids, repos=repos)
    out_dir = reviews_dir(slug, day)
    needs_work = []
    for pr in ask:
        item = _item(store, workspace_id, out_dir, pr, login)
        if client is not None:
            item.update(since_my_review(client, pr, login))
        needs_work.append(item)
    return {
        "dir": str(out_dir),
        "summary": str(out_dir / "summary.md"),
        "filter_agent": filter_agent(store, workspace_id),
        "queue": [_item(store, workspace_id, out_dir, pr, login) for pr in queue],
        "needs_work": needs_work,
        "skipped": skipped,
    }
