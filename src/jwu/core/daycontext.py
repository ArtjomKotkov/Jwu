"""Контекст дневного анализа: промпт, строки PR и рендер markdown/JSON.

Живёт в core, а не в CLI, чтобы MCP-инструмент ``jwu_day_context`` и команда
``jwu action day-analyze`` отдавали ровно один и тот же текст.
"""

from __future__ import annotations

from .dates import fmt_ago, fmt_dt
from .models import PR, pr_note_key
from .service import DayContext

_DAY_PROMPT = """## Что нужно сделать
Составь КРАТКУЮ сводку-план рабочего дня по данным ниже (без глубокого погружения — только суть, я разберусь сам).
Сочетай ДВА среза: дельты (что изменилось) И текущее состояние PR/задач (даже без свежей дельты состояние может требовать действия).
Для каждого моего PR/задачи — конкретный следующий шаг. Ориентиры:
- PR `состояние: конфликт` → поправить merge-конфликт (приоритет, если апрувы собраны).
- PR `состояние: красный билд` (или дельта `build_failed`) → разобрать падение сборки (`jwu build <PR>`), починить; `build_fixed` — сборка снова зелёная.
- PR `апрувы собраны`, без NEEDS_WORK/конфликта/красного билда, а задача не на тестах → перевести задачу на тесты.
- PR `открытые задачи: N` (задачи на комментах = чек-лист правок) → закрыть их: `jwu pr <PR>` показывает задачи под комментами.
- PR `есть NEEDS_WORK` / новые комменты → ответить/поправить по замечаниям.
- PR `нет ревьюверов` → назначить ревьюверов; `ждёт апрувов` давно → пнуть.
- Дельта `returned_from_testing` (задачу вернули с тестов) и `qa_comment` (комментарий тестировщика в задаче на тестах) → это доработка: разобрать, что нашли, и запланировать.
- `заметка: «…»` у задачи/PR — закреплённая status-заметка (jwu note … --kind status): это и есть «почему висит», учитывай её раньше эвристик; обновить — `jwu note <ключ> "…" --kind status`.
- Раздел «Застряло» (пороги — настройка воркспейса `jwu workspace thresholds`) → назови КАЖДЫЙ пункт явно: «PR … застрял N дн», «задача … на тестах N дн» и что с ним делать (пнуть / мержить / закрыть).
- Упоминание с пометкой `· новое` → прочитать, понять, что от меня хотят, и ответить.
- База — дельты: новые комменты, смена статуса, апрувы, новые PR; `resolved` → закрыть работу.
Пиши сжато, маркерами, без воды; группируй по действиям."""


def _pr_state(pr: PR) -> str:
    """Короткая готовность PR для эвристик: что мешает мержу прямо сейчас."""
    if pr.conflicted:
        return "конфликт"
    if pr.build_state == "FAILED":
        return "красный билд"
    if pr.tasks_open:
        return f"открытые задачи: {pr.tasks_open}"
    if any((r.status or "") == "NEEDS_WORK" for r in pr.reviewers):
        return "есть NEEDS_WORK"
    if not pr.reviewers:
        return "нет ревьюверов"
    if all(r.approved for r in pr.reviewers):
        return "апрувы собраны"
    return "ждёт апрувов"


def _pr_line(pr: PR, status_note: str = "") -> str:
    note = f'; заметка: «{status_note}»' if status_note else ""
    revs = ", ".join(
        f"{r.display_name or r.name}:{'A' if r.approved else (r.status or 'N')}"
        for r in pr.reviewers
    ) or "—"
    conflict = "КОНФЛИКТ" if pr.conflicted else ("ok" if pr.conflicted is False else "?")
    return (f'- {pr.project}/{pr.repository}#{pr.id} "{pr.title}" — {conflict}; '
            f"состояние: {_pr_state(pr)}; сборка: {_BUILD_RU.get(pr.build_state, 'нет')}; "
            f"задач: {pr.tasks_open} открытых / {pr.tasks_resolved} закрытых; "
            f"ревью: {revs}; комментов: {pr.comment_count}; "
            f"обновлён: {fmt_ago(pr.updated)}{note}")


# Сводный статус сборок PR словами — для контекста дневного анализа и таблиц.
_BUILD_RU = {"FAILED": "красная", "INPROGRESS": "идёт", "SUCCESSFUL": "зелёная", "": "нет"}


def _render_day_context_md(ctx: DayContext) -> str:
    mode = " · кратко: только требующее действия" if ctx.brief else ""
    L: list[str] = [
        "# Контекст дневного анализа (jwu)",
        f"Пользователь: {ctx.me_display or '—'} ({ctx.user or '—'}). Синк: {ctx.synced_at or '—'}.{mode}",
        "",
        _DAY_PROMPT,
        "",
        f"## Изменения с прошлого синка ({len(ctx.deltas)})",
    ]
    L += [f"- [{d.kind}] {d.key} {d.detail} — {d.summary}" for d in ctx.deltas] or ["- нет"]

    if ctx.stuck:
        th = ctx.thresholds
        L.append(f"\n## Застряло ({len(ctx.stuck)}) — пороги: PR без движения {th.get('stale_pr_days')} дн, "
                 f"ждёт апрувов {th.get('approval_wait_days')} дн, ждёт моего ревью {th.get('review_wait_days')} дн, "
                 f"на тестах {th.get('testing_days')} дн")
        L += [f"- ⏳ {x['key']} — {x['days']} дн: {x['reason']} — {x['title']}" for x in ctx.stuck]
    L.append(f"\n## Мои задачи ({len(ctx.mine)})")
    L += [
        f"- {it.key} [{it.status}] ({it.priority}) assignee: {it.assignee or '—'} — {it.summary}"
        + (f"; заметка: «{ctx.status_notes[it.key]}»" if ctx.status_notes.get(it.key) else "")
        for it in ctx.mine
    ] or ["- нет"]

    for header, prs in (("Мои PR", ctx.prs_mine), ("PR на ревью", ctx.prs_review)):
        L.append(f"\n## {header} ({len(prs)})")
        if not prs:
            L.append("- нет")
        for pr in prs:
            L.append(_pr_line(pr, ctx.status_notes.get(pr_note_key(pr.project, pr.repository, pr.id), "")))
            for c in ctx.pr_comments.get(pr.id, [])[:8]:
                loc = f"{c.file}:{c.line} " if c.file else ""
                text = " ".join((c.text or "").split())[:200]
                L.append(f"    - {loc}{c.author}: {text}")

    fresh = [m for m in ctx.mentions if not m.seen]
    L.append(f"\n## Упоминания ({len(ctx.mentions)}, новых {len(fresh)})")
    if not ctx.mentions:
        L.append("- нет")
    for m in ctx.mentions:
        mark = " · новое" if not m.seen else ""
        when = fmt_dt(m.created) if m.created else "—"
        L.append(f"- {m.task_key} [{when}] от {m.author or '—'}{mark} — {m.summary}")
        L.append(f"  > {' '.join((m.text or '').split())[:300]}")
    return "\n".join(L)


def _day_context_json(ctx: DayContext) -> dict:
    return {
        "user": ctx.user,
        "me_display": ctx.me_display,
        "synced_at": ctx.synced_at,
        "brief": ctx.brief,
        "thresholds": ctx.thresholds,
        "stuck": ctx.stuck,
        "status_notes": ctx.status_notes,
        "deltas": [d.model_dump() for d in ctx.deltas],
        "mine": [i.model_dump() for i in ctx.mine],
        "prs_mine": [p.model_dump() for p in ctx.prs_mine],
        "prs_review": [p.model_dump() for p in ctx.prs_review],
        "mentions": [m.model_dump() for m in ctx.mentions],
        "pr_comments": {
            str(pid): [c.model_dump() for c in cs] for pid, cs in ctx.pr_comments.items()
        },
    }



# Публичные имена (CLI и старые импорты используют подчёркнутые).
DAY_PROMPT = _DAY_PROMPT
BUILD_RU = _BUILD_RU
pr_state = _pr_state
pr_line = _pr_line
render_day_context_md = _render_day_context_md
day_context_json = _day_context_json
