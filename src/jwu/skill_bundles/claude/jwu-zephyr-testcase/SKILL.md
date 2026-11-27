---
name: jwu-zephyr-testcase
description: Use when the user wants to read, write or fix test cases in Zephyr Scale (Jira test management) through jwu — «напиши тест-кейсы по задаче», «заведи кейс в зефир», «какие кейсы есть по PROJ-123», «поправь кейс QA-T12», «привяжи кейс к задаче», «/jwu-zephyr-testcase [KEY]». Collects the task, its existing cases, the project's fields and folders, and the workspace test-case rules; a tester agent drafts cases; shows a preview and creates/updates/links ONLY after the user's explicit «да». Deleting cases is impossible in jwu by design.
---

# jwu: тест-кейсы Zephyr Scale

> **Сначала правила воркспейса.** Ответа `jwu_workspace_current()` с `rules_md` нет в контексте
> этой сессии (новая сессия, `/clear`, компакт) — сначала пройди **jwu-session-init**. Без правил
> воркспейса не действуй: запреты (⛔) в них обязательны.

## Overview

Кейсы живут в проекте кейсов контура (`zephyr.project`), задачи — где угодно: связь кейса с
задачей — `issueLinks`. Кейсы пишет **агент-тестировщик**: свой у воркспейса (`testing.agent`)
или базовый `jwu-testing-agent`. Правила оформления — **регламент** воркспейса: инструкция
`zephyr-testcase-rules` (загружается одной командой, см. ниже). Регламент главнее всего.

> MCP: `jwu_zephyr_context(issue_key)`, `jwu_zephyr_case(key)`, `jwu_zephyr_search(query)`,
> `jwu_zephyr_folders()`, запись — `jwu_zephyr_create/update/link/folder_create(…, user_approved)`.
> Bash: `jwu zephyr setup|projects|fields|folders|case|search|for-issue|create|update|link|folder-create|rules`.

## Шаги

1. **Не настроено** (ошибка «Zephyr не настроен») — спроси проект кейсов и
   `jwu zephyr setup --project KEY` (`jwu zephyr projects` — где Zephyr включён).
2. **Контекст одним вызовом:** `jwu_zephyr_context(KEY)` → существующие кейсы задачи, папки,
   поля проекта, агент, есть ли регламент. Регламент есть — `jwu_guide("zephyr-testcase-rules")`
   целиком.
3. **Что проверять:** карточка задачи (`jwu_task`), журнал работы по ней (что сделано), PR.
4. **Кейсы пишет агент** — Agent с `subagent_type` = поле `agent`: передай задачу, что сделано,
   существующие кейсы, поля, папки и **текст регламента**. Сам кейсы не сочиняй.
5. **Превью пользователю** — как вернул агент: таблица и кейсы, «что я предположил». Правки —
   учти, покажи снова.
6. **«да» →** нет папки — `jwu_zephyr_folder_create(path, user_approved=True)`; каждый кейс —
   `jwu_zephyr_create(…, issue_links=[KEY], user_approved=True)`. Покажи ключи и ссылки.
7. Правка кейса — `jwu_zephyr_case` → изменения → превью «было → стало» → «да» →
   `jwu_zephyr_update`. Привязка к задаче — `jwu_zephyr_link`.

## Регламент

Регламент — инструкция воркспейса, а не текст скилла: обновляется одной командой, без правки
скиллов в Claude Code, Cursor и Codex:

```bash
jwu zephyr rules docs/qa/test-case-rules.md     # = jwu guide add zephyr-testcase-rules …
```

## Чего не делать

- Создавать, править, привязывать или заводить папки без «да».
- Дублировать существующие кейсы задачи — дополняй.
- Ставить статус, приоритет и значения своих полей не из списка проекта.
- Удалять кейсы — в jwu этого нет и не будет.
