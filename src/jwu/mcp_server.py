"""MCP-сервер jwu: инструменты поверх сервисного слоя.

Даёт Claude Code структурный доступ к тем же данным/действиям, что и CLI
(`jwu task/pr/job/...`), но без похода через shell: типизированные вызовы, JSON-ответы
и — главное — ПЕРЕИСПОЛЬЗУЕМАЯ сессия. Сервис создаётся лениво и живёт на процесс,
поэтому вход в Jira/SDESK (в т.ч. сессионный за гейтом) выполняется один раз.

Read: workspaces, workspace_current, workspace_paths, workspace_suggest, rules, tasks, task, changes,
prs, pr, builds, build, attachments, jobs, features, worklogs, task_branches, issue_create_meta,
issue_similar, issue_transitions, auth_refresh. Write: workspace_create, workspace_add_path, workspace_remove_path,
workspace_tag, workspace_use, rule_add, rule_edit, rule_rm, note, worklog, comment, issue_create,
issue_link, issue_transition, issue_attach, job_start, job_add,
job_link, job_status, feature_add, feature_status, feature_edit, feature_rm, queue_add, queue_take,
queue_finish, queue_edit, queue_clear, queue_slots (очередь задач сессии; чтение — queue). Почти все записи — локальные (память работ/заметок/фич); ВНЕШНЯЯ запись
у `jwu_worklog` (таймтрекер), `jwu_comment` (комментарий — в SDESK его читает КЛИЕНТ),
`jwu_issue_create`, `jwu_issue_link`, `jwu_issue_transition`, `jwu_issue_attach`,
`jwu_pr_task_add` и `jwu_pr_task_done` (задачи на комментах PR в Bitbucket),
`jwu_pr_comment` (комментарий/ответ в PR), `jwu_pr_review` (мой статус ревью) и
`jwu_pr_create` (создание PR) — вызывать только по явному подтверждению пользователя. У пишущих инструментов с
`dry_run` порядок обязателен: сначала превью, показать его пользователю, потом запись.

Всё работает в контексте ВОРКСПЕЙСА. По умолчанию он определяется по рабочей папке
процесса сервера (в Claude Code это корень проекта); любой инструмент принимает
`workspace=` для явного выбора. Сервисы кэшируются ПО воркспейсам, поэтому логин в
Jira/SDESK всё так же выполняется один раз на воркспейс.

Соединения живут вместе с процессом: протухшую СЕССИЮ Jira клиент переустанавливает
сам (см. JiraClient._send), а сменившиеся креды подхватываются только через
`jwu_auth_refresh` — после 401 зови его и повторяй вызов.

Транспорт — stdio (запуск как подпроцесс Claude Code). Инструменты объявлены async,
чтобы все обращения к клиентам и SQLite-хранилищу шли из одного потока event-loop
(sqlite-соединение Store привязано к своему потоку — check_same_thread).
"""

from __future__ import annotations

import asyncio
import atexit
import os
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from importlib.metadata import PackageNotFoundError, version as _pkg_version

from . import __version__
from .core import gitinfo, handoff
from .core import workspaces as ws_mod
from .core.config import db_path
from .core.maintenance import ensure_db_available
from .core.models import (
    JOB_RECORD_KINDS, LOCAL_FEATURE_STATUSES, QUEUE_MODES, QUEUE_STATUSES, WORKSPACE_RULE_KINDS,
    Workspace,
)
from .core.service import Service
from .core.store import QueueBusy, Store

# Допустимые статусы работы (как в CLI `jwu job status`).
_JOB_STATUSES = ("active", "done", "paused", "cancelled")

mcp = FastMCP("jwu", instructions=(
    "Когда нужен ответ пользователя, чтобы идти дальше, задавай вопрос интерактивным меню "
    "(инструмент AskUserQuestion), а не текстом в конце ответа. Это и подтверждение «да / нет» "
    "перед записью наружу, коммитом или пушем, и выбор следующего шага после разбора: «разобрался — "
    "делаем по плану, от какой базы, или иначе?». Варианты — 2–4 конкретных действия, рекомендуемый "
    "первым; так же — «закрыть работу?». Нет такого инструмента (Cursor, Codex) — обычным текстом. "
    "Пуш ветки работы — только jwu_job_push, не git push. Тесты, сборки и прочие долгие прогоны — "
    "в фоне с таймаутом и под наблюдением: 5 минут без нового вывода — зависание, останови и разбери."))

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
        try:   # сессия видна на доске с первого вызова jwu, а не только после /jwu-todo
            _session_name(store)
        except Exception:  # noqa: BLE001
            pass
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


def _version_warning() -> Optional[str]:
    """Сервер живёт на старом коде, а пакет уже обновлён: пора перезапустить сессию.

    Метаданные читаются с диска при каждом вызове, `__version__` — из памяти процесса.
    Расхождение появляется ровно после `pipx install --force` и исчезает после
    перезапуска Claude Code. Скилл jwu-update это видит и говорит пользователю.
    """
    running, installed = _version(), _installed_version()
    if installed in ("dev", running):
        return None
    return (f"MCP-сервер jwu работает на версии {running}, а установлена {installed}: "
            f"перезапусти сессию Claude Code, иначе новых инструментов не будет.")


def _with_version(payload: dict) -> dict:
    warning = _version_warning()
    if warning:
        payload["version_warning"] = warning
    return payload


def _job_summary(job) -> dict:
    """Работа без журнала: id, статус, заголовок, когда обновлена, последняя запись."""
    last = job.records[-1] if job.records else None
    return {
        "id": job.id, "status": job.status, "title": job.title, "task_key": job.task_key,
        "updated_at": job.updated_at, "records": len(job.records),
        "last_record": ({"kind": last.kind, "text": last.text[:300], "ts": last.ts,
                         "branch": last.branch, "commit": last.commit} if last else None),
    }


def _stamp(payload: dict, workspace: Workspace) -> dict:
    """Пометить ответ воркспейсом: запись не должна молча уехать не в тот контур."""
    payload["workspace"] = workspace.slug
    return payload


def _require_jira(svc: Service) -> Service:
    """Проверить, что у контура есть трекер задач (Jira или GitHub Issues)."""
    if svc.tasks_client is None:
        slug = svc.workspace.slug if svc.workspace else "?"
        raise ValueError(
            f"В воркспейсе «{slug}» нет провайдера задач (контур локальный). "
            f"Используй jwu_features / jwu_jobs, либо выбери провайдера: "
            f"jwu_workspace_provider(provider=\"jira\"|\"github\")"
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


def _workspace_payload(store: Store, ws: Workspace, *, with_context: bool = True) -> dict:
    """Карточка воркспейса. ``with_context=False`` — без правил и папок: в списке ВСЕХ
    контуров они лишние (на вопрос «какие есть воркспейсы» не должны приезжать правила
    каждого проекта), там хватает счётчиков."""
    store.use_workspace(ws.id)
    jobs = store.list_jobs()
    payload = {
        "id": ws.id,
        "slug": ws.slug,
        "name": ws.name,
        # provider — откуда контур берёт задачи и PR: local | jira | github.
        # Флаги ниже производные, оставлены для совместимости со старыми скиллами.
        "provider": ws.provider,
        "jira_enabled": ws.jira_enabled,
        "github_enabled": ws.github_enabled,
        "bitbucket_enabled": ws.bitbucket_enabled,
        "prs_enabled": ws.prs_enabled,
        "jobs": len(jobs),
        "jobs_active": len([j for j in jobs if j.status == "active"]),
        "features": len(store.list_features()),
        "rules": len(store.list_rules()),
    }
    if with_context:
        # Контекст проекта одним куском: папки с тегами (где что лежит) и правила
        # текстом (как тут принято). Общие правила целиком, у остальных — только
        # заголовки; полный текст правил своего репозитория — jwu_rules(tag=…).
        payload.update(store.workspace_context())
    return payload


@mcp.tool()
async def jwu_workspaces() -> list[dict]:
    """Все воркспейсы jwu: slug, подключённые интеграции, папки и счётчики.

    Воркспейс — контур работы: свои папки, свои интеграции и свои локальные данные.
    """
    store = _registry()
    items = store.list_workspaces()
    payload = [_workspace_payload(store, ws, with_context=False) for ws in items]
    store.use_workspace(store._default_workspace_id())
    return payload


@mcp.tool()
async def jwu_workspace_current() -> dict:
    """Какой воркспейс сейчас активен и ПОЧЕМУ (source: explicit|env|cwd|active|only).

    Вызывай первым, когда неясен контекст: по `provider` видно, откуда берутся задачи и
    PR. `jira`/`github` — работают jwu_task/jwu_prs/jwu_build (у GitHub ключ задачи
    выглядит как `repo#42`, таймтрекера нет). `local` — внешних задач нет, работай с
    фичами (jwu_features) и работами (jwu_jobs).
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
    return _with_version(payload)


@mcp.tool()
async def jwu_version() -> dict:
    """Версия кода, на котором работает MCP-сервер, и версия установленного пакета.

    Расходятся — пакет обновили, а сессия ещё на старом коде: перезапусти Claude Code
    (`version_warning` говорит то же самое). Без сети.
    """
    return _with_version({"running": _version(), "installed": _installed_version()})


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
async def jwu_tasks(
    view: str = "mine",
    jql: Optional[str] = None,
    workspace: Optional[str] = None,
) -> list[dict]:
    """Список задач Jira по вью или произвольному JQL — то же, что `jwu tasks`.

    ХОДИТ В СЕТЬ (в отличие от jwu_jobs/jwu_changes, которые читают память).
    view: mine — задачи на мне. jql перекрывает view.

    Про упоминания: `view="mentions"` идёт по СТАРОМУ пути (JQL + дотягивание карточки
    каждого кандидата) и отдаёт ЗАДАЧИ, а не записи упоминаний. Сами упоминания —
    отдельная сущность, она копится при синке и видна на вкладке «Упоминания» дашборда.
    """
    svc = _require_jira(_full_svc(workspace))
    return [i.model_dump() for i in svc.tasks(view, jql=jql)]


@mcp.tool()
async def jwu_changes(workspace: Optional[str] = None) -> list[dict]:
    """Накопленные изменения с прошлого синка — то же, что `jwu changes`. Без сети.

    Дельты копятся, пока их не закроют явно (в дашборде — `c` / `C`): новые комментарии,
    смена статуса, новые PR и коммиты, апрувы, merge-конфликты, исчезновение из выборки.
    Полезно как быстрый ответ на «что поменялось, пока меня не было».
    """
    return [d.model_dump() for d in _store_only(workspace).pending_changes()]


@mcp.tool()
async def jwu_day_context(brief: bool = True, as_json: bool = False,
                          workspace: Optional[str] = None) -> dict:
    """Контекст дневного анализа — то же, что `jwu action day-analyze`, но без shell:
    фулл-синк, затем markdown с промптом, дельтами, разделом «Застряло», задачами, PR
    (с состоянием, сборкой, задачами на комментах, status-заметками) и упоминаниями.

    brief=True — только требующее действия (рекомендуется). as_json=True — вдобавок
    структурированный `data`. Требует трекер задач в контуре. Синк занимает до минуты.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    from .core import daycontext

    svc = _require_jira(_full_svc(workspace))
    ctx = svc.collect_day_context(brief=brief)
    payload: dict = {"brief": brief, "markdown": daycontext.render_day_context_md(ctx)}
    if as_json:
        payload["data"] = daycontext.day_context_json(ctx)
    return payload


@mcp.tool()
async def jwu_mentions(unseen: bool = False, limit: int = 50, workspace: Optional[str] = None) -> list[dict]:
    """Упоминания меня в комментариях (из памяти последнего синка, без сети): задача,
    автор, текст, дата, seen. unseen=True — только непрочитанные.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    store = _store_only(workspace)
    items = store.unseen_mentions() if unseen else store.list_mentions(limit=limit)
    return [m.model_dump() for m in items[:limit]]


@mcp.tool()
async def jwu_mentions_seen(ids: Optional[list[int]] = None, workspace: Optional[str] = None) -> dict:
    """Пометить упоминания прочитанными (локальная запись): перечисленные ids или все
    (ids=None). Зови, когда пользователь разобрал упоминания в сводке.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    store = _store_only(workspace)
    store.mark_mentions_seen(None if ids is None else list(ids))
    return _stamp({"ok": True, "ids": ids}, _resolve(workspace))


@mcp.tool()
async def jwu_mentions_archive(older_than_days: int = 30, include_unseen: bool = False,
                               workspace: Optional[str] = None) -> dict:
    """Удалить из памяти упоминания старше N дней (локальная запись; по умолчанию только
    прочитанные).

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    store = _store_only(workspace)
    n = store.archive_mentions(older_than_days=older_than_days, seen_only=not include_unseen)
    return _stamp({"removed": n}, _resolve(workspace))


@mcp.tool()
async def jwu_thresholds(workspace: Optional[str] = None) -> dict:
    """Пороги «давно» контура (дни): stale_pr_days, approval_wait_days, review_wait_days,
    testing_days, mention_days. По ним day-analyze считает раздел «Застряло». Менять —
    `jwu workspace thresholds --stale-pr N …`.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    from .core import thresholds as th_mod

    store = _store_only(workspace)
    return th_mod.load(store, store.workspace_id).as_dict()


@mcp.tool()
async def jwu_review_queue(
    include_approved: bool = False,
    include_needs_work: bool = False,
    prs: Optional[list[int]] = None,
    repos: Optional[list[str]] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Очередь ревью для скилла jwu-review-queue: PR на моём ревью без тех, где мой статус
    уже APPROVED (include_approved=True — с ними), с фильтром по номерам `prs` и
    репозиториям `repos`. Ничего не пишет наружу.

    По каждому PR: project/repo/target, ключ задачи, мой статус, `reviewer_agent`
    (ревьювер репозитория из настроек, иначе reviewer-jwu-sample), файлы разделов
    `review_file` (вывод ревьювера) и `facts_file` (вывод фильтра). `dir`/`summary` —
    каталог сводки в данных jwu (вне репозиториев), `filter_agent` — агент фильтра
    замечаний, `skipped` — пропущенные PR с причиной.

    `needs_work` — PR, где мой статус уже NEEDS_WORK: в очередь сами не идут, их надо
    отдельно спросить у пользователя («проверять ли?»). У каждого — те же поля, что в
    очереди, плюс `my_review_at`, `new_commits` (коммитов после моего ревью; 0 — автор
    ещё не правил), `last_commit_at` (epoch ms). Выбранные пользователем добавь в очередь
    как есть. include_needs_work=True или явные `prs` — сразу в очередь, без вопроса.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    from .core import reviewq

    ws = _resolve(workspace)
    svc = _full_svc(workspace)
    items = svc.prs("review", with_conflicts=False, with_builds=False)
    result = reviewq.plan(svc.store, ws.id, ws.slug, items, svc._resolve_username(),
                          include_approved=include_approved, include_needs_work=include_needs_work,
                          ids=prs, repos=repos, client=svc.pr_client)
    Path(result["dir"]).mkdir(parents=True, exist_ok=True)
    return _stamp(result, ws)


@mcp.tool()
async def jwu_voice_profile(workspace: Optional[str] = None) -> dict:
    """Голос пользователя: какой субагент пишет внешние тексты от его имени, профиль стиля
    (markdown, правится пользователем) и сводка корпуса его реальных текстов.

    `analysis` — раздел «Анализ корпуса» в профиле: есть ли, когда сделан, по каким
    каналам (`channels`), устарел ли (`stale`). Пишут по профилю; примеры корпуса нужны,
    только если канала нет в анализе или пользователь сказал «не похоже на меня».

    Любой внешний текст от имени пользователя (коммент в PR, ответ ревьюверу, коммент в
    Jira/SDESK, текст задачи, 4test, коммит) пишет субагент `agent` — вызывающий скилл
    передаёт ему факты, а не сочиняет сам. Без сети.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    from .core import voice as voice_mod

    ws = _resolve(workspace)
    store = _store_only(workspace)
    return _stamp({"agent": voice_mod.agent_name(store, ws.id),
                   "profile_path": str(voice_mod.ensure_profile(ws.slug)),
                   "profile_md": voice_mod.read_profile(ws.slug),
                   "corpus": voice_mod.corpus_stats(ws.slug),
                   "analysis": voice_mod.analysis_info(ws.slug)}, ws)


@mcp.tool()
async def jwu_voice_analysis_save(markdown: str, channels: list[str],
                                  workspace: Optional[str] = None) -> dict:
    """Сохранить раздел «Анализ корпуса» в профиль голоса (заменяет прежний, ручные правки
    профиля не трогает). Локальная запись — ТОЛЬКО после того, как пользователь увидел
    анализ и сказал «да» (скилл jwu-voice-profile). channels — каналы, которые анализ
    покрывает (pr_comment, jira_comment, commit …).
    """
    from .core import voice as voice_mod

    ws = _resolve(workspace)
    path = voice_mod.save_analysis(ws.slug, markdown, channels=channels)
    return _stamp({"ok": True, "profile_path": str(path), "analysis": voice_mod.analysis_info(ws.slug)}, ws)


@mcp.tool()
async def jwu_voice_examples(channel: str, audience: Optional[str] = None, limit: int = 8,
                             workspace: Optional[str] = None) -> dict:
    """Примеры реальных текстов пользователя для канала — few-shot для агента голоса.

    channel — pr_comment | pr_reply | pr_review_body | pr_task | pr_description |
    jira_comment | sdesk_client | task | 4test | commit | message. audience — colleague |
    qa | analyst | client. Если в канале мало текстов, добираются соседние по жанру.
    Без сети; пустой корпус собирает `jwu voice collect` (или jwu_voice_collect).
    """
    from .core import voice as voice_mod

    ws = _resolve(workspace)
    return _stamp({"channel": channel,
                   "examples": voice_mod.examples(ws.slug, channel, audience=audience, limit=limit)}, ws)


@mcp.tool()
async def jwu_voice_feedback(channel: str, verdict: str, before: str, after: str = "",
                             reason: str = "", audience: str = "",
                             workspace: Optional[str] = None) -> dict:
    """Дописать в профиль голоса пару «было → стало» (verdict=edited), «отклонено, почему»
    (rejected) или удачный пример (accepted). Локальная запись — но ТОЛЬКО после того, как
    пользователь согласился записать эту правку в профиль.
    """
    from .core import voice as voice_mod

    ws = _resolve(workspace)
    path = voice_mod.add_feedback(ws.slug, channel=channel, verdict=verdict, before=before,
                                  after=after, reason=reason, audience=audience)
    return _stamp({"ok": True, "profile_path": str(path)}, ws)


@mcp.tool()
async def jwu_voice_collect(days: int = 180, workspace: Optional[str] = None) -> dict:
    """Собрать/обновить корпус голоса: мои комменты и описания в PR, мои комменты в
    Jira/SDESK, мои коммиты за `days` дней. Внешние системы только ЧИТАЮТСЯ; пишется
    локальный файл корпуса. Долго (десятки запросов) — звать по просьбе пользователя.
    Переписку с ассистентом в корпус не класть никогда.
    """
    from .core import voice as voice_mod

    ws = _resolve(workspace)
    svc = _full_svc(workspace)
    report = voice_mod.collect(svc, ws.slug, [p.path for p in ws.paths], days=days)
    return _stamp({"added": report.added, "total": report.total, "by_source": report.by_source,
                   "errors": report.errors[:20]}, ws)


@mcp.tool()
async def jwu_ssh_servers(workspace: Optional[str] = None) -> dict:
    """SSH-стенды воркспейса (без секретов) и как до них достучаться через ssh-mcp.

    Сам jwu по SSH не ходит: команды на стенде выполняют инструменты MCP-сервера
    `mcp_server` (префикс `tools_prefix`, напр. `mcp__ssh-<slug>__execute-command` с
    `connectionName=<имя стенда>`). Если таких инструментов в сессии нет — стенды не
    выданы: `jwu ssh install` и перезапуск сессии. `tag` стенда — тег папки воркспейса,
    к которой он относится; `policy=readonly` — разрешён только просмотр (whitelist/blacklist
    в ответе). Логи смотри в `allowed_remote_paths` и `description`. Добавлять стенды —
    `jwu ssh add` (секреты вводит пользователь сам, не проси их в чат).

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    from .core import ssh as ssh_mod

    ws = _resolve(workspace)
    store = _store_only(workspace)
    return _stamp(ssh_mod.describe(store, ws.id, ws.slug), ws)


@mcp.tool()
async def jwu_task(key: str, workspace: Optional[str] = None) -> dict:
    """Полная карточка задачи Jira/SDESK по ключу (напр. ACME-123 или SDESK-1234).

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
                  with_builds: bool = True, workspace: Optional[str] = None) -> list[dict]:
    """Список PR по роли: view = "review" (ждут моего ревью) или "mine" (мои).

    Источник — хостинг контура: Bitbucket у jira-контура, GitHub у github-контура.

    with_conflicts=True добавляет статус merge-конфликта по каждому PR (чуть медленнее).
    with_builds=True добавляет статусы CI-сборок по head-коммиту (`builds` и сводный
    `build_state`: FAILED | INPROGRESS | SUCCESSFUL | ""), ещё два запроса на PR.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _full_svc(workspace)
    return [p.model_dump() for p in svc.prs(view, with_conflicts=with_conflicts,
                                           with_builds=with_builds)]


@mcp.tool()
async def jwu_pr(
    pr_id: int,
    project: Optional[str] = None,
    repo: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Детали одного PR: статус конфликта, ревьюеры, все комментарии ревью (с file:line
    и вложенностью), коммиты, статусы сборок и связанные работы.

    project/repo — проект и репозиторий (у Bitbucket ключ проекта и slug, у GitHub owner
    и имя репозитория); если не заданы, берутся дефолтные из конфига. Если у PR
    NEEDS_WORK, а комментарии пусты — укажи project/repo.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _full_svc(workspace)
    detail = svc.pr_detail(project, repo, pr_id)
    jobs = svc.jobs_for_pr(pr_id, project or "", repo or "")
    payload = detail.pr.model_dump()
    payload["comments"] = [c.model_dump() for c in detail.comments]
    payload["commits"] = detail.commits
    # Работы — сводкой: полный журнал (сотни записей) раздувал ответ до 100 КБ.
    # Детали — jwu_jobs(pr=…) / jwu_job_handoff(job_id).
    payload["jobs"] = [_job_summary(j) for j in jobs]
    # Открытые задачи на комментах — чек-лист правок по PR, отдельным списком.
    payload["open_tasks"] = [
        t.model_dump() for c in detail.comments for t in c.tasks if not t.resolved
    ]
    # Файлы в описании и комментах (скриншоты): скачать — jwu_pr_attachments(download=True).
    payload["attachments"] = [a.model_dump() for a in detail.pr.attachments] + [
        a.model_dump() for c in detail.comments for a in c.attachments
    ]
    return payload


@mcp.tool()
async def jwu_pr_attachments(
    pr_id: int,
    download: bool = False,
    dest: Optional[str] = None,
    project: Optional[str] = None,
    repo: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Вложения PR Bitbucket: файлы, вставленные в описание и в комментарии ревью
    (скриншоты «как выглядит» / «что сломано»). У каждого — `comment_id` якоря (пусто —
    из описания), чтобы понимать, к какой реплике относится картинка.

    download=True — скачать в dest (по умолчанию <tmp>/jwu/pr/<project>-<repo>-<id>);
    локальные пути в `downloaded[].path` — их потом читать через Read. Только Bitbucket.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _full_svc(workspace)
    default_project, default_repo = svc.default_pr_ref()
    proj = project or default_project
    rp = repo or default_repo
    detail = svc.pr_detail(proj, rp, pr_id)
    items = list(detail.pr.attachments) + [a for c in detail.comments for a in c.attachments]
    payload: dict = {
        "pr": pr_id, "project": proj, "repo": rp,
        "attachments": [a.model_dump() for a in items],
    }
    if download:
        dest_dir = Path(dest) if dest else svc.pr_attachments_dir(proj, rp, pr_id)
        downloaded = svc.download_pr_attachments(proj, rp, pr_id, dest=dest_dir, detail=detail)
        payload["dest"] = str(dest_dir)
        payload["downloaded"] = [
            {"name": a.name, "kind": a.kind, "comment_id": a.comment_id, "path": str(p)}
            for a, p in downloaded
        ]
    return payload


@mcp.tool()
async def jwu_pr_diff(
    pr_id: int,
    path: Optional[str] = None,
    max_chars: int = 60000,
    numbered: bool = False,
    project: Optional[str] = None,
    repo: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Unified diff PR — ревью и ответы на замечания без локального клона репозитория.

    path — только один файл (так и бери большие PR: сначала список файлов из общего
    диффа по строкам `diff --git`, потом по файлам). max_chars — обрезать с пометкой.
    numbered=True — дифф с номерами строк слева («старая новая │ ±текст»): правый номер
    и есть `line` для inline-коммента в jwu_pr_comment. Работает у Bitbucket и GitHub.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _full_svc(workspace)
    text = svc.pr_diff(project, repo, pr_id, path=path, max_chars=max_chars)
    files = [ln.split(" b/", 1)[-1] for ln in text.splitlines() if ln.startswith("diff --git ")]
    if numbered:
        from .core import diffmap

        text = diffmap.numbered(text)
    return {"pr": pr_id, "path": path, "files": files, "diff": text}


@mcp.tool()
async def jwu_pr_comment(
    pr_id: int,
    text: str,
    reply_to: Optional[int] = None,
    path: Optional[str] = None,
    line: Optional[int] = None,
    line_type: Optional[str] = None,
    side: Optional[str] = None,
    project: Optional[str] = None,
    repo: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: комментарий в PR (Bitbucket/GitHub) — звать только после того, как
    пользователь увидел текст целиком и явно подтвердил (скилл jwu-pr-comment).

    reply_to — id комментария ревьювера, в тред которого отвечаем («поправил», «не
    согласен, потому что…»); path+line — новый inline-коммент на строку файла; без
    обоих — общий комментарий к PR.

    line — номер строки НОВОЙ версии файла (правый номер в jwu_pr_diff(numbered=True)).
    Тип строки (ADDED/CONTEXT/REMOVED) jwu берёт из диффа PR сам; строки нет в диффе —
    ошибка до отправки. Удалённую строку («-») адресуй side="FROM" и номером старой
    версии. line_type — ручное переопределение, сверяется с диффом. В ответе
    `anchored`: false — коммент не привязался к диффу (см. `warning`).

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _full_svc(workspace)
    result = svc.pr_comment_add(project, repo, pr_id, text, parent_id=reply_to, path=path,
                                line=line, line_type=line_type, side=side)
    payload = {"pr": pr_id, "id": result.get("id", ""), "reply_to": reply_to,
               "path": path, "line": line}
    for key in ("line_type", "file_type", "anchored", "warning"):
        if key in result:
            payload[key] = result[key]
    return _stamp(payload, _resolve(workspace))


@mcp.tool()
async def jwu_pr_comment_delete(
    pr_id: int,
    comment_id: int,
    project: Optional[str] = None,
    repo: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: удалить СВОЙ комментарий в PR — только после явного «да»
    пользователя (показать, какой коммент удаляем). Чужие комменты jwu не удаляет.
    Bitbucket не даёт удалить коммент, на который уже ответили.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _full_svc(workspace)
    return _stamp({"pr": pr_id, **svc.pr_comment_delete(project, repo, pr_id, comment_id)},
                  _resolve(workspace))


@mcp.tool()
async def jwu_pr_comment_edit(pr_id: int, comment_id: int, text: str, project: Optional[str] = None,
                              repo: Optional[str] = None, workspace: Optional[str] = None) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: заменить текст своего коммента в PR (Bitbucket/GitHub). Ответ —
    before/after. Только СВОЁ (иначе отказ): покажи было → стало и дождись «да»."""
    svc = _full_svc(workspace)
    return _stamp({"pr": pr_id, **svc.pr_comment_update(project, repo, pr_id, comment_id, text)},
                  _resolve(workspace))



@mcp.tool()
async def jwu_pr_update(pr_id: int, title: Optional[str] = None, description: Optional[str] = None,
                        project: Optional[str] = None, repo: Optional[str] = None,
                        workspace: Optional[str] = None) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: поправить заголовок и/или описание своего PR (None — не трогать;
    ревьюеры сохраняются). Ответ — before/after. Только СВОЁ (иначе отказ): покажи было → стало и дождись «да»."""
    svc = _full_svc(workspace)
    return _stamp(svc.pr_update(project, repo, pr_id, title=title, description=description),
                  _resolve(workspace))


@mcp.tool()
async def jwu_pr_task_edit(task_id: int, text: str, workspace: Optional[str] = None) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: поправить текст своей задачи на комменте PR (Bitbucket, до 10 слов;
    текст формулирует пользователь). Только СВОЁ (иначе отказ): покажи было → стало и дождись «да»."""
    svc = _full_svc(workspace)
    return _stamp(svc.pr_task_update(task_id, text).model_dump(), _resolve(workspace))


@mcp.tool()
async def jwu_pr_task_delete(task_id: int, workspace: Optional[str] = None) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: удалить свою задачу на комменте PR (Bitbucket). Закрыть — это
    jwu_pr_task_done, удаление — если задача была лишней. Только СВОЁ (иначе отказ): покажи было → стало и дождись «да»."""
    svc = _full_svc(workspace)
    return _stamp(svc.pr_task_delete(task_id), _resolve(workspace))


@mcp.tool()
async def jwu_comment_edit(key: str, comment_id: str, text: str, client_facing: bool = False,
                           workspace: Optional[str] = None) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: заменить текст своего комментария в задаче Jira/SDESK. Для SDESK
    (читает КЛИЕНТ) — только с client_facing=True после отдельного подтверждения.
    id коммента — из jwu_task (comments[].id). Ответ — before/after. Только СВОЁ (иначе отказ): покажи было → стало и дождись «да»."""
    svc = _require_jira(_full_svc(workspace))
    return _stamp(svc.issue_comment_update(key, comment_id, text, client_facing=client_facing),
                  _resolve(workspace))


@mcp.tool()
async def jwu_comment_delete(key: str, comment_id: str, client_facing: bool = False,
                             workspace: Optional[str] = None) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: удалить свой комментарий в задаче Jira/SDESK (для SDESK —
    client_facing=True). Необратимо: покажи текст удаляемого и дождись «да». Только СВОЁ (иначе отказ): покажи было → стало и дождись «да»."""
    svc = _require_jira(_full_svc(workspace))
    return _stamp(svc.issue_comment_delete(key, comment_id, client_facing=client_facing),
                  _resolve(workspace))


@mcp.tool()
async def jwu_issue_update(key: str, summary: Optional[str] = None, description: Optional[str] = None,
                           assignee: Optional[str] = None, priority: Optional[str] = None,
                           labels: Optional[list[str]] = None, workspace: Optional[str] = None) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: поправить поля задачи Jira — заголовок, описание (wiki-разметка),
    исполнитель (логин; "" — снять), приоритет (имя), метки (список целиком). None — не
    трогать. Удаления задач в jwu НЕТ и не будет. Показать было → стало, дождаться «да»."""
    svc = _require_jira(_full_svc(workspace))
    return _stamp(svc.issue_update(key, summary=summary, description=description, assignee=assignee,
                                   priority=priority, labels=labels), _resolve(workspace))


@mcp.tool()
async def jwu_worklog_edit(key: str, worklog_id: str, time: Optional[str] = None,
                           started: Optional[str] = None, comment: Optional[str] = None,
                           workspace: Optional[str] = None) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: поправить свой ворклог — время («1h 30m»), начало («2026-09-30 10:00»
    в поясе контура или ISO со смещением), описание. id — из jwu_worklogs. Сдвиг одного
    ворклога ломает цепочку дня — предложи сдвинуть соседние. Только СВОЁ (иначе отказ): покажи было → стало и дождись «да»."""
    svc = _require_jira(_full_svc(workspace))
    return _stamp(svc.worklog_update(key, worklog_id, time=time, started=started, comment=comment),
                  _resolve(workspace))


@mcp.tool()
async def jwu_worklog_delete(key: str, worklog_id: str, workspace: Optional[str] = None) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: удалить свой ворклог (id — из jwu_worklogs). Только СВОЁ (иначе отказ): покажи было → стало и дождись «да»."""
    svc = _require_jira(_full_svc(workspace))
    return _stamp(svc.worklog_delete(key, worklog_id), _resolve(workspace))



def _zephyr(workspace: Optional[str], project: Optional[str] = None, *, require: bool = True):
    svc = _full_svc(workspace)
    try:
        z = svc.zephyr(require_project=require and not project)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if project:
        z.project_key = project
    return z


@mcp.tool()
async def jwu_job_push(job_id: int, repo: str = "", dry_run: bool = True, user_approved: bool = False,
                       workspace: Optional[str] = None) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: запушить ветку рабочей копии работы — вместо голого git push.

    Пушит только текущую ветку копии ЭТОЙ работы в одноимённую ветку origin, без force,
    под политикой git воркспейса (jwu_git_policy): allow — сразу, ask — нужен user_approved
    («да» пользователя), grant — нужен допуск пользователя из бота/терминала, deny — никогда.
    Отказ политики — ответ {ok: false, blocked, rule, reason, ask_user, retry}: сделай, что в
    ask_user, и повтори. Откажет и git: незакоммиченное, origin ушёл вперёд, не та ветка.
    dry_run=True (по умолчанию) — что уйдёт: ветка, новые коммиты.
    repo — какая копия, если у работы их несколько."""
    from .core import jobspace

    store = _store_only(workspace)
    job = store.get_job(job_id)
    if job is None:
        raise ValueError(f"Работа #{job_id} не найдена")
    try:
        res = jobspace.push_worktree(store, job, repo, dry_run=dry_run, user_approved=user_approved,
                                     notify=_grant_notify)
    except jobspace.JobSpaceError as exc:
        raise ValueError(str(exc)) from exc
    return _stamp(res, _resolve(workspace))


def _grant_notify(store: Store, req: dict) -> None:
    """Запрос допуска — кнопками в Telegram (если бот настроен)."""
    from .core import gitgrant

    gitgrant.notify(store, req)


def _git_call(path: str, workspace: Optional[str], fn) -> dict:
    """Общая обёртка jwu_git_*: контекст репозитория, отказ политики — ответом, а не ошибкой."""
    import os

    from .core import gitops

    store = _store_only(workspace)
    try:
        ctx = gitops.context(store, path or os.getcwd(), notify=_grant_notify)
        res = fn(ctx)
    except gitops.Blocked as b:
        res = b.to_dict()
    except (gitops.GitOpError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    return _stamp(res, _resolve(workspace))


@mcp.tool()
async def jwu_git_status(path: str = "", workspace: Optional[str] = None) -> dict:
    """Git: состояние репозитория в папке path (по умолчанию — папка сессии): репозиторий
    (project/repo), копия работы или основная папка, ветка, HEAD, ahead/behind, изменённые
    файлы, конфликты, идёт ли merge/rebase, целевая ветка. Только чтение.

    Пишущий git — ТОЛЬКО через jwu_git_* (голый git из Bash режет хук политики)."""
    from .core import gitops

    return _git_call(path, workspace, gitops.status)


@mcp.tool()
async def jwu_git_diff(path: str = "", staged: bool = False, ref: str = "", paths: Optional[list[str]] = None,
                       stat: bool = False, workspace: Optional[str] = None) -> dict:
    """Git: дифф рабочего дерева (staged — индекса; ref — против коммита/ветки; stat — только
    сводка). Только чтение; длинный вывод обрезается."""
    from .core import gitops

    return _git_call(path, workspace, lambda c: gitops.diff(c, staged=staged, ref=ref, paths=paths, stat=stat))


@mcp.tool()
async def jwu_git_log(path: str = "", n: int = 20, ref: str = "", workspace: Optional[str] = None) -> dict:
    """Git: последние n коммитов (sha, автор, дата, тема). Только чтение."""
    from .core import gitops

    return _git_call(path, workspace, lambda c: gitops.log(c, n=n, ref=ref))


@mcp.tool()
async def jwu_git_commit(message: str, path: str = "", files: Optional[list[str]] = None,
                         user_approved: bool = False, workspace: Optional[str] = None) -> dict:
    """Git: add + commit (files — только эти, иначе всё). Сообщение — из jwu-commit-message.
    Под политикой: в копии работы обычно allow, в основной папке и общих ветках — ask
    (user_approved после «да»), в прод-ветках — deny. Отказ — {blocked, ask_user, retry}."""
    from .core import gitops

    return _git_call(path, workspace, lambda c: gitops.commit(c, message, files=files, user_approved=user_approved))


@mcp.tool()
async def jwu_git_switch(branch: str, path: str = "", create: bool = False, base: str = "",
                         user_approved: bool = False, workspace: Optional[str] = None) -> dict:
    """Git: переключиться на ветку (create — создать, base — от чего). Под политикой."""
    from .core import gitops

    return _git_call(path, workspace,
                     lambda c: gitops.switch(c, branch, create=create, base=base, user_approved=user_approved))


@mcp.tool()
async def jwu_git_sync(path: str = "", action: str = "start", target: str = "", mode: str = "",
                       user_approved: bool = False, workspace: Optional[str] = None) -> dict:
    """Git: подтянуть целевую ветку в текущую. action: start (fetch + merge|rebase origin/<target>;
    target и mode по умолчанию из политики репозитория; перед этим backup-ссылка) | continue
    (конфликты решены — завершить) | abort (отменить merge/rebase). Конфликт — ok:false и список
    файлов: реши их правками и вызови continue. Откатить всё — jwu_git_undo(backup)."""
    from .core import gitops

    def run(c):
        if action == "continue":
            return gitops.sync_continue(c)
        if action == "abort":
            return gitops.sync_abort(c)
        if action != "start":
            raise ValueError("action: start | continue | abort")
        return gitops.sync(c, target=target, mode=mode, user_approved=user_approved)

    return _git_call(path, workspace, run)


@mcp.tool()
async def jwu_git_stash(path: str = "", action: str = "push", message: str = "", user_approved: bool = False,
                        workspace: Optional[str] = None) -> dict:
    """Git: stash push (с неотслеживаемыми) | pop | apply | list. Под политикой."""
    from .core import gitops

    return _git_call(path, workspace,
                     lambda c: gitops.stash(c, action, message=message, user_approved=user_approved))


@mcp.tool()
async def jwu_git_discard(path: str = "", files: Optional[list[str]] = None, untracked: bool = False,
                          user_approved: bool = False, workspace: Optional[str] = None) -> dict:
    """Git: выкинуть несохранённое (files — только эти; untracked — и неотслеживаемые файлы).
    Перед этим backup-ссылка (неотслеживаемые в неё не попадают). Обычно ask — после «да»."""
    from .core import gitops

    return _git_call(path, workspace,
                     lambda c: gitops.discard(c, files=files, untracked=untracked, user_approved=user_approved))


@mcp.tool()
async def jwu_git_history(action: str, path: str = "", message: str = "", ref: str = "",
                          keep_changes: bool = True, user_approved: bool = False,
                          workspace: Optional[str] = None) -> dict:
    """Git: переписать НЕзапушенную историю. action: amend (последний коммит + текущие правки;
    message — новое сообщение) | reset (ветку на ref; keep_changes — правки остаются в дереве).
    Запушенное не переписывает (нужен был бы force) — откажет. Перед этим backup-ссылка."""
    from .core import gitops

    def run(c):
        if action == "amend":
            return gitops.amend(c, message, user_approved=user_approved)
        if action == "reset":
            return gitops.reset_to(c, ref, keep_changes=keep_changes, user_approved=user_approved)
        raise ValueError("action: amend | reset")

    return _git_call(path, workspace, run)


@mcp.tool()
async def jwu_git_push(path: str = "", force: bool = False, dry_run: bool = False, user_approved: bool = False,
                       workspace: Optional[str] = None) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: пуш текущей ветки в одноимённую на origin. dry_run — что уйдёт.
    force — только --force-with-lease и только если ветка разошлась (своя, переписанная).
    Под политикой: ask — user_approved после «да»; grant (develop, release* …) — «да» мало,
    нужен допуск пользователя (запрос уходит в бот; ask_user скажет как); deny — никогда.
    Для копии работы удобнее jwu_job_push(job_id)."""
    from .core import gitops

    return _git_call(path, workspace,
                     lambda c: gitops.push(c, force=force, dry_run=dry_run, user_approved=user_approved))


@mcp.tool()
async def jwu_git_branch_delete(branch: str, path: str = "", remote: bool = False, user_approved: bool = False,
                                workspace: Optional[str] = None) -> dict:
    """Git: удалить ветку (remote — и на origin, ВНЕШНЯЯ запись). Перед этим backup-ссылка. Под политикой."""
    from .core import gitops

    return _git_call(path, workspace,
                     lambda c: gitops.delete_branch(c, branch, remote=remote, user_approved=user_approved))


@mcp.tool()
async def jwu_git_worktree_remove(path: str, delete_branch: bool = True, user_approved: bool = False,
                                  workspace: Optional[str] = None) -> dict:
    """Git: убрать рабочую копию (git worktree) — вместо голого `git worktree remove/prune`.

    path — копия: только в <репо>/.claude/worktrees/ (копии субагентов с isolation: "worktree")
    или в каталоге работ jwu, иначе отказ. Несохранённое в копии — норма (субагент оставляет
    правки в индексе): перед удалением всё уходит в бэкап — патч (staged + unstaged +
    неотслеживаемое) в каталоге данных jwu и backup-ссылка refs/jwu/backup/…-worktree-<имя>,
    хранятся backup_days. Потом worktree remove --force, prune и удаление ветки копии
    (delete_branch): служебная worktree-* — вместе с копией, другая (ветка работы) — по своему
    решению политики delete_branch. path = корень репозитория — только prune.
    Под политикой, операция worktree (по умолчанию ask: user_approved после «да»). Ответ: что
    удалено, ветка, backup {ref, patch, files, restore}; отказ — {blocked, ask_user, retry}."""
    import os

    from .core import gitops

    store = _store_only(workspace)
    try:
        res = gitops.worktree_remove(store, path or os.getcwd(), delete_branch=delete_branch,
                                     user_approved=user_approved, notify=_grant_notify)
    except gitops.Blocked as b:
        res = b.to_dict()
    except (gitops.GitOpError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    return _stamp(res, _resolve(workspace))


@mcp.tool()
async def jwu_git_tag(name: str, path: str = "", message: str = "", push: bool = False,
                      user_approved: bool = False, workspace: Optional[str] = None) -> dict:
    """Git: тег на HEAD (message — аннотированный; push — запушить, ВНЕШНЯЯ запись). Обычно grant."""
    from .core import gitops

    return _git_call(path, workspace,
                     lambda c: gitops.tag(c, name, message=message, push_tag=push, user_approved=user_approved))


@mcp.tool()
async def jwu_git_undo(path: str = "", ref: str = "", user_approved: bool = False,
                       workspace: Optional[str] = None) -> dict:
    """Git: откат к backup-ссылке jwu (снимаются перед sync/discard/amend/reset/undo).
    Без ref — список ссылок. С ref — вернуть ветку и дерево; всегда после «да» (user_approved)."""
    from .core import gitops

    return _git_call(path, workspace, lambda c: gitops.undo(c, ref, user_approved=user_approved))


@mcp.tool()
async def jwu_git_policy(path: str = "", workspace: Optional[str] = None) -> dict:
    """Git: политика для репозитория в path и его текущей ветки — какой уровень у каждой
    операции (allow | ask | grant | deny) и каким правилом решено, плюс ожидающие допуски.
    Только чтение: менять политику и выдавать допуски агент НЕ может (это делает пользователь
    сам: jwu git policy edit / jwu git grant)."""
    from .core import gitops, gitpolicy

    def run(c):
        table = {op: gitpolicy.decide(c.policy, op, remote=c.remote, branch=c.branch, where=c.where).to_dict()
                 for op in gitpolicy.OPS}
        return {"ok": True, "repo": c.remote, "branch": c.branch, "where": c.where,
                "guard": gitpolicy.guard_on(c.policy, c.remote),
                "ops": {op: {"level": d["level"], "rule": d["rule"]} for op, d in table.items()},
                "pending_grants": gitpolicy.pending_grants(c.store)}

    return _git_call(path, workspace, run)


@mcp.tool()
async def jwu_sweep(apply: bool = False, groups: Optional[list[str]] = None, force: bool = False,
                    user_approved: bool = False, workspace: Optional[str] = None) -> dict:
    """Уборка за всеми сессиями (все воркспейсы). Без apply — только список по группам:
    closed_jobs (неубранное у закрытых работ), orphan_docker (docker с меткой работы, а работы
    нет или закрыта), orphan_worktrees (копии в каталоге работ без живой работы и копии субагентов .claude/worktrees/agent-* без изменений > 2 ч — их несохранённое уходит в бэкап), temp (скачанные
    вложения старше суток), dead_sessions (сессии молчат > 2 ч: их работы — в паузу), dead_workers
    (воркеры оркестратора, чей процесс умер без «готово»: работы — в паузу); kept —
    что не тронется (незакоммиченное/незапушенное) и почему. Открытые работы не трогаются.

    apply=True — убрать выбранные groups, только после «да» (user_approved=True). force=True —
    убрать и копии из kept: несохранённое ПРОПАДЁТ — отдельное явное «да» на этот список."""
    from .core import sweep

    store = _store_only(workspace)
    plan = sweep.scan(store)
    if not apply:
        return _stamp(plan, _resolve(workspace))
    if not user_approved:
        raise ValueError("Уборка — только после «да» пользователя на показанный список.")
    return _stamp({"plan": plan, **sweep.apply(store, groups or list(sweep.GROUPS), force=force, plan=plan)},
                  _resolve(workspace))


@mcp.tool()
async def jwu_worker_start(name: str, task: str, cwd: str, jobs: Optional[list[int]] = None,
                           permission_mode: str = "", user_approved: bool = False,
                           workspace: Optional[str] = None) -> dict:
    """Оркестратор: поднять воркер — фоновую сессию Claude (Remote Control) с именем и заданием.

    ТОЛЬКО по прямой команде пользователя («возьми X воркеру») и ровно столько воркеров, сколько
    он сказал — user_approved=True. name — по делу и уникальное («PROJ-1 · выгрузка»), task —
    задание (шапку «кто ты, как отчитываться» jwu добавит сам), cwd — папка репозитория задачи,
    jobs — работы воркера (задание запишется в их журнал, kind=brief). Перед запуском текущая
    сессия должна быть оркестратором: `jwu worker orchestrator --name <своё имя>` (Bash).
    Возвращает запись воркера со ссылкой url на его сессию (может быть пустой — появится в list)."""
    from .core import workers

    if not user_approved:
        raise ValueError("Воркер — только по команде пользователя: user_approved=True после его «возьми … воркеру».")
    store = _store_only(workspace)
    ws = _resolve(workspace)
    info = workers.start(store, name, task, cwd=cwd, jobs=jobs or [], workspace=ws.slug,
                         permission_mode=permission_mode)
    for job_id in jobs or []:
        store.add_job_record(job_id, f"Задание воркеру «{info['name']}»:\n{task.strip()}", kind="brief")
    info["url"] = workers.wait_url(store, info["name"])
    return _stamp(info, ws)


@mcp.tool()
async def jwu_worker_list(workspace: Optional[str] = None) -> dict:
    """Воркеры и их состояние (без сети): state — starting | working | waiting (ждёт пользователя)
    | stuck | done | silent (не отчитывался > 30 мин) | dead (процесса нет, а «готово» не было);
    note — что сейчас, url — ссылка на сессию, jobs — работы. Порядок: ждущие и упавшие — первыми."""
    from .core import workers

    store = _store_only(workspace)
    items = workers.list_workers(store)
    return _stamp({"workers": items, "summary": workers.summary(items),
                   "orchestrator": workers.orchestrator(store)}, _resolve(workspace))


@mcp.tool()
async def jwu_worker_report(state: str, note: str = "", jobs: Optional[list[int]] = None, name: str = "",
                            workspace: Optional[str] = None) -> dict:
    """Воркер: отчёт о себе — state working | waiting (перед вопросом пользователю) | stuck | done
    (все задачи закрыты), note — что сейчас одной строкой, jobs — добавить свои работы.
    name по умолчанию — имя этой сессии (JWU_SESSION_NAME)."""
    from .core import workers

    who = name or os.environ.get("JWU_SESSION_NAME", "")
    if not who:
        raise ValueError("Эта сессия не воркер (нет JWU_SESSION_NAME) — передай name.")
    return _stamp(workers.report(_store_only(workspace), who, state, note, jobs=jobs or []), _resolve(workspace))


@mcp.tool()
async def jwu_worker_stop(name: str, force: bool = False, user_approved: bool = False,
                          workspace: Optional[str] = None) -> dict:
    """Оркестратор: остановить воркер и убрать его запись. Когда воркер сказал «готово» и его
    работы закрыты — можно без вопроса (user_approved=True ставит оркестратор по скиллу); с
    открытыми работами — только force=True после явного «да» пользователя (работы уйдут в паузу)."""
    from .core import workers

    if not user_approved:
        raise ValueError("Остановка воркера — по правилу скилла или после «да» пользователя: user_approved=True.")
    return _stamp(workers.stop(_store_only(workspace), name, force=force), _resolve(workspace))


@mcp.tool()
async def jwu_job_reviewer(repo: str = "", workspace: Optional[str] = None) -> dict:
    """Кого звать ревьювером своих правок в репозитории (без записи, без вопросов пользователю):
    reviewer — субагент из настроек воркспейса для repo (папка/слаг репозитория), иначе дефолт;
    max_iterations — сколько раз подряд гонять «правки → ревью», пока остаются блокеры
    (настройка job.review.max_iterations, по умолчанию 3), дальше — вопрос пользователю."""
    from .core import reviewq
    from .core import voice as voice_mod

    store = _store_only(workspace)
    ws_id = store.workspace_id
    raw = (store.workspace_settings(ws_id).get("job.review.max_iterations") or "").strip()
    return _stamp({"repo": repo, "reviewer": reviewq.reviewer_for(store, ws_id, repo) if repo else reviewq.DEFAULT_REVIEWER,
                   "max_iterations": int(raw) if raw.isdigit() and int(raw) > 0 else 3,
                   "voice_agent": (store.workspace_settings(ws_id).get(voice_mod.AGENT_SETTING) or "").strip()
                   or voice_mod.DEFAULT_AGENT},
                  _resolve(workspace))


@mcp.tool()
async def jwu_zephyr_context(issue_key: str, project: Optional[str] = None,
                             workspace: Optional[str] = None) -> dict:
    """Всё для работы с тест-кейсами задачи одним вызовом (без записи): кейсы, уже привязанные к
    задаче; папки, куда класть новые (папки её кейсов); статусы, приоритеты и свои поля проекта
    кейсов; агент тестировщика воркспейса и есть ли инструкция-регламент (jwu_guide)."""
    from .core import guides, zephyr

    z = _zephyr(workspace, project)
    store = _store_only(workspace)
    cases = z.for_issue(issue_key)
    try:
        guides.get_guide(store, store.workspace_id, zephyr.RULES_GUIDE)
        rules = zephyr.RULES_GUIDE
    except guides.GuideError:
        rules = ""
    return _stamp({
        "project": z.project_key, "issue": issue_key,
        "cases": [{"key": c.get("key"), "name": c.get("name"), "folder": c.get("folder"),
                   "status": c.get("status"), "url": z.case_url(c.get("key", ""))} for c in cases],
        "suggested_folders": z.suggest_folder(issue_key),
        "fields": z.fields(), "agent": zephyr.agent(store, store.workspace_id), "rules_guide": rules,
    }, _resolve(workspace))


@mcp.tool()
async def jwu_zephyr_case(key: str, workspace: Optional[str] = None) -> dict:
    """Тест-кейс Zephyr Scale целиком: шаги (testScript), папка, статус, связи с задачами. Без записи."""
    z = _zephyr(workspace, require=False)
    return _stamp({**z.case(key), "url": z.case_url(key)}, _resolve(workspace))


@mcp.tool()
async def jwu_zephyr_search(query: str, limit: int = 50, workspace: Optional[str] = None) -> dict:
    """Поиск кейсов по TQL Zephyr Scale (напр. projectKey = "QA" AND folder = "/smoke"). Без записи."""
    return _stamp({"cases": _zephyr(workspace, require=False).search(query, limit)}, _resolve(workspace))


@mcp.tool()
async def jwu_zephyr_folders(project: Optional[str] = None, workspace: Optional[str] = None) -> dict:
    """Дерево папок кейсов проекта: путь и число кейсов. Без записи."""
    return _stamp({"folders": _zephyr(workspace, project).folders()}, _resolve(workspace))


@mcp.tool()
async def jwu_zephyr_create(name: str, steps: list[dict], objective: str = "", precondition: str = "",
                            folder: str = "", status: str = "", priority: str = "",
                            labels: Optional[list[str]] = None, issue_links: Optional[list[str]] = None,
                            custom_fields: Optional[dict] = None, project: Optional[str] = None,
                            user_approved: bool = False, workspace: Optional[str] = None) -> dict:
    """Создать тест-кейс — ВНЕШНЯЯ запись, только после «да» пользователя на показанный кейс
    (user_approved=True). steps — [{"description", "testData"?, "expectedResult"}].
    issue_links — ключи задач (могут быть из другого проекта, чем проект кейсов)."""
    if not user_approved:
        raise ValueError("Кейс создаётся только после «да» пользователя на показанный текст.")
    z = _zephyr(workspace, project)
    res = z.create(name=name, steps=steps, objective=objective, precondition=precondition, folder=folder,
                   status=status, priority=priority, labels=labels or [], issueLinks=issue_links or [],
                   customFields=custom_fields or {})
    return _stamp({**res, "url": z.case_url(res.get("key", ""))}, _resolve(workspace))


@mcp.tool()
async def jwu_zephyr_update(key: str, name: Optional[str] = None, steps: Optional[list[dict]] = None,
                            objective: Optional[str] = None, precondition: Optional[str] = None,
                            folder: Optional[str] = None, status: Optional[str] = None,
                            priority: Optional[str] = None, labels: Optional[list[str]] = None,
                            custom_fields: Optional[dict] = None, user_approved: bool = False,
                            workspace: Optional[str] = None) -> dict:
    """Поправить тест-кейс — ВНЕШНЯЯ запись, только после «да» (user_approved=True). Передавай только меняемое."""
    if not user_approved:
        raise ValueError("Правка кейса — только после «да» пользователя на показанные изменения.")
    z = _zephyr(workspace, require=False)
    res = z.update(key, steps=steps, name=name, objective=objective, precondition=precondition,
                   folder=folder, status=status, priority=priority, labels=labels, customFields=custom_fields)
    return _stamp({**res, "url": z.case_url(key)}, _resolve(workspace))


@mcp.tool()
async def jwu_zephyr_link(key: str, issue_keys: list[str], user_approved: bool = False,
                          workspace: Optional[str] = None) -> dict:
    """Привязать кейс к задачам (добавляет к уже привязанным) — ВНЕШНЯЯ запись, только после «да»."""
    if not user_approved:
        raise ValueError("Привязка — только после «да» пользователя.")
    return _stamp(_zephyr(workspace, require=False).link(key, issue_keys), _resolve(workspace))


@mcp.tool()
async def jwu_zephyr_folder_create(path: str, project: Optional[str] = None, user_approved: bool = False,
                                   workspace: Optional[str] = None) -> dict:
    """Завести папку кейсов (напр. /Чат/Вложения) — ВНЕШНЯЯ запись, только после «да».
    Сначала посмотри jwu_zephyr_folders: папка часто уже есть."""
    if not user_approved:
        raise ValueError("Папка заводится только после «да» пользователя.")
    return _stamp(_zephyr(workspace, project).create_folder(path), _resolve(workspace))


@mcp.tool()
async def jwu_confluence_page(page_id: str, with_body: bool = True, workspace: Optional[str] = None) -> dict:
    """Страница Confluence: заголовок, пространство, версия, путь (предки), url и текст в
    формате storage (XHTML). Без записи. Удаления страниц в jwu нет."""
    svc = _full_svc(workspace)
    return _stamp(svc.confluence().page(page_id, with_body=with_body), _resolve(workspace))


@mcp.tool()
async def jwu_confluence_children(page_id: str, workspace: Optional[str] = None) -> dict:
    """Дочерние страницы Confluence (id, title, version, url). Без записи. «Рядом с
    страницей X» = дочерняя того же родителя: parent_id из jwu_confluence_page(X)."""
    svc = _full_svc(workspace)
    return _stamp({"page_id": page_id, "children": svc.confluence().children(page_id)}, _resolve(workspace))


@mcp.tool()
async def jwu_confluence_search(cql: str, limit: int = 25, workspace: Optional[str] = None) -> dict:
    """Поиск страниц Confluence по CQL, напр. `space = DOCS and title ~ "LINE"`. Без записи."""
    svc = _full_svc(workspace)
    return _stamp({"cql": cql, "results": svc.confluence().search(cql, limit=limit)}, _resolve(workspace))


@mcp.tool()
async def jwu_confluence_create(title: str, body: str, parent_id: Optional[str] = None,
                                space: Optional[str] = None, fmt: str = "storage",
                                dry_run: bool = True, workspace: Optional[str] = None) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: создать страницу Confluence. parent_id — под какой страницей
    (пространство берётся у родителя), иначе space или настройка контура. body — storage
    (XHTML) или вики-разметка при fmt="wiki". dry_run=True (по умолчанию) — превью: где
    появится, заголовок, объём; занятый заголовок — ошибка. Показать пользователю и только
    после «да» — dry_run=False. Удаления страниц в jwu НЕТ."""
    svc = _full_svc(workspace)
    return _stamp(svc.confluence_create(title=title, body=body, parent_id=parent_id, space=space,
                                        fmt=fmt, dry_run=dry_run), _resolve(workspace))


@mcp.tool()
async def jwu_confluence_update(page_id: str, title: Optional[str] = None, body: Optional[str] = None,
                                fmt: str = "storage", message: str = "", dry_run: bool = True,
                                workspace: Optional[str] = None) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: новая версия страницы Confluence — заголовок и/или текст целиком
    (None — оставить). Текст заменяется ПОЛНОСТЬЮ: сначала прочитай jwu_confluence_page и
    правь его. dry_run=True — превью (версия, было/стало по заголовку и объёму); после «да»
    — dry_run=False. message — комментарий к версии. Удаления страниц в jwu НЕТ."""
    svc = _full_svc(workspace)
    return _stamp(svc.confluence_update(page_id, title=title, body=body, fmt=fmt, message=message,
                                        dry_run=dry_run), _resolve(workspace))


@mcp.tool()
async def jwu_video_frames(path: str, every: float = 5, start: Optional[str] = None,
                           end: Optional[str] = None, width: int = 1280, max_frames: int = 60,
                           sheet: bool = True, out_dir: Optional[str] = None) -> dict:
    """Нарезать локальное видео (запись экрана из вложений задачи/PR) на кадры для анализа.

    every — шаг в секундах; start/end — отрезок («90», «1:30», «1m30s»; по умолчанию всё
    видео). На каждом кадре отметка времени. sheet=True — ещё и контактный лист: сетка
    4×4 миниатюр (`sheets`). Порядок: прочитай лист(ы) через Read, найди интересный момент,
    открой нужные кадры (`frames[].path`) целиком; детальнее — повтори с мелким шагом на
    узком отрезке. Видео скачивается так: jwu_attachments(key, download=True,
    kinds=["video"]). Нужен системный ffmpeg. Ничего не отправляет наружу.
    """
    from .core import frames as frames_mod

    res = frames_mod.extract(Path(path), every=every, start=frames_mod.parse_time(start),
                             end=frames_mod.parse_time(end), width=width, max_frames=max_frames,
                             sheet=sheet, out_dir=Path(out_dir) if out_dir else None)
    return res.as_dict()


@mcp.tool()
async def jwu_pr_create(
    target: str,
    title: str,
    source: Optional[str] = None,
    description: str = "",
    from_job: Optional[int] = None,
    reviewers: Optional[list[str]] = None,
    project: Optional[str] = None,
    repo: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: создать PR (Bitbucket/GitHub) из уже запушенной ветки. Только после
    того, как пользователь увидел заголовок, описание, ветки и ревьюверов и сказал «да».

    source — ветка-источник (по умолчанию текущая ветка папки процесса); from_job —
    взять заголовок/описание из журнала работы (title/description перекрывают).

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _full_svc(workspace)
    branch = source or gitinfo.head_state(Path.cwd())[0]
    if not branch:
        raise ValueError("Не понять ветку-источник: передай source")
    if from_job is not None:
        job_title, job_body = svc.pr_draft_from_job(from_job)
        title = title or job_title
        description = description or job_body
    pull = svc.pr_create(project, repo, source=branch, target=target, title=title,
                         description=description, reviewers=list(reviewers or []))
    return _stamp(pull.model_dump(), _resolve(workspace))


@mcp.tool()
async def jwu_pr_people(names: Optional[list[str]] = None, workspace: Optional[str] = None) -> dict:
    """Ревьюверы для PR (без записи). Без names — кого пользователь обычно ставит (`usual`,
    по его PR): предложи их вариантами меню. names — как пользователь их назвал словами
    («Олег Смирнов», «Саша Волков», фамилия, логин): по каждому — кандидаты
    (`name` — логин для jwu_pr_create(reviewers=…), `display_name`). Один кандидат — бери;
    несколько — уточни меню; ноль — скажи, кого не нашёл."""
    svc = _full_svc(workspace)
    out: dict = {"usual": svc.usual_reviewers()}
    if names:
        out["found"] = {n: svc.find_people(n) for n in names if n.strip()}
    return _stamp(out, _resolve(workspace))


@mcp.tool()
async def jwu_pr_review(
    pr_id: int,
    status: str,
    text: str = "",
    project: Optional[str] = None,
    repo: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: мой статус ревью на чужом PR — APPROVED | NEEDS_WORK | UNAPPROVED.
    Только после явного подтверждения пользователя; обычно после ревью субагентом
    (скилл jwu-job-review) и показа выводов.

    text — замечание: у GitHub обязательно для NEEDS_WORK и уходит в отзыв; у Bitbucket
    статус без текста, поэтому текст отправляется отдельным общим комментарием.
    UNAPPROVED (снять апрув) есть только у Bitbucket.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _full_svc(workspace)
    result = svc.pr_review(project, repo, pr_id, status, body=text)
    comment_id = None
    if text.strip() and svc.bitbucket is not None:
        comment_id = svc.pr_comment_add(project, repo, pr_id, text).get("id")
    return _stamp({"pr": pr_id, "status": status.upper().replace("-", "_"),
                   "comment_id": comment_id, "result": result}, _resolve(workspace))


@mcp.tool()
async def jwu_pr_tasks(
    pr_id: int,
    project: Optional[str] = None,
    repo: Optional[str] = None,
    workspace: Optional[str] = None,
) -> list[dict]:
    """Задачи на комментах PR (Bitbucket tasks) — чек-лист «что поправить»: id, текст,
    state OPEN|RESOLVED, автор, comment_id якоря. Открытые задачи = что ещё не сделано
    по этому PR; закрывай их по мере правок через jwu_pr_task_done. Только Bitbucket.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _full_svc(workspace)
    return [t.model_dump() for t in svc.pr_tasks(project, repo, pr_id)]


@mcp.tool()
async def jwu_pr_task_add(
    pr_id: int,
    text: str,
    comment_id: Optional[int] = None,
    comment_text: Optional[str] = None,
    path: Optional[str] = None,
    line: Optional[int] = None,
    project: Optional[str] = None,
    repo: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ в Bitbucket: повесить задачу на коммент PR. Звать только после
    явного подтверждения пользователя (скилл jwu-pr-task).

    text — ТЕКСТ ЗАДАЧИ ФОРМУЛИРУЕТ ПОЛЬЗОВАТЕЛЬ, не придумывай его сам; до 10 слов,
    длиннее инструмент отказывает. comment_id — коммент-якорь (любой, включая свой);
    без него jwu оставляет СВОЙ коммент (общий, либо на строку path:line) с текстом
    comment_text (по умолчанию — текст задачи) и вешает задачу на него.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _full_svc(workspace)
    task = svc.pr_task_add(project, repo, pr_id, text, comment_id=comment_id,
                           comment_text=comment_text, path=path, line=line)
    return _stamp(task.model_dump(), _resolve(workspace))


@mcp.tool()
async def jwu_pr_task_done(
    task_ids: list[int],
    reopen: bool = False,
    workspace: Optional[str] = None,
) -> list[dict]:
    """ВНЕШНЯЯ ЗАПИСЬ в Bitbucket: закрыть задачи PR (RESOLVED) — сделано. reopen=True
    открывает их заново. Одно подтверждение пользователя на всю пачку task_ids.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _full_svc(workspace)
    tasks = svc.pr_task_set_state(list(task_ids), "OPEN" if reopen else "RESOLVED")
    return [_stamp(t.model_dump(), _resolve(workspace)) for t in tasks]


@mcp.tool()
async def jwu_builds(
    pr_id: int,
    project: Optional[str] = None,
    repo: Optional[str] = None,
    workspace: Optional[str] = None,
) -> list[dict]:
    """Статусы CI-сборок по head-коммиту PR (быстро: build-status Bitbucket либо
    check-runs GitHub).

    Не зависит от трекера задач. project/repo — по умолчанию из конфига воркспейса.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    svc = _builds_svc(workspace)
    default_project, default_repo = svc.default_pr_ref()
    proj = project or default_project
    rp = repo or default_repo
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
    У каждого вложения `referenced_by` — где оно вставлено в текст: "description" и/или
    id комментариев (у комментария есть `images`). Так понятно, к какой реплике скриншот.

    kinds — какие виды качать (image|log|doc|archive|video); по умолчанию все, кроме видео:
    видео качается только явно (kinds=["video"]) — оно тяжёлое. Смотреть его — через
    jwu_video_frames(path). dest — каталог (по умолчанию <tmp>/jwu/<KEY>). При download
    возвращает локальные пути в `downloaded[].path` — их потом читать через Read.

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
    provider: str = "local",
    bitbucket: bool = False,
    paths: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
    use: bool = False,
) -> dict:
    """Создать воркспейс — контур со своими папками, провайдером и данными.

    slug — короткое имя латиницей (home-jwu, work). paths — папки, которые сразу
    привязать: по ним jwu потом определяет воркспейс автоматически.
    provider — ОДИН источник задач и PR: `local` (свои фичи, ничего снаружи),
    `jira` (Jira + Bitbucket + Jenkins) или `github` (Issues + PR + Actions).
    По умолчанию контур локальный, креды не нужны вовсе; доступы настраиваются потом
    командой `jwu configure -W <slug>`. bitbucket=True имеет смысл только у jira.
    use=True делает воркспейс активным по умолчанию для команд вне привязанных папок.
    """
    store = _registry()
    try:
        ws = ws_mod.create(store, slug, name=name, provider=provider, bitbucket=bitbucket,
                           paths=list(paths or []), tags=list(tags or []))
    except ws_mod.WorkspaceError as exc:
        raise ValueError(str(exc))
    if use:
        ws_mod.set_active(store, ws)
    return _workspace_payload(store, ws)


@mcp.tool()
async def jwu_workspace_provider(
    provider: str,
    bitbucket: Optional[bool] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Сменить провайдера контура: `local` | `jira` | `github`.

    Провайдер — единственный источник задач и PR; смешивать их нельзя. Локальные данные
    (работы, фичи, заметки, правила) при смене остаются на месте. После смены нужны
    доступы: `jwu configure -W <slug>`.
    """
    store = _registry()
    ws = _resolve(workspace)
    try:
        updated = ws_mod.set_provider(store, ws, provider, bitbucket=bitbucket)
    except ws_mod.WorkspaceError as exc:
        raise ValueError(str(exc))
    return _workspace_payload(store, updated)


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
async def jwu_rules(
    kind: Optional[str] = None,
    tag: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """ПРАВИЛА ВОРКСПЕЙСА целиком: что нельзя делать, как что делается, общая инфа.

    Это знание о проекте, которого нет в коде: запреты (⛔ constraint — обязательны
    к исполнению), инструкции (howto — как поднять стенд, прогнать тесты), соглашения
    (convention — ветки, формат коммита), грабли (gotcha) и справка (info).

    tag — тег папки («legacy-бэкенд», «фронт»): вернёт правила ЭТОГО репозитория ПЛЮС
    общие. Вызывай с тегом, как только понял, где именно правишь: в
    jwu_workspace_current правила с тегами приходят только списком, без текста.
    """
    if kind is not None and kind not in WORKSPACE_RULE_KINDS:
        raise ValueError(f"Недопустимый тип {kind!r}. "
                         f"Допустимо: {', '.join(WORKSPACE_RULE_KINDS)}")
    store = _store_only(workspace)
    if kind is not None:
        rows = store.list_rules(kind=kind, tag=tag)
        body = "\n".join(f"- #{r.id} {r.title}" + (f"\n      {r.text}" if r.text else "")
                          for r in rows) or "правил такого типа нет"
        return {"workspace": _resolve(workspace).slug, "kind": kind, "tag": tag,
                "rules_md": body, "known_tags": store.all_tags(store.workspace_id)}
    return {
        "workspace": _resolve(workspace).slug,
        "tag": tag,
        "rules_md": store.rules_markdown(tag=tag) or "правил в этом воркспейсе нет",
        "known_tags": store.all_tags(store.workspace_id),
    }


@mcp.tool()
async def jwu_rule_add(
    title: str,
    text: str = "",
    kind: str = "info",
    tag: str = "",
    user_approved: bool = False,
    workspace: Optional[str] = None,
) -> dict:
    """Записать правило воркспейса — то, что должно пережить эту сессию.

    ⛔ ТОЛЬКО с явного «да» пользователя на эту формулировку. Сначала покажи title и text,
    спроси «сохранить правило?», дождись «да» — и только тогда user_approved=True. Без
    него инструмент откажет. Свои выводы, «прецеденты» и разовые находки сессии — НЕ
    правила: это журнал работы (jwu_job_add) или заметка на задаче (jwu_note).

    Зови, когда пользователь формулирует что-то про ПРОЕКТ, а не про текущую задачу:
    «никогда не пушь в develop», «стенд поднимается так-то», «фронт лежит там-то».
    Разовое ограничение конкретного цикла работы — это jwu_job_add(kind='constraint'),
    а не правило.

    title — суть одной строкой (её видно в списках), text — подробности (можно
    многострочные). kind: constraint | howto | info | convention | gotcha.
    tag — тег папки, если правило только про этот репозиторий; иначе правило общее.
    """
    if not user_approved:
        raise ValueError("Правила воркспейса пишутся только с явного «да» пользователя на ЭТУ "
                         "формулировку. Покажи ему title и text, спроси «сохранить правило?» и "
                         "вызови снова с user_approved=True. Сам не решай.")
    if kind not in WORKSPACE_RULE_KINDS:
        raise ValueError(f"Недопустимый тип {kind!r}. "
                         f"Допустимо: {', '.join(WORKSPACE_RULE_KINDS)}")
    store = _store_only(workspace)
    return store.add_rule(title, text=text, kind=kind, tag=tag).model_dump()


@mcp.tool()
async def jwu_rule_edit(
    rule_id: int,
    title: Optional[str] = None,
    text: Optional[str] = None,
    kind: Optional[str] = None,
    tag: Optional[str] = None,
    user_approved: bool = False,
    workspace: Optional[str] = None,
) -> dict:
    """Изменить правило воркспейса. Передавай только те поля, что меняешь.

    ⛔ ТОЛЬКО с явного «да» пользователя на новую формулировку (user_approved=True).
    """
    if not user_approved:
        raise ValueError("Правила воркспейса пишутся только с явного «да» пользователя на ЭТУ "
                         "формулировку. Покажи ему title и text, спроси «сохранить правило?» и "
                         "вызови снова с user_approved=True. Сам не решай.")
    if kind is not None and kind not in WORKSPACE_RULE_KINDS:
        raise ValueError(f"Недопустимый тип {kind!r}. "
                         f"Допустимо: {', '.join(WORKSPACE_RULE_KINDS)}")
    store = _store_only(workspace)
    if store.get_rule(rule_id) is None:
        raise ValueError(f"Правило #{rule_id} не найдено в этом воркспейсе")
    store.update_rule(rule_id, title=title, text=text, kind=kind, tag=tag)
    return store.get_rule(rule_id).model_dump()


@mcp.tool()
async def jwu_rule_rm(rule_id: int, user_approved: bool = False,
                      workspace: Optional[str] = None) -> dict:
    """Удалить правило воркспейса (правило устарело / больше не действует).

    ⛔ ТОЛЬКО с явного «да» пользователя (user_approved=True).
    """
    if not user_approved:
        raise ValueError("Правила воркспейса пишутся только с явного «да» пользователя на ЭТУ "
                         "формулировку. Покажи ему title и text, спроси «сохранить правило?» и "
                         "вызови снова с user_approved=True. Сам не решай.")
    store = _store_only(workspace)
    rule = store.get_rule(rule_id)
    if rule is None:
        raise ValueError(f"Правило #{rule_id} не найдено в этом воркспейсе")
    store.delete_rule(rule_id)
    return {"removed": rule.model_dump()}


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
async def jwu_note(key: str, text: str, kind: str = "context", pin: bool = False,
                   workspace: Optional[str] = None) -> dict:
    """Заметка-контекст в локальную память (наружу не постит) по ЛЮБОМУ ключу: задача
    PROJ-1, PR PROJ/repo#42 (см. pr_note_key), ветка. kind — context | decision | gotcha |
    todo | status; status — одна закреплённая строка «почему висит / что сейчас», её
    видно в jwu prs, дневном анализе и дашборде. Читается там, где сущность открывают:
    jwu_task, jwu_pr, jwu_context, handoff — без поиска работы.
    """
    ws = _resolve(workspace)
    return _stamp(_store_only(workspace).add_note(key, text, kind=kind, pinned=pin).model_dump(), ws)


@mcp.tool()
async def jwu_notes(key: str, workspace: Optional[str] = None) -> list[dict]:
    """Заметки-контекст по ключу (задача, PR PROJ/repo#42, ветка). Без сети."""
    return [n.model_dump() for n in _store_only(workspace).get_notes(key)]


@mcp.tool()
async def jwu_context(key: str, workspace: Optional[str] = None) -> dict:
    """Что я знаю по задаче или PR БЕЗ разбора работ и без сети: заметки-контекст
    (закреплённый status первым), последняя работа с её последней записью и git-состоянием,
    у PR — снапшот последнего синка (состояние, сборка, задачи). Ключ: PROJ-1 либо PROJ/repo#42.

    Зови первым, когда открываешь задачу/PR в новой сессии: обычно этого хватает, чтобы
    понять «где мы», а jwu_job_handoff нужен уже для продолжения конкретной работы.
    """
    import re as _re

    store = _store_only(workspace)
    notes = store.get_notes(key)
    payload: dict = {
        "key": key,
        "status": next((n.text for n in reversed(notes) if n.kind == "status" and n.pinned), ""),
        "notes": [n.model_dump() for n in notes],
    }
    match = _re.match(r"^(?P<project>[^/#]+)/(?P<repo>[^#]+)#(?P<id>\d+)$", key)
    if match:
        pr_id = int(match.group("id"))
        jobs = store.jobs_for_pr(pr_id, match.group("project"), match.group("repo"))
        snap = next((p for p in store.latest_prs(None)
                     if p.id == pr_id and p.project == match.group("project") and p.repository == match.group("repo")), None)
        if snap is not None:
            payload["pr"] = {
                "title": snap.title, "state": snap.state, "conflicted": snap.conflicted,
                "build_state": snap.build_state, "tasks_open": snap.tasks_open,
                "reviewers": [{"name": r.display_name or r.name, "status": r.status} for r in snap.reviewers],
                "updated": snap.updated,
            }
    else:
        jobs = store.jobs_for_task(key)
        snap_issue = next((i for i in store.latest_issues(None) if i.key == key), None)
        if snap_issue is not None:
            payload["task"] = {"summary": snap_issue.summary, "status": snap_issue.status,
                               "assignee": snap_issue.assignee, "updated": snap_issue.updated}
        try:
            from .core import branches as br

            found = br.collect(br.workspace_roots(store), key=key)
            payload["branches"] = [{**b.as_dict(), "summary": br.summary_line(b)} for b in found[:10]]
        except Exception:  # noqa: BLE001 — git недоступен: контекст всё равно полезен
            payload["branches"] = []
    if jobs:
        last = jobs[0]
        rec = last.records[-1] if last.records else None
        payload["last_job"] = {
            "id": last.id, "status": last.status, "title": last.title, "updated_at": last.updated_at,
            "last_record": rec.model_dump() if rec else None,
            "branch": next((r.branch for r in reversed(last.records) if r.branch), ""),
            "commit": next((r.commit for r in reversed(last.records) if r.commit), ""),
            "prs": [p.model_dump() for p in last.prs],
        }
        payload["jobs_total"] = len(jobs)
    return payload


@mcp.tool()
async def jwu_feature_edit(
    ref: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    priority: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Изменить локальную фичу (ref — id или ключ вида HOMEJWU-1).

    Передавай только те поля, что меняешь. Смена статуса — отдельный jwu_feature_status.
    """
    store = _store_only(workspace)
    feature = store.get_feature(ref)
    if feature is None:
        raise ValueError(f"Фича {ref!r} не найдена в этом воркспейсе")
    store.update_feature(feature.id, title=title, description=description,
                         priority=priority)
    return _stamp(store.get_feature(feature.id).model_dump(), _resolve(workspace))


@mcp.tool()
async def jwu_feature_rm(ref: str, workspace: Optional[str] = None) -> dict:
    """Удалить локальную фичу. Работы по ней остаются, но теряют якорь.

    Необратимо — зови только по явной просьбе пользователя. Если фича просто больше
    не актуальна, честнее сменить статус на cancelled (jwu_feature_status).
    """
    store = _store_only(workspace)
    feature = store.get_feature(ref)
    if feature is None:
        raise ValueError(f"Фича {ref!r} не найдена в этом воркспейсе")
    linked = store.list_jobs(feature_id=feature.id)
    store.delete_feature(feature.id)
    return _stamp({"removed": feature.model_dump(), "jobs_unanchored": len(linked)},
                  _resolve(workspace))


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
    # Контекст проекта приезжает ровно в момент старта работы: даже если скилл пропустил
    # шаг с jwu_workspace_current, папки с тегами и правила окажутся в контексте ДО правок.
    payload.update(store.workspace_context())
    return _with_version(_stamp(payload, _resolve(workspace)))


@mcp.tool()
async def jwu_job_add(
    job_id: int,
    text: str,
    kind: str = "note",
    status: Optional[str] = None,
    cwd: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Добавить запись в работу (фаза/пункт/замечание/баг/прогон тестов и т.п.).

    kind — один из: phase, note, decision, remark, constraint, warning, bug,
    bug-resolved, test-pass, test-fail, todo, review, summary. status — опц. (напр. "done" у фаз).
    summary — одна строка простыми словами, что сделано за шаг («перенёс правку в 10.8,
    починил тесты»): без хешей, путей и номеров сборок. Её показывает сводка дня в Telegram
    и берёт трекинг времени — ставь в конце каждого законченного куска работы.
    К записи прикладываются ветка и коммит HEAD репозитория в cwd (по умолчанию —
    папка процесса; передай cwd, если правишь в другой папке воркспейса). Читается из
    .git, в git ничего не пишется.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    if kind not in JOB_RECORD_KINDS:
        raise ValueError(f"Недопустимый kind {kind!r}. Допустимо: {', '.join(JOB_RECORD_KINDS)}")
    store = _store_only(workspace)
    if store.get_job(job_id) is None:
        raise ValueError(f"Работа #{job_id} не найдена")
    branch, commit = gitinfo.head_state(cwd or Path.cwd())
    rec = store.add_job_record(job_id, text, kind=kind, status=status, branch=branch, commit=commit)
    return _stamp(rec.model_dump(), _resolve(workspace))


@mcp.tool()
async def jwu_job_handoff(
    job_id: int,
    offline: bool = False,
    save: bool = False,
    workspace: Optional[str] = None,
) -> dict:
    """Передача работы другой сессии: самодостаточный markdown-промпт (поле `markdown`)
    по логу работы — задача, ветка и коммит по последней записи, что сделано / что
    осталось, запреты и решения, состояние PR (блокеры, открытые задачи на комментах,
    замечания без ответа), правила воркспейса и шаги «как продолжить».

    offline=True — без сети (задача и PR из снапшотов последнего синка). save=True —
    записать выжимку заметкой-контекстом на задачу и PR (jwu_context их покажет).

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    store = _store_only(workspace)
    job = store.get_job(job_id)
    if job is None:
        raise ValueError(f"Работа #{job_id} не найдена")
    svc = None
    if not offline:
        try:
            svc = _full_svc(workspace)
        except Exception:  # noqa: BLE001 — нет доступов: соберём из памяти
            svc = None
    data = handoff.collect(store, job, svc=svc, offline=offline or svc is None)
    saved = handoff.save_context_notes(store, data) if save else []
    return _stamp({
        "job_id": job.id, "anchor": job.anchor, "branch": data.branch, "commit": data.commit,
        "context_notes": saved,
        "issue_source": data.issue_source,
        "open_tasks": [t.model_dump() for p in data.prs for t in p.open_tasks],
        "markdown": handoff.render(data),
    }, _resolve(workspace))


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
    keys: list[str] = []
    if status == "done":
        # выжимка работы уезжает заметкой-контекстом на задачу и её PR
        job = store.get_job(job_id)
        keys = handoff.save_context_notes(store, handoff.collect(store, job, offline=True))
    return _stamp({"ok": True, "job_id": job_id, "status": status, "context_notes": keys},
                  _resolve(workspace))



@mcp.tool()
async def jwu_job_close(job_id: int, summary: str, cleanup: bool = True,
                        workspace: Optional[str] = None) -> dict:
    """Закрыть работу: уборка → итог summary → статус done (+ выжимка заметкой на задачу и PR).

    summary — ОБЯЗАТЕЛЕН: одна строка простыми словами, что сделано («перенёс правку в
    10.8 и починил тесты») — её берёт трекинг времени. cleanup=True — сначала убрать всё,
    что создала работа (стенд, контейнеры, вольюмы, сети, образы, кеш, рабочие копии).
    Копия с незакоммиченным/незапушенным не удаляется: тогда работа НЕ закрывается, в ответе
    blocked=True и что мешает — разберись с пользователем (закоммить/запушь/подтверди потерю
    → jwu_job_cleanup(force=True)) и вызови снова.
    """
    from .core import jobspace

    if not summary.strip():
        raise ValueError("summary обязателен: одна строка простыми словами, что сделано")
    store = _store_only(workspace)
    if store.get_job(job_id) is None:
        raise ValueError(f"Работа #{job_id} не найдена")
    report = jobspace.cleanup(store, job_id) if cleanup else None
    if report is not None and (report.blocked or report.errors):
        return _stamp({"ok": False, "job_id": job_id, "closed": False, "cleanup": report.to_dict()},
                      _resolve(workspace))
    if report is not None and report.removed:
        store.add_job_record(job_id, "Уборка: " + "; ".join(report.removed)[:900], kind="note")
    store.add_job_record(job_id, summary.strip(), kind="summary")
    store.set_job_status(job_id, "done")
    job = store.get_job(job_id)
    keys = handoff.save_context_notes(store, handoff.collect(store, job, offline=True))
    return _stamp({"ok": True, "job_id": job_id, "closed": True, "context_notes": keys,
                   "cleanup": report.to_dict() if report else None}, _resolve(workspace))


@mcp.tool()
async def jwu_job_reopen(job_id: int, workspace: Optional[str] = None) -> dict:
    """Переоткрыть закрытую работу (done/cancelled/paused → active)."""
    store = _store_only(workspace)
    job = store.get_job(job_id)
    if job is None:
        raise ValueError(f"Работа #{job_id} не найдена")
    store.set_job_status(job_id, "active")
    store.add_job_record(job_id, f"Работа переоткрыта (была {job.status})", kind="phase")
    return _stamp({"ok": True, "job_id": job_id, "was": job.status}, _resolve(workspace))


@mcp.tool()
async def jwu_job_worktree(job_id: int, repo: str, branch: str = "", base: str = "",
                           workspace: Optional[str] = None) -> dict:
    """Рабочая копия (git worktree) репозитория для работы — ТОЛЬКО когда будут правки кода.

    Копия ложится в общую папку воркспейса (jobs.dir) с номером работы в имени
    (<repo>-job<id>-<ключ>). Уже есть — вернёт её (created=False). Перед созданием
    проверяется свободное место; мало — ошибка с подсказкой, что убрать. branch — ветка
    копии: есть — берётся, нет — создаётся от base (по умолчанию HEAD репозитория).
    Дальше ВСЯ работа с кодом — в возвращённом пути ref, не в основной папке.
    """
    from .core import jobspace

    store = _store_only(workspace)
    job = store.get_job(job_id)
    if job is None:
        raise ValueError(f"Работа #{job_id} не найдена")
    try:
        res = jobspace.add_worktree(store, job, repo, branch=branch, base=base)
    except jobspace.JobSpaceError as exc:
        raise ValueError(str(exc)) from exc
    if res.get("created"):
        store.add_job_record(job_id, f"Рабочая копия: {res['ref']} (ветка {res['meta']['branch']})", kind="note")
    return _stamp(res, _resolve(workspace))


@mcp.tool()
async def jwu_job_resource(job_id: int, kind: str, ref: str, workspace: Optional[str] = None) -> dict:
    """Запомнить, что создала работа, чтобы уборка снесла и это.

    kind: container | volume | network | image | compose | path | worktree. Docker-ресурсы
    ВСЕГДА создавай с меткой jwu.job=<id> (compose: -p jwu-job<id>) — их уборка найдёт и
    без записи; записывай то, что метку не несёт (образ, собранный без --label, каталог кеша).
    """
    from .core import jobspace

    if kind not in jobspace.RESOURCE_KINDS:
        raise ValueError(f"kind: {', '.join(jobspace.RESOURCE_KINDS)}")
    store = _store_only(workspace)
    if store.get_job(job_id) is None:
        raise ValueError(f"Работа #{job_id} не найдена")
    rid = store.add_job_resource(job_id, kind, ref)
    return _stamp({"ok": True, "id": rid, "label": jobspace.label(job_id),
                   "compose_project": jobspace.compose_project(job_id)}, _resolve(workspace))


@mcp.tool()
async def jwu_job_resources(job_id: int, workspace: Optional[str] = None) -> dict:
    """Что создала работа и ещё не убрано, плюс её docker-метка и имя compose-проекта."""
    from .core import jobspace

    store = _store_only(workspace)
    return _stamp({"label": jobspace.label(job_id), "compose_project": jobspace.compose_project(job_id),
                   "items": store.job_resources(job_id)}, _resolve(workspace))



@mcp.tool()
async def jwu_job_ports(job_id: int, names: list[str], workspace: Optional[str] = None) -> dict:
    """Свободные порты для стенда работы: names — роли («db», «redis», «web», «kafka») → порт.

    Повторный вызов отдаёт те же порты. Выданное записано за работой и освобождается при
    закрытии — два стенда параллельных работ не столкнутся. Используй ВМЕСТО портов по
    умолчанию из compose/инструкции (через переменные окружения или override-файл).
    """
    from .core import jobspace

    store = _store_only(workspace)
    if store.get_job(job_id) is None:
        raise ValueError(f"Работа #{job_id} не найдена")
    try:
        ports = jobspace.allocate_ports(store, job_id, names)
    except jobspace.JobSpaceError as exc:
        raise ValueError(str(exc)) from exc
    return _stamp({"ports": ports}, _resolve(workspace))


@mcp.tool()
async def jwu_job_cleanup(job_id: int, dry_run: bool = True, force: bool = False,
                          workspace: Optional[str] = None) -> dict:
    """Убрать всё, что создала работа: стенд, контейнеры, вольюмы, сети, образы, кеш, копии.

    По умолчанию dry_run=True — только показать список. Копии с незакоммиченным или
    незапушенным попадают в kept и не удаляются; force=True — удалить с потерей, ТОЛЬКО после
    явного «да» пользователя.
    """
    from .core import jobspace

    store = _store_only(workspace)
    if store.get_job(job_id) is None:
        raise ValueError(f"Работа #{job_id} не найдена")
    report = jobspace.cleanup(store, job_id, dry_run=dry_run, force=force)
    if not dry_run and report.removed:
        store.add_job_record(job_id, "Уборка: " + "; ".join(report.removed)[:900], kind="note")
    return _stamp(report.to_dict(), _resolve(workspace))



# Имя этой сессии Claude Code для доски: у каждой сессии свой процесс MCP-сервера, поэтому
# имя живёт здесь и одно на всю сессию — агенту не нужно ни спрашивать, ни помнить его.
_SESSION_NAME = ""


def _session_name(store: "Store", explicit: str = "") -> str:
    global _SESSION_NAME
    from .core import todo as todo_mod

    if explicit.strip():
        _SESSION_NAME = explicit.strip()
    if not _SESSION_NAME:   # воркер оркестратора: имя на доске = имя сессии для сообщений
        _SESSION_NAME = os.environ.get("JWU_SESSION_NAME", "").strip()
    if not _SESSION_NAME:
        _SESSION_NAME = todo_mod.random_session_name(todo_mod.active_session_names(store))
    _start_heartbeat(store.path)
    return _SESSION_NAME


_HEARTBEAT_STARTED = False


def _start_heartbeat(db_file: str) -> None:
    """Раз в минуту отмечать сессию в БД — доска видит, жива ли она (свой поток, своё соединение)."""
    global _HEARTBEAT_STARTED
    if _HEARTBEAT_STARTED:
        return
    _HEARTBEAT_STARTED = True
    import threading
    import time as _time

    from .core import todo as todo_mod

    def loop() -> None:
        while True:
            try:
                own = Store(db_file)
                try:
                    todo_mod.heartbeat(own, _SESSION_NAME, pid=os.getpid(), cwd=str(Path.cwd()))
                finally:
                    own.close()
            except Exception:  # noqa: BLE001 — отметка не критична
                pass
            _time.sleep(60)

    threading.Thread(target=loop, name="jwu-session-heartbeat", daemon=True).start()


@mcp.tool()
async def jwu_todo(session: str = "", refresh: Optional[bool] = None,
                   workspace: Optional[str] = None) -> dict:
    """Доска «что делать» из кеша (без сети): нумерованные пункты по группам.

    Группы: simple — мой PR с конфликтом/красной сборкой без замечаний людей; build — мой PR без
    сборки на последнем коммите и не в очереди CI (там, где CI сборки присылает) — запустить
    сборку/тесты; demo — задачи в статусах Dev at stand / Demo (настройка todo.statuses.demo):
    подготовить и провести демо, с PR и апрувами; fixes — правки
    (needs work, открытые задачи PR, возврат с QA; конфликт и сборка там тоже); todo —
    несделанные задачи на мне (не начата / в работе, без открытого PR); review — чужие PR
    ждут моего ревью. У пункта taken — уже взят (работа, кем, сколько назад).
    session — НЕ спрашивай у пользователя: пусто — у сессии уже есть (или придумается) имя
    вроде «Shiny-Gold», оно вернётся в поле session. Передавай, только если пользователь сам
    попросил назвать сессию иначе. Номера запоминаются за сессией,
    так что jwu_todo_take(numbers) берёт ровно то, что показано. markdown — готовый вывод:
    показывай его КАК ЕСТЬ — с приоритетом и статусом у каждого пункта и в этом порядке
    (в работе выше не начатого, дальше по приоритету); не переписывай и не сокращай.
    refresh: None — обновить сам, если синку больше 30 мин; True — обновить сейчас (пользователь
    попросил «обнови», «свежее», «сходи в сеть»); False — только кеш. Обновление — синк через
    демон (один на все сессии, до нескольких минут), без демона — синк этого контура.
    """
    from .core import jobspace
    from .core import todo as todo_mod
    from .core.service import _read_identity

    store = _store_only(workspace)
    login = _read_identity(store).get("user", "")
    session = _session_name(store, session)
    board = todo_mod.build(store, store.workspace_id, login, session=session)
    refreshed = None
    if refresh or (refresh is None and board.stale):
        path, ws_id = store.path, store.workspace_id

        def _refresh() -> dict:   # своё соединение: sqlite не любит чужие потоки
            own = Store(path, ws_id)
            try:
                return todo_mod.refresh(own)
            finally:
                own.close()

        refreshed = await asyncio.to_thread(_refresh)
        board = todo_mod.build(store, store.workspace_id, login, session=session)
    try:
        disk = jobspace.check_space(store, store.workspace_id, with_docker=False).summary()
    except OSError:
        disk = ""
    return _stamp({**board.to_dict(), "disk": disk, "refreshed": refreshed,
                   "markdown": todo_mod.render_md(board, disk=f"Диск: {disk}" if disk else "")},
                  _resolve(workspace))



@mcp.tool()
async def jwu_todo_release(session: str, workspace: Optional[str] = None) -> dict:
    """Освободить работы другой сессии, которая молчит (💤 на доске): они уходят в паузу, а
    их задачи снова свободны. Только по просьбе пользователя («освободи Brave-Fox»)."""
    from .core import todo as todo_mod

    store = _store_only(workspace)
    alive = todo_mod.sessions(store).get(session, {}).get("alive")
    if alive:
        raise ValueError(f"Сессия «{session}» жива — её работы не трогаю. Пусть закроет сама.")
    return _stamp({"released": todo_mod.release(store, session)}, _resolve(workspace))


@mcp.tool()
async def jwu_todo_take(numbers: Optional[list[int]] = None, group: str = "", session: str = "",
                        for_worker: str = "", workspace: Optional[str] = None) -> dict:
    """Взять пункты доски в работу этой сессией: по номерам из её последнего jwu_todo или всю группу.

    Заводит работы атомарно (две сессии одно не возьмут): цель в заголовке («Конфликт и
    красная сборка PR #1092», «Правки по ревью PR #1027», «Сделать задачу», «Ревью PR #…»),
    PR привязан, запись «Взято сессией «…»». Уже взятое — в skipped с кем и когда.
    group: simple | build | fixes | demo | todo | review — свободные пункты группы целиком.
    Дальше по каждой взятой работе — jwu-continue-job (работа уже есть), по очереди.
    for_worker — оркестратор берёт пункты для воркера с этим именем (работы помечаются им,
    имя самой сессии не меняется).
    """
    from .core import todo as todo_mod
    from .core.service import _read_identity

    if group and group not in todo_mod.GROUPS:
        raise ValueError(f"group: {', '.join(todo_mod.GROUPS)}")
    store = _store_only(workspace)
    session = for_worker.strip() or _session_name(store, session)
    result = todo_mod.take(store, store.workspace_id, _read_identity(store).get("user", ""),
                           numbers=numbers or [], group=group, session=session)
    return _stamp({**result, "session": session}, _resolve(workspace))



@mcp.tool()
async def jwu_guides(workspace: Optional[str] = None) -> dict:
    """Список инструкций: свои воркспейса и общие для всех (имя, заголовок, тег)."""
    from .core import guides as g

    store = _store_only(workspace)
    return _stamp({"guides": [i.brief() for i in g.list_guides(store, store.workspace_id)]}, _resolve(workspace))


@mcp.tool()
async def jwu_guide(name: str, workspace: Optional[str] = None) -> dict:
    """Инструкция целиком — когда правило ссылается на неё («по инструкции backend-stand»)
    или когда поднимаешь стенд / ставишь инструмент. Выполняй по шагам, как написано."""
    from .core import guides as g

    store = _store_only(workspace)
    try:
        return _stamp(g.get_guide(store, store.workspace_id, name).to_dict(), _resolve(workspace))
    except g.GuideError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
async def jwu_guide_save(name: str, title: str, text: str, tag: str = "", scope: str = "workspace",
                         user_approved: bool = False, workspace: Optional[str] = None) -> dict:
    """Сохранить инструкцию (полный пошаговый текст). scope: workspace | global (для всех воркспейсов).

    ⛔ ТОЛЬКО с явного «да» пользователя на этот текст (user_approved=True), как и правила.
    """
    from .core import guides as g

    if not user_approved:
        raise ValueError("Инструкции пишутся только с явного «да» пользователя на этот текст. "
                         "Покажи его, спроси и вызови снова с user_approved=True.")
    if scope not in ("workspace", "global"):
        raise ValueError("scope: workspace | global")
    store = _store_only(workspace)
    try:
        item = g.save_guide(store, store.workspace_id, g.Guide(name=name, title=title, text=text, tag=tag, scope=scope))
    except g.GuideError as exc:
        raise ValueError(str(exc)) from exc
    return _stamp(item.brief(), _resolve(workspace))



@mcp.tool()
async def jwu_duty_question(summary: str, author: str = "", when: str = "", where: str = "",
                            event_id: str = "", link: str = "", details: str = "",
                            workspace: Optional[str] = None) -> dict:
    """Записать вопрос с дежурства — номер «Д<N>». РАБОТА НЕ ЗАВОДИТСЯ: она появится, только когда
    вопрос возьмут (jwu_duty_question_take). То же сообщение (event_id) — тот же вопрос, детали
    обновятся. summary — суть (клиент + симптом); author/when/where — кто, когда, где;
    details — что спрашивает, что у него есть, что уже ответили, срочность. Наружу ничего не пишет.
    """
    from .core import duty

    store = _store_only(workspace)
    return _stamp(duty.question_add(store, summary=summary, author=author, when=when, where=where,
                                    event_id=event_id, link=link, details=details), _resolve(workspace))


@mcp.tool()
async def jwu_duty_questions(include_closed: bool = False, workspace: Optional[str] = None) -> dict:
    """Вопросы дежурства: открытые и взятые (с работой), по просьбе — и закрытые."""
    from .core import duty

    store = _store_only(workspace)
    return _stamp({"questions": duty.questions(store, include_closed=include_closed)}, _resolve(workspace))


@mcp.tool()
async def jwu_duty_question_take(question: str, workspace: Optional[str] = None) -> dict:
    """«Берём Д12» — взять вопрос в работу этой сессией: заводится работа «🛟 Дежурство: …» с
    источником и деталями; дальше — jwu-continue-job по ней (разбор, ответ голосом, закрытие)."""
    from .core import duty

    store = _store_only(workspace)
    try:
        return _stamp(duty.question_take(store, question, session=_session_name(store)), _resolve(workspace))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
async def jwu_duty_question_close(question: str, workspace: Optional[str] = None) -> dict:
    """«Закрой вопрос Д12», «не берём» — закрыть, не заводя работы: ни итога, ни строки в учёте
    времени. Если вопрос уже взят и над ним работали — закрывай его работу через jwu-close-job."""
    from .core import duty

    store = _store_only(workspace)
    try:
        return _stamp(duty.question_close(store, question), _resolve(workspace))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
async def jwu_disk(path: Optional[str] = None, workspace: Optional[str] = None) -> dict:
    """Свободное место (папка рабочих копий или path), docker и неубранное у закрытых работ.

    ok=False — места меньше порога (disk.min_free_gb, 20 ГБ): НЕ создавай копий, не поднимай
    стенд и не собирай образы — сначала скажи пользователю и предложи убрать (leftovers,
    docker «можно освободить»). Docker и агент виснут на полном диске.
    """
    from .core import jobspace

    store = _store_only(workspace)
    return _stamp(jobspace.check_space(store, store.workspace_id, path).to_dict(), _resolve(workspace))

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


@mcp.tool()
async def jwu_worklog_links(date: str, keys: Optional[list[str]] = None,
                            workspace: Optional[str] = None) -> dict:
    """Ссылки на мои ворклоги за день (поиск в Jira; SDESK — если среди keys есть его задачи).
    Показывай их пользователю ПОСЛЕ трека — чтобы он сам посмотрел, что записалось."""
    from .core import timechain

    ws = _resolve(workspace)
    store = _store_only(workspace)
    cfg = ws_mod.config_for_workspace(store, ws)
    return _stamp({"links": timechain.worklog_links(cfg, date, keys or [])}, ws)


@mcp.tool()
async def jwu_worklog_timezone(tz: Optional[str] = None, workspace: Optional[str] = None) -> dict:
    """Часовой пояс для трекинга времени (настройка контура worklog.timezone).

    Без `tz` — прочитать (пусто — не задан: спроси пользователя один раз). С `tz` —
    сохранить: IANA-имя (Europe/Moscow) или короткое («МСК»). Локальная запись.
    """
    from .core import timechain

    ws = _resolve(workspace)
    store = _store_only(workspace)
    value = timechain.set_timezone(store, ws.id, tz) if tz else timechain.get_timezone(store, ws.id)
    return _stamp({"timezone": value}, ws)


@mcp.tool()
async def jwu_worklog_chain(
    items: list[dict],
    start: str,
    on: Optional[str] = None,
    tz: Optional[str] = None,
    dry_run: bool = True,
    workspace: Optional[str] = None,
) -> dict:
    """Ворклоги ЦЕПОЧКОЙ: каждый начинается ровно там, где закончился предыдущий.

    items — [{"key": "PROJ-1", "time": "1h 30m", "comment": "…"}, …] по порядку;
    start — «10:00»; on — YYYY-MM-DD (по умолчанию сегодня); tz — пояс (по умолчанию
    настройка контура, см. jwu_worklog_timezone). dry_run=True (по умолчанию) — только
    план: по строке «с · по · длительность», итог, `overlaps` (пересечения с уже
    затреканным за день) и `existing_ends_at` (где кончается затреканное). Показать план
    пользователю, и только после «да» — dry_run=False: ВНЕШНЯЯ запись в Jira по
    порядку, стоп на первой ошибке (`failed`, записанное — в `written`).
    """
    from datetime import date as _date

    svc = _require_jira(_full_svc(workspace))
    day = on or _date.today().isoformat()
    return _stamp(svc.worklog_chain(items, start=start, day=day, tz=tz or "", dry_run=dry_run),
                  _resolve(workspace))


@mcp.tool()
async def jwu_auth_refresh(workspace: Optional[str] = None) -> dict:
    """Переподключиться к Jira/SDESK/Bitbucket свежими кредами и проверить доступ.

    Сервер живёт всю сессию Claude Code и держит соединения открытыми, поэтому креды,
    прочитанные при первом обращении, живут вместе с ним. Протухшую СЕССИЮ Jira клиент
    переустанавливает сам; а вот если пользователь поменял пароль или PAT (`jwu configure`),
    сервер об этом не узнает — соединения нужно пересоздать этим инструментом.

    Зови его, когда любой инструмент вернул 401 («токен невалиден», «сессия не
    восстановилась»), и повтори исходный вызов. Ответ — тот же отчёт, что у
    `jwu auth check`: по системам, у каждой `ok` либо `error`. Ничего не пишет
    и никаких секретов не показывает.
    """
    ws = _resolve(workspace)
    for cache in (_full, _builds):
        svc = cache.pop(ws.id, None)
        if svc is not None:
            try:
                svc.close()
            except Exception:  # noqa: BLE001 — соединение и так считаем мёртвым
                pass
    try:
        checked = _full_svc(workspace).auth_check()
    except Exception as exc:  # noqa: BLE001 — логин может упасть прямо здесь
        return _stamp({"ok": False, "error": str(exc)}, ws)
    ok = all(item.get("ok") for item in checked.values()) if checked else True
    return _stamp({"ok": ok, "auth": checked}, ws)


@mcp.tool()
async def jwu_worklogs(
    on: str,
    keys: Optional[list[str]] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Мои worklog'и за дату — что уже затрекано. Без записи.

    Без `keys` отвечает на вопрос «что вообще затрекано за такой-то день»: задачи
    находятся сами по JQL (worklogAuthor/worklogDate) в Jira и SDESK. Со списком ключей
    смотрит только их. Обязательная проверка перед jwu_worklog, чтобы не задвоить время.

    on — дата YYYY-MM-DD. Ответ: {KEY: [{id, time, seconds, comment, started}, …]};
    id — для jwu_worklog_edit / jwu_worklog_delete.
    """
    svc = _require_jira(_full_svc(workspace))
    targets = list(keys or []) or svc.my_worklog_keys_on(on)
    return svc.my_worklogs_on(targets, on)


@mcp.tool()
async def jwu_comment(
    key: str,
    text: str,
    client_facing: bool = False,
    dry_run: bool = True,
    workspace: Optional[str] = None,
) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: комментарий в задачу Jira/SDESK. Только по явному подтверждению.

    Это НЕ локальная заметка (для неё есть jwu_note) — текст уходит в саму Jira и
    править и удалять — jwu_comment_edit / jwu_comment_delete. Порядок обязателен: сначала dry_run=True (по умолчанию)
    — вернётся текст и признак `client_facing`; ПОКАЗАТЬ текст пользователю целиком,
    дождаться явного «да» и только потом повторить с dry_run=False.

    Комментарий во внутренней задаче читает команда. Комментарий в SDESK **читает
    КЛИЕНТ** — для таких ключей нужен ещё и client_facing=True, и подтверждение
    пользователя должно быть получено именно на отправку клиенту, а не «вообще».
    """
    svc = _require_jira(_full_svc(workspace))
    to_client = svc.key_is_client_facing(key)
    if dry_run:
        return {"ok": False, "dry_run": True, "key": key, "text": text,
                "client_facing": to_client,
                "hint": ("Задача SDESK: текст увидит клиент — показать его пользователю "
                         "дословно и получить согласие на отправку КЛИЕНТУ"
                         if to_client else
                         "Показать текст пользователю и получить подтверждение")}
    result = svc.add_comment(key, text, client_facing=client_facing)
    return {"ok": True, "key": key, "id": result.get("id", ""), "client_facing": to_client}


@mcp.tool()
async def jwu_branches(key: Optional[str] = None, all_branches: bool = False,
                       limit: int = 40, workspace: Optional[str] = None) -> list[dict]:
    """Где лежит код по задаче: локальные ветки во всех репозиториях воркспейса, свежие
    первыми — репозиторий, ветка, коммит, дата, апстрим, ahead/behind, `current`
    (checkout'нута в клоне), `worktree` (путь, если checkout'нута там), `dirty`
    (незакоммиченных файлов там, где checkout'нута), `task_key` из имени, `note` —
    закреплённый статус ветки. key — ключ задачи или кусок имени; без key — все
    ветки с ключом задачи в имени; all_branches — вообще все. limit — сколько самых
    свежих вернуть (в больших контурах веток сотни; без key бери по ключу, а не всё).
    Только чтение git, без сети.

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке (текущий
    можно узнать через jwu_workspace_current).
    """
    from .core import branches as br

    store = _store_only(workspace)
    items = br.collect(br.workspace_roots(store), key=key, all_branches=all_branches)
    items = items[: max(1, int(limit))]
    status = store.status_notes([b.branch for b in items])
    return [{**b.as_dict(), "note": status.get(b.branch, ""), "summary": br.summary_line(b)}
            for b in items]


@mcp.tool()
async def jwu_task_branches(
    key: str,
    branch_patterns: Optional[list[str]] = None,
    workspace: Optional[str] = None,
) -> dict:
    """В какие релизные ветки доехал фикс задачи, а в какие нет. Без записи.

    Отвечает на дежурный вопрос «баг известный, но есть ли фикс в версии клиента»:
    коммиты и PR берутся из dev-панели задачи, ветки — из локальных клонов репозиториев
    воркспейса (git branch --contains). Ничего не фетчит: если клон давно не обновлялся,
    свежих веток в нём может не быть.

    `repos[].reached` — ветки, куда доехали ВСЕ коммиты задачи; `partial` — куда доехала
    часть (самый опасный случай); `missing` — где фикса нет. Ветки идут от свежих к
    старым. `dev_ok=false` значит, что dev-панель ответила не полностью и пустой список
    коммитов означает «не знаем», а не «коммитов нет».

    branch_patterns — свои шаблоны релизных веток (по умолчанию release/*, hotfix/*,
    master, main, develop).
    """
    svc = _require_jira(_full_svc(workspace))
    return svc.task_branch_reach(key, patterns=branch_patterns)


@mcp.tool()
async def jwu_issue_create_meta(project: str, workspace: Optional[str] = None) -> dict:
    """Что можно создать в проекте: типы задач и обязательные поля (createmeta). Без записи.

    Читающий инструмент: показывает, какие типы задач доступны в проекте и какие поля
    он требует. Полезно вызвать ДО jwu_issue_create, если проект незнакомый — у разных
    проектов обязательные поля разные. Пустой `issue_types` — проекта нет либо нет прав
    на создание в нём.
    """
    svc = _require_jira(_full_svc(workspace))
    meta = svc.tasks_client.create_meta(project) if svc.tasks_client else {}
    return {
        "project": meta.get("key", "") or project,
        "issue_types": [
            {
                "name": t.get("name", ""),
                "subtask": bool(t.get("subtask")),
                "required": [
                    f.get("name", fid)
                    for fid, f in (t.get("fields") or {}).items()
                    if f.get("required") and fid not in ("project", "issuetype", "reporter")
                ],
            }
            for t in (meta.get("issuetypes") or [])
        ],
    }


@mcp.tool()
async def jwu_issue_create(
    project: str,
    summary: str,
    description: Optional[str] = None,
    issuetype: str = "Task",
    priority: Optional[str] = None,
    assignee: Optional[str] = None,
    labels: Optional[list[str]] = None,
    components: Optional[list[str]] = None,
    fix_versions: Optional[list[str]] = None,
    parent: Optional[str] = None,
    from_job: Optional[int] = None,
    dry_run: bool = True,
    workspace: Optional[str] = None,
) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: создать задачу в Jira/SDESK. Только по явному подтверждению.

    Создаёт РЕАЛЬНУЮ задачу, которую увидит команда (а в SDESK — клиент). Порядок
    обязателен: сначала dry_run=True (по умолчанию) — вернётся итоговый payload и
    результат проверки по createmeta, ничего не отправляя; ПОКАЗАТЬ этот текст
    пользователю целиком, дождаться явного «да» и только потом повторить вызов с
    dry_run=False. Без подтверждения пользователя dry_run=False НЕ вызывать.

    Поля Jira Server: description — обычный wiki-текст (не ADF), assignee и другие
    пользователи задаются логином, issuetype/priority/components/fixVersions — именами.
    parent — для подзадач. Инстанс выбирается по префиксу ключа проекта. from_job — id
    работы jwu: описание собирается из её лога (фазы, баги, ревью, прогоны тестов) как
    ЧЕРНОВИК, который пользователь правит перед отправкой; вместе с description не
    используется.

    Ответ: `ok` (создано ли), `fields` (что уходит/ушло в Jira), `check` (проверка
    createmeta: `problems` — почему нельзя отправлять), `issue` (ключ и ссылка).

    workspace — воркспейс jwu; по умолчанию определяется по рабочей папке.
    """
    svc = _require_jira(_full_svc(workspace))
    return svc.create_issue(
        project, summary,
        description=description, issuetype=issuetype, priority=priority,
        assignee=assignee, labels=labels, components=components,
        fix_versions=fix_versions, parent=parent, from_job=from_job, dry_run=dry_run,
    )


@mcp.tool()
async def jwu_issue_similar(
    project: str,
    summary: str,
    months: int = 12,
    workspace: Optional[str] = None,
) -> list[dict]:
    """Похожие по тексту задачи проекта — проверка на дубль. Без записи.

    Разбираемый баг вполне может оказаться точной копией задачи годичной давности:
    проверять стоит ДО того, как заводить новую (jwu_issue_create с dry_run=True делает
    этот же поиск сам и кладёт результат в поле `similar`). Поиск нечёткий — это
    подсказка человеку, решение за ним.
    """
    svc = _require_jira(_full_svc(workspace))
    return svc.find_similar_issues(project, summary, months=months)


@mcp.tool()
async def jwu_issue_transitions(key: str, workspace: Optional[str] = None) -> list[dict]:
    """Какие переходы по процессу доступны задаче прямо сейчас. Без записи.

    Набор зависит от ТЕКУЩЕГО статуса и схемы проекта — спрашивать нужно перед каждым
    переводом, а не помнить. Ответ: [{id, name, to}], где `to` — статус после перехода.
    """
    svc = _require_jira(_full_svc(workspace))
    return svc.transitions(key)


@mcp.tool()
async def jwu_issue_transition(
    key: str,
    target: str,
    workspace: Optional[str] = None,
) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: перевести задачу по процессу. Только по явному подтверждению.

    Статус задачи видит вся команда, а обратный переход из нового статуса может быть
    недоступен. Сначала jwu_issue_transitions — показать пользователю доступные переходы,
    дождаться выбора и «да», и только потом звать этот инструмент.

    target — имя перехода (или его id). Неизвестное имя вернёт ошибку со списком
    доступных, ничего не изменив.
    """
    svc = _require_jira(_full_svc(workspace))
    return {"ok": True, **svc.transition(key, target)}


@mcp.tool()
async def jwu_issue_attach(
    key: str,
    paths: list[str],
    dry_run: bool = True,
    workspace: Optional[str] = None,
) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: приложить файлы к задаче. Только по явному подтверждению.

    Вложение видно всем, у кого есть доступ к задаче (в SDESK — клиенту), и удалить его
    через jwu нельзя. Сначала dry_run=True (по умолчанию) — вернётся список файлов с
    размерами; показать его пользователю, дождаться «да», потом dry_run=False.

    ВАЖНО: har-файлы, логи и выгрузки часто содержат токены, куки и персональные данные.
    Прежде чем прикладывать — скажи пользователю, что именно уедет в задачу.
    """
    svc = _require_jira(_full_svc(workspace))
    if dry_run:
        files = []
        for name in paths:
            path = Path(name).expanduser()
            files.append({"path": str(path), "exists": path.is_file(),
                          "size": path.stat().st_size if path.is_file() else 0})
        return {"ok": False, "dry_run": True, "key": key, "files": files,
                "client_facing": svc.key_is_client_facing(key),
                "hint": "Показать список пользователю и получить подтверждение"}
    return {"ok": True, "key": key, "attachments": svc.add_attachment(key, paths)}


@mcp.tool()
async def jwu_issue_link(
    inward: str,
    outward: str,
    link_type: str = "Relates",
    workspace: Optional[str] = None,
) -> dict:
    """ВНЕШНЯЯ ЗАПИСЬ: связать две задачи в Jira. Только по явному подтверждению.

    Направление как в Jira: link_type применяется от `inward` к `outward`
    («PROJ-2 blocks PROJ-1» — inward=PROJ-2, outward=PROJ-1, link_type=Blocks).
    Неизвестный тип связи вернёт список доступных, а не создаст мусор. Обе задачи
    должны жить в одном инстансе (Jira ↔ SDESK связать нельзя).
    """
    svc = _require_jira(_full_svc(workspace))
    return {"ok": True, **svc.link_issues(inward, outward, link_type)}


# --------------------------------------------------------------------------- #
# Очередь задач сессии
# --------------------------------------------------------------------------- #


@mcp.tool()
async def jwu_queue(
    status: Optional[str] = None,
    mode: Optional[str] = None,
    all_items: bool = False,
    workspace: Optional[str] = None,
) -> dict:
    """Очередь задач сессии: пункты по порядку + сводка (slots, slots_free, main_running).

    По умолчанию — только queued и running; all_items=True — вместе с закрытыми.
    status — queued | running | done | failed | cancelled; mode — main | parallel.
    Без сети. Скилл **jwu-queue** описывает, как сессия с ней работает.
    """
    store = _store_only(workspace)
    items = store.queue_list(status=status, mode=mode, open_only=not all_items and not status)
    payload = {**store.queue_counts(), "items": [i.model_dump() for i in items]}
    return _stamp(payload, _resolve(workspace))


@mcp.tool()
async def jwu_queue_add(
    text: str,
    mode: str = "main",
    job_id: Optional[int] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Положить пункт в КОНЕЦ очереди и вернуться к текущему делу.

    mode: main (ДЕФОЛТ) — выполнит сам основной поток, подряд, после текущего атомарного
    шага; parallel — субагент, когда освободится слот. ⛔ parallel ставь ТОЛЬКО если
    пользователь в этой просьбе явно сказал «параллельно» / «отдай агенту»; сам так не
    решай, даже если пункт выглядит независимым. job_id — работа, в журнал которой
    уедет итог. Локальная память, подтверждения не требует.
    """
    if mode not in QUEUE_MODES:
        raise ValueError(f"Недопустимый режим {mode!r}. Допустимо: {', '.join(QUEUE_MODES)}")
    ws = _resolve(workspace)
    return _stamp(_store_only(workspace).queue_add(text, mode=mode, job_id=job_id).model_dump(), ws)


@mcp.tool()
async def jwu_queue_take(
    mode: Optional[str] = None,
    worker: str = "",
    workspace: Optional[str] = None,
) -> dict:
    """Взять следующий пункт в работу (queued → running), атомарно.

    mode="main" — следующий пункт основного потока (в нём выполняется один пункт за
    раз); mode="parallel" — пункт для субагента, если есть свободный слот; без mode —
    первый по порядку. Ответ: {"item": …} либо {"item": null, "busy": bool, "reason": …}:
    busy=True — занято (закрой текущий пункт или подожди слот), busy=False — пусто.
    worker — метка исполнителя (имя субагента), видна в списке.
    """
    if mode is not None and mode not in QUEUE_MODES:
        raise ValueError(f"Недопустимый режим {mode!r}. Допустимо: {', '.join(QUEUE_MODES)}")
    ws = _resolve(workspace)
    store = _store_only(workspace)
    try:
        item = store.queue_take(mode=mode, worker=worker)
    except QueueBusy as exc:
        return _stamp({"item": None, "busy": True, "reason": str(exc)}, ws)
    if item is None:
        return _stamp({"item": None, "busy": False, "reason": "подходящих пунктов нет"}, ws)
    return _stamp({"item": item.model_dump(), "busy": False, "reason": ""}, ws)


@mcp.tool()
async def jwu_queue_finish(
    item_id: int,
    status: str = "done",
    result: str = "",
    workspace: Optional[str] = None,
) -> dict:
    """Закрыть пункт: done | failed | cancelled — либо вернуть в очередь: queued (release).

    result — итог одной строкой (что сделано / почему не вышло). Если у пункта есть
    job_id, итог стоит продублировать записью в работу (jwu_job_add) — очередь не журнал.
    """
    store = _store_only(workspace)
    if status == "queued":
        item = store.queue_release(item_id)
    elif status in ("done", "failed", "cancelled"):
        item = store.queue_finish(item_id, status, result=result)
    else:
        raise ValueError("status — done | failed | cancelled | queued")
    return _stamp(item.model_dump(), _resolve(workspace))


@mcp.tool()
async def jwu_queue_edit(
    item_id: int,
    text: Optional[str] = None,
    mode: Optional[str] = None,
    job_id: Optional[int] = None,
    position: Optional[int] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Править пункт: текст, режим (только у ещё не взятых), работа, место в очереди.

    position — место среди ожидающих, 1 — первым. Передавай только то, что меняешь.
    """
    store = _store_only(workspace)
    item = store.queue_update(item_id, text=text, mode=mode, job_id=job_id)
    if position is not None:
        store.queue_move(item_id, position)
        item = store.queue_get(item_id) or item
    return _stamp(item.model_dump(), _resolve(workspace))


@mcp.tool()
async def jwu_queue_clear(
    include_queued: bool = False,
    workspace: Optional[str] = None,
) -> dict:
    """Удалить закрытые пункты (done / failed / cancelled); include_queued=True — и ожидающие.

    Ожидающие сносить только по явной просьбе пользователя. Выполняемые не трогаются.
    """
    statuses: tuple[str, ...] = ("done", "failed", "cancelled")
    if include_queued:
        statuses += ("queued",)
    n = _store_only(workspace).queue_clear(statuses=statuses)
    return _stamp({"ok": True, "removed": n}, _resolve(workspace))


@mcp.tool()
async def jwu_queue_slots(
    slots: Optional[int] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Лимит параллельных слотов воркспейса (настройка queue.slots, по умолчанию 3).

    Без slots — показать; с slots — задать (≥ 1). Менять по просьбе пользователя.
    """
    store = _store_only(workspace)
    if slots is not None:
        store.set_queue_slots(slots)
    return _stamp(store.queue_counts(), _resolve(workspace))


@mcp.tool()
async def jwu_addons() -> dict:
    """Установленные аддоны jwu и что они предлагают после шагов jwu (after: {скилл: подсказка}).

    После jwu-track-time и других шагов загляни сюда: если у аддона есть продолжение для
    этого шага — предложи его пользователю одной строкой (сам не запускай).
    """
    from .core import addons

    return {"addons": addons.describe()}


def _load_addon_mcp() -> None:
    from .core import addons

    for addon in addons.load():
        addon.call("register_mcp", mcp)


_load_addon_mcp()


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
