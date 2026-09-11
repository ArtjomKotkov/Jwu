---
name: jwu-review-pr
description: Use when the user wants to review SOMEONE ELSE's pull request from the session — «поревьюй PR 10725», «посмотри чужой PR», «что там в PR на ревью», «прогони ревью по PR <id>», «/jwu-review-pr <id> [reviewer-subagent]». Pulls the diff through jwu (no clone needed), runs a reviewer subagent, then — only after the user's explicit «да» — leaves remarks as PR comments, tasks on comments and sets the review status. Own pre-commit review is jwu-job-review, not this.
---

# jwu: ревью чужого PR из сессии

## Overview

jwu-job-review смотрит СВОЙ локальный дифф до коммита. Этот скилл — про PR коллеги: дифф
берётся через jwu (клон не нужен), разбор делает субагент-ревьювер, а всё, что уходит в
Bitbucket/GitHub (комменты, задачи на комментах, статус), — только после явного «да».

> **MCP-first.** `jwu_pr(pr_id, project, repo)` — карточка: описание, ревьюверы, `comments[]`,
> `open_tasks`, `attachments`, `build_state`; `jwu_pr_diff(pr_id, path)` — unified diff;
> `jwu_pr_attachments(download=True)` — скриншоты; запись — `jwu_pr_comment`,
> `jwu_pr_task_add`, `jwu_pr_review`. Bash-фолбэк: `jwu pr <id> --diff`, `jwu pr-comment`,
> `jwu pr-task add`, `jwu pr-review`.

## Аргументы

`/jwu-review-pr <PR_ID> [reviewer-subagent] [--project P --repo R]`. Субагент — как в
jwu-job-review: проектный ревьювер главнее `reviewer-jwu-sample`; если не передан и из
контекста не очевиден — спроси. Проект и репозиторий — из `jwu_prs(view="review")`,
если пользователь назвал только номер.

## Шаги

1. **Контекст PR.** `jwu_context("PROJ/repo#ID")` (свои прошлые заметки, статус) и
   `jwu_pr(...)`: заголовок, описание, ветки, ревьюверы, комменты, открытые задачи,
   сборка. Скриншоты в описании/комментах — скачай и прочитай через Read.
2. **Задача.** Ключ — из ветки/заголовка (`task_key` в ответе `jwu_pr`); `jwu_task(key)`
   за требованиями и договорённостями. Без задачи — ревью по описанию PR, скажи об этом.
3. **Дифф.** `jwu_pr_diff(pr_id)`; больше ~60 КБ — по файлам через `path` (список файлов
   в поле `files`). В субагент — весь дифф, иначе «потеря правок из target» не проверится.
4. **Субагент.** `Agent` с `subagent_type = <ревьювер>` и промптом как в jwu-job-review
   (задача, метаданные PR, уже сделанные внешние замечания, дифф). Пометь: «это ЧУЖОЙ PR,
   ревью для комментариев автору».
5. **Покажи выводы пользователю**: блокеры, важное, мелочи, с `file:line`. Ничего ещё
   не отправлено.
6. **Согласуй, что уходит наружу**, по пунктам:
   - замечание → `jwu_pr_comment(pr_id, text, path=…, line=…)` (inline) или общий;
   - «надо поправить» с чекбоксом → `jwu_pr_task_add(pr_id, text, comment_id=…)`, текст
     до 10 слов формулирует пользователь (скилл jwu-pr-task);
   - статус → `jwu_pr_review(pr_id, "APPROVED"|"NEEDS_WORK", text)` — отдельное «да».
   Каждый текст показывай целиком; правки пользователя — повтор превью.
7. **Локально** — `jwu_note("PROJ/repo#ID", "…", kind="status")`: «ревью сделано,
   ждём правок по X» — чтобы day-analyze и следующая сессия знали.

## Чего делать НЕ надо

- Отправлять комменты или ставить статус без явного подтверждения на КАЖДЫЙ вид действия.
- Апрувить «раз субагент ничего не нашёл» — решение об апруве принимает пользователь.
- Дублировать уже сделанные коллегами замечания — они в `comments[]`.
- Ревьюить свой PR этим скиллом — там jwu-job-review по локальному диффу.
