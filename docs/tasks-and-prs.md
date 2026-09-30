# Задачи, PR и память

## Чтение

```bash
jwu tasks --view mine|mentions · jwu task ACME-1 · jwu attachments ACME-1 --download
jwu prs --view mine|review · jwu pr 1204 --project ACME --repo shop-api [--diff [--numbered]] [--download]
jwu builds 1204 · jwu build 1204            # статусы CI и разбор падения из Jenkins/Actions
jwu branches [ACME-1]                       # локальные ветки по задаче во всех репозиториях
jwu mentions list --unseen · jwu mentions read --all · jwu mentions archive --older-than 30
jwu action day-analyze --brief              # контекст + промпт для дневной сводки
```

## Память

**Работы (jobs)** — журнал одного цикла правок: фазы, баги, прогоны тестов, решения, ревью.
`jwu job add` прикладывает ветку и коммит HEAD (читает `.git`, в git ничего не пишет).
`jwu job handoff <id>` собирает самодостаточный промпт для следующей сессии; `jwu job done`
кладёт выжимку в контекст задачи и PR.

**Заметки-контекст** живут на любом ключе — задаче, PR (`ACME/shop-api#1204`), ветке — и
всплывают там, где сущность открывают: карточка задачи и PR, дашборд, дневной анализ,
handoff. Вид `status` — одна закреплённая строка «почему висит».

```bash
jwu note ACME/shop-api#1204 "ждём ответа по таймауту" --kind status
jwu notes ACME-1 · jwu note ACME-1 "порт делаем в 2.4" --kind decision
```

**Локальные фичи** — мини-трекер для контуров без Jira: `jwu feature add|list|status|edit`.

## Запись наружу

Всё, что уходит в Jira, Bitbucket, GitHub или Confluence, — только по явному
подтверждению: в CLI без `--yes` команда показывает превью и ничего не пишет.

| Что | Команды |
|---|---|
| задачи Jira | `issue create\|link\|transition\|attach`, `issue edit` (поля; **удаления задач нет**) |
| комменты в задачах | `comment`, `comment-edit`, `comment-delete` (SDESK — только с `--to-client`) |
| время | `worklog`, `worklog-chain`, `worklog-edit`, `worklog-delete`, `worklog-tz` |
| PR | `pr-create`, `pr-edit` (заголовок/описание), `pr-review approve\|needs-work\|unapprove` |
| комменты в PR | `pr-comment` (общий, ответ в тред, на строку), `pr-comment-edit`, `pr-comment-delete` |
| задачи на комментах PR | `pr-task list\|add\|done\|reopen\|edit\|delete` (до 10 слов) |

Править и удалять jwu даёт **только своё** — на чужое отказ ещё до запроса на запись.

### Трекинг времени цепочкой

```bash
jwu worklog-tz МСК                                   # один раз: пояс воркспейса
jwu worklog-chain --start 10:00 \
  --item "ACME-412|2h 30m|идемпотентный ключ оплаты" \
  --item "ACME-398|45m|Ревью"
```

Каждый ворклог начинается там, где закончился предыдущий, — время дня идёт сплошным
отрезком. Без `--yes` — план «с · по · длительность» и пересечения с уже затреканным; при
ошибке запись останавливается, чтобы цепочка не порвалась молча.

### Коммент на строку

`--line` — номер строки новой версии файла (правый номер в `jwu pr <id> --diff --numbered`),
тип строки (ADDED/CONTEXT/REMOVED) jwu берёт из диффа PR. Строки нет в диффе — ошибка до
отправки; удалённая строка — `--side FROM` и номер старой версии. Не привязался — jwu скажет
сразу после отправки.
