"""Передача работы другой сессии: самодостаточный промпт по логу работы.

Скилл jwu-resume-job собирал картину руками из десятка вызовов, и каждая сессия делала
это по-своему. Здесь один текст, который можно скормить новой сессии целиком: задача,
где стоять в git, что сделано, что осталось, какие запреты и решения действуют, что с
PR (конфликт, сборка, открытые задачи на комментах, незакрытые замечания), правила
контура и — в конце — как продолжать.

Источник — локальная память jwu. Сеть используется только по желанию (``svc`` задан и
``offline=False``): карточка задачи и живое состояние PR. Без сети берётся то, что
лежит в снапшотах последнего синка, и это честно помечается.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from .dates import fmt_dt
from .models import Issue, Job, JobRecord, PR, PRComment, PRTask

if TYPE_CHECKING:
    from .service import Service
    from .store import Store

# Статусы, в которых задача считается «на тестах» — те же, что в эвристиках day-analyze.
_TESTING_MARKERS = ("TEST", "ТЕСТ", "QA")


@dataclass
class PRState:
    """Живое (или из снапшота) состояние PR для передачи работы."""

    ref: str
    pr: Optional[PR] = None
    open_tasks: list[PRTask] = field(default_factory=list)
    unresolved_remarks: list[PRComment] = field(default_factory=list)
    source: str = "снапшот"  # снапшот | сеть | нет данных


@dataclass
class Handoff:
    job: Job
    issue: Optional[Issue] = None
    issue_source: str = "нет данных"
    branch: str = ""
    commit: str = ""
    prs: list[PRState] = field(default_factory=list)
    rules_md: str = ""
    paths: list[dict] = field(default_factory=list)
    workspace: str = ""


def _last_git_state(records: list[JobRecord]) -> tuple[str, str]:
    for r in reversed(records):
        if r.branch or r.commit:
            return r.branch, r.commit
    return "", ""


def _snapshot_issue(store: "Store", key: str) -> Optional[Issue]:
    if not key:
        return None
    for issue in store.latest_issues(None):
        if issue.key == key:
            return issue
    return None


def _snapshot_pr(store: "Store", pr_id: int, project: str, repo: str) -> Optional[PR]:
    for pr in store.latest_prs(None):
        if pr.id == pr_id and (not project or pr.project == project) and (not repo or pr.repository == repo):
            return pr
    return None


def _open_bugs(records: list[JobRecord]) -> list[JobRecord]:
    """Баги без парного bug-resolved (по порядку: n-й resolved закрывает n-й bug)."""
    bugs = [r for r in records if r.kind == "bug"]
    resolved = sum(1 for r in records if r.kind == "bug-resolved")
    return bugs[resolved:]


def collect(store: "Store", job: Job, *, svc: "Optional[Service]" = None,
            offline: bool = False) -> Handoff:
    """Собрать данные для передачи: память всегда, сеть — если есть сервис и не offline."""
    ws = store.get_workspace(store.workspace_id)
    out = Handoff(job=job, workspace=ws.slug if ws else "")
    out.branch, out.commit = _last_git_state(job.records)

    # задача: сеть → снапшот
    if job.task_key:
        if svc is not None and not offline and svc.tasks_client is not None:
            try:
                out.issue = svc.issue(job.task_key)
                out.issue_source = "сеть"
            except Exception:  # noqa: BLE001 — сеть не обязательна
                out.issue = None
        if out.issue is None:
            out.issue = _snapshot_issue(store, job.task_key)
            out.issue_source = "снапшот" if out.issue else "нет данных"

    # PR: сеть (детали, задачи, комменты) → снапшот
    for link in job.prs:
        ref = f"{link.project}/{link.repo}#{link.pr_id}" if link.project else f"#{link.pr_id}"
        state = PRState(ref=ref)
        if svc is not None and not offline and svc.pr_client is not None:
            try:
                detail = svc.pr_detail(link.project or None, link.repo or None, link.pr_id)
                state.pr = detail.pr
                state.open_tasks = [t for c in detail.comments for t in c.tasks if not t.resolved]
                # незакрытые замечания: верхнеуровневые inline-комменты не автора PR,
                # у которых нет ответа (ответы = записи глубже)
                me = detail.pr.author
                by_id = {c.id: c for c in detail.comments}
                replied: set[str] = set()
                prev_top: Optional[PRComment] = None
                for c in detail.comments:
                    if c.depth == 0:
                        prev_top = c
                    elif prev_top is not None and c.author == me:
                        replied.add(prev_top.id)
                state.unresolved_remarks = [
                    c for c in detail.comments
                    if c.depth == 0 and c.file and c.author != me and c.id not in replied
                    and c.id in by_id
                ]
                state.source = "сеть"
            except Exception:  # noqa: BLE001
                state.pr = None
        if state.pr is None:
            state.pr = _snapshot_pr(store, link.pr_id, link.project, link.repo)
            state.source = "снапшот" if state.pr else "нет данных"
        out.prs.append(state)

    ctx = store.workspace_context()
    out.rules_md = ctx.get("rules_md", "") or ""
    out.paths = ctx.get("paths", []) or []
    return out


def _short(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _pr_block(state: PRState) -> list[str]:
    L = [f"### PR {state.ref} ({state.source})"]
    pr = state.pr
    if pr is None:
        L.append("- данных нет: PR не синкался и сеть недоступна")
        return L
    blockers = []
    if pr.conflicted:
        blockers.append("конфликт")
    if pr.build_state == "FAILED":
        blockers.append("красный билд")
    if pr.tasks_open:
        blockers.append(f"открытых задач {pr.tasks_open}")
    if any((r.status or "") == "NEEDS_WORK" for r in pr.reviewers):
        blockers.append("needs work")
    L.append(f'- "{pr.title}" {pr.source_branch} → {pr.target_branch}, {pr.state}')
    reviewers = ", ".join(f"{r.display_name or r.name}:{r.status or '—'}" for r in pr.reviewers)
    L.append(f"- блокеры: {', '.join(blockers) if blockers else 'нет'}; ревью: {reviewers or '—'}")
    if pr.build_state:
        L.append(f"- сборка: {pr.build_state}")
    if state.open_tasks:
        L.append("- открытые задачи на комментах (чек-лист правок):")
        L += [f"  - [ ] #{t.id} {t.text}" for t in state.open_tasks]
    if state.unresolved_remarks:
        L.append("- замечания без ответа:")
        L += [f"  - {c.file}:{c.line} {c.author}: {_short(c.text, 160)}" for c in state.unresolved_remarks[:12]]
    return L


def render(h: Handoff) -> str:
    """Markdown-промпт для следующей сессии."""
    job = h.job
    L: list[str] = []
    anchor = job.task_key or job.feature_key or f"#{job.id}"
    L.append(f"# Передача работы #{job.id}: {anchor} — {job.title or '—'}")
    L.append(f"Воркспейс: {h.workspace or '—'} · статус работы: {job.status} · "
             f"обновлена: {fmt_dt(job.updated_at) if job.updated_at else '—'}")
    L.append("")
    L.append("Это самодостаточный контекст для продолжения работы в новой сессии Claude Code. "
             "Прочитай целиком, затем подтверди у пользователя следующий шаг и веди лог через "
             "скилл jwu-track-job. Код правь только в папках воркспейса.")

    # задача
    L.append("")
    L.append(f"## Задача ({h.issue_source})")
    if h.issue is not None:
        i = h.issue
        L.append(f"- {i.key} [{i.status}] ({i.priority or '—'}) assignee: {i.assignee or '—'} — {i.summary}")
        if i.description:
            L.append(f"- описание: {_short(i.description, 700)}")
        recent = [c for c in i.comments][-3:]
        if recent:
            L.append("- последние комментарии:")
            L += [f"  - {c.author}: {_short(c.body, 200)}" for c in recent]
    elif job.task_key:
        L.append(f"- {job.task_key}: карточка недоступна (нет снапшота и сети)")
    else:
        L.append(f"- работа без задачи трекера; якорь: {anchor}")

    # git
    L.append("")
    L.append("## Где стоять в git")
    if h.branch or h.commit:
        L.append(f"- ветка `{h.branch or '(detached)'}`, коммит `{h.commit or '?'}` — "
                 f"по последней записи работы; проверь `git status`, локальные правки могли остаться незакоммиченными")
    else:
        L.append("- в записях работы нет git-состояния (записи делались вне репозитория или до 1.14)")
    if h.paths:
        L.append("- папки воркспейса: " + "; ".join(
            f"{p['path']}" + (f" ({', '.join(p['tags'])})" if p.get("tags") else "") for p in h.paths[:8]))

    # прогресс
    recs = job.records
    done_phases = [r for r in recs if r.kind == "phase" and (r.status or "").lower() == "done"]
    open_phases = [r for r in recs if r.kind == "phase" and (r.status or "").lower() != "done"]
    todos = [r for r in recs if r.kind == "todo" and (r.status or "").lower() != "done"]
    bugs = _open_bugs(recs)
    tests = [r for r in recs if r.kind in ("test-pass", "test-fail")]
    L.append("")
    L.append("## Что сделано")
    L += [f"- ✅ {r.text}" for r in done_phases] or ["- пока ничего не отмечено сделанным"]
    if tests:
        last = tests[-1]
        L.append(f"- тесты (последний прогон): {'зелёные' if last.kind == 'test-pass' else 'КРАСНЫЕ'} — {_short(last.text, 160)}")
    L.append("")
    L.append("## Что осталось")
    rest = [f"- ⏳ фаза: {r.text}" for r in open_phases] + [f"- 📌 {r.text}" for r in todos] \
        + [f"- 🐛 не исправлен: {r.text}" for r in bugs]
    L += rest or ["- открытых фаз, todo и багов в логе нет"]

    guards = [r for r in recs if r.kind == "constraint"]
    decisions = [r for r in recs if r.kind == "decision"]
    if guards or decisions:
        L.append("")
        L.append("## Запреты и решения (действуют)")
        L += [f"- ⛔ {r.text}" for r in guards]
        L += [f"- 🧭 {r.text}" for r in decisions]
    reviews = [r for r in recs if r.kind == "review"]
    if reviews:
        L.append("")
        L.append("## Последнее ревью")
        L.append(_short(reviews[-1].text, 900))

    # PR
    if h.prs:
        L.append("")
        L.append("## Pull requests")
        for state in h.prs:
            L += _pr_block(state)

    # правила
    if h.rules_md.strip():
        L.append("")
        L.append("## Правила воркспейса")
        L.append(h.rules_md.strip())

    L.append("")
    L.append("## Как продолжить")
    steps = []
    if h.paths:
        steps.append(f"перейди в папку воркспейса ({h.paths[0]['path']})")
    if h.branch:
        steps.append(f"`git checkout {h.branch}` и `git status`")
    steps.append("прочитай лог работы полностью, если нужны детали: `jwu job show "
                 f"{job.id}` / `jwu_jobs`")
    if any(s.open_tasks for s in h.prs):
        steps.append("закрывай открытые задачи PR по мере правок (скилл jwu-pr-task, после подтверждения)")
    steps.append("каждую фазу/баг/прогон тестов записывай: `jwu job add "
                 f"{job.id} --kind …` (скилл jwu-track-job)")
    steps.append("в git ничего от jwu не оставляй: ветка и коммит читаются, но не пишутся")
    L += [f"{n}. {s}" for n, s in enumerate(steps, 1)]
    return "\n".join(L)
