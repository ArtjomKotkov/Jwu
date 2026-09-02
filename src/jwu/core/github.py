"""Клиент GitHub (REST API v3) — задачи (Issues), PR и сборки (Actions) одним провайдером.

У Jira-контура источников три (Jira, Bitbucket, Jenkins), у GitHub-контура — один хост,
поэтому один класс закрывает обе роли: он и «клиент задач» (как ``JiraClient``), и
«клиент PR» (как ``BitbucketClient``). Сервисный слой работает с ними через одинаковый
набор методов и о провайдере не знает.

Роль задач:  ``myself``, ``search``, ``issue``, ``worklogs``, ``add_worklog``,
             ``download_attachment``, ``mention_marker``.
Роль PR:     ``ping``, ``dashboard_prs``, ``pr``, ``fill_merge_status``, ``latest_commit``,
             ``pr_commits``, ``pr_comments``, ``my_review_at``, ``build_statuses``,
             ``build_report``.

Авторизация — Personal Access Token: ``Authorization: Bearer <PAT>``. Работает и с
github.com, и с GitHub Enterprise (там ``api_url`` = ``https://host/api/v3``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import httpx

from .models import (
    PR,
    BuildReport,
    BuildStatus,
    Comment,
    DevBranch,
    DevPullRequest,
    Issue,
    PRComment,
    Reviewer,
    TestCaseFailure,
    gh_ms,
    gh_time,
    github_key,
    parse_github_key,
)

# Вью PR -> квалификатор поиска GitHub. Роли те же, что у dashboard-эндпоинта Bitbucket.
PR_VIEW_QUERY = {
    "mine": "is:pr is:open author:@me",
    "review": "is:pr is:open review-requested:@me",
}

# https://github.com/o/r/actions/runs/123/job/456 — из ссылки на check-run достаём
# идентификаторы прогона и джобы: по ним и лежит вся детализация Actions.
_ACTIONS_URL_RE = re.compile(r"/actions/runs/(\d+)(?:/job/(\d+))?")

# Сколько записей тянуть на страницу и сколько страниц максимум (защита от «всей истории»).
_PER_PAGE = 100
_MAX_PAGES = 10

# Шаг джобы Actions, который считаем падением.
_FAILED_STEP_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required"})


class GitHubError(RuntimeError):
    """Ошибка обращения к GitHub (с кодом ответа, если есть)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def parse_actions_url(url: str) -> tuple[int, int | None] | None:
    """Из ссылки на сборку GitHub Actions вытащить ``(run_id, job_id | None)``."""
    match = _ACTIONS_URL_RE.search(url or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)) if match.group(2) else None


def _hunk_lines(diff_hunk: str, max_lines: int = 24) -> list[str]:
    """Строки ``diff_hunk`` без заголовка ``@@`` — контекст inline-коммента.

    GitHub отдаёт кусок диффа, ПОСЛЕДНЯЯ строка которого и есть прокомментированная, —
    поэтому якорь всегда в конце (см. ``anchor_idx`` ниже).
    """
    lines = [ln for ln in (diff_hunk or "").splitlines() if not ln.startswith("@@")]
    return lines[-max_lines:]


class GitHubClient:
    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        owner: str = "",
        repos: Optional[list[str]] = None,
        web_url: str = "https://github.com",
        views: Optional[dict[str, str]] = None,
        client: Optional[httpx.Client] = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.web_url = web_url.rstrip("/")
        self.owner = owner
        self.repos = list(repos or [])
        self.views = dict(views or {})
        self._owner_kind: str | None = None  # user | organization (для квалификатора поиска)
        self._me: dict | None = None
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.api_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- низкоуровневое --------------------------------------------------- #

    def _raise_for(self, resp: httpx.Response) -> None:
        if resp.status_code == 401:
            raise GitHubError("401: токен GitHub невалиден", 401)
        if resp.status_code == 403:
            # 403 у GitHub — это и «нет прав», и исчерпанный лимит запросов;
            # различать их важно, потому что чинятся они по-разному.
            if resp.headers.get("x-ratelimit-remaining") == "0":
                raise GitHubError("403: исчерпан лимит запросов GitHub — подожди сброса", 403)
            raise GitHubError("403: нет прав в GitHub (проверь scope токена)", 403)
        if resp.status_code >= 400:
            raise GitHubError(f"{resp.status_code}: {resp.text[:200]}", resp.status_code)

    def _request(self, path: str, params: dict | None = None) -> httpx.Response:
        try:
            return self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise GitHubError(f"Сеть/GitHub недоступен: {exc}") from exc

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = self._request(path, params)
        self._raise_for(resp)
        return resp.json()

    def _get_list(self, path: str, params: dict | None = None) -> list[dict]:
        resp = self._request(path, params)
        self._raise_for(resp)
        data = resp.json()
        return data if isinstance(data, list) else []

    def _paged(
        self, path: str, params: dict | None = None, *, key: str = ""
    ) -> list[dict]:
        """Все страницы списочного эндпоинта (page/per_page, до ``_MAX_PAGES``).

        ``key`` нужен эндпоинтам Actions: они, в отличие от остального REST API,
        отдают не массив, а объект вида ``{"total_count": N, "jobs": [...]}``.
        """
        params = dict(params or {})
        params.setdefault("per_page", _PER_PAGE)
        out: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            params["page"] = page
            if key:
                batch = self._get(path, params).get(key, []) or []
            else:
                batch = self._get_list(path, params)
            out.extend(batch)
            if len(batch) < params["per_page"]:
                break
        return out

    def _post(self, path: str, body: dict) -> dict:
        try:
            resp = self._client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise GitHubError(f"Сеть/GitHub недоступен: {exc}") from exc
        self._raise_for(resp)
        return resp.json() if resp.content else {}

    # --- личность и область поиска ---------------------------------------- #

    def ping(self) -> dict:
        """Проверка токена: ``/user`` требует авторизации."""
        return self._get("/user")

    def myself(self) -> dict:
        """Текущий пользователь в терминах jwu (как ``/myself`` Jira).

        У GitHub нет отдельного «отображаемого имени» в большинстве профилей —
        тогда показываем логин, чтобы шапка дашборда не пустовала.
        """
        if self._me is None:
            raw = self._get("/user")
            self._me = {
                "name": raw.get("login", "") or "",
                "displayName": raw.get("name", "") or raw.get("login", "") or "",
                "emailAddress": raw.get("email", "") or "",
            }
        return self._me

    def mention_marker(self, login: str) -> str:
        """Как в тексте выглядит упоминание меня (у Jira это ``[~login]``)."""
        return f"@{login}"

    def _scope(self) -> str:
        """Квалификатор поиска, ограничивающий выборку репозиториями контура.

        Заданы конкретные репозитории — ищем в них; иначе ограничиваемся владельцем
        (``org:`` или ``user:`` — узнаём тип один раз). Без владельца область не сужаем:
        ``assignee:@me`` и так отдаёт только мои задачи.
        """
        if self.repos:
            return " ".join(f"repo:{self._full_repo(r)}" for r in self.repos)
        if not self.owner:
            return ""
        if self._owner_kind is None:
            try:
                self._owner_kind = (self._get(f"/users/{self.owner}").get("type", "")
                                    or "user").lower()
            except GitHubError:
                self._owner_kind = "user"
        return f"org:{self.owner}" if self._owner_kind == "organization" else f"user:{self.owner}"

    def _full_repo(self, repo: str) -> str:
        """``repo`` → ``owner/repo`` (если owner не указан прямо в имени)."""
        return repo if "/" in repo else f"{self.owner}/{repo}"

    def _split_repo(self, project: str, repo: str) -> tuple[str, str]:
        """(owner, repo) из пары, как её хранит jwu; пустой owner добирается из конфига."""
        if repo and "/" in repo:
            owner, _, name = repo.partition("/")
            return owner, name
        return project or self.owner, repo

    def _search(self, query: str, *, limit: int = 100) -> list[dict]:
        """Поиск issues/PR. GitHub отдаёт максимум 1000 совпадений — этого с запасом."""
        out: list[dict] = []
        per_page = min(_PER_PAGE, limit)
        for page in range(1, _MAX_PAGES + 1):
            try:
                data = self._get(
                    "/search/issues",
                    params={"q": query, "per_page": per_page, "page": page,
                            "sort": "updated", "order": "desc"},
                )
            except GitHubError as exc:
                # 422 тут значит ровно одно: owner/репозитории из настроек GitHub не видны
                # токену. Сообщение GitHub про это ничего не подсказывает, а причина всегда
                # в конфиге контура либо в правах токена — говорим прямо.
                if exc.status_code != 422:
                    raise
                where = ", ".join(self.repos) or self.owner or "—"
                raise GitHubError(
                    f"GitHub не нашёл, где искать: {where}. Проверь owner и репозитории "
                    f"(jwu workspace show) и права токена: у fine-grained токена resource "
                    f"owner должен совпадать с владельцем репозиториев, а сами репозитории "
                    f"— быть в списке разрешённых.",
                    422,
                ) from exc
            items = data.get("items", []) or []
            out.extend(items)
            if len(items) < per_page or len(out) >= min(limit, int(data.get("total_count", 0))):
                break
        return out[:limit]

    # --- задачи (роль JiraClient) ----------------------------------------- #

    def search(self, query: str, *, max_results: int = 100, **_: object) -> list[Issue]:
        """Задачи по запросу в синтаксисе поиска GitHub (аналог JQL-поиска Jira)."""
        scope = self._scope()
        full = f"{query} {scope}".strip() if scope else query
        return [
            Issue.from_github(raw, default_owner=self.owner)
            for raw in self._search(full, limit=max_results)
            if "pull_request" not in raw  # PR у GitHub тоже issue — в задачи их не пускаем
        ]

    def issue(self, key: str, *, with_dev: bool = True) -> Issue:
        """Карточка задачи: поля, описание, комментарии + связанные ветки и PR."""
        ref = self._require_ref(key)
        owner, repo, number = ref
        raw = self._get(f"/repos/{owner}/{repo}/issues/{number}")
        issue = Issue.from_github(raw, default_owner=self.owner)
        try:
            issue.comments = [
                Comment.from_github(c)
                for c in self._paged(f"/repos/{owner}/{repo}/issues/{number}/comments")
            ]
        except GitHubError:
            pass  # без комментариев карточка всё равно полезна
        if with_dev:
            issue.dev_ok = self._fill_dev(issue, owner, repo, number)
        return issue

    def _require_ref(self, key: str) -> tuple[str, str, int]:
        ref = parse_github_key(
            key,
            default_owner=self.owner,
            default_repo=self.repos[0] if len(self.repos) == 1 else "",
        )
        if ref is None:
            raise GitHubError(
                f"«{key}» не похоже на задачу GitHub. Ожидается repo#42 или owner/repo#42."
            )
        return ref

    def _fill_dev(self, issue: Issue, owner: str, repo: str, number: int) -> bool:
        """Связанные PR (из ленты событий) и ветки задачи. False — если что-то не доехало.

        ``dev_ok`` важен так же, как у Jira: пустой из-за сбоя список PR не должен
        выглядеть как «PR действительно нет» и плодить фантомные дельты.
        """
        ok = True
        try:
            for event in self._paged(f"/repos/{owner}/{repo}/issues/{number}/timeline"):
                src = ((event.get("source") or {}).get("issue") or {})
                if event.get("event") not in ("cross-referenced", "connected"):
                    continue
                if "pull_request" not in src:
                    continue
                pr_repo = (src.get("repository") or {}).get("name", "") or repo
                issue.pull_requests.append(DevPullRequest(
                    id=str(src.get("number", "")),
                    name=src.get("title", "") or "",
                    url=src.get("html_url", "") or "",
                    status=self._pr_state(src),
                ))
        except GitHubError:
            ok = False
        try:
            # Ветка задачи в GitHub — та, что названа по её номеру («42-fix-crash»):
            # именно так их создаёт кнопка «Create a branch» и так их принято называть.
            prefix = f"{number}-"
            for br in self._get_list(
                f"/repos/{owner}/{repo}/branches", {"per_page": _PER_PAGE}
            ):
                name = br.get("name", "") or ""
                if name.startswith(prefix) or f"#{number}" in name:
                    issue.branches.append(DevBranch(
                        name=name,
                        url=f"{self.web_url}/{owner}/{repo}/tree/{name}",
                        repository=repo,
                    ))
        except GitHubError:
            ok = False
        return ok

    @staticmethod
    def _pr_state(raw: dict) -> str:
        if (raw.get("state", "") or "").lower() == "open":
            return "OPEN"
        merged = (raw.get("pull_request") or {}).get("merged_at") or raw.get("merged_at")
        return "MERGED" if merged else "DECLINED"

    def add_comment(self, key: str, body: str) -> dict:
        """Комментарий к Issue/PR (в GitHub это один и тот же эндпоинт)."""
        owner, repo, number = self._require_ref(key)
        return self._post(f"/repos/{owner}/{repo}/issues/{number}/comments", {"body": body})

    _NO_CREATE = (
        "Создание задач в jwu сделано для Jira (REST v2 + createmeta) и в контуре GitHub "
        "не поддержано. Заводи Issue в вебе или через gh issue create."
    )

    def create_meta(self, project: str) -> dict:
        """В GitHub нет схемы полей проекта — проверять createmeta нечего."""
        raise GitHubError(self._NO_CREATE)

    def create_issue(self, fields: dict) -> dict:
        """Создание задач в контуре GitHub не поддержано — отказываемся явно."""
        raise GitHubError(self._NO_CREATE)

    def transitions(self, key: str) -> list[dict]:
        """У Issue GitHub нет схемы переходов — только open/closed."""
        return []

    def do_transition(self, key: str, transition_id: str) -> dict:
        raise GitHubError(
            "В GitHub нет переходов по процессу: у Issue только open/closed. "
            "Меняй состояние в вебе или через gh issue close/reopen."
        )

    def add_attachment(self, key: str, path) -> list[dict]:
        raise GitHubError(
            "GitHub REST API не умеет прикладывать файлы к Issue: вложения там — "
            "ссылки в тексте, загруженные через веб-интерфейс."
        )

    def link_types(self) -> list[dict]:
        """Типизированных связей между Issue в GitHub нет."""
        return []

    def link_issues(self, inward: str, outward: str, link_type: str) -> dict:
        """Связей вида Jira issueLink в GitHub нет — только упоминания в тексте."""
        raise GitHubError(
            "В GitHub нет типизированных связей между задачами: сошлись на задачу "
            "упоминанием в тексте (#42) или закрывающим ключевым словом."
        )

    def add_worklog(self, key: str, time_spent: str, **_: object) -> dict:
        """В GitHub таймтрекера нет — честно отказываемся, а не пишем в никуда."""
        raise GitHubError(
            "В GitHub нет таймтрекера: залогировать время некуда. "
            "Учитывай его в записях работы (jwu job add)."
        )

    def worklogs(self, key: str) -> list[dict]:
        return []

    def download_attachment(self, url: str, dest: Path) -> Path:
        """Скачать вложение по абсолютной ссылке (в issue они лежат ссылками в тексте).

        Вложения GitHub живут на отдельных хостах (user-images.githubusercontent.com и т.п.)
        и авторизации не требуют. Токен туда не отправляем: утечь он может ровно так —
        по невнимательности, вместе с картинкой из чужого комментария.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        own_host = urlsplit(self.api_url).netloc
        client = self._client if urlsplit(url).netloc == own_host else httpx.Client(
            timeout=self._client.timeout, follow_redirects=True
        )
        try:
            with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise GitHubError(f"{resp.status_code}: не скачать вложение {url}",
                                      resp.status_code)
                with dest.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise GitHubError(f"Сеть/GitHub недоступен при скачивании вложения: {exc}") from exc
        finally:
            if client is not self._client:
                client.close()
        return dest

    # --- PR (роль BitbucketClient) ---------------------------------------- #

    def dashboard_prs(self, view: str, *, state: str = "OPEN") -> list[PR]:
        """Мои PR (author) или ждущие моего ревью (review-requested).

        Поиск отдаёт issue-представление PR — без веток, ревьюверов и мержабельности,
        поэтому по каждому найденному PR дочитываем карточку. Для личных проектов это
        единицы запросов, зато дальше все экраны работают на полных данных.
        """
        query = PR_VIEW_QUERY.get(view)
        if query is None:
            raise GitHubError(f"Неизвестный вью PR: {view!r} (mine|review)")
        if state and state.upper() != "OPEN":
            query = query.replace("is:open", f"is:{state.lower()}")
        scope = self._scope()
        found = self._search(f"{query} {scope}".strip() if scope else query)
        out: list[PR] = []
        for raw in found:
            owner, repo = self._repo_from_url(raw.get("repository_url", ""))
            number = int(raw.get("number", 0) or 0)
            if not (owner and repo and number):
                continue
            try:
                out.append(self.pr(owner, repo, number))
            except GitHubError:
                continue  # один недоступный репозиторий не должен ронять весь список
        return out

    @staticmethod
    def _repo_from_url(url: str) -> tuple[str, str]:
        parts = [p for p in (url or "").split("/") if p]
        return (parts[-2], parts[-1]) if len(parts) >= 2 else ("", "")

    def pr(self, project: str, repo: str, pr_id: int, *, with_merge: bool = True) -> PR:
        """Карточка PR + фактические статусы ревью (в payload PR их нет)."""
        owner, name = self._split_repo(project, repo)
        raw = self._get(f"/repos/{owner}/{name}/pulls/{pr_id}")
        pull = PR.from_github(raw)
        try:
            pull.reviewers = self._reviewers(owner, name, pr_id, requested=pull.reviewers)
        except GitHubError:
            pass
        return pull

    def _reviewers(
        self, owner: str, repo: str, pr_id: int, *, requested: list[Reviewer] | None = None
    ) -> list[Reviewer]:
        """Ревьюверы: запрошенные + те, кто уже высказался (последний отзыв каждого).

        Комментирующие отзывы (COMMENTED) статус не меняют — иначе «оставил коммент»
        выглядело бы как «посмотрел и не возражает».
        """
        by_login: dict[str, Reviewer] = {r.name: r for r in (requested or [])}
        for review in self._paged(f"/repos/{owner}/{repo}/pulls/{pr_id}/reviews"):
            login = ((review.get("user") or {}).get("login", "") or "")
            state = (review.get("state", "") or "").upper()
            if not login or state not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                continue
            by_login[login] = Reviewer.from_github(
                login, "" if state == "DISMISSED" else state
            )
        return list(by_login.values())

    def fill_merge_status(self, pr: PR) -> None:
        """Догрузить статус merge-конфликта (у GitHub он приходит вместе с карточкой PR).

        Метод оставлен ради единого контракта с Bitbucket, где за него отвечает отдельный
        эндпоинт. Если конфликт уже известен — сети не касаемся.
        """
        if pr.conflicted is not None:
            return
        owner, name = self._split_repo(pr.project, pr.repository)
        pr.apply_github_merge(self._get(f"/repos/{owner}/{name}/pulls/{pr.id}"))

    def merge_status(self, project: str, repo: str, pr_id: int) -> dict:
        """Сырые поля мержабельности — для совместимости с контрактом Bitbucket."""
        owner, name = self._split_repo(project, repo)
        return self._get(f"/repos/{owner}/{name}/pulls/{pr_id}")

    def latest_commit(self, project: str, repo: str, pr_id: int) -> str:
        """SHA головного коммита PR (дёшево — прямо из карточки)."""
        owner, name = self._split_repo(project, repo)
        raw = self._get(f"/repos/{owner}/{name}/pulls/{pr_id}")
        return ((raw.get("head") or {}).get("sha", "") or "")

    def pr_commits(self, project: str, repo: str, pr_id: int, *, limit: int = 25) -> list[dict]:
        owner, name = self._split_repo(project, repo)
        raw = self._get_list(
            f"/repos/{owner}/{name}/pulls/{pr_id}/commits", {"per_page": limit}
        )
        out = []
        for c in raw:
            commit = c.get("commit") or {}
            out.append({
                "id": (c.get("sha", "") or "")[:11],
                "message": (commit.get("message", "") or "").strip(),
                "author": ((c.get("author") or {}).get("login")
                           or (commit.get("author") or {}).get("name", "") or ""),
            })
        return out

    def my_review_at(self, project: str, repo: str, pr_id: int, login: str) -> int | None:
        """Дата (epoch ms) моего последнего ревью-действия по PR."""
        owner, name = self._split_repo(project, repo)
        best: int | None = None
        for review in self._paged(f"/repos/{owner}/{name}/pulls/{pr_id}/reviews"):
            if ((review.get("user") or {}).get("login", "") or "") != login:
                continue
            if (review.get("state", "") or "").upper() not in (
                "APPROVED", "CHANGES_REQUESTED", "DISMISSED"
            ):
                continue
            ts = gh_ms(review.get("submitted_at", "") or "")
            if ts and (best is None or ts > best):
                best = ts
        return best

    def pr_comments(self, project: str, repo: str, pr_id: int) -> list[PRComment]:
        """Комментарии PR: обсуждение, тексты ревью и inline-замечания с куском диффа.

        Порядок как у Bitbucket — свежие треды первыми, ответы внутри треда по времени.
        """
        owner, name = self._split_repo(project, repo)
        threads: list[list[PRComment]] = []

        for raw in self._paged(f"/repos/{owner}/{name}/issues/{pr_id}/comments"):
            threads.append([PRComment(
                id=str(raw.get("id", "")),
                author=((raw.get("user") or {}).get("login", "") or ""),
                text=raw.get("body", "") or "",
                created=gh_ms(raw.get("created_at", "") or ""),
            )])

        for raw in self._paged(f"/repos/{owner}/{name}/pulls/{pr_id}/reviews"):
            body = (raw.get("body", "") or "").strip()
            if not body:
                continue  # отзыв без текста — это статус, а не комментарий
            state = (raw.get("state", "") or "").upper()
            mark = {"APPROVED": "✅ ", "CHANGES_REQUESTED": "🔴 "}.get(state, "")
            threads.append([PRComment(
                id=str(raw.get("id", "")),
                author=((raw.get("user") or {}).get("login", "") or ""),
                text=f"{mark}{body}",
                created=gh_ms(raw.get("submitted_at", "") or ""),
            )])

        # inline-замечания: верхнеуровневые образуют тред, ответы (in_reply_to_id) в него падают
        inline = self._paged(f"/repos/{owner}/{name}/pulls/{pr_id}/comments")
        roots: dict[int, list[PRComment]] = {}
        for raw in inline:
            cid = int(raw.get("id", 0) or 0)
            parent = raw.get("in_reply_to_id")
            lines = _hunk_lines(raw.get("diff_hunk", "") or "")
            comment = PRComment(
                id=str(cid),
                author=((raw.get("user") or {}).get("login", "") or ""),
                text=raw.get("body", "") or "",
                created=gh_ms(raw.get("created_at", "") or ""),
                file=raw.get("path", "") or "",
                line=raw.get("line") or raw.get("original_line"),
                depth=0 if parent is None else 1,
                # GitHub присылает дифф, ПОСЛЕДНЯЯ строка которого и есть прокомментированная
                context=lines if parent is None else [],
                anchor_idx=(len(lines) - 1) if (parent is None and lines) else -1,
            )
            if parent is None:
                roots[cid] = [comment]
            else:
                roots.setdefault(int(parent), []).append(comment)
        threads.extend(roots.values())

        threads.sort(key=lambda group: group[0].created, reverse=True)
        return [c for group in threads for c in group]

    # --- сборки (GitHub Actions) ------------------------------------------ #

    def build_statuses(
        self, commit_sha: str, *, project: str = "", repo: str = ""
    ) -> list[BuildStatus]:
        """Статусы сборок по коммиту: check-runs (Actions) + классические commit statuses.

        В отличие от Bitbucket, у GitHub нет глобального поиска по SHA — нужен репозиторий,
        поэтому ``project``/``repo`` обязательны.
        """
        owner, name = self._split_repo(project, repo)
        if not (owner and name and commit_sha):
            return []
        out: list[BuildStatus] = []
        try:
            data = self._get(
                f"/repos/{owner}/{name}/commits/{commit_sha}/check-runs",
                {"per_page": _PER_PAGE},
            )
            out.extend(
                BuildStatus.from_github_check(raw)
                for raw in data.get("check_runs", []) or []
            )
        except GitHubError:
            pass
        try:
            # Внешние CI (Codecov, Travis и пр.) живут отдельным API статусов коммита.
            data = self._get(f"/repos/{owner}/{name}/commits/{commit_sha}/status")
            out.extend(
                BuildStatus.from_github_status(raw)
                for raw in data.get("statuses", []) or []
            )
        except GitHubError:
            pass
        return out

    def build_report(self, project: str, repo: str, chosen: BuildStatus) -> BuildReport:
        """Разбор сборки Actions: результат прогона, упавшие шаги и хвост лога.

        Логика та же, что у Jenkins-отчёта: без доступа к Actions отчёт мягко деградирует
        до статуса чек-рана с пояснением в ``note``.
        """
        owner, name = self._split_repo(project, repo)
        report = BuildReport(
            ci="github-actions",
            state=chosen.state,
            name=chosen.name,
            url=chosen.url,
            description=chosen.description,
        )
        parsed = parse_actions_url(chosen.url)
        if parsed is None:
            report.note = (
                "Это не сборка GitHub Actions (внешний CI) — детализация доступна "
                f"по ссылке: {chosen.url}" if chosen.url else "У сборки нет ссылки на CI."
            )
            return report
        run_id, job_id = parsed
        report.job_path = f"{owner}/{name}"
        report.number = run_id

        try:
            run = self._get(f"/repos/{owner}/{name}/actions/runs/{run_id}")
            report.details_available = True
            report.result = (run.get("conclusion", "") or "").upper()
            report.building = (run.get("status", "") or "") != "completed"
            report.sha = run.get("head_sha", "") or ""
            report.branch = run.get("head_branch", "") or ""
            report.name = report.name or (run.get("name", "") or "")
            report.number = int(run.get("run_number", run_id) or run_id)

            jobs = self._paged(
                f"/repos/{owner}/{name}/actions/runs/{run_id}/jobs",
                {"filter": "latest"}, key="jobs",
            )
            failed_jobs = [
                j for j in jobs
                if (j.get("conclusion", "") or "").lower() in _FAILED_STEP_CONCLUSIONS
            ]
            report.summary = {
                "fail": len(failed_jobs),
                "passed": len([j for j in jobs
                               if (j.get("conclusion", "") or "").lower() == "success"]),
                "skip": len([j for j in jobs
                             if (j.get("conclusion", "") or "").lower() == "skipped"]),
            }
            for job in failed_jobs:
                for step in job.get("steps", []) or []:
                    if (step.get("conclusion", "") or "").lower() not in _FAILED_STEP_CONCLUSIONS:
                        continue
                    report.failures.append(TestCaseFailure(
                        class_name=job.get("name", "") or "",
                        name=step.get("name", "") or "",
                        status=(step.get("conclusion", "") or "").upper(),
                        error_details=f"шаг {step.get('number', '?')} упал"
                                      f" ({gh_time(step.get('completed_at', '') or '')})",
                    ))
            # Лог берём у упавшей джобы (или у той, на которую указывала ссылка).
            target = job_id or (failed_jobs[0].get("id") if failed_jobs else None)
            if target and not report.building:
                report.console_tail = self.job_log_tail(owner, name, int(target))
        except GitHubError as exc:
            report.note = f"GitHub Actions недоступны: {exc}"
        return report

    def job_log_tail(self, owner: str, repo: str, job_id: int, *, chars: int = 6000) -> str:
        """Хвост лога джобы Actions. Пусто, если логи уже вычищены по ретенции."""
        try:
            resp = self._client.get(
                f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs", follow_redirects=True
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"Сеть/GitHub недоступен: {exc}") from exc
        if resp.status_code == 404:
            return ""  # логи протухли — это норма, а не ошибка
        self._raise_for(resp)
        text = resp.text
        return text[-chars:] if len(text) > chars else text

    # --- вспомогательное для сервисного слоя ------------------------------ #

    def issue_key(self, repo: str, number: int, *, owner: str = "") -> str:
        """Ключ задачи в формате jwu: ``repo#42`` (или ``owner/repo#42`` для чужого owner'а)."""
        return github_key(repo, number, owner=owner, default_owner=self.owner)

    def issue_url(self, key: str) -> str:
        """Ссылка на задачу в вебе (в API-ответах она есть, но ключ приходит и «сухим»)."""
        ref = parse_github_key(
            key, default_owner=self.owner,
            default_repo=self.repos[0] if len(self.repos) == 1 else "",
        )
        if ref is None:
            return ""
        owner, repo, number = ref
        return f"{self.web_url}/{owner}/{repo}/issues/{number}"
