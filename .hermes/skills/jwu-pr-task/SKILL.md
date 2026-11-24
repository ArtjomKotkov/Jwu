---
name: jwu-pr-task
description: "Чек-лист задач на комментариях Bitbucket PR."
---

# jwu: задачи на комментариях PR

Загрузи `skill_view(name="jwu-session-init")`; без проверенной привязки и правил пройди его. Требуй `source="cwd"`, непустой `slug` и совпадающий `cwd` из `jwu workspace current --json` с явным `terminal.workdir` проекта. Соблюдай ⛔ и правила тега. Ниже CLI-команды идут после `jwu --workspace <slug>` с тем же `workdir`. MCP необязателен: только если `jwu_workspace_current` подтверждает тот же `source`, `slug`, `cwd`; передавай `workspace=<slug>` в поддерживаемые вызовы. Если чтения не хватает, уточни через `clarify`, если доступен, иначе спроси в чате.

Только Bitbucket (`provider=jira`, PR подключены). Для GitHub задач на комментариях нет; не подменяй их правкой тела PR. Не дублируй чек-лист PR в jobs.

1. Прочитай карточку и комментарии `jwu_pr(pr_id, project=…, repo=…)` / `pr ID --project PROJECT --repo REPO --json`, список `jwu_pr_tasks(pr_id, project=…, repo=…)` / `pr-task list ID --project PROJECT --repo REPO --json`.
2. Новый текст задачи — от пользователя, не более 10 слов. Если текста нет, спроси или предложи одну строку и дай принять/исправить; не копируй замечание и не обрезай молча. Якорь — любой существующий коммент (`comment_id`) либо новый свой общий/inline. Для inline проверь правый номер в `jwu_pr_diff(pr_id, path=…, numbered=True)` / `pr ID --project PROJECT --repo REPO --diff --path FILE --numbered`. У нового якоря `comment_text` по умолчанию равен тексту задачи. При необходимости для формы нового якоря возьми раздел «Голос пользователя» из `skill_view(name="jwu-pr-comment")`; сам текст задачи не переписывай без просьбы.
3. Любая запись — точное превью цели и всех полей, полный текст (при правке «было → стало», при удалении — что исчезнет), затем новое явное «да». Меняется план — подтверждай заново. Для создания покажи PR, ID, автора и текст якоря либо полный текст и расположение нового комментария. `jwu_pr_task_add(pr_id, text, comment_id=…, comment_text=…, path=…, line=…, project=…, repo=…)` / `pr-task add ID TEXT [--comment CID | [--comment-text TEXT] [--file PATH --line N]] --project PROJECT --repo REPO --yes --json`. Сверь ID, `OPEN`, текст и `comment_id` по списку; при новом якоре перечитай и комментарий.
4. Закрытие/переоткрытие: покажи точные ID, тексты, итоговые состояния и для закрытия — что исправлено. Одно «да» на точную пачку. `jwu_pr_task_done(task_ids=[…], reopen=False|True)` / `pr-task done ID… --yes --json` или `pr-task reopen ID… --yes --json`.
5. Своя задача: `jwu_pr_task_edit(task_id, text)` / `pr-task edit ID TEXT --yes --json` (текст от пользователя по тому же правилу); `jwu_pr_task_delete(task_id)` / `pr-task delete ID --yes --json` — именно удалить, не отметить выполненной. Сверь авторство до записи.
6. После записи перечитай список каждого затронутого PR и проверь каждый ID, состояние, текст или отсутствие. При частичном или неясном результате сообщи факты; не повторяй весь вызов, если, например, уже создался якорь.
