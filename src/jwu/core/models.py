"""Pydantic-модели и парсинг сырых ответов Jira / Bitbucket.

Сырые JSON Jira/Bitbucket сильно вложены; модели держат уже «плоское» представление,
а классметоды `from_jira_*` / `from_bitbucket_*` инкапсулируют разбор.
"""

from __future__ import annotations

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


class TestCaseFailure(BaseModel):
    """Один упавший тест-кейс из Jenkins testReport."""

    class_name: str = ""
    name: str = ""
    status: str = ""  # FAILED | REGRESSION | ERROR
    error_details: str = ""
    stack: str = ""


class BuildReport(BaseModel):
    """Детальный разбор одной сборки: статус из Bitbucket + (если есть токен) данные Jenkins.

    Деградирует мягко: без доступа в Jenkins остаётся state/url/description из Bitbucket,
    а ``jenkins_available=False`` и ``note`` объясняют, почему нет детализации.
    """

    state: str = ""        # из build-status Bitbucket
    name: str = ""
    url: str = ""
    description: str = ""
    job_path: str = ""
    number: int = 0
    jenkins_available: bool = False
    result: str = ""       # из Jenkins (FAILURE/SUCCESS/None если идёт)
    building: bool = False
    sha: str = ""
    branch: str = ""
    summary: Optional[dict] = None  # {"fail": int, "passed": int, "skip": int}
    failures: list[TestCaseFailure] = Field(default_factory=list)
    console_tail: str = ""
    note: str = ""


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


class Workspace(BaseModel):
    """Контур работы: набор папок + свой конфиг интеграций + свои локальные данные.

    ``jira_enabled``/``bitbucket_enabled`` объявляются ЯВНО при создании, а не выводятся
    из наличия токена: воркспейс без Jira должен вести себя предсказуемо (скрытые вкладки,
    внятные отказы Jira-команд) ещё до того, как креды вообще настроены.
    """

    id: int = 0
    slug: str = ""
    name: str = ""
    jira_enabled: bool = False
    bitbucket_enabled: bool = False
    archived: bool = False
    created_at: str = ""
    updated_at: str = ""
    paths: list[WorkspacePath] = Field(default_factory=list)
    # Счётчик работ для списков/экрана выбора: заполняется вызывающим по требованию
    # (Store сам его не считает — это лишний запрос на каждое чтение воркспейса).
    jobs_count: int = 0

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
