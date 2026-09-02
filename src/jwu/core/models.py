"""Pydantic-модели и парсинг сырых ответов Jira / Bitbucket / GitHub.

Сырые JSON сильно вложены; модели держат уже «плоское» представление, а классметоды
`from_jira_*` / `from_bitbucket_*` / `from_github_*` инкапсулируют разбор. Одни и те же
модели наполняются любым провайдером — поэтому TUI, память и дельты про провайдера
ничего не знают.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, Field, computed_field


def _get(d: Any, *path: str, default: Any = None) -> Any:
    """Безопасно достать вложенное значение по пути ключей."""
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


# --------------------------------------------------------------------------- #
# Jira
# --------------------------------------------------------------------------- #


class Comment(BaseModel):
    id: str
    author: str = ""
    author_key: str = ""
    body: str = ""
    created: str = ""
    updated: str = ""

    @classmethod
    def from_jira(cls, raw: dict) -> "Comment":
        return cls(
            id=str(raw.get("id", "")),
            author=_get(raw, "author", "displayName", default="") or "",
            author_key=_get(raw, "author", "name", default="") or "",
            body=raw.get("body", "") or "",
            created=raw.get("created", "") or "",
            updated=raw.get("updated", "") or "",
        )

    @classmethod
    def from_github(cls, raw: dict) -> "Comment":
        login = _get(raw, "user", "login", default="") or ""
        return cls(
            id=str(raw.get("id", "")),
            author=login,
            author_key=login,  # у GitHub логин и есть отображаемое имя
            body=raw.get("body", "") or "",
            created=gh_time(raw.get("created_at", "") or ""),
            updated=gh_time(raw.get("updated_at", "") or ""),
        )


# Расширение → вид вложения (фильтр «что качать» + иконки). Видео мы не качаем.
_ATTACH_EXTS: dict[str, set[str]] = {
    "image": {"png", "jpg", "jpeg", "gif", "bmp", "webp", "svg", "tiff", "tif", "ico", "heic"},
    "log": {"log", "txt", "out", "json", "har", "xml", "csv", "yaml", "yml", "md",
            "ini", "conf", "properties", "trace", "tsv"},
    "doc": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "rtf", "odt", "ods"},
    "archive": {"zip", "tar", "gz", "tgz", "rar", "7z", "bz2", "xz"},
    "video": {"mp4", "mov", "avi", "mkv", "webm", "wmv", "flv", "m4v", "mpg", "mpeg"},
}
_EXT_TO_KIND: dict[str, str] = {ext: k for k, exts in _ATTACH_EXTS.items() for ext in exts}

# Виды, которые имеет смысл скачивать для анализа (видео и прочее — мимо).
DOWNLOADABLE_ATTACH_KINDS = ("image", "log", "doc", "archive")


def classify_attachment(filename: str, mime: str = "") -> str:
    """Вид вложения по расширению, с откатом на mime: image|log|doc|archive|video|other."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in _EXT_TO_KIND:
        return _EXT_TO_KIND[ext]
    m = (mime or "").lower()
    if m.startswith("image/"):
        return "image"
    if m.startswith("video/"):
        return "video"
    if m.startswith("text/"):
        return "log"
    return "other"


class Attachment(BaseModel):
    id: str = ""
    filename: str = ""
    mime: str = ""
    size: int = 0          # байты
    created: str = ""
    author: str = ""
    url: str = ""          # абсолютный URL контента на хосте Jira

    @computed_field  # type: ignore[prop-decorator]
    @property
    def kind(self) -> str:
        return classify_attachment(self.filename, self.mime)

    @classmethod
    def from_jira(cls, raw: dict) -> "Attachment":
        return cls(
            id=str(raw.get("id", "")),
            filename=raw.get("filename", "") or "",
            mime=raw.get("mimeType", "") or "",
            size=int(raw.get("size", 0) or 0),
            created=raw.get("created", "") or "",
            author=_get(raw, "author", "displayName", default="") or "",
            url=raw.get("content", "") or "",
        )


class IssueLink(BaseModel):
    type: str = ""
    direction: str = ""  # "inward" | "outward"
    key: str = ""
    summary: str = ""
    status: str = ""

    @classmethod
    def from_jira(cls, raw: dict) -> Optional["IssueLink"]:
        type_obj = raw.get("type", {}) or {}
        if "outwardIssue" in raw:
            issue = raw["outwardIssue"]
            direction, label = "outward", type_obj.get("outward", "")
        elif "inwardIssue" in raw:
            issue = raw["inwardIssue"]
            direction, label = "inward", type_obj.get("inward", "")
        else:
            return None
        return cls(
            type=label or type_obj.get("name", ""),
            direction=direction,
            key=issue.get("key", ""),
            summary=_get(issue, "fields", "summary", default="") or "",
            status=_get(issue, "fields", "status", "name", default="") or "",
        )


class DevBranch(BaseModel):
    name: str = ""
    url: str = ""
    repository: str = ""


class DevCommit(BaseModel):
    id: str = ""
    message: str = ""
    url: str = ""


class DevPullRequest(BaseModel):
    id: str = ""
    name: str = ""
    url: str = ""
    status: str = ""  # OPEN | MERGED | DECLINED


class Issue(BaseModel):
    key: str
    summary: str = ""
    status: str = ""
    assignee: str = ""
    reporter: str = ""
    priority: str = ""
    created: str = ""
    updated: str = ""
    resolution: str = ""
    description: str = ""
    comments: list[Comment] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    links: list[IssueLink] = Field(default_factory=list)
    # Метки GitHub (у Jira роль меток играет status) — по ним в Issues и понимают,
    # что происходит с задачей, поэтому в карточке и в таблице они нужны.
    labels: list[str] = Field(default_factory=list)
    branches: list[DevBranch] = Field(default_factory=list)
    commits: list[DevCommit] = Field(default_factory=list)
    pull_requests: list[DevPullRequest] = Field(default_factory=list)
    # Достоверны ли dev-данные (ветки/PR): False, если dev-status не запрашивался
    # (with_dev=False) или запрос упал. Нужно, чтобы пустой pr-список из-за сбоя не
    # затирал реально известные PR и не плодил фантомные new_pr на следующем синке.
    dev_ok: bool = True

    @classmethod
    def from_jira(cls, raw: dict) -> "Issue":
        f = raw.get("fields", {}) or {}
        links: list[IssueLink] = []
        for link_raw in f.get("issuelinks", []) or []:
            link = IssueLink.from_jira(link_raw)
            if link is not None:
                links.append(link)
        # Jira отдаёт комментарии в хронологическом порядке (старые сверху) — сохраняем.
        comments = [
            Comment.from_jira(c)
            for c in _get(f, "comment", "comments", default=[]) or []
        ]
        attachments = [Attachment.from_jira(a) for a in f.get("attachment", []) or []]
        return cls(
            key=raw.get("key", ""),
            summary=f.get("summary", "") or "",
            status=_get(f, "status", "name", default="") or "",
            assignee=_get(f, "assignee", "displayName", default="") or "",
            reporter=_get(f, "reporter", "displayName", default="") or "",
            priority=_get(f, "priority", "name", default="") or "",
            created=f.get("created", "") or "",
            updated=f.get("updated", "") or "",
            resolution=_get(f, "resolution", "name", default="") or "",
            description=f.get("description", "") or "",
            comments=comments,
            attachments=attachments,
            links=links,
        )

    @classmethod
    def from_github(cls, raw: dict, *, default_owner: str = "") -> "Issue":
        """Issue GitHub → та же модель задачи, что и у Jira.

        ``status`` собирается из ``state``/``state_reason``: у GitHub нет рабочего процесса
        Jira, и «Открыта / Закрыта / Не будет сделана» — всё, что он про задачу знает;
        остальное живёт в метках.
        """
        owner, repo = _github_repo_from_url(raw.get("repository_url", ""))
        state = (raw.get("state", "") or "").lower()
        reason = (raw.get("state_reason", "") or "").lower()
        if state == "closed":
            status = "Не будет сделана" if reason == "not_planned" else "Закрыта"
        else:
            status = "Открыта"
        assignees = raw.get("assignees") or []
        assignee = _get(raw, "assignee", "login", default="") or (
            (assignees[0] or {}).get("login", "") if assignees else ""
        )
        return cls(
            key=github_key(repo, raw.get("number", 0), owner=owner, default_owner=default_owner),
            summary=raw.get("title", "") or "",
            status=status,
            assignee=assignee or "",
            reporter=_get(raw, "user", "login", default="") or "",
            created=gh_time(raw.get("created_at", "") or ""),
            updated=gh_time(raw.get("updated_at", "") or ""),
            resolution=reason if state == "closed" else "",
            description=raw.get("body", "") or "",
            labels=[
                (lb.get("name", "") if isinstance(lb, dict) else str(lb))
                for lb in raw.get("labels", []) or []
            ],
            dev_ok=False,  # ветки/PR приезжают отдельным запросом (см. GitHubClient.issue)
        )

    def apply_dev_status(self, detail: dict) -> None:
        """Заполнить ветки/коммиты/PR из ответа /rest/dev-status/.../detail."""
        # dataType=branch отдаёт ветки на верхнем уровне, repository вложен в каждую ветку.
        for br in detail.get("branches", []) or []:
            repo = br.get("repository")
            repo_name = repo.get("name", "") if isinstance(repo, dict) else (repo or "")
            self.branches.append(
                DevBranch(
                    name=br.get("name", ""),
                    url=br.get("url", ""),
                    repository=repo_name,
                )
            )
        for repo in detail.get("repositories", []) or []:
            repo_name = repo.get("name", "")
            for br in repo.get("branches", []) or []:
                self.branches.append(
                    DevBranch(
                        name=br.get("name", ""),
                        url=br.get("url", ""),
                        repository=repo_name,
                    )
                )
            for cm in repo.get("commits", []) or []:
                self.commits.append(
                    DevCommit(
                        id=cm.get("displayId", cm.get("id", "")),
                        message=cm.get("message", ""),
                        url=cm.get("url", ""),
                    )
                )
        for pr in detail.get("pullRequests", []) or []:
            self.pull_requests.append(
                DevPullRequest(
                    id=str(pr.get("id", "")),
                    name=pr.get("name", ""),
                    url=pr.get("url", ""),
                    status=pr.get("status", ""),
                )
            )


# --------------------------------------------------------------------------- #
# Bitbucket
# --------------------------------------------------------------------------- #


class Reviewer(BaseModel):
    name: str = ""
    display_name: str = ""
    approved: bool = False
    status: str = ""  # APPROVED | UNAPPROVED | NEEDS_WORK

    @classmethod
    def from_bitbucket(cls, raw: dict) -> "Reviewer":
        user = raw.get("user", {}) or {}
        return cls(
            name=user.get("name", ""),
            display_name=user.get("displayName", ""),
            approved=bool(raw.get("approved", False)),
            status=raw.get("status", ""),
        )

    @classmethod
    def from_github(cls, login: str, state: str = "") -> "Reviewer":
        """Ревьювер GitHub. ``state`` — из отзыва (APPROVED / CHANGES_REQUESTED / …).

        Пустой state = ревью запрошено, но не сделано, — это UNAPPROVED в терминах jwu:
        так «ждёт моего ревью» и «просили доработать» остаются разными состояниями.
        """
        state = (state or "").upper()
        status = {
            "APPROVED": "APPROVED",
            "CHANGES_REQUESTED": "NEEDS_WORK",
        }.get(state, "UNAPPROVED")
        return cls(
            name=login,
            display_name=login,
            approved=status == "APPROVED",
            status=status,
        )


class BuildStatus(BaseModel):
    """Статус CI-сборки по коммиту (build-status API Bitbucket — то, что видно на странице PR).

    ``state``: SUCCESSFUL | FAILED | INPROGRESS. ``url`` ведёт на сборку в CI (Jenkins) —
    это мост к детальному разбору падения (см. JenkinsClient).
    """

    state: str = ""
    key: str = ""
    name: str = ""
    url: str = ""
    description: str = ""
    date_added: int = 0  # epoch ms

    @classmethod
    def from_bitbucket(cls, raw: dict) -> "BuildStatus":
        return cls(
            state=raw.get("state", "") or "",
            key=raw.get("key", "") or "",
            name=raw.get("name", "") or "",
            url=raw.get("url", "") or "",
            description=raw.get("description", "") or "",
            date_added=int(raw.get("dateAdded", 0) or 0),
        )

    @classmethod
    def from_github_check(cls, raw: dict) -> "BuildStatus":
        """check-run GitHub (в т.ч. джоба GitHub Actions) → статус сборки.

        Незавершённый ран — INPROGRESS независимо от conclusion (его ещё нет);
        завершённый переводится по conclusion, где neutral/skipped считаются зелёными:
        они не про поломку, а про «шаг не выполнялся».
        """
        completed = (raw.get("status", "") or "").lower() == "completed"
        conclusion = (raw.get("conclusion", "") or "").lower()
        state = _GH_CONCLUSION_STATE.get(conclusion, "FAILED") if completed else "INPROGRESS"
        summary = _get(raw, "output", "title", default="") or ""
        return cls(
            state=state,
            key=str(raw.get("id", "") or ""),
            name=raw.get("name", "") or "",
            url=raw.get("html_url", "") or raw.get("details_url", "") or "",
            description=summary or (conclusion if completed else "выполняется"),
            date_added=gh_ms(raw.get("started_at", "") or raw.get("completed_at", "") or ""),
        )

    @classmethod
    def from_github_status(cls, raw: dict) -> "BuildStatus":
        """Классический commit status GitHub (внешние CI вроде Travis/Codecov)."""
        return cls(
            state=_GH_STATUS_STATE.get((raw.get("state", "") or "").lower(), "FAILED"),
            key=raw.get("context", "") or "",
            name=raw.get("context", "") or "",
            url=raw.get("target_url", "") or "",
            description=raw.get("description", "") or "",
            date_added=gh_ms(raw.get("updated_at", "") or raw.get("created_at", "") or ""),
        )


class TestCaseFailure(BaseModel):
    """Один упавший тест-кейс из Jenkins testReport."""

    class_name: str = ""
    name: str = ""
    status: str = ""  # FAILED | REGRESSION | ERROR
    error_details: str = ""
    stack: str = ""


class BuildReport(BaseModel):
    """Детальный разбор одной сборки: статус из хостинга + данные CI, если до них есть доступ.

    Формат общий для обоих провайдеров: у Jira-контура это Bitbucket + Jenkins, у
    GitHub-контура — check-run + GitHub Actions. Деградирует мягко: без доступа к CI
    остаются state/url/description, а ``details_available=False`` и ``note`` объясняют,
    почему нет детализации.
    """

    ci: str = "jenkins"    # jenkins | github-actions — чей разбор перед нами
    state: str = ""        # состояние сборки по данным хостинга (build-status / check-run)
    name: str = ""
    url: str = ""
    description: str = ""
    job_path: str = ""     # путь джобы Jenkins либо owner/repo для Actions
    number: int = 0
    details_available: bool = False
    result: str = ""       # из CI (FAILURE/SUCCESS/пусто, если ещё идёт)
    building: bool = False
    sha: str = ""
    branch: str = ""
    summary: Optional[dict] = None  # {"fail": int, "passed": int, "skip": int}
    failures: list[TestCaseFailure] = Field(default_factory=list)
    console_tail: str = ""
    note: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def jenkins_available(self) -> bool:
        """Совместимость: так это поле звалось, пока CI был только один (Jenkins)."""
        return self.details_available and self.ci == "jenkins"


class PRComment(BaseModel):
    id: str
    author: str = ""
    text: str = ""
    created: int = 0
    file: str = ""          # путь файла для inline-коммента, иначе пусто
    line: Optional[int] = None
    depth: int = 0          # 0 — верхний уровень, >0 — ответ
    context: list[str] = Field(default_factory=list)  # строки диффа вокруг (с +/-/ )
    anchor_idx: int = -1    # индекс прокомментированной строки в context (-1 = неизвестно)


class PR(BaseModel):
    id: int
    title: str = ""
    description: str = ""
    state: str = ""  # OPEN | MERGED | DECLINED
    author: str = ""
    source_branch: str = ""
    target_branch: str = ""
    project: str = ""
    repository: str = ""
    url: str = ""
    created: int = 0
    updated: int = 0
    reviewers: list[Reviewer] = Field(default_factory=list)
    comment_count: int = 0  # из properties.commentCount (дёшево, из списочного ответа)
    # заполняется отдельным запросом /merge:
    conflicted: Optional[bool] = None
    can_merge: Optional[bool] = None
    # проставляется в sync (дешёвый /commits?limit=1) для детекта новых коммитов:
    latest_commit: str = ""
    # проставляются командой `prs --mine-reviews` (из activities, для текущего юзера):
    my_review_status: str = ""           # APPROVED | NEEDS_WORK — мой статус по PR
    my_review_at: Optional[int] = None   # дата (ms) моего апрува/needs-work, из activities
    # статусы CI-сборок по head-коммиту (build-status API); пусто, если не запрашивались:
    builds: list[BuildStatus] = Field(default_factory=list)

    @classmethod
    def from_bitbucket(cls, raw: dict) -> "PR":
        from_ref = raw.get("fromRef", {}) or {}
        to_ref = raw.get("toRef", {}) or {}
        repo = from_ref.get("repository", {}) or to_ref.get("repository", {}) or {}
        return cls(
            id=int(raw.get("id", 0)),
            title=raw.get("title", "") or "",
            description=raw.get("description", "") or "",
            state=raw.get("state", "") or "",
            author=_get(raw, "author", "user", "displayName", default="") or "",
            source_branch=from_ref.get("displayId", "") or "",
            target_branch=to_ref.get("displayId", "") or "",
            project=_get(repo, "project", "key", default="") or "",
            repository=repo.get("slug", "") or "",
            url=_get(raw, "links", "self", default=[{}])[0].get("href", "")
            if isinstance(_get(raw, "links", "self"), list)
            else "",
            created=int(raw.get("createdDate", 0) or 0),
            updated=int(raw.get("updatedDate", 0) or 0),
            reviewers=[Reviewer.from_bitbucket(r) for r in raw.get("reviewers", []) or []],
            comment_count=int(_get(raw, "properties", "commentCount", default=0) or 0),
        )

    @classmethod
    def from_github(cls, raw: dict) -> "PR":
        """PR GitHub → та же модель, что и у Bitbucket.

        ``project`` — owner (организация или пользователь), ``repository`` — имя репозитория:
        пара (project, repo) адресует PR одинаково у обоих провайдеров, поэтому память,
        привязка PR к работам и экраны дашборда остаются общими.

        Ревьюверы здесь — только ЗАПРОШЕННЫЕ (в payload PR отзывов нет); фактические
        статусы досыпает клиент из ``/pulls/{n}/reviews``.
        """
        base_repo = _get(raw, "base", "repo", default={}) or {}
        head_repo = _get(raw, "head", "repo", default={}) or {}
        repo = base_repo or head_repo
        if (raw.get("state", "") or "").lower() == "open":
            state = "OPEN"
        else:
            state = "MERGED" if raw.get("merged_at") or raw.get("merged") else "DECLINED"
        pull = cls(
            id=int(raw.get("number", 0) or 0),
            title=raw.get("title", "") or "",
            description=raw.get("body", "") or "",
            state=state,
            author=_get(raw, "user", "login", default="") or "",
            source_branch=_get(raw, "head", "ref", default="") or "",
            target_branch=_get(raw, "base", "ref", default="") or "",
            project=_get(repo, "owner", "login", default="") or "",
            repository=repo.get("name", "") or "",
            url=raw.get("html_url", "") or "",
            created=gh_ms(raw.get("created_at", "") or ""),
            updated=gh_ms(raw.get("updated_at", "") or ""),
            reviewers=[
                Reviewer.from_github(u.get("login", "") or "")
                for u in raw.get("requested_reviewers", []) or []
            ],
            comment_count=int(raw.get("comments", 0) or 0)
            + int(raw.get("review_comments", 0) or 0),
            latest_commit=_get(raw, "head", "sha", default="") or "",
        )
        pull.apply_github_merge(raw)
        return pull

    def apply_github_merge(self, raw: dict) -> None:
        """Конфликт/мержабельность из полей PR GitHub (отдельного эндпоинта у него нет).

        ``mergeable`` приходит null, пока GitHub считает мерж в фоне — тогда честнее
        оставить «неизвестно», чем показать зелёное или красное наугад.
        """
        mergeable = raw.get("mergeable")
        state = (raw.get("mergeable_state", "") or "").lower()
        if mergeable is None and not state:
            return
        self.conflicted = state == "dirty" or mergeable is False
        self.can_merge = bool(mergeable) and state in ("clean", "has_hooks", "unstable", "")

    def apply_merge_status(self, merge: dict) -> None:
        """Заполнить статус конфликта из ответа /pull-requests/{id}/merge."""
        self.can_merge = bool(merge.get("canMerge", False))
        conflicted = merge.get("conflicted")
        if conflicted is None:
            # Bitbucket иногда отдаёт список vetoes вместо флага
            vetoes = merge.get("vetoes", []) or []
            conflicted = any("conflict" in (v.get("summaryMessage", "").lower()) for v in vetoes)
        self.conflicted = bool(conflicted)


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #

# Ключ задачи GitHub: `repo#42` (внутри своего owner'а) либо `owner/repo#42`.
# Формат выбран так, чтобы ключ читался в таблицах, в имени ветки и в коммите —
# ровно как ключ Jira, только с решёткой, к которой GitHub и так приучил.
GITHUB_KEY_RE = re.compile(r"(?:([A-Za-z0-9][\w.-]*)/)?([A-Za-z0-9][\w.-]*)#(\d+)")

# Ссылка на issue/PR в теле или ветке: `#42` без указания репозитория.
GITHUB_SHORT_REF_RE = re.compile(r"(?<![\w/#])#(\d+)\b")

# ISO-время GitHub — «2026-01-02T03:04:05Z». Python 3.10 не понимает суффикс Z,
# поэтому приводим к «+00:00» ещё на разборе, а не в каждом месте показа.
def gh_time(value: str) -> str:
    value = (value or "").strip()
    return value[:-1] + "+00:00" if value.endswith("Z") else value


def gh_ms(value: str) -> int:
    """ISO-время GitHub → epoch ms (в PR-модели даты хранятся числом, как в Bitbucket)."""
    from datetime import datetime

    iso = gh_time(value)
    if not iso:
        return 0
    try:
        return int(datetime.fromisoformat(iso).timestamp() * 1000)
    except ValueError:
        return 0


def github_key(repo: str, number: int | str, *, owner: str = "", default_owner: str = "") -> str:
    """Ключ задачи/PR: `repo#42`; с чужим owner'ом — `owner/repo#42`."""
    if owner and default_owner and owner.lower() != default_owner.lower():
        return f"{owner}/{repo}#{number}"
    return f"{repo}#{number}"


def parse_github_key(
    key: str, *, default_owner: str = "", default_repo: str = ""
) -> tuple[str, str, int] | None:
    """`owner/repo#42` | `repo#42` | `#42` | `42` → (owner, repo, number).

    Недостающие части добираются из дефолтов воркспейса. None — если это вообще не
    похоже на ключ GitHub (например, ключ Jira ``PROJ-123``).
    """
    key = (key or "").strip()
    if not key:
        return None
    match = GITHUB_KEY_RE.fullmatch(key)
    if match:
        owner, repo, number = match.group(1), match.group(2), int(match.group(3))
        return (owner or default_owner, repo, number)
    bare = key[1:] if key.startswith("#") else key
    if bare.isdigit() and default_repo:
        return (default_owner, default_repo, int(bare))
    return None


# Заключение check-run / состояние commit-status -> состояние сборки в модели jwu.
_GH_CONCLUSION_STATE = {
    "success": "SUCCESSFUL",
    "neutral": "SUCCESSFUL",
    "skipped": "SUCCESSFUL",
    "failure": "FAILED",
    "timed_out": "FAILED",
    "action_required": "FAILED",
    "cancelled": "FAILED",
    "stale": "FAILED",
    "startup_failure": "FAILED",
}
_GH_STATUS_STATE = {
    "success": "SUCCESSFUL",
    "failure": "FAILED",
    "error": "FAILED",
    "pending": "INPROGRESS",
}


def _github_repo_from_url(url: str) -> tuple[str, str]:
    """(owner, repo) из ``repository_url`` вида https://api.github.com/repos/o/r."""
    parts = [p for p in (url or "").split("/") if p]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return "", ""


# --------------------------------------------------------------------------- #
# Воркспейсы
# --------------------------------------------------------------------------- #


class WorkspacePath(BaseModel):
    """Папка, отнесённая к воркспейсу (абсолютный путь после resolve).

    ``tags`` — по ним папку находят: «legacy-бэкенд», «новая-версия», «фронт». Именно
    теги отвечают на вопрос «где что лежит», когда в воркспейсе несколько репозиториев.
    """

    id: int = 0
    workspace_id: int = 0
    path: str = ""
    label: str = ""
    tags: list[str] = Field(default_factory=list)
    added_at: str = ""


# Провайдер воркспейса — ЕДИНСТВЕННЫЙ источник задач и PR для контура. Их не смешивают:
# проект живёт либо в Jira (+Bitbucket), либо в GitHub, либо вообще без внешнего трекера.
# Выбирается при создании воркспейса и меняется командой `jwu workspace provider`.
WORKSPACE_PROVIDERS = ["local", "jira", "github"]

# провайдер -> (подпись, что он даёт)
PROVIDER_LABELS: dict[str, tuple[str, str]] = {
    "local": ("локальный", "фичи и работы в памяти jwu, без внешних систем"),
    "jira": ("Jira", "задачи из Jira/SDESK, PR из Bitbucket, сборки из Jenkins"),
    "github": ("GitHub", "задачи из Issues, PR и сборки из Actions"),
}


class Workspace(BaseModel):
    """Контур работы: набор папок + провайдер задач/PR + свои локальные данные.

    ``provider`` объявляется ЯВНО при создании, а не выводится из наличия токена:
    воркспейс без внешнего трекера должен вести себя предсказуемо (скрытые вкладки,
    внятные отказы Jira/GitHub-команд) ещё до того, как креды вообще настроены.

    ``jira_enabled``/``github_enabled`` — производные от провайдера (их читают TUI, MCP
    и скиллы). Отдельным флагом остаётся только ``bitbucket_enabled``: у Jira-контура
    Bitbucket может и не быть, а вот у GitHub PR и задачи приходят из одного места.
    """

    id: int = 0
    slug: str = ""
    name: str = ""
    provider: str = "local"        # local | jira | github
    bitbucket_enabled: bool = False  # имеет смысл только при provider="jira"
    archived: bool = False
    created_at: str = ""
    updated_at: str = ""
    paths: list[WorkspacePath] = Field(default_factory=list)
    # Счётчик работ для списков/экрана выбора: заполняется вызывающим по требованию
    # (Store сам его не считает — это лишний запрос на каждое чтение воркспейса).
    jobs_count: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def jira_enabled(self) -> bool:
        return self.provider == "jira"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def github_enabled(self) -> bool:
        return self.provider == "github"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def prs_enabled(self) -> bool:
        """Есть ли у контура место, откуда брать PR (вкладки «Мои PR» / «На ревью»)."""
        return self.github_enabled or (self.jira_enabled and self.bitbucket_enabled)

    @property
    def provider_label(self) -> str:
        """Как называть провайдера человеку: «Jira», «GitHub», «локальный»."""
        return PROVIDER_LABELS.get(self.provider, (self.provider, ""))[0]

    @property
    def label(self) -> str:
        """Как показывать воркспейс человеку: «Название (slug)» либо просто slug."""
        return f"{self.name} ({self.slug})" if self.name and self.name != self.slug else self.slug


# --------------------------------------------------------------------------- #
# Память: дельты и заметки
# --------------------------------------------------------------------------- #


class Delta(BaseModel):
    key: str
    # new_issue | status_change | new_comment | new_pr | new_conflict | resolved
    # | gone (задача ушла из выборки) | pr_gone (PR смержен/отклонён) | new_pr_comment …
    kind: str
    summary: str = ""
    detail: str = ""
    # Для дельт исчезновения (gone/pr_gone) — вкладка, из которой ушла сущность
    # (mine|mentions|prs_mine|prs_review): сама сущность уже не в списке вкладки,
    # поэтому маршрутизировать её в TUI можно только по этой подсказке.
    section: str = ""


class Note(BaseModel):
    key: str
    author: str = "claude"
    text: str = ""
    ts: str = ""


class Mention(BaseModel):
    """Один комментарий, в котором меня упомянули ([~login]) — самостоятельная запись.

    Упоминание — это СОБЫТИЕ, а не задача: оно случилось один раз, и дальше состояние
    задачи к нему отношения не имеет. Поэтому запись создаётся в момент, когда упоминание
    впервые увидено, и больше не меняется; карточка задачи тянется из сети только при
    заходе внутрь. ``summary`` — заголовок задачи на тот момент, чтобы список читался
    без единого сетевого запроса.
    """

    id: int = 0
    task_key: str = ""
    comment_id: str = ""
    author: str = ""
    text: str = ""
    created: str = ""    # когда написан комментарий (по данным Jira)
    summary: str = ""    # заголовок задачи на момент упоминания
    seen: bool = False   # прочитано ли (снимается при заходе внутрь)
    added_at: str = ""


# --------------------------------------------------------------------------- #
# Работы (jobs)
# --------------------------------------------------------------------------- #


# Типы записей работы (jwu job add --kind) — единый источник для CLI-валидации и рендера.
JOB_RECORD_KINDS = [
    "phase", "note", "decision", "remark", "constraint", "warning",
    "bug", "bug-resolved", "test-pass", "test-fail", "todo", "review",
]

# kind -> (бейдж, цвет rich) для выделения в выводе (CLI + TUI).
# Типы без бейджа рендерятся нейтрально как "· {kind}".
JOB_RECORD_BADGES: dict[str, tuple[str, str]] = {
    "decision":     ("🧭 РЕШЕНИЕ", "cyan"),
    "constraint":   ("⛔ ЗАПРЕТ", "red"),
    "warning":      ("⚠ ВНИМАНИЕ", "yellow"),
    "bug":          ("🐛 БАГ", "red"),
    "bug-resolved": ("✅ БАГ ИСПРАВЛЕН", "green"),
    "test-pass":    ("🧪 ТЕСТЫ OK", "green"),
    "test-fail":    ("🧪 ТЕСТЫ УПАЛИ", "red"),
    "todo":         ("📌 TODO", "magenta"),
    "review":       ("🔍 РЕВЬЮ", "blue"),
}


class JobRecord(BaseModel):
    id: int = 0
    job_id: int = 0
    # phase | note | remark | constraint (запрет) | warning | bug | bug-resolved
    kind: str = "note"
    text: str = ""
    status: Optional[str] = None  # опц.: для phase напр. pending | done
    ts: str = ""


class JobPRLink(BaseModel):
    pr_id: int = 0
    project: str = ""
    repo: str = ""


class Job(BaseModel):
    id: int = 0
    task_key: str = ""          # ключ Jira; пусто — работа без задачи Jira
    title: str = ""
    status: str = "active"      # active | done | paused
    created_at: str = ""
    updated_at: str = ""
    # Локальная фича как якорь работы (воркспейс без Jira). Взаимоисключимо с task_key.
    feature_id: Optional[int] = None
    feature_key: str = ""       # денормализация для рендера (JOIN при чтении)
    records: list[JobRecord] = Field(default_factory=list)
    prs: list[JobPRLink] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def anchor(self) -> str:
        """К чему привязана работа: задача Jira, локальная фича либо ничего."""
        return self.task_key or self.feature_key or f"#{self.id}"


# Как записи работы раскладываются по разделам описания задачи. Порядок разделов —
# порядок этого списка: сначала что сделано, потом чем это грозит, потом детали.
_JOB_TEXT_SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("Что сделано", ("phase",)),
    ("Найденные проблемы", ("bug", "bug-resolved")),
    ("Ограничения и предупреждения", ("constraint", "warning")),
    ("Принятые решения", ("decision",)),
    ("Тесты", ("test-pass", "test-fail")),
    ("Ревью", ("review", "remark")),
    ("Осталось сделать", ("todo",)),
    ("Заметки", ("note",)),
]


def job_description_text(job: "Job") -> str:
    """Черновик описания задачи из лога работы — wiki-разметкой Jira Server.

    Данные для описания в работе уже собраны (фазы, найденные баги, ревью, прогоны
    тестов) — не хватало только рендера. Это именно ЧЕРНОВИК: пользователь правит его
    перед отправкой, поэтому здесь ничего не додумывается, только раскладывается по
    разделам в человекочитаемом виде.
    """
    lines: list[str] = []
    if job.title:
        lines.append(f"*{job.title}*")
    anchors = [a for a in (job.task_key, job.feature_key) if a]
    if anchors:
        lines.append(f"Работа jwu #{job.id}, якорь: {', '.join(anchors)}")
    prs = ", ".join(f"{p.repo or p.project}#{p.pr_id}" for p in job.prs)
    if prs:
        lines.append(f"PR: {prs}")

    for title, kinds in _JOB_TEXT_SECTIONS:
        records = [r for r in job.records if r.kind in kinds]
        if not records:
            continue
        lines.extend(["", f"h3. {title}"])
        for record in records:
            badge = JOB_RECORD_BADGES.get(record.kind, ("", ""))[0]
            # Бейдж несёт смысл («⛔ ЗАПРЕТ», «🧪 ТЕСТЫ УПАЛИ»), но внутри своего раздела
            # он избыточен — оставляем только там, где в разделе смешаны разные типы.
            mark = f"{badge}: " if badge and len(kinds) > 1 else ""
            status = f" [{record.status}]" if record.status else ""
            text = (record.text or "").strip()
            first, *rest = text.splitlines() or [""]
            lines.append(f"* {mark}{first}{status}")
            lines.extend(f"** {line}" for line in rest if line.strip())
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- #
# Локальные фичи (мини-трекер воркспейса без Jira)
# --------------------------------------------------------------------------- #


# Статусы локальной фичи — единый источник для CLI-валидации и рендера.
LOCAL_FEATURE_STATUSES = ["open", "in_progress", "review", "done", "cancelled"]

# статус -> (подпись, цвет rich)
LOCAL_FEATURE_BADGES: dict[str, tuple[str, str]] = {
    "open":        ("открыта", "white"),
    "in_progress": ("в работе", "yellow"),
    "review":      ("на ревью", "blue"),
    "done":        ("готова", "green"),
    "cancelled":   ("отменена", "dim"),
}


# --------------------------------------------------------------------------- #
# Правила воркспейса
# --------------------------------------------------------------------------- #


# Типы правил контура — единый источник для CLI-валидации и рендера (как JOB_RECORD_KINDS).
WORKSPACE_RULE_KINDS = ["constraint", "howto", "info", "convention", "gotcha"]

# kind -> (бейдж, цвет rich). ⛔ и ⚠ намеренно те же, что у записей работы: одна и та же
# вещь должна выглядеть одинаково, где бы она ни встретилась.
WORKSPACE_RULE_BADGES: dict[str, tuple[str, str]] = {
    "constraint": ("⛔ ЗАПРЕТ", "red"),        # что нельзя делать
    "howto":      ("🛠 КАК ДЕЛАТЬ", "cyan"),   # поднять стенд, прогнать тесты, накатить миграции
    "info":       ("ℹ ИНФО", "white"),         # где что лежит, кто за что отвечает, ссылки
    "convention": ("📐 СОГЛАШЕНИЕ", "blue"),   # код-стайл, нейминг, ветки, формат коммита
    "gotcha":     ("⚠ ГРАБЛИ", "yellow"),      # известные подводные камни
}


class WorkspaceRule(BaseModel):
    """Правило контура: то, что знает о проекте человек, но не знает агент.

    ``title`` и ``text`` разделены намеренно: правило вида «как поднимать стенд» —
    это абзац, а в списках (таблица дашборда, индекс для агента) нужна одна строка.
    Резать текст по первому переводу строки было бы гаданием.

    ``tag`` пустой — правило общее для воркспейса; иначе оно относится к папкам с этим
    тегом («legacy-бэкенд», «фронт») — теми же, что раздаются в workspace_path_tags.
    """

    id: int = 0
    workspace_id: int = 0
    kind: str = "info"
    title: str = ""
    text: str = ""
    tag: str = ""
    created_at: str = ""
    updated_at: str = ""

    def index_entry(self) -> dict:
        """Компактная запись для индекса: что правило есть, без его текста."""
        return {"id": self.id, "kind": self.kind, "tag": self.tag, "title": self.title}


class LocalFeature(BaseModel):
    """Задача локального трекера воркспейса — замена карточки Jira, когда Jira нет.

    Ключ (``HOMEJWU-1``) намеренно совместим по формату с ключами Jira: имя ветки
    ``HOMEJWU-1-dark-theme`` тогда даёт префикс коммита по той же регулярке, что и раньше.
    """

    id: int = 0
    workspace_id: int = 0
    key: str = ""
    title: str = ""
    status: str = "open"
    priority: str = ""
    description: str = ""
    created_at: str = ""
    updated_at: str = ""
