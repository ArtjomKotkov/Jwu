"""MCP-сервер jwu: инструменты поверх сервисного слоя.

Даёт Claude Code структурный доступ к тем же данным/действиям, что и CLI
(`jwu task/pr/job/...`), но без похода через shell: типизированные вызовы, JSON-ответы
и — главное — ПЕРЕИСПОЛЬЗУЕМАЯ сессия. Сервис создаётся лениво и живёт на процесс,
поэтому вход в Jira/SDESK (в т.ч. сессионный за гейтом) выполняется один раз.

Read: task, prs, pr, builds, build, attachments, jobs.
Write: note, worklog, job_start, job_add, job_link, job_status. Почти все записи —
локальные (память работ/заметок); ВНЕШНЯЯ запись только у `jwu_worklog` (таймтрекер
Jira/SDESK) — вызывать по явному подтверждению пользователя.

Транспорт — stdio (запуск как подпроцесс Claude Code). Инструменты объявлены async,
чтобы все обращения к клиентам и SQLite-хранилищу шли из одного потока event-loop
(sqlite-соединение Store привязано к своему потоку — check_same_thread).
"""

from __future__ import annotations

import atexit
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .core.config import db_path, load_config
from .core.maintenance import ensure_db_available
from .core.models import JOB_RECORD_KINDS
from .core.service import Service
from .core.store import Store

# Допустимые статусы работы (как в CLI `jwu job status`).
_JOB_STATUSES = ("active", "done", "paused", "cancelled")

mcp = FastMCP("jwu")

# Ленивые синглтоны на процесс — тут и живёт переиспользуемая сессия/соединения.
_full: Service | None = None      # полный сервис (Jira/SDESK + Bitbucket + Jenkins)
_builds: Service | None = None    # лёгкий сервис для сборок (Bitbucket + Jenkins, без Jira)
_store: Store | None = None       # только память (работы/заметки), без сети


def _ensure_db() -> None:
    ensure_db_available(db_path())


def _full_svc() -> Service:
    """Полный сервис с логином в Jira/SDESK (создаётся один раз на процесс)."""
    global _full
    if _full is None:
        _ensure_db()
        _full = Service.from_config(load_config())
    return _full


def _builds_svc() -> Service:
    """Сервис для сборок: Bitbucket + Jenkins, без зависимости от Jira (как CLI build/builds)."""
    global _builds
    if _builds is None:
        _ensure_db()
        _builds = Service.for_builds(load_config())
    return _builds


def _store_only() -> Store:
    """Только локальная память (работы) — без сети и токенов (как CLI `jobs`)."""
    global _store
    if _store is None:
        _ensure_db()
        _store = Store(str(db_path()))
    return _store


@atexit.register
def _cleanup() -> None:
    for svc in (_full, _builds):
        try:
            if svc is not None:
                svc.close()
        except Exception:  # noqa: BLE001 — гасим на завершении процесса
            pass
    try:
        if _store is not None:
            _store.close()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Инструменты (read-only)
# --------------------------------------------------------------------------- #


@mcp.tool()
async def jwu_task(key: str) -> dict:
    """Полная карточка задачи Jira/SDESK по ключу (напр. WMCTASKS-123 или SDESK-39336).

    Инстанс выбирается по префиксу ключа автоматически. Возвращает поля задачи,
    описание, все комментарии, links, dev-панель (ветки/PR), плюс локальные заметки
    (`notes`), связанные работы (`jobs`) и статусы CI-сборок по OPEN-PR (`pr_builds`).
    """
    svc = _full_svc()
    issue = svc.issue(key)
    notes = svc.get_notes(key)
    jobs = svc.jobs_for_task(key)
    pr_builds: dict[str, list] = {}
    for pr in [p for p in issue.pull_requests if p.status == "OPEN"][:5]:
        try:
            pr_builds[pr.id] = svc.build_statuses_for_pr_url(pr.url)
        except Exception:  # noqa: BLE001 — сборки не критичны для карточки
            pr_builds[pr.id] = []
    payload = issue.model_dump()
    payload["notes"] = [n.model_dump() for n in notes]
    payload["jobs"] = [j.model_dump() for j in jobs]
    payload["pr_builds"] = {pid: [b.model_dump() for b in bs] for pid, bs in pr_builds.items()}
    return payload


@mcp.tool()
async def jwu_prs(view: str = "review", with_conflicts: bool = True) -> list[dict]:
    """Список PR из Bitbucket по роли: view = "review" (ждут моего ревью) или "mine" (мои).

    with_conflicts=True добавляет статус merge-конфликта по каждому PR (чуть медленнее).
    """
    svc = _full_svc()
    return [p.model_dump() for p in svc.prs(view, with_conflicts=with_conflicts)]


@mcp.tool()
async def jwu_pr(
    pr_id: int,
    project: Optional[str] = None,
    repo: Optional[str] = None,
) -> dict:
    """Детали одного PR: статус конфликта, ревьюеры, все комментарии ревью (с file:line
    и вложенностью), коммиты, статусы сборок и связанные работы.

    project/repo — ключ проекта и slug репозитория Bitbucket; если не заданы, берутся
    дефолтные из конфига. Если у PR NEEDS_WORK, а комментарии пусты — укажи project/repo.
    """
    svc = _full_svc()
    detail = svc.pr_detail(project, repo, pr_id)
    jobs = svc.jobs_for_pr(pr_id, project or "", repo or "")
    payload = detail.pr.model_dump()
    payload["comments"] = [c.model_dump() for c in detail.comments]
    payload["commits"] = detail.commits
    payload["jobs"] = [j.model_dump() for j in jobs]
    return payload


@mcp.tool()
async def jwu_builds(
    pr_id: int,
    project: Optional[str] = None,
    repo: Optional[str] = None,
) -> list[dict]:
    """Статусы CI-сборок по head-коммиту PR (быстро, из build-status API Bitbucket).

    Не зависит от Jira. project/repo — по умолчанию из конфига.
    """
    svc = _builds_svc()
    proj = project or svc.cfg.bitbucket.project
    rp = repo or svc.cfg.bitbucket.repo
    return [b.model_dump() for b in svc.build_statuses_for_pr(proj, rp, pr_id)]


@mcp.tool()
async def jwu_build(
    pr_id: int,
    project: Optional[str] = None,
    repo: Optional[str] = None,
    url: Optional[str] = None,
) -> Optional[dict]:
    """Детальный разбор сборки PR: статус из Bitbucket + причина падения из Jenkins
    (упавшие тест-кейсы, хвост консоли). По умолчанию берётся упавшая сборка по
    head-коммиту; url — конкретная сборка Jenkins. None — если сборок по коммиту нет.

    Не зависит от Jira. Без Jenkins-токена деградирует до статуса из Bitbucket.
    """
    svc = _builds_svc()
    report = svc.build_report(project, repo, pr_id, build_url=url)
    return report.model_dump() if report is not None else None


@mcp.tool()
async def jwu_attachments(
    key: str,
    download: bool = False,
    kinds: Optional[list[str]] = None,
    dest: Optional[str] = None,
) -> dict:
    """Вложения задачи: список с видами и счётчиками; с download=True — скачать в tmp.

    kinds — какие виды качать (image|log|doc|archive); по умолчанию все, кроме видео
    (видео никогда не качается). dest — каталог (по умолчанию <tmp>/jwu/<KEY>). При
    download возвращает локальные пути в `downloaded[].path` — их потом читать через Read.
    """
    svc = _full_svc()
    issue = svc.issue(key)
    atts = issue.attachments
    counts: dict[str, int] = {}
    for a in atts:
        counts[a.kind] = counts.get(a.kind, 0) + 1
    payload: dict = {
        "key": key,
        "counts": counts,
        "attachments": [a.model_dump() for a in atts],
    }
    if download:
        dest_dir = Path(dest) if dest else svc.attachments_dir(key)
        downloaded = svc.download_attachments(key, kinds=kinds or None, dest=dest_dir, issue=issue)
        payload["dest"] = str(dest_dir)
        payload["downloaded"] = [
            {"filename": att.filename, "kind": att.kind, "path": str(p)}
            for att, p in downloaded
        ]
    return payload


@mcp.tool()
async def jwu_jobs(
    task: Optional[str] = None,
    pr: Optional[int] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """Список работ (jobs) из локальной памяти — по задаче / PR / статусу. Без сети."""
    store = _store_only()
    return [j.model_dump() for j in store.list_jobs(task_key=task, pr_id=pr, status=status)]


# --------------------------------------------------------------------------- #
# Инструменты (write). Локальная память — кроме jwu_worklog (внешняя запись в Jira).
# --------------------------------------------------------------------------- #


@mcp.tool()
async def jwu_note(key: str, text: str) -> dict:
    """Записать заметку по задаче в локальную память jwu (в Jira не постит)."""
    return _store_only().add_note(key, text).model_dump()


@mcp.tool()
async def jwu_job_start(task_key: str, title: str = "") -> dict:
    """Начать НОВУЮ работу (job) по задаче. Возвращает созданную работу + список уже
    существующих работ по этой задаче (их не продолжаем — всегда новый цикл = новая работа)."""
    store = _store_only()
    existing = store.jobs_for_task(task_key)
    job = store.create_job(task_key, title)
    payload = job.model_dump()
    payload["existing_jobs"] = [
        {"id": j.id, "status": j.status, "title": j.title} for j in existing
    ]
    return payload


@mcp.tool()
async def jwu_job_add(
    job_id: int,
    text: str,
    kind: str = "note",
    status: Optional[str] = None,
) -> dict:
    """Добавить запись в работу (фаза/пункт/замечание/баг/прогон тестов и т.п.).

    kind — один из: phase, note, decision, remark, constraint, warning, bug,
    bug-resolved, test-pass, test-fail, todo, review. status — опц. (напр. "done" у фаз).
    """
    if kind not in JOB_RECORD_KINDS:
        raise ValueError(f"Недопустимый kind {kind!r}. Допустимо: {', '.join(JOB_RECORD_KINDS)}")
    store = _store_only()
    if store.get_job(job_id) is None:
        raise ValueError(f"Работа #{job_id} не найдена")
    return store.add_job_record(job_id, text, kind=kind, status=status).model_dump()


@mcp.tool()
async def jwu_job_link(job_id: int, pr: int, project: str = "", repo: str = "") -> dict:
    """Привязать PR к работе (project — ключ проекта Bitbucket, repo — slug)."""
    store = _store_only()
    if store.get_job(job_id) is None:
        raise ValueError(f"Работа #{job_id} не найдена")
    store.link_job_pr(job_id, pr, project=project, repo=repo)
    return {"ok": True, "job_id": job_id, "pr": pr, "project": project, "repo": repo}


@mcp.tool()
async def jwu_job_status(job_id: int, status: str) -> dict:
    """Сменить статус работы: active | done | paused | cancelled."""
    if status not in _JOB_STATUSES:
        raise ValueError(f"Недопустимый статус {status!r}. Допустимо: {', '.join(_JOB_STATUSES)}")
    store = _store_only()
    if store.get_job(job_id) is None:
        raise ValueError(f"Работа #{job_id} не найдена")
    store.set_job_status(job_id, status)
    return {"ok": True, "job_id": job_id, "status": status}


@mcp.tool()
async def jwu_worklog(
    key: str,
    time: str,
    comment: Optional[str] = None,
    started: Optional[str] = None,
) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: залогировать время в таймтрекер Jira/SDESK (worklog).

    time — в формате Jira: «2h 30m», «45m», «1d 4h». started — ISO 8601 (по умолчанию
    текущий момент). Инстанс выбирается по префиксу ключа. Вызывать ТОЛЬКО по явному
    подтверждению пользователя — это реальная запись в Jira, не локальная память.
    """
    result = _full_svc().add_worklog(key, time, comment=comment, started=started)
    return {"ok": True, "key": key, "timeSpent": time, "worklog": result}


def main() -> None:
    """Точка входа: запустить MCP-сервер по stdio.

    Логи httpx приглушаем до WARNING — иначе каждый HTTP-запрос сыплется в stderr
    (сам протокол в stdout это не ломает, но шумит в логах сервера)."""
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
