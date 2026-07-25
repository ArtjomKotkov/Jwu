"""MCP-сервер jwu: инструменты поверх сервисного слоя.

Даёт Claude Code структурный доступ к тем же данным/действиям, что и CLI
(`jwu task/pr/job/...`), но без похода через shell: типизированные вызовы, JSON-ответы
и — главное — ПЕРЕИСПОЛЬЗУЕМАЯ сессия. Сервис создаётся лениво и живёт на процесс,
поэтому вход в Jira/SDESK (в т.ч. сессионный за гейтом) выполняется один раз.

Read: workspaces, workspace_current, workspace_paths, workspace_suggest, task, prs, pr, builds, build, attachments, jobs,
features. Write: workspace_create, workspace_add_path, workspace_remove_path,
workspace_tag, workspace_use, note, worklog, job_start, job_add, job_link, job_status, feature_add,
feature_status. Почти все записи — локальные (память работ/заметок/фич); ВНЕШНЯЯ запись
только у `jwu_worklog` (таймтрекер Jira/SDESK) — вызывать по явному подтверждению.

Всё работает в контексте ВОРКСПЕЙСА. По умолчанию он определяется по рабочей папке
процесса сервера (в Claude Code это корень проекта); любой инструмент принимает
`workspace=` для явного выбора. Сервисы кэшируются ПО воркспейсам, поэтому логин в
Jira/SDESK всё так же выполняется один раз на воркспейс.

Транспорт — stdio (запуск как подпроцесс Claude Code). Инструменты объявлены async,
чтобы все обращения к клиентам и SQLite-хранилищу шли из одного потока event-loop
(sqlite-соединение Store привязано к своему потоку — check_same_thread).
"""

from __future__ import annotations

import atexit
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from importlib.metadata import PackageNotFoundError, version as _pkg_version

from . import __version__
from .core import workspaces as ws_mod
from .core.config import db_path
from .core.maintenance import ensure_db_available
from .core.models import JOB_RECORD_KINDS, LOCAL_FEATURE_STATUSES, Workspace
from .core.service import Service
from .core.store import Store

# Допустимые статусы работы (как в CLI `jwu job status`).
_JOB_STATUSES = ("active", "done", "paused", "cancelled")

mcp = FastMCP("jwu")

# Ленивые кэши ПО воркспейсам — тут и живёт переиспользуемая сессия/соединения.
_full: dict[int, Service] = {}    # полный сервис (Jira/SDESK + Bitbucket + Jenkins)
_builds: dict[int, Service] = {}  # лёгкий сервис для сборок (Bitbucket + Jenkins, без Jira)
_stores: dict[int, Store] = {}    # только память (работы/заметки/фичи), без сети
_base_store: Store | None = None  # без скоупа: нужен, чтобы вообще найти воркспейс


def _ensure_db() -> None:
    ensure_db_available(db_path())


def _registry() -> Store:
    """Store без скоупа — реестр воркспейсов (по нему же идёт резолв)."""
    global _base_store
    if _base_store is None:
        _ensure_db()
        _base_store = Store(str(db_path()))
    return _base_store


def _resolve(workspace: Optional[str] = None) -> Workspace:
    """Воркспейс вызова: явный параметр → JWU_WORKSPACE → папка процесса → активный."""
    store = _registry()
    ws_mod.migrate_legacy_config(store)  # однократный доезд старого конфига, как в CLI
    return ws_mod.resolve_workspace(store, explicit=workspace)


def _full_svc(workspace: Optional[str] = None) -> Service:
    """Полный сервис воркспейса с логином в Jira/SDESK (создаётся один раз на воркспейс)."""
    ws = _resolve(workspace)
    svc = _full.get(ws.id)
    if svc is None:
        svc = _full[ws.id] = Service.for_workspace(ws)
    return svc


def _builds_svc(workspace: Optional[str] = None) -> Service:
    """Сервис для сборок: Bitbucket + Jenkins, без зависимости от Jira (как CLI build/builds)."""
    ws = _resolve(workspace)
    svc = _builds.get(ws.id)
    if svc is None:
        svc = _builds[ws.id] = Service.builds_for_workspace(ws)
    return svc


def _store_only(workspace: Optional[str] = None) -> Store:
    """Только локальная память воркспейса — без сети и токенов (как CLI `jobs`)."""
    ws = _resolve(workspace)
    store = _stores.get(ws.id)
    if store is None:
        store = _stores[ws.id] = Store(str(db_path()), ws.id)
    return store


def _version() -> str:
    """Версия ИСПОЛНЯЕМОГО кода — из ``jwu.__version__``, а не из метаданных пакета.

    Метаданные при editable-установке остаются от момента установки и врут: сервер
    может крутить свежий код, отчитываясь старой версией (и наоборот). Сервер живёт
    долго, поэтому расхождение с установленной версией — главный признак того, что
    сессию пора перезапустить.
    """
    return __version__


def _installed_version() -> str:
    """Версия по метаданным пакета — для контраста с версией кода."""
    try:
        return _pkg_version("jwu")
    except PackageNotFoundError:  # запуск из исходников
        return "dev"


def _stamp(payload: dict, workspace: Workspace) -> dict:
    """Пометить ответ воркспейсом: запись не должна молча уехать не в тот контур."""
    payload["workspace"] = workspace.slug
    return payload


def _require_jira(svc: Service) -> Service:
    if svc.jira is None:
        slug = svc.workspace.slug if svc.workspace else "?"
        raise ValueError(
            f"В воркспейсе «{slug}» Jira не подключена. Используй jwu_features / jwu_jobs, "
            f"либо подключи Jira: jwu configure -W {slug} --jira-host …"
        )
    return svc


@atexit.register
def _cleanup() -> None:
    for svc in list(_full.values()) + list(_builds.values()):
        try:
            svc.close()
        except Exception:  # noqa: BLE001 — гасим на завершении процесса
            pass
    for store in list(_stores.values()) + ([_base_store] if _base_store else []):
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Инструменты (read-only)
# --------------------------------------------------------------------------- #


def _workspace_payload(store: Store, ws: Workspace) -> dict:
    store.use_workspace(ws.id)
    jobs = store.list_jobs()
    return {
        "id": ws.id,
        "slug": ws.slug,
        "name": ws.name,
        "jira_enabled": ws.jira_enabled,
        "bitbucket_enabled": ws.bitbucket_enabled,
        "paths": [p.path for p in ws.paths],
        "jobs": len(jobs),
        "jobs_active": len([j for j in jobs if j.status == "active"]),
        "features": len(store.list_features()),
    }


@mcp.tool()
async def jwu_workspaces() -> list[dict]:
    """Все воркспейсы jwu: slug, подключённые интеграции, папки и счётчики.

    Воркспейс — контур работы: свои папки, свои интеграции и свои локальные данные.
    """
    store = _registry()
    items = store.list_workspaces()
    payload = [_workspace_payload(store, ws) for ws in items]
    store.use_workspace(store._default_workspace_id())
    return payload


@mcp.tool()
async def jwu_workspace_current() -> dict:
    """Какой воркспейс сейчас активен и ПОЧЕМУ (source: explicit|env|cwd|active|only).

    Вызывай первым, когда неясен контекст: по `jira_enabled` видно, можно ли вообще
    звать Jira-инструменты (jwu_task/jwu_attachments/jwu_worklog). Если Jira нет —
    работай с локальными фичами (jwu_features) и работами (jwu_jobs).
    """
    store = _registry()
    ws_mod.migrate_legacy_config(store)
    try:
        res = ws_mod.resolve(store)
    except ws_mod.WorkspaceNotSelected as exc:
        return {"workspace": None, "error": str(exc), "cwd": str(Path.cwd())}
    payload = _workspace_payload(store, res.workspace)
    payload.update({"source": res.source, "matched_path": res.matched_path,
                    "cwd": str(Path.cwd()), "jwu_version": _version(),
                    "installed_version": _installed_version()})
    return payload


@mcp.tool()
async def jwu_features(
    status: Optional[str] = None,
    workspace: Optional[str] = None,
) -> list[dict]:
    """Локальные фичи воркспейса — мини-трекер для контуров без Jira.

    Фича играет роль карточки задачи: её ключ (напр. HOMEJWU-1) служит якорем работы
    и префиксом ветки/коммита. status — open | in_progress | review | done | cancelled.
    """
    if status is not None and status not in LOCAL_FEATURE_STATUSES:
        raise ValueError(f"Недопустимый статус {status!r}. "
                         f"Допустимо: {', '.join(LOCAL_FEATURE_STATUSES)}")
    store = _store_only(workspace)
    return [f.model_dump() for f in store.list_features(status=status)]


@mcp.tool()
async def jwu_task(key: str, workspace: Optional[str] = None) -> dict:
    """Полная карточка задачи Jira/SDESK по ключу (напр. WMCTASKS-123 или SDESK-39336).

    Инстанс выбирается по префиксу ключа автоматически. Возвращает поля задачи,
    описание, все комментарии, links, dev-панель (ветки/PR), плюс локальные заметки
    (`notes`), связанные работы (`jobs`) и статусы CI-сборок по OPEN-PR (`pr_builds`).

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _require_jira(_full_svc(workspace))
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
async def jwu_prs(view: str = "review", with_conflicts: bool = True,
                  workspace: Optional[str] = None) -> list[dict]:
    """Список PR из Bitbucket по роли: view = "review" (ждут моего ревью) или "mine" (мои).

    with_conflicts=True добавляет статус merge-конфликта по каждому PR (чуть медленнее).

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _full_svc(workspace)
    return [p.model_dump() for p in svc.prs(view, with_conflicts=with_conflicts)]


@mcp.tool()
async def jwu_pr(
    pr_id: int,
    project: Optional[str] = None,
    repo: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Детали одного PR: статус конфликта, ревьюеры, все комментарии ревью (с file:line
    и вложенностью), коммиты, статусы сборок и связанные работы.

    project/repo — ключ проекта и slug репозитория Bitbucket; если не заданы, берутся
    дефолтные из конфига. Если у PR NEEDS_WORK, а комментарии пусты — укажи project/repo.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _full_svc(workspace)
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
    workspace: Optional[str] = None,
) -> list[dict]:
    """Статусы CI-сборок по head-коммиту PR (быстро, из build-status API Bitbucket).

    Не зависит от Jira. project/repo — по умолчанию из конфига воркспейса.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _builds_svc(workspace)
    proj = project or svc.cfg.bitbucket.project
    rp = repo or svc.cfg.bitbucket.repo
    return [b.model_dump() for b in svc.build_statuses_for_pr(proj, rp, pr_id)]


@mcp.tool()
async def jwu_build(
    pr_id: int,
    project: Optional[str] = None,
    repo: Optional[str] = None,
    url: Optional[str] = None,
    workspace: Optional[str] = None,
) -> Optional[dict]:
    """Детальный разбор сборки PR: статус из Bitbucket + причина падения из Jenkins
    (упавшие тест-кейсы, хвост консоли). По умолчанию берётся упавшая сборка по
    head-коммиту; url — конкретная сборка Jenkins. None — если сборок по коммиту нет.

    Не зависит от Jira. Без Jenkins-токена деградирует до статуса из Bitbucket.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _builds_svc(workspace)
    report = svc.build_report(project, repo, pr_id, build_url=url)
    return report.model_dump() if report is not None else None


@mcp.tool()
async def jwu_attachments(
    key: str,
    download: bool = False,
    kinds: Optional[list[str]] = None,
    dest: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Вложения задачи: список с видами и счётчиками; с download=True — скачать в tmp.

    kinds — какие виды качать (image|log|doc|archive); по умолчанию все, кроме видео
    (видео никогда не качается). dest — каталог (по умолчанию <tmp>/jwu/<KEY>). При
    download возвращает локальные пути в `downloaded[].path` — их потом читать через Read.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _require_jira(_full_svc(workspace))
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
    feature: Optional[str] = None,
    workspace: Optional[str] = None,
) -> list[dict]:
    """Список работ (jobs) воркспейса — по задаче / PR / статусу / локальной фиче. Без сети.

    feature — id или ключ локальной фичи (HOMEJWU-1); нужен там, где Jira не подключена."""
    store = _store_only(workspace)
    feature_id = None
    if feature:
        row = store.get_feature(feature)
        if row is None:
            raise ValueError(f"Фича {feature!r} не найдена в этом воркспейсе")
        feature_id = row.id
    return [j.model_dump() for j in store.list_jobs(
        task_key=task, pr_id=pr, status=status, feature_id=feature_id)]


# --------------------------------------------------------------------------- #
# Инструменты (write). Локальная память — кроме jwu_worklog (внешняя запись в Jira).
# --------------------------------------------------------------------------- #


@mcp.tool()
async def jwu_workspace_suggest(path: Optional[str] = None) -> dict:
    """ЧТО стоит завести для папки: имя воркспейса, какие репозитории привязать, какие теги.

    Ничего не создаёт — только предложение, показать его пользователю и спросить.
    Если папка уже привязана, вернёт `already_bound` и текущий воркспейс: тогда заводить
    новый не надо, максимум добавить папки (jwu_workspace_add_path) или теги.
    Применять — jwu_workspace_create + jwu_workspace_add_path.
    """
    store = _registry()
    sg = ws_mod.suggest(store, path)
    return {
        "path": sg.path,
        "already_bound": sg.already_bound,
        "workspace": sg.workspace.slug if sg.workspace else None,
        "suggested": {
            "slug": sg.slug,
            "name": sg.name,
            "paths": sg.suggested_paths,
            "tags": sg.suggested_tags,
        },
        "repos": [{"root": r.root, "name": r.name, "branch": r.branch} for r in sg.repos],
        "jwu_version": _version(),
    }


@mcp.tool()
async def jwu_workspace_create(
    slug: str,
    name: str = "",
    jira: bool = False,
    bitbucket: bool = False,
    paths: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
    use: bool = False,
) -> dict:
    """Создать воркспейс — контур со своими папками, интеграциями и данными.

    slug — короткое имя латиницей (home-jwu, work). paths — папки, которые сразу
    привязать: по ним jwu потом определяет воркспейс автоматически. Интеграции
    объявляются ЯВНО (jira/bitbucket): по умолчанию их нет и креды не нужны вовсе —
    подключить потом можно командой `jwu configure -W <slug>`.
    use=True делает воркспейс активным по умолчанию для команд вне привязанных папок.
    """
    store = _registry()
    try:
        ws = ws_mod.create(store, slug, name=name, jira=jira, bitbucket=bitbucket,
                           paths=list(paths or []), tags=list(tags or []))
    except ws_mod.WorkspaceError as exc:
        raise ValueError(str(exc))
    if use:
        ws_mod.set_active(store, ws)
    return _workspace_payload(store, ws)


@mcp.tool()
async def jwu_workspace_add_path(
    path: str,
    label: str = "",
    tags: Optional[list[str]] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Привязать папку к воркспейсу — по ней он и будет определяться автоматически.

    Путь приводится к абсолютному (симлинки резолвятся). Одна папка принадлежит ровно
    одному воркспейсу; у вложенных папок выигрывает самая глубокая привязка.
    Несуществующая папка — не ошибка (может быть на внешнем диске), придёт `warning`.
    tags — метки для поиска («legacy-бэкенд», «новая-версия»), см. jwu_workspace_paths.
    """
    store = _registry()
    ws = _resolve(workspace)
    try:
        norm, warning = ws_mod.add_path(store, ws, path, label, tags=list(tags or []))
    except ws_mod.WorkspaceError as exc:
        raise ValueError(str(exc))
    payload = _workspace_payload(store, store.get_workspace(ws.id) or ws)
    payload.update({"added_path": norm, "warning": warning})
    return payload


@mcp.tool()
async def jwu_workspace_remove_path(
    path: str,
    workspace: Optional[str] = None,
) -> dict:
    """Отвязать папку от воркспейса (сама папка на диске не трогается)."""
    store = _registry()
    ws = _resolve(workspace)
    norm = ws_mod.normalize_path(path)
    removed = store.remove_workspace_path(norm, ws.id)
    if not removed:
        raise ValueError(f"Папка {norm} не привязана к воркспейсу «{ws.slug}»")
    payload = _workspace_payload(store, store.get_workspace(ws.id) or ws)
    payload["removed_path"] = norm
    return payload


@mcp.tool()
async def jwu_workspace_paths(
    tag: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """ГДЕ ЛЕЖИТ КОД воркспейса: его папки с тегами (и список известных тегов).

    Основной код проекта находится ИМЕННО в этих папках — ищи и правь в них, а не
    где придётся. tag — фильтр: «legacy-бэкенд», «новая-версия», «фронт» и т.п.
    """
    store = _registry()
    ws = _resolve(workspace)
    rows = store.workspace_paths(ws.id, tag=tag)
    return {
        "workspace": ws.slug,
        "tag": tag,
        "paths": [p.model_dump() for p in rows],
        "known_tags": store.all_tags(ws.id),
    }


@mcp.tool()
async def jwu_workspace_tag(
    path: str,
    add: Optional[list[str]] = None,
    remove: Optional[list[str]] = None,
    replace: Optional[list[str]] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Проставить папке теги — чтобы потом было понятно, где что лежит.

    add — добавить, remove — убрать, replace — заменить весь набор. Теги произвольные
    («legacy-бэкенд», «новая-версия»); по ним ищет jwu_workspace_paths(tag=…).
    Папка должна быть уже привязана (jwu_workspace_add_path).
    """
    store = _registry()
    ws = _resolve(workspace)
    norm = ws_mod.normalize_path(path)
    row = next((p for p in store.workspace_paths(ws.id) if p.path == norm), None)
    if row is None:
        raise ValueError(
            f"Папка {norm} не привязана к воркспейсу «{ws.slug}» — сначала jwu_workspace_add_path"
        )
    if replace is not None:
        tags = store.set_path_tags(row.id, list(replace))
    else:
        if add:
            store.add_path_tags(row.id, list(add))
        tags = (store.remove_path_tags(row.id, list(remove)) if remove
                else store._path_tags([row.id]).get(row.id, []))
    return {"path": norm, "tags": tags, "workspace": ws.slug}


@mcp.tool()
async def jwu_workspace_use(workspace: str) -> dict:
    """Сделать воркспейс активным по умолчанию.

    ПОБОЧНЫЙ ЭФФЕКТ: это меняет выбор и для CLI пользователя, и для дашборда — там, где
    папка не привязана ни к одному воркспейсу. Если нужно просто выполнить одну операцию
    в другом контуре, не переключай активный, а передай `workspace=` в нужный инструмент.
    """
    store = _registry()
    ws = ws_mod.find_workspace(store, workspace)
    if ws is None:
        known = ", ".join(w.slug for w in store.list_workspaces()) or "—"
        raise ValueError(f"Воркспейс {workspace!r} не найден. Известные: {known}")
    ws_mod.set_active(store, ws)
    return _workspace_payload(store, ws)


@mcp.tool()
async def jwu_feature_add(
    title: str,
    description: str = "",
    priority: str = "",
    workspace: Optional[str] = None,
) -> dict:
    """Завести локальную фичу — якорь работы там, где Jira не подключена.

    Ключ (напр. HOMEJWU-1) генерируется автоматически и годится как префикс ветки.
    """
    ws = _resolve(workspace)
    return _stamp(_store_only(workspace).create_feature(
        title, description=description, priority=priority).model_dump(), ws)


@mcp.tool()
async def jwu_feature_status(
    feature: str,
    status: str,
    workspace: Optional[str] = None,
) -> dict:
    """Сменить статус локальной фичи: open | in_progress | review | done | cancelled.

    feature — id или ключ (HOMEJWU-1).
    """
    if status not in LOCAL_FEATURE_STATUSES:
        raise ValueError(f"Недопустимый статус {status!r}. "
                         f"Допустимо: {', '.join(LOCAL_FEATURE_STATUSES)}")
    store = _store_only(workspace)
    row = store.get_feature(feature)
    if row is None:
        raise ValueError(f"Фича {feature!r} не найдена в этом воркспейсе")
    store.update_feature(row.id, status=status)
    return {"ok": True, "key": row.key, "status": status}


@mcp.tool()
async def jwu_note(key: str, text: str, workspace: Optional[str] = None) -> dict:
    """Записать заметку по задаче в локальную память воркспейса (в Jira не постит)."""
    ws = _resolve(workspace)
    return _stamp(_store_only(workspace).add_note(key, text).model_dump(), ws)


@mcp.tool()
async def jwu_job_start(
    task_key: str = "",
    title: str = "",
    feature: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Начать НОВУЮ работу (job). Возвращает созданную работу + список уже существующих
    работ по тому же якорю (их не продолжаем — всегда новый цикл = новая работа).

    Якорь: ключ задачи Jira (task_key) ЛИБО локальная фича (feature — id или ключ).
    Без обоих работа заводится без якоря — тогда обязателен title."""
    store = _store_only(workspace)
    if task_key and feature:
        raise ValueError("Нужно что-то одно: task_key ИЛИ feature")
    feature_row = None
    if feature:
        feature_row = store.get_feature(feature)
        if feature_row is None:
            raise ValueError(f"Фича {feature!r} не найдена в этом воркспейсе")
    if not task_key and not feature_row and not title:
        raise ValueError("Работе без якоря нужен title, иначе её не опознать")
    existing = (store.jobs_for_task(task_key) if task_key
                else store.list_jobs(feature_id=feature_row.id) if feature_row else [])
    job = store.create_job(task_key, title,
                           feature_id=feature_row.id if feature_row else None)
    payload = job.model_dump()
    payload["existing_jobs"] = [
        {"id": j.id, "status": j.status, "title": j.title} for j in existing
    ]
    return _stamp(payload, _resolve(workspace))


@mcp.tool()
async def jwu_job_add(
    job_id: int,
    text: str,
    kind: str = "note",
    status: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Добавить запись в работу (фаза/пункт/замечание/баг/прогон тестов и т.п.).

    kind — один из: phase, note, decision, remark, constraint, warning, bug,
    bug-resolved, test-pass, test-fail, todo, review. status — опц. (напр. "done" у фаз).

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    if kind not in JOB_RECORD_KINDS:
        raise ValueError(f"Недопустимый kind {kind!r}. Допустимо: {', '.join(JOB_RECORD_KINDS)}")
    store = _store_only(workspace)
    if store.get_job(job_id) is None:
        raise ValueError(f"Работа #{job_id} не найдена")
    return _stamp(store.add_job_record(job_id, text, kind=kind, status=status).model_dump(),
                  _resolve(workspace))


@mcp.tool()
async def jwu_job_link(job_id: int, pr: int, project: str = "", repo: str = "",
                       workspace: Optional[str] = None) -> dict:
    """Привязать PR к работе (project — ключ проекта Bitbucket, repo — slug)."""
    store = _store_only(workspace)
    if store.get_job(job_id) is None:
        raise ValueError(f"Работа #{job_id} не найдена")
    store.link_job_pr(job_id, pr, project=project, repo=repo)
    return _stamp({"ok": True, "job_id": job_id, "pr": pr, "project": project, "repo": repo},
                  _resolve(workspace))


@mcp.tool()
async def jwu_job_status(job_id: int, status: str, workspace: Optional[str] = None) -> dict:
    """Сменить статус работы: active | done | paused | cancelled."""
    if status not in _JOB_STATUSES:
        raise ValueError(f"Недопустимый статус {status!r}. Допустимо: {', '.join(_JOB_STATUSES)}")
    store = _store_only(workspace)
    if store.get_job(job_id) is None:
        raise ValueError(f"Работа #{job_id} не найдена")
    store.set_job_status(job_id, status)
    return _stamp({"ok": True, "job_id": job_id, "status": status}, _resolve(workspace))


@mcp.tool()
async def jwu_worklog(
    key: str,
    time: str,
    comment: Optional[str] = None,
    started: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: залогировать время в таймтрекер Jira/SDESK (worklog).

    time — в формате Jira: «2h 30m», «45m», «1d 4h». started — ISO 8601 (по умолчанию
    текущий момент). Инстанс выбирается по префиксу ключа. Вызывать ТОЛЬКО по явному
    подтверждению пользователя — это реальная запись в Jira, не локальная память.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _require_jira(_full_svc(workspace))
    result = svc.add_worklog(key, time, comment=comment, started=started)
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
