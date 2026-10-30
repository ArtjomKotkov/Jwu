---
name: jwu-cleanup
description: Use when the user wants to clean up leftovers after all jwu sessions — «почисти за всеми сессиями», «убери мусор», «что осталось от работ», «освободи место», «где-то сессия упала — прибери», «/jwu-cleanup». Finds across all workspaces what was left when something went wrong — stands and docker of closed or missing jobs, worktrees without a live job, old downloaded attachments, sessions silent for hours that still hold jobs — shows it grouped, and removes the chosen groups only after the user's «да». Never touches open jobs; never deletes uncommitted or unpushed work without a separate explicit «да».
---

# jwu: уборка за всеми сессиями

> **Сначала правила воркспейса.** Ответа `jwu_workspace_current()` с `rules_md` нет в контексте
> этой сессии (новая сессия, `/clear`, компакт) — сначала пройди **jwu-session-init**. Без правил
> воркспейса не действуй: запреты (⛔) в них обязательны.

## Overview

Обычно работа убирает за собой сама, при закрытии (**jwu-close-job**). Этот скилл — для
случаев, когда что-то пошло не так: сессия упала посреди работы, работу закрыли без уборки,
стенд подняли без записи, копию положили руками. Смотрит **все воркспейсы**.

> MCP: `jwu_sweep()` — список, `jwu_sweep(apply=True, groups=[…], user_approved=True)` —
> убрать. Bash: `jwu sweep [--only …] [--yes] [--force] [--json]`; место — `jwu_disk()`.

| группа | что это |
|---|---|
| `closed_jobs` | у закрытых работ остались стенд, контейнеры, вольюмы, образы, каталоги, копии |
| `orphan_docker` | docker с меткой работы или проектом `jwu-job<N>`, а работы нет или она закрыта |
| `orphan_worktrees` | папки в каталоге работ с `-job<N>` в имени без живой работы; копии субагентов `.claude/worktrees/agent-*`, не менявшиеся больше 2 часов, — их несохранённое уходит в бэкап (патч + `refs/jwu/backup/…`), убираются под политикой git (`worktree`) |
| `temp` | скачанные вложения задач и PR старше суток |
| `dead_sessions` | сессии молчат дольше 2 часов: их работы уйдут в паузу и снова станут свободны на доске |
| `kept` | что **не** тронется и почему: незакоммиченное или незапушенное |

## Шаги

1. **Список.** `jwu_sweep()` и `jwu_disk()`. Пусто — скажи «убирать нечего» и сколько места.
2. **Покажи по группам**: группа · сколько · что именно (номера работ, пути, сессии).
   Отдельно — `kept` с причинами.
3. **Меню** (AskUserQuestion, multiSelect): группы, где что-то есть, все отмечены — «убрать».
   Плюс вариант «ничего не трогать».
4. **«Да» →** `jwu_sweep(apply=True, groups=[выбранные], user_approved=True)`.
5. **`kept` не пуст** — отдельный вопрос меню на **этот список**: «оставить» (первым),
   «сначала закоммитить и запушить» (по обычным правилам, через `jwu_job_push`), «удалить с
   потерей несохранённого». Только явный выбор последнего → `force=True`.
6. **Итог:** что убрано по группам, ошибки, сколько места стало (`jwu_disk()`).

## Чего не делать

- Трогать открытые (активные, на паузе) работы и их ресурсы — для них есть **jwu-close-job**.
- Удалять незакоммиченное или незапушенное без отдельного «да» именно на это.
- Сносить docker без метки jwu «заодно»: уборка трогает только своё.
