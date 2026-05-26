"""Сервисный слой: связывает клиентов Jira/Bitbucket и SQLite-память.

CLI обращается только сюда. Здесь же — локальная доводка «упоминаний» и оркестрация sync.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .bitbucket import BitbucketClient
from .config import (
    Config,
    bitbucket_token,
    jira_login,
    jira_proxy_basic,
    jira_token,
    load_config,
)
from .jira import JiraClient
from .models import (
    Analysis,
    Attachment,
    DOWNLOADABLE_ATTACH_KINDS,
    Delta,
    Issue,
    Job,
    Note,
    PR,
    PRComment,
)
from .store import Store

_UNSAFE_NAME_RE = re.compile(r"[^\w.\- ]+", re.UNICODE)


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
    synced_at: str | None = None
    deltas: list[Delta] = field(default_factory=list)
    mine: list[Issue] = field(default_factory=list)
    prs_mine: list[PR] = field(default_factory=list)
    prs_review: list[PR] = field(default_factory=list)
    # (issue, тексты комментов с упоминанием меня)
    mentions: list[tuple[Issue, list[str]]] = field(default_factory=list)
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
    mentions: list[Issue] = field(default_factory=list)
    prs_mine: list[PR] = field(default_factory=list)
    prs_review: list[PR] = field(default_factory=list)
    analyses: list[Analysis] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        return {
            "user": self.user,
            "display_name": self.display_name,
            "email": self.email,
            "last_sync": self.last_sync,
            "deltas": [d.model_dump() for d in self.deltas],
            "mine": [i.model_dump() for i in self.mine],
            "mentions": [i.model_dump() for i in self.mentions],
            "prs_mine": [p.model_dump() for p in self.prs_mine],
            "prs_review": [p.model_dump() for p in self.prs_review],
            "analyses": [a.model_dump() for a in self.analyses],
            "jobs": [j.model_dump() for j in self.jobs],
        }


# ключ персистентного кэша личности пользователя в Store.meta
_IDENTITY_META = "identity"


def _read_identity(store: Store) -> dict:
    """Кэш личности (user/display_name/email + отпечаток кредов) из памяти."""
    raw = store.get_meta(_IDENTITY_META)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001 — битый кэш не критичен
        return {}


def dashboard_from_memory(store: Store, user: str = "") -> DashboardData:
    """Собрать дашборд из памяти (свежайшие снапшоты по сущностям). Сеть/токены не нужны.

    Личность (имя/почта) берётся из персистентного кэша — поэтому показывается сразу
    после перезапуска, до первого синка.
    """
    ident = _read_identity(store)
    return DashboardData(
        user=user or ident.get("user", ""),
        display_name=ident.get("display_name", ""),
        email=ident.get("email", ""),
        last_sync={
            section: store.last_sync_at(token) for section, token in SECTION_TOKEN.items()
        },
        deltas=store.pending_changes(),  # накопленные изменения (до явного закрытия)
        mine=store.latest_issues("mine"),
        mentions=store.latest_issues("mentions"),
        prs_mine=store.latest_prs("mine"),
        prs_review=store.latest_prs("review"),
        analyses=store.list_analyses(),
        jobs=store.list_jobs(),  # все работы (включая закрытые/завершённые)
    )


class Service:
    def __init__(self, cfg: Config, jira: JiraClient, bitbucket: BitbucketClient, store: Store) -> None:
        self.cfg = cfg
        self.jira = jira
        self.bitbucket = bitbucket
        self.store = store
        self._me: dict | None = None      # кэш /myself на время жизни сервиса
        self._cred_fp: str | None = None  # кэш отпечатка кредов

    @classmethod
    def from_config(cls, cfg: Config | None = None, *, db_path: str | None = None) -> "Service":
        from .config import db_path as default_db_path

        cfg = cfg or load_config()
        login = jira_login(cfg)
        if login is not None:
            # за Jira стоит nginx Basic-гейт + сессионная авторизация
            jira = JiraClient(
                cfg.jira.base_url,
                proxy_basic=jira_proxy_basic(cfg),
                session_login=login,
            )
            if not cfg.jira.username:
                cfg.jira.username = login[0]
        else:
            # гейта нет — обычный PAT через Bearer
            jira = JiraClient(cfg.jira.base_url, jira_token(cfg))
        bitbucket = BitbucketClient(cfg.bitbucket.base_url, bitbucket_token(cfg))
        store = Store(db_path or str(default_db_path()))
        return cls(cfg, jira, bitbucket, store)

    def close(self) -> None:
        self.jira.close()
        self.bitbucket.close()
        self.store.close()

    def __enter__(self) -> "Service":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- Jira ----------------------------------------------------------- #

    def _myself(self) -> dict:
        """/myself с кэшем на время жизни сервиса (один запрос на синк)."""
        if self._me is None:
            try:
                self._me = self.jira.myself()
            except Exception:  # noqa: BLE001 — данные о пользователе не критичны
                self._me = {}
        return self._me

    def _resolve_username(self) -> str:
        if self.cfg.jira.username:
            return self.cfg.jira.username
        return self._myself().get("name", "") or ""

    def _cred_fingerprint(self) -> str:
        """Хэш кредов (base_url + логин + токен/сессия). Меняется при смене кредов.

        Сами секреты не хранятся — только sha256. Чтение из keychain мемоизируется
        на время жизни сервиса.
        """
        if self._cred_fp is not None:
            return self._cred_fp
        mats = [self.cfg.jira.base_url, self.cfg.jira.username]
        for fn in (jira_token, jira_login, jira_proxy_basic):
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
            return (cached.get("user", self.cfg.jira.username),
                    cached.get("display_name", ""), cached.get("email", ""))
        login = self.cfg.jira.username or me.get("name", "") or ""
        display = me.get("displayName", "") or ""
        email = me.get("emailAddress", "") or ""
        if login:
            self.store.set_meta(_IDENTITY_META, json.dumps({
                "fp": fp, "user": login,
                "display_name": display, "email": email,
            }))
        return login, display, email

    def tasks(self, view: str = "mine", *, jql: str | None = None) -> list[Issue]:
        """Список задач по именованному вью или произвольному JQL."""
        if jql is None:
            jql = self.cfg.jira.views.get(view)
            if jql is None:
                raise ValueError(f"Неизвестный вью: {view!r}. Доступны: {', '.join(self.cfg.jira.views)}")
        issues = self.jira.search(jql)
        if view == "mentions" and jql is self.cfg.jira.views.get("mentions"):
            issues = self._filter_mentions(issues)
        return issues

    def _filter_mentions(self, issues: list[Issue]) -> list[Issue]:
        """Локально оставить задачи, где меня реально упомянули в комментах ([~username])."""
        username = self._resolve_username()
        if not username:
            return issues  # имени нет — не можем уточнить, отдаём как есть
        marker = f"[~{username}]"
        kept: list[Issue] = []
        for issue in issues:
            # комментов может не быть в списочном ответе — дотягиваем карточку
            if not issue.comments:
                try:
                    issue = self.jira.issue(issue.key, with_dev=False)
                except Exception:  # noqa: BLE001
                    continue
            if any(marker in (c.body or "") for c in issue.comments):
                kept.append(issue)
        return kept

    def issue(self, key: str) -> Issue:
        return self.jira.issue(key, with_dev=True)

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
        issue = issue or self.jira.issue(key, with_dev=False)
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
            path = self.jira.download_attachment(att.url, dest / name)
            results.append((att, path))
        return results

    # --- Bitbucket ------------------------------------------------------ #

    def prs(self, view: str = "review", *, with_conflicts: bool = True) -> list[PR]:
        prs = self.bitbucket.dashboard_prs(view)
        if with_conflicts:
            for pr in prs:
                if pr.project and pr.repository:
                    try:
                        pr.apply_merge_status(
                            self.bitbucket.merge_status(pr.project, pr.repository, pr.id)
                        )
                    except Exception:  # noqa: BLE001
                        pass
        return prs

    def pr(self, pr_id: int, *, project: str | None = None, repo: str | None = None) -> PR:
        return self.bitbucket.pr(
            project or self.cfg.bitbucket.project,
            repo or self.cfg.bitbucket.repo,
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
        # детальный снапшот: комменты + dev-панель
        for key in seen:
            try:
                full = self.jira.issue(key, with_dev=True)
            except Exception:  # noqa: BLE001
                full = seen[key]
            self.store.save_issue_snapshot(run_id, full, sorted(key_views.get(key, [])))
        return counts

    def _sync_prs(self, run_id: int, views: list[str]) -> dict[str, int]:
        pr_seen: dict[int, PR] = {}
        pr_views: dict[int, set[str]] = {}
        counts: dict[str, int] = {}
        for view in views:
            try:
                prs = self.bitbucket.dashboard_prs(view)
            except Exception:  # noqa: BLE001
                continue
            counts[f"prs:{view}"] = len(prs)
            for pr in prs:
                pr_seen.setdefault(pr.id, pr)
                pr_views.setdefault(pr.id, set()).add(view)
        for pr in pr_seen.values():
            if pr.project and pr.repository:
                try:
                    pr.apply_merge_status(
                        self.bitbucket.merge_status(pr.project, pr.repository, pr.id)
                    )
                except Exception:  # noqa: BLE001
                    pass
                try:
                    pr.latest_commit = self.bitbucket.latest_commit(
                        pr.project, pr.repository, pr.id
                    )
                except Exception:  # noqa: BLE001
                    pass
            self.store.save_pr_snapshot(run_id, pr, sorted(pr_views.get(pr.id, [])))
        return counts

    def sync(self) -> SyncResult:
        """Полный синк всех секций в одном прогоне (для `jwu sync`)."""
        run_id = self.store.start_sync_run(["mine", "mentions", "prs:mine", "prs:review"])
        counts = self._sync_tasks(run_id, ["mine", "mentions"])
        counts |= self._sync_prs(run_id, ["mine", "review"])
        deltas = self.store.compute_changes(run_id)
        self.store.add_pending_changes(run_id, deltas)  # копим до явного закрытия
        self.store.finish_sync_run(run_id, counts)
        return SyncResult(run_id=run_id, counts=counts, deltas=deltas)

    def sync_section(self, section: str) -> SyncResult:
        """Синк одной секции/вкладки: mine | mentions | prs_mine | prs_review."""
        if section in ("mine", "mentions"):
            run_id = self.store.start_sync_run([section])
            counts = self._sync_tasks(run_id, [section])
        elif section in ("prs_mine", "prs_review"):
            view = "mine" if section == "prs_mine" else "review"
            run_id = self.store.start_sync_run([f"prs:{view}"])
            counts = self._sync_prs(run_id, [view])
        else:
            raise ValueError(f"Неизвестная секция: {section!r}")
        deltas = self.store.compute_changes(run_id)
        self.store.add_pending_changes(run_id, deltas)  # копим до явного закрытия
        self.store.finish_sync_run(run_id, counts)
        return SyncResult(run_id=run_id, counts=counts, deltas=deltas)

    def pr_detail(self, project: str | None, repo: str | None, pr_id: int) -> "PRDetail":
        """Лениво: PR + статус конфликта + комменты (с дифф-контекстом) + коммиты."""
        project = project or self.cfg.bitbucket.project
        repo = repo or self.cfg.bitbucket.repo
        pr = self.bitbucket.pr(project, repo, pr_id)
        try:
            comments = self.bitbucket.pr_comments(project, repo, pr_id)
        except Exception:  # noqa: BLE001
            comments = []
        try:
            commits = self.bitbucket.pr_commits(project, repo, pr_id)
        except Exception:  # noqa: BLE001
            commits = []
        return PRDetail(pr=pr, comments=comments, commits=commits)

    def collect_day_context(self, *, max_pr_comments: int = 8) -> DayContext:
        """Фулл-синк + расширенный контекст для дневного анализа Claude Code."""
        self.sync()
        d = dashboard_from_memory(self.store, self.cfg.jira.username)
        user = self.cfg.jira.username
        marker = f"[~{user}]" if user else None

        mentions: list[tuple[Issue, list[str]]] = []
        for issue in d.mentions:
            texts = [c.body for c in issue.comments if marker and marker in (c.body or "")]
            mentions.append((issue, texts))

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
            if pr.project and pr.repository:
                try:
                    pr_comments[pr.id] = self.bitbucket.pr_comments(
                        pr.project, pr.repository, pr.id
                    )
                except Exception:  # noqa: BLE001
                    pass
        return DayContext(
            user=user,
            synced_at=self.store.last_sync_at(),
            deltas=d.deltas,
            mine=d.mine,
            prs_mine=d.prs_mine,
            prs_review=d.prs_review,
            mentions=mentions,
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
        result: dict = {}
        try:
            me = self.jira.myself()
            result["jira"] = {"ok": True, "user": me.get("name"), "name": me.get("displayName")}
        except Exception as exc:  # noqa: BLE001
            result["jira"] = {"ok": False, "error": str(exc)}
        try:
            self.bitbucket.ping()
            result["bitbucket"] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            result["bitbucket"] = {"ok": False, "error": str(exc)}
        return result
