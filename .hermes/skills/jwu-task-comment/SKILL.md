---
name: jwu-task-comment
description: "Читай и пиши комментарии Jira и Support через jwu."
---

# jwu: комментарии Jira и Support

Загрузи `skill_view(name="jwu-session-init")`; без проверенной привязки и правил пройди его. Требуй `source="cwd"`, непустой `slug` и совпадающий `cwd` из `jwu workspace current --json` с явным `terminal.workdir` проекта. Соблюдай ⛔ и правила тега. Ниже CLI-команды идут после `jwu --workspace <slug>` с тем же `workdir`. MCP необязателен: только если `jwu_workspace_current` подтверждает тот же `source`, `slug`, `cwd`; передавай `workspace=<slug>` в поддерживаемые вызовы. Если чтения не хватает, уточни через `clarify`, если доступен, иначе спроси в чате.

Только `provider=jira`. Это не `jwu_note`: текст видят люди.

1. Открой `jwu_task(key)` / `task KEY --json`, прочитай задачу и комментарии. Черновик по-русски, wiki-разметкой Jira Server (`{code}…{code}`, `*жирный*`). Для клиента только проверенные факты: без внутреннего кода, веток, ключей разработки, неподтверждённых гипотез и атрибуции ассистента. Для голоса используй раздел «Голос пользователя» из `skill_view(name="jwu-pr-comment")`: `jira_comment`, аудитория `colleague`, `qa` или `analyst`; для клиента `support_client`, `client`.
2. Определи видимость: `jwu_comment(key, text, dry_run=True)` → `client_facing`. CLI `comment KEY -m TEXT --dry-run --json` (или `-F FILE`, `-F -` для stdin). CLI без `--to-client` на Support сообщает `client_facing_confirmation_required` даже в dry-run: этого достаточно для классификации. При неопределённой видимости не отправляй.
3. Перед любой записью покажи точный ключ, операцию и полный текст в блоке кода (для правки «было → стало», для удаления — полный удаляемый текст и ID). Для Support явно скажи «это прочитает клиент» или что клиент увидит изменение/удаление; попроси отдельное явное согласие именно на действие для клиента. Только новое «да» после превью разрешает запись, правки требуют нового превью.
4. Создание: `jwu_comment(key, text, dry_run=False, client_facing=…)` / `comment KEY -m TEXT --yes --json`, для Support добавить `--to-client`. Перечитай карточку и проверь текст по возвращённому ID. При связанной работе после проверки оставь локальную заметку `jwu_job_add(job_id, "Ответ в KEY", kind="note")` / `job add JOB_ID "Ответ в KEY" --kind note --no-git`.
5. Правка и необратимое удаление — только своего комментария: возьми ID из `comments`, сверь автора, видимость проверь тем же dry-run. После подтверждения `jwu_comment_edit(key, comment_id, text, client_facing=…)` / `comment-edit KEY CID -m TEXT --yes --json`; `jwu_comment_delete(key, comment_id, client_facing=…)` / `comment-delete KEY CID --yes --json`. Для Support оба CLI-вызова с `--to-client`. Перечитай карточку и сверь текст или отсутствие этого ID. При неясном результате не повторяй запись вслепую.
