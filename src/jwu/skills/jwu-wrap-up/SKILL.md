---
name: jwu-wrap-up
description: Use when the user is ending a session or a chunk of work and wants everything put away so the next session (or another machine) can pick up — «закругляемся», «заканчиваем на сегодня», «сохрани, где мы остановились», «подведи итог и запиши», «/jwu-wrap-up». Logs the final phase into the job, saves a handoff as context notes on the task and PR, marks reviewed mentions as seen, and offers to close finished PR tasks / the job. Only local memory writes without confirmation; anything external asks first.
---

# jwu: закруглить сессию

## Overview

Сессия заканчивается, а через час или на другой машине придёт следующая. Всё, что она
должна знать, надо положить туда, где она это найдёт: в работу (что сделано в этом
цикле) и в контекст сущностей (что важно по задаче и PR вообще). Локальная память —
пишем сразу; всё, что видят коллеги, — только после «да».

> **MCP-first.** `jwu_job_add`, `jwu_job_handoff(job_id, save=True)`, `jwu_job_status`,
> `jwu_note`, `jwu_mentions_seen`, `jwu_pr_task_done`, `jwu_memory` через bash
> `jwu memory sync`.

## Шаги

1. **Какая работа.** Если работа не в контексте — `jwu_jobs(status="active")`; несколько —
   спроси. Работы нет — переходи к шагу 4 (контекст можно записать и без работы).
2. **Дописать журнал** (jwu-track-job): последняя фаза с её статусом, незакрытые
   `todo`, последний прогон тестов, если был. Ветка и коммит прикрепятся сами.
3. **Итог — в память.** `jwu_job_handoff(job_id, save=True)`: выжимка (сделано, осталось,
   git, открытые задачи PR) ляжет заметкой-контекстом на задачу и PR. Покажи пользователю
   markdown handoff — это и есть «где мы остановились». Если работа завершена целиком —
   `jwu_job_status(job_id, "done")` (сам пишет контекст).
4. **Status одной строкой** на задачу и PR: `jwu_note(key, "…", kind="status")` — «почему
   висит / что дальше» так, как это увидит дневной анализ завтра.
5. **Разобранные упоминания** — `jwu_mentions_seen(ids)`, чтобы завтра не всплыли «новыми».
6. **Наружу — с подтверждением:** закрыть сделанные задачи PR (jwu-pr-task), ответить в
   тред (jwu-pr-comment), перевести задачу по статусу (jwu-task-status), затрекать
   время (jwu-track-time). Перечисли, что предлагаешь, и жди «да» по каждому.
7. **Синк памяти на другую машину**, если настроен: `jwu memory sync` (bash).

## Чего делать НЕ надо

- Закрывать работу «раз сессия кончилась» — done только если работа реально завершена.
- Оставлять итог только в чате: чат исчезнет, память — нет.
- Писать в git: коммит и пуш — отдельное решение пользователя (jwu-commit-message).
