"""Сервисный слой: связывает клиентов провайдера и SQLite-память.

CLI обращается только сюда. Здесь же — локальная доводка «упоминаний» и оркестрация sync.

У контура ОДИН провайдер задач и PR (см. ``Workspace.provider``), поэтому сервис держит
две ссылки — ``tasks_client`` и ``pr_client``, — а не по клиенту на каждую систему:

- ``jira``   → JiraClient (+SDESK) и BitbucketClient (+Jenkins);
- ``github`` → один GitHubClient в обеих ролях (Issues, PR, Actions);
- ``local``  → оба None: работы и фичи живут в памяти jwu и сети не требуют.

Свойства ``jira``/``bitbucket``/``github`` отвечают «а это клиент такого-то типа?» —
через них внешний код (CLI, MCP) проверяет, что доступно в текущем контуре.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .bitbucket import BitbucketClient
from .config import (
    Config,
    bitbucket_token,
    github_token,
    jenkins_auth,
    jira_login,
    jira_proxy_basic,
    jira_token,
    load_config,
    sdesk_enabled,
    sdesk_login,
    sdesk_proxy_basic,
    sdesk_token,
)
from .github import GitHubClient
from .jenkins import JenkinsClient, JenkinsError, parse_build_url
from .jira import JiraClient, build_create_fields, check_create_fields
from .models import (
    Attachment,
    DOWNLOADABLE_ATTACH_KINDS,
    GITHUB_SHORT_REF_RE,
    BuildReport,
    BuildStatus,
    Delta,
    github_key,
    Issue,
    job_description_text,
    Job,
    LocalFeature,
    Mention,
    Note,
    PR,
    PRComment,
    TestCaseFailure,
    Workspace,
    WorkspacePath,
    WorkspaceRule,
)
from .store import Store

_UNSAFE_NAME_RE = re.compile(r"[^\w.\- ]+", re.UNICODE)
# Ключ Jira-задачи в имени ветки/заголовке PR (напр. PROJ-123 / ABC-4567).
_PR_TASK_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-[0-9]+)\b")
# project/repo/id из URL PR Bitbucket (для статусов сборок по dev-панели задачи).
_BITBUCKET_PR_URL_RE = re.compile(r"/projects/([^/]+)/repos/([^/]+)/pull-requests/(\d+)")
# То же для GitHub: https://github.com/owner/repo/pull/42
_GITHUB_PR_URL_RE = re.compile(r"github[^/]*/([^/]+)/([^/]+)/pull/(\d+)")
# Алиасы «ключ из ветки PR → канонический ключ задачи в Jira» — лежат одним JSON
# в meta под этим ключом. Нужны, когда Jira слила старую задачу в новый ключ
# (PR ссылается на старый, а snapshot пишется под канонический).
_PR_TASK_ALIAS_META = "pr_task_aliases"


def _load_pr_task_aliases(store: Store) -> dict[str, str]:
    raw = store.get_workspace_meta(_PR_TASK_ALIAS_META)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _jira_like_client(
    base_url: str,
    *,
    token: str | None = None,
    login: tuple[str, str] | None = None,
    proxy_basic: tuple[str, str] | None = None,
) -> JiraClient:
    """Собрать клиента Jira-подобного инстанса (Jira / SDESK) по кредам.

    ``login`` задан → сессионная авторизация за nginx Basic-гейтом (``proxy_basic``);
    иначе — обычный PAT через ``Authorization: Bearer``. При session_login сетевой
    логин происходит уже здесь (в конструкторе JiraClient).
    """
    if login is not None:
        return JiraClient(base_url, proxy_basic=proxy_basic, session_login=login)
    return JiraClient(base_url, token or "")


def _build_sdesk_client(cfg: Config) -> JiraClient:
    """Клиент SDESK по его СВОИМ кредам (сессия за гейтом либо PAT). Может кинуть
    JiraError на логине — вызывается лениво, поэтому падение SDESK не рушит Jira."""
    slogin = sdesk_login(cfg)
    if slogin is not None:
        return _jira_like_client(
            cfg.sdesk.base_url, login=slogin, proxy_basic=sdesk_proxy_basic(cfg)
        )
    return _jira_like_client(cfg.sdesk.base_url, token=sdesk_token(cfg))


# Слова короче этого в поиск дублей не идут: предлоги и «для»/«при» только зашумляют.
_MIN_SEARCH_WORD = 4
_SEARCH_WORD_RE = re.compile(r"\w{%d,}" % _MIN_SEARCH_WORD, re.UNICODE)


def _text_search_terms(summary: str, *, limit: int = 3) -> list[str]:
    """Значимые слова summary для JQL ``text ~ …``, от самых длинных к коротким.

    Спецсимволы Lucene (кавычки, дефисы, скобки) в запрос не попадают вовсе: они меняют
    смысл запроса (ведущий дефис — это NOT) и ломают синтаксис. Берём только слова,
    длинные вперёд: именно они различают задачи, а короткие («для», «при») зашумляют.

    Список, а не строка, потому что слова надо соединять через AND: ``text ~ "a b"``
    в Jira — поиск ФРАЗЫ, и на реальных заголовках он не находит ничего.
    """
    words = _SEARCH_WORD_RE.findall(summary or "")
    uniq = list(dict.fromkeys(words))
    uniq.sort(key=len, reverse=True)
    return uniq[:limit]


def _safe_filename(name: str) -> str:
    """Обезвредить имя файла под запись на диск: убрать пути и спецсимволы."""
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    name = _UNSAFE_NAME_RE.sub("_", name)
    return name[:120]


# токены секций в sync_runs.views (для last_sync по вкладке)
SECTION_TOKEN = {
    "mine": "mine",
    "mentions": "mentions",
    "prs_mine": "prs:mine",
    "prs_review": "prs:review",
}


@dataclass
class SyncResult:
    run_id: int
    counts: dict[str, int]
    deltas: list[Delta]


@dataclass
class PRDetail:
    pr: PR
    comments: list[PRComment] = field(default_factory=list)
    commits: list[dict] = field(default_factory=list)


@dataclass
class DayContext:
    """Расширенный контекст дня для анализа Claude Code (после фулл-синка)."""

    user: str = ""
    me_display: str = ""  # отображаемое имя (по нему отличают «на мне» от «не на мне»)
    synced_at: str | None = None
    deltas: list[Delta] = field(default_factory=list)
    mine: list[Issue] = field(default_factory=list)
    prs_mine: list[PR] = field(default_factory=list)
    prs_review: list[PR] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)
    # pr_id -> комменты (только для flagged PR: конфликт / NEEDS_WORK)
    pr_comments: dict[int, list[PRComment]] = field(default_factory=dict)


@dataclass
class DashboardData:
    """Агрегированный снимок для дашборда (собирается из памяти, без сети)."""

    user: str = ""           # Jira-логин (name)
    display_name: str = ""   # человекочитаемое имя из /myself
    email: str = ""          # почта из /myself
    # время последнего синка по секциям: mine | mentions | prs_mine | prs_review
    last_sync: dict[str, str | None] = field(default_factory=dict)
    deltas: list[Delta] = field(default_factory=list)
    mine: list[Issue] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)
    prs_mine: list[PR] = field(default_factory=list)
    prs_review: list[PR] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)
    # Контекст воркспейса: сам воркспейс, его папки и локальные фичи. Флаги интеграций
    # продублированы отдельно, чтобы TUI мог решать про видимость вкладок без workspace.
    workspace: Workspace | None = None
    # Все воркспейсы (со счётчиком работ) — вкладка «Workspace» управляет ими,
    # поэтому список нужен прямо в снимке, а не отдельным колбэком.
    workspaces: list[Workspace] = field(default_factory=list)
    paths: list[WorkspacePath] = field(default_factory=list)
    features: list[LocalFeature] = field(default_factory=list)
    rules: list[WorkspaceRule] = field(default_factory=list)
    # Провайдер контура: им TUI решает, какие вкладки показывать (см. tasks_enabled/prs_enabled).
    provider: str = "jira"
    bitbucket_enabled: bool = True
    # Куда и под кем мы залогинены — это должно быть видно в футере дашборда, чтобы
    # после смены контура нельзя было спутать рабочую Jira с личным GitHub.
    env_label: str = ""      # «PROJ @ jira.example.com» / «dndeck @ github.com»
    web_base: str = ""       # хост для ссылок на задачи (Jira base_url либо github web_url)
    owner: str = ""          # owner GitHub — нужен, чтобы собрать ссылку по ключу repo#42
    # key задачи → её последний известный статус и текущий assignee;
    # для колонок «Назначен» / «Статус» в PR-таблицах.
    task_status: dict[str, str] = field(default_factory=dict)
    task_assignee: dict[str, str] = field(default_factory=dict)

    @property
    def tasks_enabled(self) -> bool:
        """Есть ли откуда брать задачи (вкладки «Мои» и «Упоминания»)."""
        return self.provider in ("jira", "github")

    @property
    def prs_enabled(self) -> bool:
        """Есть ли откуда брать PR (вкладки «Мои PR» и «На ревью»)."""
        return self.provider == "github" or (
            self.provider == "jira" and self.bitbucket_enabled
        )

    @property
    def jira_enabled(self) -> bool:
        return self.provider == "jira"

    @property
    def github_enabled(self) -> bool:
        return self.provider == "github"

    def to_json_dict(self) -> dict:
        return {
            "user": self.user,
            "display_name": self.display_name,
            "email": self.email,
            "workspace": self.workspace.model_dump() if self.workspace else None,
            "workspaces": [w.model_dump() for w in self.workspaces],
            "paths": [p.model_dump() for p in self.paths],
            "features": [f.model_dump() for f in self.features],
            "rules": [r.model_dump() for r in self.rules],
            "provider": self.provider,
            "env_label": self.env_label,
            "web_base": self.web_base,
            "owner": self.owner,
            "jira_enabled": self.jira_enabled,
            "github_enabled": self.github_enabled,
            "bitbucket_enabled": self.bitbucket_enabled,
            "last_sync": self.last_sync,
            "deltas": [d.model_dump() for d in self.deltas],
            "mine": [i.model_dump() for i in self.mine],
            "mentions": [m.model_dump() for m in self.mentions],
            "prs_mine": [p.model_dump() for p in self.prs_mine],
            "prs_review": [p.model_dump() for p in self.prs_review],
            "jobs": [j.model_dump() for j in self.jobs],
            "task_status": self.task_status,
            "task_assignee": self.task_assignee,
        }


# ключ персистентного кэша личности пользователя в Store.meta
_IDENTITY_META = "identity"


def _read_identity(store: Store) -> dict:
    """Кэш личности (user/display_name/email + отпечаток кредов) из памяти."""
    raw = store.get_workspace_meta(_IDENTITY_META)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001 — битый кэш не критичен
        return {}


def _environment(store: Store, ws: Workspace | None) -> dict:
    """Куда смотрит контур: подпись окружения, хост для ссылок и owner (для GitHub).

    Читается из настроек воркспейса, а не из глобального конфига: дашборд переключают
    между контурами прямо на ходу, и «где мы сейчас» обязано меняться вместе с ними.
    """
    if ws is None or ws.provider == "local":
        return {"env_label": "локальный контур" if ws is not None else "", }
    from urllib.parse import urlparse

    from .workspaces import config_for_workspace

    try:
        cfg = config_for_workspace(store, ws)
    except Exception:  # noqa: BLE001 — подпись окружения не стоит падения дашборда
        return {}
    if ws.provider == "github":
        host = urlparse(cfg.github.web_url).netloc or cfg.github.web_url
        repos = cfg.github.repo_list
        what = repos[0] if len(repos) == 1 else (cfg.github.owner or "")
        return {
            "env_label": f"{what} @ {host}".strip(" @"),
            "web_base": cfg.github.web_url,
            "owner": cfg.github.owner,
        }
    host = urlparse(cfg.jira.base_url).netloc or cfg.jira.base_url
    return {
        "env_label": f"{cfg.jira.project} @ {host}".strip(" @"),
        "web_base": cfg.jira.base_url,
    }


def dashboard_from_memory(store: Store, user: str = "") -> DashboardData:
    """Собрать дашборд из памяти (свежайшие снапшоты по сущностям). Сеть/токены не нужны.

    Личность (имя/почта) берётся из персистентного кэша — поэтому показывается сразу
    после перезапуска, до первого синка.
    """
    ident = _read_identity(store)
    ws = store.get_workspace(store.workspace_id)
    # Список всех контуров со счётчиком работ — для вкладки управления воркспейсами.
    # Считаем COUNT'ом по чужим контурам: снимок собирается на каждом обновлении дашборда,
    # и вычитывать ради одной цифры весь журнал работ соседнего проекта незачем. Скоуп
    # store при этом не трогаем — дальше он читает данные текущего воркспейса.
    all_workspaces = store.list_workspaces()
    for item in all_workspaces:
        item.jobs_count = store.jobs_count(item.id)
    # Все известные задачи по ключу → статус (для колонки «Статус задачи» в PR-таблицах).
    # Берём из всех вью разом, чтобы статус был и для PR с чужой задачей (review).
    all_issues = store.latest_issues(None)
    task_status = {i.key: i.status for i in all_issues if i.key}
    task_assignee = {i.key: i.assignee for i in all_issues if i.key}
    # Алиасы из ключей в ветках PR → канонические ключи Jira (см. _snapshot_pr_tasks):
    # дублируем статус/assignee, чтобы lookup по pr_task_key(pr) сработал.
    for branch_key, canonical_key in _load_pr_task_aliases(store).items():
        if canonical_key in task_status and branch_key not in task_status:
            task_status[branch_key] = task_status[canonical_key]
        if canonical_key in task_assignee and branch_key not in task_assignee:
            task_assignee[branch_key] = task_assignee[canonical_key]
    return DashboardData(
        user=user or ident.get("user", ""),
        display_name=ident.get("display_name", ""),
        email=ident.get("email", ""),
        last_sync={
            section: store.last_sync_at(token) for section, token in SECTION_TOKEN.items()
        },
        deltas=store.pending_changes(),  # накопленные изменения (до явного закрытия)
        mine=store.latest_issues("mine"),
        mentions=store.list_mentions(),
        prs_mine=store.latest_prs("mine"),
        prs_review=store.latest_prs("review"),
        jobs=store.list_jobs(),  # все работы (включая закрытые/завершённые)
        workspace=ws,
        workspaces=all_workspaces,
        paths=ws.paths if ws else [],
        features=store.list_features(),
        rules=store.list_rules(),
        provider=ws.provider if ws else "jira",
        bitbucket_enabled=ws.bitbucket_enabled if ws else True,
        **_environment(store, ws),
        task_status=task_status,
        task_assignee=task_assignee,
    )


class Service:
    def __init__(
        self,
        cfg: Config,
        tasks: "JiraClient | GitHubClient | None",
        prs: "BitbucketClient | GitHubClient | None",
        store: Store,
        jenkins: JenkinsClient | None = None,
        sdesk: JiraClient | None = None,
        sdesk_factory: "Callable[[], JiraClient] | None" = None,
        workspace: Workspace | None = None,
    ) -> None:
        self.cfg = cfg
        # Воркспейс, в контексте которого работает сервис (None — старый глобальный режим).
        # Его provider определяет, какие клиенты вообще созданы.
        self.workspace = workspace
        # Клиент задач и клиент PR. У GitHub-контура это ОДИН объект в обеих ролях.
        self.tasks_client = tasks
        # Второй Jira-инстанс (SDESK): строится ЛЕНИВО (self._sdesk_factory) при первом
        # обращении к его ключу — чтобы недоступный/неверно настроенный SDESK не рушил
        # работу с основной Jira. Задачи с его префиксом ключа обслуживаются им, всё
        # остальное (вью/синк/дашборд) остаётся на основной Jira. Тесты могут передать
        # готовый клиент через sdesk=...
        self.sdesk = sdesk
        self._sdesk_factory = sdesk_factory
        self.pr_client = prs
        self.jenkins = jenkins
        self.store = store
        self._me: dict | None = None      # кэш /myself на время жизни сервиса
        self._cred_fp: str | None = None  # кэш отпечатка кредов

    # --- какой провайдер перед нами --------------------------------------- #

    @property
    def provider(self) -> str:
        """local | jira | github — по типу клиентов (или по воркспейсу, если их нет)."""
        if isinstance(self.tasks_client, GitHubClient) or isinstance(self.pr_client, GitHubClient):
            return "github"
        if self.tasks_client is not None or self.pr_client is not None:
            return "jira"
        return self.workspace.provider if self.workspace else "local"

    @property
    def jira(self) -> JiraClient | None:
        """Клиент Jira — или None, если задачи берутся не из Jira."""
        return self.tasks_client if isinstance(self.tasks_client, JiraClient) else None

    @property
    def bitbucket(self) -> BitbucketClient | None:
        """Клиент Bitbucket — или None, если PR приходят не оттуда."""
        return self.pr_client if isinstance(self.pr_client, BitbucketClient) else None

    @property
    def github(self) -> GitHubClient | None:
        """Клиент GitHub — или None, если контур не на GitHub."""
        return self.pr_client if isinstance(self.pr_client, GitHubClient) else None

    @classmethod
    def from_config(cls, cfg: Config | None = None, *, db_path: str | None = None,
                    workspace_id: int | None = None) -> "Service":
        from .config import db_path as default_db_path

        cfg = cfg or load_config()
        login = jira_login(cfg)
        if login is not None:
            # за Jira стоит nginx Basic-гейт + сессионная авторизация
            jira = _jira_like_client(
                cfg.jira.base_url, login=login, proxy_basic=jira_proxy_basic(cfg)
            )
            if not cfg.jira.username:
                cfg.jira.username = login[0]
        else:
            # гейта нет — обычный PAT через Bearer
            jira = _jira_like_client(cfg.jira.base_url, token=jira_token(cfg))
        # SDESK — второй Jira-инстанс (если подключён): строится ЛЕНИВО, чтобы его
        # логин (сессия за гейтом либо PAT) не выполнялся, пока не понадобится задача
        # с его префиксом ключа. Иначе недоступный SDESK ронял бы все команды.
        sdesk_factory = (lambda: _build_sdesk_client(cfg)) if sdesk_enabled(cfg) else None
        bitbucket = BitbucketClient(cfg.bitbucket.base_url, bitbucket_token(cfg))
        # Jenkins опционален: клиент создаётся всегда (для парсинга/единообразия),
        # но без токена — без auth, и глубокие вызовы вернут 401/403, что мы ловим.
        jenkins = JenkinsClient(cfg.jenkins.base_url, jenkins_auth(cfg))
        store = Store(db_path or str(default_db_path()), workspace_id)
        return cls(cfg, jira, bitbucket, store, jenkins, sdesk_factory=sdesk_factory)

    @classmethod
    def for_builds(cls, cfg: Config | None = None, *, db_path: str | None = None,
                   workspace_id: int | None = None) -> "Service":
        """Лёгкий сервис для CI-сборок: только Bitbucket + Jenkins, без логина в Jira.

        Разбор упавших сборок нужен ровно тогда, когда CI красный, — и не должен зависеть
        от доступности Jira. Поэтому build/builds ходят через этот фабричный метод.
        """
        from .config import db_path as default_db_path

        cfg = cfg or load_config()
        bitbucket = BitbucketClient(cfg.bitbucket.base_url, bitbucket_token(cfg))
        jenkins = JenkinsClient(cfg.jenkins.base_url, jenkins_auth(cfg))
        store = Store(db_path or str(default_db_path()), workspace_id)
        return cls(cfg, None, bitbucket, store, jenkins)

    @staticmethod
    def _github_client(cfg: Config) -> GitHubClient:
        """Клиент GitHub по конфигу воркспейса — он же и задачи, и PR, и сборки."""
        return GitHubClient(
            cfg.github.api_url,
            github_token(cfg),
            owner=cfg.github.owner,
            repos=cfg.github.repo_list,
            web_url=cfg.github.web_url,
            views=cfg.github.views,
        )

    @classmethod
    def for_workspace(
        cls, workspace: Workspace, cfg: Config | None = None, *, db_path: str | None = None
    ) -> "Service":
        """Сервис в контексте воркспейса: клиенты ровно под его провайдера.

        Локальный контур вообще не требует кредов — ни один токен не запрашивается,
        и работа с фичами/работами возможна «из коробки».

        Store открывается ПЕРВЫМ: конфиг воркспейса (и его секреты) читаются из этой же
        БД, поэтому источник секретов должен держать живое соединение сервиса, а не чужое.
        """
        from .config import db_path as default_db_path
        from .workspaces import config_for_workspace

        store = Store(db_path or str(default_db_path()), workspace.id)
        cfg = cfg or config_for_workspace(store, workspace)

        if workspace.provider == "github":
            gh = cls._github_client(cfg)
            # Один клиент в обеих ролях: у GitHub задачи, PR и сборки живут на одном хосте.
            return cls(cfg, gh, gh, store, workspace=workspace)

        jira = None
        sdesk_factory = None
        if workspace.jira_enabled:
            login = jira_login(cfg)
            if login is not None:
                jira = _jira_like_client(
                    cfg.jira.base_url, login=login, proxy_basic=jira_proxy_basic(cfg)
                )
                if not cfg.jira.username:
                    cfg.jira.username = login[0]
            else:
                jira = _jira_like_client(cfg.jira.base_url, token=jira_token(cfg))
            sdesk_factory = (lambda: _build_sdesk_client(cfg)) if sdesk_enabled(cfg) else None

        bitbucket = jenkins = None
        if workspace.bitbucket_enabled:
            bitbucket = BitbucketClient(cfg.bitbucket.base_url, bitbucket_token(cfg))
            jenkins = JenkinsClient(cfg.jenkins.base_url, jenkins_auth(cfg))

        return cls(cfg, jira, bitbucket, store, jenkins,
                   sdesk_factory=sdesk_factory, workspace=workspace)

    @classmethod
    def builds_for_workspace(
        cls, workspace: Workspace, cfg: Config | None = None, *, db_path: str | None = None
    ) -> "Service":
        """Хостинг + CI в контексте воркспейса, без логина в трекер (см. for_builds).

        Разбор красной сборки не должен зависеть от доступности Jira — поэтому у
        Jira-контура здесь поднимаются только Bitbucket и Jenkins. У GitHub-контура
        клиент всё равно один, но роль задач намеренно остаётся пустой.
        """
        from .config import db_path as default_db_path
        from .workspaces import config_for_workspace

        store = Store(db_path or str(default_db_path()), workspace.id)
        cfg = cfg or config_for_workspace(store, workspace)
        if workspace.provider == "github":
            return cls(cfg, None, cls._github_client(cfg), store, workspace=workspace)
        bitbucket = BitbucketClient(cfg.bitbucket.base_url, bitbucket_token(cfg))
        jenkins = JenkinsClient(cfg.jenkins.base_url, jenkins_auth(cfg))
        return cls(cfg, None, bitbucket, store, jenkins, workspace=workspace)

    def close(self) -> None:
        clients = [self.tasks_client, self.pr_client, self.sdesk, self.jenkins]
        seen: list[int] = []
        for client in clients:
            # tasks_client и pr_client у GitHub-контура — один и тот же объект
            if client is None or id(client) in seen:
                continue
            seen.append(id(client))
            client.close()
        self.store.close()

    def __enter__(self) -> "Service":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- Jira ----------------------------------------------------------- #

    def _sdesk_client(self) -> JiraClient | None:
        """Материализовать SDESK-клиент при первом обращении (здесь и происходит логин).

        Тесты могут передать готовый клиент через ``sdesk=`` — тогда фабрика не нужна.
        """
        if self.sdesk is None and self._sdesk_factory is not None:
            self.sdesk = self._sdesk_factory()
        return self.sdesk

    def _key_is_sdesk(self, key: str) -> bool:
        """Принадлежит ли ключ инстансу SDESK (по совпадению префикса с sdesk.project)."""
        if not sdesk_enabled(self.cfg):
            return False
        return key.split("-", 1)[0].upper() == self.cfg.sdesk.project.upper()

    def _client_for_key(self, key: str) -> "JiraClient | GitHubClient | None":
        """Выбрать инстанс по префиксу ключа: SDESK-* → SDESK-инстанс, иначе основная Jira.

        Резолвинг по одной задаче (карточка/worklog/вложения) ходит в тот инстанс,
        которому принадлежит ключ. Вью/синк/дашборд остаются на основной Jira.
        SDESK строится лениво прямо здесь, поэтому его логин случается только для его
        ключей — и его ошибки не задевают команды по основной Jira.
        """
        if self._key_is_sdesk(key):
            client = self._sdesk_client()
            if client is not None:
                return client
        return self.tasks_client

    def _require_tasks(self) -> "JiraClient | GitHubClient":
        """Клиент задач или внятный отказ: у локального контура его нет вовсе."""
        if self.tasks_client is None:
            raise ValueError(
                "В этом воркспейсе нет провайдера задач (локальный контур). "
                "Задачи ведутся фичами: jwu feature add"
            )
        return self.tasks_client

    def _myself(self) -> dict:
        """Личность из провайдера задач, с кэшем на время жизни сервиса."""
        if self.tasks_client is None:
            return {}  # локальный контур — личности «извне» просто нет
        if self._me is None:
            try:
                self._me = self.tasks_client.myself()
            except Exception:  # noqa: BLE001 — данные о пользователе не критичны
                self._me = {}
        return self._me

    def _configured_username(self) -> str:
        """Логин, заданный в настройках провайдера (пусто — узнаем у самого провайдера)."""
        return self.cfg.github.username if self.provider == "github" else self.cfg.jira.username

    def _resolve_username(self) -> str:
        return self._configured_username() or (self._myself().get("name", "") or "")

    def _views(self) -> dict[str, str]:
        """Именованные выборки задач текущего провайдера (JQL либо поиск GitHub)."""
        return self.cfg.github.views if self.provider == "github" else self.cfg.jira.views

    def _mention_marker(self, login: str) -> str:
        """Как выглядит упоминание меня в тексте: ``[~login]`` в Jira, ``@login`` в GitHub."""
        marker = getattr(self.tasks_client, "mention_marker", None)
        return marker(login) if callable(marker) else f"[~{login}]"

    def _cred_fingerprint(self) -> str:
        """Хэш кредов (base_url + логин + токен/сессия). Меняется при смене кредов.

        Сами секреты не хранятся — только sha256. Чтение из keychain мемоизируется
        на время жизни сервиса.
        """
        if self._cred_fp is not None:
            return self._cred_fp
        if self.provider == "github":
            mats = [self.cfg.github.api_url, self.cfg.github.owner, self.cfg.github.username]
            fns = (github_token,)
        else:
            mats = [self.cfg.jira.base_url, self.cfg.jira.username]
            fns = (jira_token, jira_login, jira_proxy_basic)
        for fn in fns:
            try:
                mats.append(repr(fn(self.cfg)))
            except Exception:  # noqa: BLE001 — нет кредов/keychain недоступен
                mats.append("")
        self._cred_fp = hashlib.sha256("\x00".join(mats).encode("utf-8")).hexdigest()
        return self._cred_fp

    def _identity(self) -> tuple[str, str, str]:
        """(login, displayName, email) для шапки дашборда.

        Кэшируется в Store с отпечатком кредов. Пока креды не изменились —
        `/myself` не дёргаем; кэш переживает перезапуск.
        """
        fp = self._cred_fingerprint()
        cached = _read_identity(self.store)
        if cached.get("fp") == fp and cached.get("user"):
            return cached["user"], cached.get("display_name", ""), cached.get("email", "")
        me = self._myself()
        if not me:  # сеть недоступна — отдаём, что было в кэше
            return (cached.get("user", self._configured_username()),
                    cached.get("display_name", ""), cached.get("email", ""))
        login = self._configured_username() or me.get("name", "") or ""
        display = me.get("displayName", "") or ""
        email = me.get("emailAddress", "") or ""
        if login:
            self.store.set_workspace_meta(_IDENTITY_META, json.dumps({
                "fp": fp, "user": login,
                "display_name": display, "email": email,
            }))
        return login, display, email

    def tasks(self, view: str = "mine", *, jql: str | None = None) -> list[Issue]:
        """Список задач по именованному вью или произвольному запросу провайдера."""
        client = self._require_tasks()
        views = self._views()
        if jql is None:
            jql = views.get(view)
            if jql is None:
                raise ValueError(f"Неизвестный вью: {view!r}. Доступны: {', '.join(views)}")
        issues = client.search(jql)
        if view == "mentions" and jql is views.get("mentions"):
            issues = self._filter_mentions(issues)
        return issues

    def _filter_mentions(self, issues: list[Issue]) -> list[Issue]:
        """Локально оставить задачи, где меня реально упомянули в комментариях.

        Поиск обоих провайдеров отдаёт задачи, где я «фигурирую», — а упоминание живёт
        в тексте комментария, поэтому последнее слово всегда за локальной проверкой.
        """
        username = self._resolve_username()
        if not username or self.tasks_client is None:
            return issues  # имени нет — не можем уточнить, отдаём как есть
        marker = self._mention_marker(username)
        kept: list[Issue] = []
        for issue in issues:
            # комментов может не быть в списочном ответе — дотягиваем карточку
            if not issue.comments:
                try:
                    issue = self.tasks_client.issue(issue.key, with_dev=False)
                except Exception:  # noqa: BLE001
                    continue
            if any(marker in (c.body or "") for c in issue.comments):
                kept.append(issue)
        return kept

    def collect_mentions(self) -> list[Mention]:
        """Найти НОВЫЕ упоминания меня и записать их в память. Возвращает только новые.

        Упоминание — событие, а не задача: запись создаётся один раз и дальше живёт сама
        по себе, поэтому здесь нет ни снапшотов, ни дельт. Кандидатов даёт JQL вью
        ``mentions``; карточку с комментариями тянем только у тех задач, что изменились
        с прошлого разбора — иначе каждый синк перечитывал бы весь двухнедельный хвост.
        """
        jql = self._views().get("mentions")
        user = self._resolve_username()
        if self.tasks_client is None or not jql or not user:
            return []
        marker = self._mention_marker(user)
        scanned = self.store.mention_scan_state()
        found: list[Mention] = []
        seen_versions: list[tuple[str, str]] = []
        for issue in self.tasks_client.search(jql):
            if issue.updated and scanned.get(issue.key) == issue.updated:
                continue  # задача не менялась — новым упоминаниям взяться неоткуда
            try:
                full = self.tasks_client.issue(issue.key, with_dev=False)
            except Exception:  # noqa: BLE001 — нет доступа/сеть: разберём в следующий раз
                continue
            for c in full.comments:
                if marker in (c.body or ""):
                    found.append(Mention(
                        task_key=full.key, comment_id=c.id, author=c.author,
                        text=c.body or "", created=c.created, summary=full.summary,
                    ))
            seen_versions.append((full.key, full.updated or issue.updated))
        self.store.set_mention_scans(seen_versions)
        return self.store.add_mentions(found)

    def issue(self, key: str) -> Issue:
        self._require_tasks()
        return self._client_for_key(key).issue(key, with_dev=True)

    def add_worklog(
        self,
        key: str,
        time_spent: str,
        *,
        comment: str | None = None,
        started: str | None = None,
    ) -> dict:
        """Залогировать время по задаче в таймтрекер Jira (worklog).

        У GitHub таймтрекера нет — клиент честно откажет (см. GitHubClient.add_worklog).
        """
        self._require_tasks()
        return self._client_for_key(key).add_worklog(
            key, time_spent, comment=comment, started=started
        )

    def _workspace_repo_roots(self) -> dict[str, str]:
        """Каталоги git-репозиториев воркспейса → имя репозитория (из origin)."""
        from . import gitinfo

        roots: dict[str, str] = {}
        for path in (self.workspace.paths if self.workspace else []):
            for info in gitinfo.find_repos(path.path):
                roots[info.root] = info.name
        return roots

    def task_branch_reach(
        self, key: str, *, patterns: list[str] | None = None
    ) -> dict:
        """В какие релизные ветки доехал фикс задачи, а в какие — нет.

        Дежурный вопрос почти всегда одной формы: «баг известный, есть ли фикс в версии
        клиента». Ответ собирается из dev-панели задачи (коммиты и PR) и локальных клонов
        репозиториев воркспейса: где коммит физически есть, там git и говорит, какие ветки
        его содержат.

        ``dev_ok=False`` в ответе значит, что dev-панель Jira ответила не полностью —
        тогда пустой список коммитов означает «не знаем», а не «коммитов нет».
        Репозитории, в которых ни одного коммита задачи не нашлось, в ответ не попадают.
        """
        from . import gitbranches

        issue = self.issue(key)
        # Dev-панель отдаёт один коммит несколько раз (он приходит и от repository,
        # и от ветки) — иначе он посчитается дважды в «доехало N из M».
        commits = list({c.id: c for c in issue.commits if c.id}.values())
        shas = [c.id for c in commits]
        roots = self._workspace_repo_roots()
        repos = gitbranches.reach(
            list(roots), shas, names=roots,
            patterns=patterns or list(gitbranches.DEFAULT_BRANCH_PATTERNS),
        )
        return {
            "key": issue.key,
            "summary": issue.summary,
            "status": issue.status,
            "dev_ok": issue.dev_ok,
            "commits": [{"sha": c.id, "message": (c.message or "").splitlines()[0][:120]}
                        for c in commits],
            "prs": self._pr_targets(issue.pull_requests),
            "searched_repos": len(roots),
            "repos": [
                {
                    "name": r.name, "root": r.root, "error": r.error,
                    "commits": [{"sha": c.sha, "found": c.found, "branches": c.branches}
                                for c in r.commits],
                    "reached": r.reached, "partial": r.partial, "missing": r.missing,
                }
                for r in repos
            ],
        }

    def _pr_targets(self, prs: list) -> list[dict]:
        """Целевые ветки PR из dev-панели: куда фикс вливался (best-effort).

        PR в dev-панели приходит без целевой ветки — её знает только хостинг, поэтому
        подтягиваем детали по URL. Хостинг недоступен или PR из чужого проекта — оставляем
        ветку пустой, но сам PR из ответа не выкидываем.
        """
        out: list[dict] = []
        for pr in prs:
            item = {"id": pr.id, "status": pr.status, "name": pr.name, "url": pr.url,
                    "target_branch": "", "repository": ""}
            for pattern in (_BITBUCKET_PR_URL_RE, _GITHUB_PR_URL_RE):
                match = pattern.search(pr.url or "")
                if not match:
                    continue
                try:
                    detail = self.pr(int(match.group(3)),
                                     project=match.group(1), repo=match.group(2))
                except Exception:  # noqa: BLE001 — нет доступа/сети: ветка просто неизвестна
                    break
                item["target_branch"] = detail.target_branch
                item["repository"] = detail.repository
                break
            out.append(item)
        return out

    def create_issue(
        self,
        project: str,
        summary: str,
        *,
        description: str | None = None,
        issuetype: str = "Task",
        priority: str | None = None,
        assignee: str | None = None,
        labels: list[str] | None = None,
        components: list[str] | None = None,
        fix_versions: list[str] | None = None,
        parent: str | None = None,
        from_job: int | None = None,
        check_duplicates: bool = True,
        dry_run: bool = False,
    ) -> dict:
        """Создать задачу в трекере, предварительно сверив поля с createmeta проекта.

        Инстанс выбирается по ключу проекта тем же правилом, что и по ключу задачи
        (SDESK-* → SDESK-инстанс). Порядок такой: собрать payload → проверить по
        createmeta → и только если проверка чистая, отправить POST. ``dry_run`` даёт
        первые два шага без записи — ровно то, что показывается пользователю до «да».

        Возвращает ``{"ok", "fields", "check", "similar", "issue"}``; при ``ok=False``
        в ``check`` лежат внятные причины (какого поля не хватает, какие значения
        допустимы), а ``issue`` пустой — наружу ничего не ушло. ``similar`` заполняется
        только на превью: похожие по тексту задачи проекта, чтобы не завести дубль.
        """
        self._require_tasks()
        client = self._client_for_key(project)
        if from_job is not None and not description:
            description = self.job_description_draft(from_job)
        fields = build_create_fields(
            project, summary,
            description=description, issuetype=issuetype, priority=priority,
            assignee=assignee, labels=labels, components=components,
            fix_versions=fix_versions, parent=parent,
        )
        check = check_create_fields(client.create_meta(project), fields)
        # Отправляем payload, приведённый по createmeta: тип задачи, приоритет,
        # компоненты и версии уходят идентификаторами, а не именами (по имени этот
        # инстанс их может не разрезолвить). Показываем пользователю тот же payload —
        # dry-run обязан совпадать с тем, что реально уедет.
        fields = check["fields"]
        if dry_run or not check["ok"]:
            # Похожие задачи ищем только на превью: это подсказка человеку до отправки,
            # а на самом создании лишний запрос ни на что не влияет.
            similar = self.find_similar_issues(project, summary) if check_duplicates else []
            return {"ok": False, "dry_run": dry_run, "fields": fields, "check": check,
                    "similar": similar, "issue": {}}
        return {"ok": True, "dry_run": False, "fields": fields, "check": check,
                "similar": [], "issue": client.create_issue(fields)}

    def add_comment(self, key: str, text: str, *, client_facing: bool = False) -> dict:
        """Написать комментарий в задачу. Для SDESK нужно отдельное согласие.

        У двух случаев принципиально разная цена ошибки: комментарий во внутреннюю
        задачу читает команда, а комментарий в SDESK читает КЛИЕНТ. Поэтому случайно
        отправить черновик клиенту нельзя: для ключей SDESK требуется явный
        ``client_facing=True`` (в CLI — флаг ``--to-client``).
        """
        self._require_tasks()
        if not (text or "").strip():
            raise ValueError("Пустой комментарий отправлять некуда")
        if self._key_is_sdesk(key) and not client_facing:
            raise ValueError(
                f"{key} — задача SDESK, её комментарии читает КЛИЕНТ. Если текст готов "
                f"для клиента, подтверди это явно: CLI — флаг --to-client, "
                f"MCP — client_facing=True."
            )
        return self._client_for_key(key).add_comment(key, text)

    def key_is_client_facing(self, key: str) -> bool:
        """Уедет ли запись по этому ключу клиенту (SDESK), а не только команде."""
        return self._key_is_sdesk(key)

    def find_similar_issues(
        self, project: str, summary: str, *, months: int = 12, limit: int = 5
    ) -> list[dict]:
        """Похожие по тексту задачи проекта — проверка на дубль ПЕРЕД созданием.

        Не украшение: баг, который сегодня разбирают, вполне может оказаться точной
        копией задачи годичной давности по другому аккаунту — и находится это обычно
        случайно. Часть задач после такой проверки вообще не заводится.

        Ищем по значимым словам summary (``text ~``), в пределах проекта и последних
        ``months`` месяцев. Поиск нечёткий: это подсказка человеку, а не приговор.
        """
        terms = _text_search_terms(summary)
        if not terms or self.provider == "github":
            return []
        weeks = max(1, int(months * 4.35))
        client = self._client_for_key(project)
        # Лесенка от строгого к мягкому: сперва все значимые слова через AND, потом
        # отбрасываем самые короткие. На узком запросе ответ точный, а если он пуст —
        # дубля с таким набором слов нет, и стоит поискать по главному слову.
        for size in range(len(terms), 0, -1):
            jql = (f'project = "{project}" AND text ~ "{" AND ".join(terms[:size])}" '
                   f'AND created >= -{weeks}w ORDER BY created DESC')
            try:
                found = client.search(jql, max_results=limit)
            except Exception:  # noqa: BLE001 — поиск дублей не должен мешать созданию
                return []
            if found:
                return [
                    {"key": i.key, "summary": i.summary, "status": i.status,
                     "resolution": i.resolution, "created": i.created}
                    for i in found[:limit]
                ]
        return []

    def job_description_draft(self, job_id: int) -> str:
        """Черновик описания задачи из лога работы (фазы, баги, ревью, прогоны тестов)."""
        job = self.store.get_job(job_id)
        if job is None:
            raise ValueError(f"Работа #{job_id} не найдена в этом воркспейсе")
        return job_description_text(job)

    def link_issues(self, inward: str, outward: str, link_type: str) -> dict:
        """Связать две задачи. Тип связи сверяется со списком инстанса до отправки.

        Инстанс берётся по ключу первой задачи: связывать задачи РАЗНЫХ инстансов
        (Jira ↔ SDESK) нельзя — это разные Jira, и такая связь просто не существует.
        """
        self._require_tasks()
        if self._key_is_sdesk(inward) != self._key_is_sdesk(outward):
            raise ValueError(
                f"{inward} и {outward} живут в разных инстансах Jira — связать их нельзя. "
                f"Сошлись на задачу ссылкой в тексте."
            )
        client = self._client_for_key(inward)
        known = [str(t.get("name", "")) for t in client.link_types() if t.get("name")]
        if known and link_type.lower() not in {n.lower() for n in known}:
            raise ValueError(
                f"Тип связи «{link_type}» инстансу неизвестен. Доступные: {', '.join(known)}"
            )
        exact = next((n for n in known if n.lower() == link_type.lower()), link_type)
        return client.link_issues(inward, outward, exact)

    def transitions(self, key: str) -> list[dict]:
        """Доступные сейчас переходы задачи: сначала показать, потом выполнять."""
        self._require_tasks()
        return self._client_for_key(key).transitions(key)

    def transition(self, key: str, target: str) -> dict:
        """Перевести задачу по имени перехода (или по его id).

        Имя сверяется с тем, что инстанс отдаёт для ТЕКУЩЕГО статуса: набор переходов
        зависит от статуса и схемы проекта, поэтому «нет такого перехода» — это почти
        всегда «из этого статуса так нельзя», и список доступных нужен в самой ошибке.
        """
        self._require_tasks()
        available = self.transitions(key)
        match = next(
            (t for t in available
             if t["id"] == target or t["name"].lower() == target.lower()),
            None,
        )
        if match is None:
            names = ", ".join(t["name"] for t in available) or "—"
            raise ValueError(
                f"Перехода «{target}» у {key} сейчас нет. Доступные: {names}"
            )
        self._client_for_key(key).do_transition(key, match["id"])
        return {"key": key, "transition": match["name"], "status": match["to"]}

    def add_attachment(self, key: str, paths: list[str]) -> list[dict]:
        """Приложить файлы к задаче (har, скриншот, выгрузка, собранные при разборе).

        Файлы проверяются ДО отправки: половина загруженных вложений — худший исход,
        чем внятный отказ. Возвращает по записи на файл: имя, размер, id в Jira.
        """
        self._require_tasks()
        targets = [Path(p).expanduser() for p in paths]
        missing = [str(p) for p in targets if not p.is_file()]
        if missing:
            raise ValueError(f"Не файлы или не найдены: {', '.join(missing)}")
        if not targets:
            raise ValueError("Не задан ни один файл")
        client = self._client_for_key(key)
        out: list[dict] = []
        for path in targets:
            for created in client.add_attachment(key, path):
                out.append({
                    "id": str(created.get("id", "")),
                    "filename": created.get("filename", path.name),
                    "size": int(created.get("size", 0) or 0),
                    "path": str(path),
                })
        return out

    def link_types(self, key: str = "") -> list[str]:
        """Имена доступных типов связей (инстанс — по ключу задачи, если он задан)."""
        self._require_tasks()
        client = self._client_for_key(key) if key else self._require_tasks()
        return [str(t.get("name", "")) for t in client.link_types() if t.get("name")]

    def my_worklog_keys_on(self, date: str) -> list[str]:
        """Задачи, в которые я что-то трекал за дату — по JQL, без списка ключей заранее.

        Без этого «посмотреть, что вообще затрекано за день» можно было только зная
        заранее, куда трекал, — а нужно ровно обратное, когда разносишь время задним
        числом. Ищем в обоих инстансах (Jira и SDESK): время уходит и туда, и туда.
        У GitHub таймтрекера нет вовсе — там список всегда пуст.
        """
        if self.provider == "github":
            return []
        jql = f'worklogAuthor = currentUser() AND worklogDate = "{date}" ORDER BY updated DESC'
        keys: list[str] = []
        clients = [self.tasks_client]
        if sdesk_enabled(self.cfg):
            try:
                clients.append(self._sdesk_client())
            except Exception:  # noqa: BLE001 — SDESK недоступен: отдаём хотя бы Jira
                pass
        for client in clients:
            if client is None:
                continue
            try:
                keys.extend(issue.key for issue in client.search(jql, fields="summary"))
            except Exception:  # noqa: BLE001 — инстанс не ответил: не роняем весь день
                continue
        return list(dict.fromkeys(keys))

    def my_worklogs_on(self, keys: list[str], date: str) -> dict[str, list[dict]]:
        """Мои worklog-записи по задачам за дату — чтобы не задвоить трекинг.

        ``date`` — YYYY-MM-DD, сравнивается с датой поля ``started`` (как его отдаёт Jira,
        в таймзоне пользователя). Возвращает только задачи, где за этот день у меня
        что-то уже залогировано: ``{KEY: [{time, seconds, comment, started}, ...]}``.
        """
        me = self._resolve_username()
        out: dict[str, list[dict]] = {}
        for key in dict.fromkeys(k for k in keys if k):  # уникальные, порядок сохранён
            try:
                wls = self._client_for_key(key).worklogs(key)
            except Exception:  # noqa: BLE001 — нет задачи/прав/сети — пропускаем
                continue
            mine = [
                {
                    "time": w.get("timeSpent", "") or "",
                    "seconds": int(w.get("timeSpentSeconds", 0) or 0),
                    "comment": w.get("comment", "") or "",
                    "started": w.get("started", "") or "",
                }
                for w in wls
                if (w.get("author") or {}).get("name") == me
                and (w.get("started", "") or "")[0:10] == date
            ]
            if mine:
                out[key] = mine
        return out

    def attachments_dir(self, key: str) -> Path:
        """Каталог по умолчанию для скачанных вложений задачи: <tmp>/jwu/<KEY>."""
        return Path(tempfile.gettempdir()) / "jwu" / key

    def download_attachments(
        self,
        key: str,
        *,
        kinds: Optional[list[str]] = None,
        dest: Optional[Path] = None,
        issue: Optional[Issue] = None,
    ) -> list[tuple[Attachment, Path]]:
        """Скачать вложения задачи выбранных видов в каталог dest.

        kinds — какие виды качать (по умолчанию image/log/doc/archive; видео никогда).
        Возвращает пары (вложение, локальный путь). Имена санитизируются, коллизии
        разводятся префиксом id вложения.
        """
        wanted = set(kinds) if kinds is not None else set(DOWNLOADABLE_ATTACH_KINDS)
        client = self._client_for_key(key)
        issue = issue or client.issue(key, with_dev=False)
        dest = Path(dest) if dest is not None else self.attachments_dir(key)
        results: list[tuple[Attachment, Path]] = []
        used: set[str] = set()
        for att in issue.attachments:
            if att.kind not in wanted or not att.url:
                continue
            name = _safe_filename(att.filename) or f"attachment-{att.id}"
            if name in used:  # коллизия имён → развести префиксом id
                name = f"{att.id}-{name}"
            used.add(name)
            path = client.download_attachment(att.url, dest / name)
            results.append((att, path))
        return results

    # --- PR (Bitbucket либо GitHub) -------------------------------------- #

    def _require_prs(self) -> "BitbucketClient | GitHubClient":
        """Клиент PR или внятный отказ."""
        if self.pr_client is None:
            raise ValueError("В этом воркспейсе не подключён хостинг репозиториев (PR негде брать)")
        return self.pr_client

    def default_pr_ref(self) -> tuple[str, str]:
        """Проект/репозиторий по умолчанию для команд, где их не указали явно."""
        if self.provider == "github":
            repos = self.cfg.github.repo_list
            return self.cfg.github.owner, (repos[0] if repos else "")
        return self.cfg.bitbucket.project, self.cfg.bitbucket.repo

    def _fill_merge_status(self, pr: PR) -> None:
        """Догрузить статус merge-конфликта у PR (у провайдеров это разные запросы)."""
        client = self.pr_client
        if client is None or not (pr.project and pr.repository):
            return
        try:
            filler = getattr(client, "fill_merge_status", None)
            if callable(filler):
                filler(pr)
            else:
                pr.apply_merge_status(client.merge_status(pr.project, pr.repository, pr.id))
        except Exception:  # noqa: BLE001 — конфликт не критичен, PR полезен и без него
            pass

    def prs(self, view: str = "review", *, with_conflicts: bool = True) -> list[PR]:
        prs = self._require_prs().dashboard_prs(view)
        if with_conflicts:
            for pr in prs:
                self._fill_merge_status(pr)
        return prs

    def my_reviews(self, *, on: str | None = None) -> list[PR]:
        """PR на ревью, где мой статус — APPROVED или NEEDS_WORK, с датой моего ревью.

        Заполняет у каждого PR `my_review_status` и `my_review_at` (дата из activities,
        epoch ms). Если задан `on` (YYYY-MM-DD, локальная дата) — оставляет только ревью,
        поставленные в этот день; PR без даты моего ревья при фильтре отбрасываются.
        """
        client = self._require_prs()
        login = self._resolve_username()
        out: list[PR] = []
        for pr in client.dashboard_prs("review"):
            mine = next((r for r in pr.reviewers if r.name == login), None)
            if mine is None or mine.status not in ("APPROVED", "NEEDS_WORK"):
                continue
            if not (pr.project and pr.repository):
                continue
            ts = client.my_review_at(pr.project, pr.repository, pr.id, login)
            pr.my_review_status = mine.status
            pr.my_review_at = ts
            if on is not None:
                if ts is None:
                    continue
                if datetime.fromtimestamp(ts / 1000).date().isoformat() != on:
                    continue
            out.append(pr)
        return out

    def pr(self, pr_id: int, *, project: str | None = None, repo: str | None = None) -> PR:
        default_project, default_repo = self.default_pr_ref()
        return self._require_prs().pr(
            project or default_project,
            repo or default_repo,
            pr_id,
        )

    # --- sync / changes ------------------------------------------------- #

    def _sync_tasks(self, run_id: int, views: list[str]) -> dict[str, int]:
        seen: dict[str, Issue] = {}
        key_views: dict[str, set[str]] = {}
        counts: dict[str, int] = {}
        for view in views:
            try:
                issues = self.tasks(view)
            except Exception:  # noqa: BLE001 — кривой вью/JQL не валит синк
                continue
            counts[f"tasks:{view}"] = len(issues)
            for issue in issues:
                seen.setdefault(issue.key, issue)
                key_views.setdefault(issue.key, set()).add(view)
        # детальный снапшот: комменты + связанные ветки/PR
        for key in seen:
            try:
                full = self.tasks_client.issue(key, with_dev=True)
            except Exception:  # noqa: BLE001
                full = seen[key]
            self.store.save_issue_snapshot(run_id, full, sorted(key_views.get(key, [])))
        return counts

    def _sync_prs(self, run_id: int, views: list[str]) -> dict[str, int]:
        pr_seen: dict[tuple[str, str, int], PR] = {}
        pr_views: dict[tuple[str, str, int], set[str]] = {}
        counts: dict[str, int] = {}
        for view in views:
            try:
                prs = self.pr_client.dashboard_prs(view)
            except Exception:  # noqa: BLE001
                continue
            counts[f"prs:{view}"] = len(prs)
            for pr in prs:
                # PR опознаётся вместе с репозиторием: в GitHub нумерация в каждом своя
                ref = (pr.project, pr.repository, pr.id)
                pr_seen.setdefault(ref, pr)
                pr_views.setdefault(ref, set()).add(view)
        for ref, pr in pr_seen.items():
            if pr.project and pr.repository:
                self._fill_merge_status(pr)
                if not pr.latest_commit:
                    try:
                        pr.latest_commit = self.pr_client.latest_commit(
                            pr.project, pr.repository, pr.id
                        )
                    except Exception:  # noqa: BLE001
                        pass
            self.store.save_pr_snapshot(run_id, pr, sorted(pr_views.get(ref, [])))
        # Подтянуть статус/assignee задач, на которые ссылаются PR — нужно для
        # колонок «Назначен»/«Статус» в дашборде. PR на чужой релизной задаче
        # никогда не попадёт в mine/mentions, но ключ в branch/title есть.
        self._snapshot_pr_tasks(run_id, list(pr_seen.values()))
        return counts

    def _task_key_from_pr(self, pr: PR) -> str:
        """Ключ задачи, к которой относится PR: из имени ветки, иначе из заголовка.

        В Jira-контуре это ``PROJ-123`` (ветка ``PROJ-123-fix``), в GitHub — номер issue:
        ветка ``42-fix-crash`` (так их называет сам GitHub) либо ``#42`` в заголовке PR.
        """
        if self.provider != "github":
            for src in (pr.source_branch, pr.title):
                match = _PR_TASK_KEY_RE.search(src or "")
                if match:
                    return match.group(1)
            return ""
        branch_match = re.match(r"^(?:\w+/)?(\d+)[-_]", pr.source_branch or "")
        number = branch_match.group(1) if branch_match else ""
        if not number:
            title_match = GITHUB_SHORT_REF_RE.search(pr.title or "")
            number = title_match.group(1) if title_match else ""
        if not number or not pr.repository:
            return ""
        return github_key(pr.repository, number, owner=pr.project,
                          default_owner=self.cfg.github.owner)

    def _snapshot_pr_tasks(self, run_id: int, prs: list[PR]) -> None:
        """Снапшотим задачи, упомянутые в branch/title PR, если их ещё нет в этом run.

        Jira может прислать другой канонический ключ (старый ключ замёрджен в новый),
        в этом случае запоминаем алиас requested_key → full.key в meta-таблице,
        чтобы дашборд разрезолвил статус/assignee по ключу из ветки PR.
        """
        keys: set[str] = set()
        for pr in prs:
            key = self._task_key_from_pr(pr)
            if key:
                keys.add(key)
        aliases = _load_pr_task_aliases(self.store)
        # Уже снапшотнутые в этом прогоне задачи (из mine/mentions) трогать нельзя:
        # их снапшот богаче (with_dev=True, есть pr_ids/branches), а pr_link-дубль
        # обеднён (pr_ids=[]). Два разных снапшота одного ключа в одном прогоне ломают
        # сравнение в compute_changes — каждый синк заново плодит ложные new_pr.
        existing = self.store.snapshotted_issue_keys(run_id)
        changed = False
        for key in keys:
            if key in existing or aliases.get(key) in existing:
                continue
            try:
                full = self._client_for_key(key).issue(key, with_dev=False)
            except Exception:  # noqa: BLE001 — отсутствующая/недоступная задача не валит синк
                continue
            if full.key not in existing:
                self.store.save_issue_snapshot(run_id, full, ["pr_link"])
                existing.add(full.key)
            if full.key and full.key != key:
                if aliases.get(key) != full.key:
                    aliases[key] = full.key
                    changed = True
            elif key in aliases:
                # ключ снова совпадает сам с собой — алиас устарел
                aliases.pop(key, None)
                changed = True
        if changed:
            self.store.set_workspace_meta(_PR_TASK_ALIAS_META, json.dumps(aliases, ensure_ascii=False))

    def sync(self) -> SyncResult:
        """Полный синк всех секций в одном прогоне (для `jwu sync`).

        Секции отключённых интеграций пропускаются целиком — и в ``views`` прогона их
        тоже нет, иначе детекция исчезновения решила бы, что вкладка «опустела».
        """
        views: list[str] = []
        if self.tasks_client is not None:
            # «mentions» остаётся в списке вью только ради отметки времени синка в
            # шапке вкладки: снапшотов у упоминаний нет (см. counts ниже).
            views += ["mine", "mentions"]
        if self.pr_client is not None:
            views += ["prs:mine", "prs:review"]
        run_id = self.store.start_sync_run(views)
        counts: dict[str, int] = {}
        if self.tasks_client is not None:
            counts |= self._sync_tasks(run_id, ["mine"])
            # Упоминания живут отдельной сущностью, вне снапшотов и дельт (см.
            # collect_mentions): их «изменение» — это появление нового упоминания.
            counts["mentions"] = len(self.collect_mentions())
        if self.pr_client is not None:
            counts |= self._sync_prs(run_id, ["mine", "review"])
        # counts фиксируем ДО compute_changes: детекция исчезновения (gone/pr_gone)
        # опирается на надёжность прогона по counts, а ради «вкладка реально пуста»
        # vs «фетч упал» это должно быть видно уже для текущего прогона.
        self.store.finish_sync_run(run_id, counts)
        deltas = self.store.compute_changes(run_id)
        self.store.add_pending_changes(run_id, deltas)  # копим до явного закрытия
        return SyncResult(run_id=run_id, counts=counts, deltas=deltas)

    def sync_section(self, section: str) -> SyncResult:
        """Синк одной секции/вкладки: mine | mentions | prs_mine | prs_review."""
        if section == "mentions":
            if self.tasks_client is None:
                raise ValueError("В этом воркспейсе нет провайдера задач")
            run_id = self.store.start_sync_run(["mentions"])
            counts = {"mentions": len(self.collect_mentions())}
            self.store.finish_sync_run(run_id, counts)
            return SyncResult(run_id=run_id, counts=counts, deltas=[])
        if section == "mine":
            if self.tasks_client is None:
                raise ValueError("В этом воркспейсе нет провайдера задач")
            run_id = self.store.start_sync_run([section])
            counts = self._sync_tasks(run_id, [section])
        elif section in ("prs_mine", "prs_review"):
            if self.pr_client is None:
                raise ValueError("В этом воркспейсе не подключён хостинг репозиториев")
            view = "mine" if section == "prs_mine" else "review"
            run_id = self.store.start_sync_run([f"prs:{view}"])
            counts = self._sync_prs(run_id, [view])
        else:
            raise ValueError(f"Неизвестная секция: {section!r}")
        self.store.finish_sync_run(run_id, counts)  # counts до compute_changes (см. sync())
        deltas = self.store.compute_changes(run_id)
        self.store.add_pending_changes(run_id, deltas)  # копим до явного закрытия
        return SyncResult(run_id=run_id, counts=counts, deltas=deltas)

    def pr_detail(self, project: str | None, repo: str | None, pr_id: int) -> "PRDetail":
        """Лениво: PR + статус конфликта + комменты (с дифф-контекстом) + коммиты."""
        client = self._require_prs()
        default_project, default_repo = self.default_pr_ref()
        project = project or default_project
        repo = repo or default_repo
        pr = client.pr(project, repo, pr_id)
        try:
            comments = client.pr_comments(project, repo, pr_id)
        except Exception:  # noqa: BLE001
            comments = []
        try:
            commits = client.pr_commits(project, repo, pr_id)
        except Exception:  # noqa: BLE001
            commits = []
        try:
            pr.builds = self.build_statuses_for_pr(project, repo, pr_id)
        except Exception:  # noqa: BLE001
            pr.builds = []
        return PRDetail(pr=pr, comments=comments, commits=commits)

    def build_statuses_for_pr(self, project: str, repo: str, pr_id: int) -> list[BuildStatus]:
        """Статусы CI-сборок по head-коммиту PR (то, что видно на странице PR)."""
        client = self._require_prs()
        sha = client.latest_commit(project, repo, pr_id)
        if not sha:
            return []
        # GitHub ищет сборки только внутри репозитория, Bitbucket — по всему инстансу;
        # project/repo передаём всегда, лишними они не будут.
        return client.build_statuses(sha, project=project, repo=repo)

    def build_statuses_for_pr_url(self, url: str) -> list[BuildStatus]:
        """Статусы сборок по URL PR из карточки задачи (парсит project/repo/id из URL)."""
        for pattern in (_BITBUCKET_PR_URL_RE, _GITHUB_PR_URL_RE):
            match = pattern.search(url or "")
            if match:
                return self.build_statuses_for_pr(
                    match.group(1), match.group(2), int(match.group(3))
                )
        return []

    def build_report(
        self,
        project: str | None,
        repo: str | None,
        pr_id: int,
        *,
        build_url: str | None = None,
    ) -> BuildReport | None:
        """Детальный разбор сборки PR: статус из хостинга + причина падения из CI.

        По умолчанию берётся упавшая сборка (иначе — первая по head-коммиту), либо
        конкретная по ``build_url``. Без доступа к CI (нет токена Jenkins, протухли логи
        Actions) отчёт деградирует до статуса с пояснением в ``note``. None — если по
        коммиту сборок нет вовсе.
        """
        default_project, default_repo = self.default_pr_ref()
        project = project or default_project
        repo = repo or default_repo
        statuses = self.build_statuses_for_pr(project, repo, pr_id)
        if build_url:
            chosen = next((b for b in statuses if b.url == build_url), None) or BuildStatus(url=build_url)
        else:
            chosen = next((b for b in statuses if b.state == "FAILED"), None)
            chosen = chosen or next((b for b in statuses if b.state == "INPROGRESS"), None)
            chosen = chosen or (statuses[0] if statuses else None)
        if chosen is None:
            return None

        # У GitHub-контура за детализацию отвечает сам клиент (Actions живут там же).
        if self.github is not None:
            return self.github.build_report(project, repo, chosen)

        report = BuildReport(
            state=chosen.state, name=chosen.name, url=chosen.url, description=chosen.description,
        )
        parsed = parse_build_url(chosen.url)
        if parsed:
            report.job_path, report.number = parsed

        if self.jenkins is None or self.jenkins.auth is None:
            report.note = "Jenkins-токен не настроен — детализация недоступна (`jwu configure`)."
            return report
        if parsed is None:
            report.note = f"Не удалось разобрать URL сборки Jenkins: {chosen.url!r}"
            return report

        job_path, number = parsed
        try:
            info = self.jenkins.build_info(job_path, number)
            report.details_available = True
            report.result = info["result"] or ""
            report.building = info["building"]
            report.sha = info["sha"]
            report.branch = info["branch"]
            report.summary = self.jenkins.test_summary(job_path, number)
            report.failures = [
                TestCaseFailure(
                    class_name=c["class"], name=c["name"], status=c["status"],
                    error_details=c["error_details"], stack=c["stack"],
                )
                for c in self.jenkins.failed_cases(job_path, number)
            ]
            # Консольный хвост нужен только для разбора провала (не для зелёной/идущей).
            if not report.building and report.result not in ("", "SUCCESS"):
                report.console_tail = self.jenkins.console_tail(job_path, number)
        except JenkinsError as exc:
            report.note = f"Jenkins недоступен: {exc}"
        return report

    def collect_day_context(self, *, max_pr_comments: int = 8) -> DayContext:
        """Фулл-синк + расширенный контекст для дневного анализа Claude Code."""
        self.sync()
        login, display, _ = self._identity()
        user = login or self._configured_username()
        d = dashboard_from_memory(self.store, user)

        # подтянуть комменты только для проблемных PR (конфликт / есть NEEDS_WORK)
        pr_comments: dict[int, list[PRComment]] = {}
        flagged = [
            p for p in (d.prs_mine + d.prs_review)
            if p.conflicted or any((r.status or "") == "NEEDS_WORK" for r in p.reviewers)
        ]
        seen: set[int] = set()
        for pr in flagged:
            if pr.id in seen or len(pr_comments) >= max_pr_comments:
                continue
            seen.add(pr.id)
            if pr.project and pr.repository and self.pr_client is not None:
                try:
                    pr_comments[pr.id] = self.pr_client.pr_comments(
                        pr.project, pr.repository, pr.id
                    )
                except Exception:  # noqa: BLE001
                    pass
        return DayContext(
            user=user,
            me_display=display,
            synced_at=self.store.last_sync_at(),
            deltas=d.deltas,
            mine=d.mine,
            prs_mine=d.prs_mine,
            prs_review=d.prs_review,
            mentions=d.mentions,
            pr_comments=pr_comments,
        )

    def changes(self) -> list[Delta]:
        return self.store.pending_changes()

    def ack_changes(self) -> None:
        """Явно закрыть накопленные изменения."""
        self.store.clear_pending_changes()

    def dashboard(self) -> DashboardData:
        """Дашборд из памяти (после возможного sync)."""
        login, display, email = self._identity()
        data = dashboard_from_memory(self.store, login)
        data.display_name = display
        data.email = email
        return data

    # --- заметки -------------------------------------------------------- #

    def add_note(self, key: str, text: str) -> Note:
        return self.store.add_note(key, text)

    def get_notes(self, key: str) -> list[Note]:
        return self.store.get_notes(key)

    def jobs_for_task(self, key: str) -> list[Job]:
        return self.store.jobs_for_task(key)

    def jobs_for_pr(self, pr_id: int, project: str = "", repo: str = "") -> list[Job]:
        return self.store.jobs_for_pr(pr_id, project, repo)

    # --- auth ----------------------------------------------------------- #

    def auth_check(self) -> dict:
        """Проверка доступов по системам текущего провайдера.

        Ключи в ответе — имена систем (jira/sdesk/bitbucket/github/jenkins); отсутствие
        ключа значит «в этом контуре такой системы нет», а не «не проверяли».
        """
        result: dict = {}
        if self.github is not None:
            try:
                me = self.github.myself()
                result["github"] = {
                    "ok": True, "user": me.get("name"), "name": me.get("displayName"),
                }
            except Exception as exc:  # noqa: BLE001
                result["github"] = {"ok": False, "error": str(exc)}
            return result
        if self.jira is None:
            return result  # локальный контур — проверять нечего
        try:
            me = self.jira.myself()
            result["jira"] = {"ok": True, "user": me.get("name"), "name": me.get("displayName")}
        except Exception as exc:  # noqa: BLE001
            result["jira"] = {"ok": False, "error": str(exc)}
        # SDESK — второй Jira-инстанс: проверяем, только если подключён. Материализация
        # (логин) идёт лениво и её ошибка не мешает отчёту по Jira/Bitbucket.
        if sdesk_enabled(self.cfg):
            try:
                me = self._sdesk_client().myself()
                result["sdesk"] = {"ok": True, "user": me.get("name"), "name": me.get("displayName")}
            except Exception as exc:  # noqa: BLE001
                result["sdesk"] = {"ok": False, "error": str(exc)}
        if self.bitbucket is not None:
            try:
                self.bitbucket.ping()
                result["bitbucket"] = {"ok": True}
            except Exception as exc:  # noqa: BLE001
                result["bitbucket"] = {"ok": False, "error": str(exc)}
        # Jenkins опционален: проверяем, только если настроен токен.
        if self.jenkins is not None and self.jenkins.auth is not None:
            try:
                self.jenkins.ping()
                result["jenkins"] = {"ok": True}
            except Exception as exc:  # noqa: BLE001
                result["jenkins"] = {"ok": False, "error": str(exc)}
        return result
