---
name: jwu-confluence
description: "Поиск, чтение, создание и правка страниц Confluence."
---

# jwu: страницы Confluence

Загрузи `skill_view(name="jwu-session-init")`; без проверенной привязки и правил пройди его. Требуй `source="cwd"`, непустой `slug` и совпадающий `cwd` из `jwu workspace current --json` с явным `terminal.workdir` проекта. Соблюдай ⛔ и правила тега. Ниже CLI-команды идут после `jwu --workspace <slug>` с тем же `workdir`. MCP необязателен: только если `jwu_workspace_current` подтверждает тот же `source`, `slug`, `cwd`; передавай `workspace=<slug>` в поддерживаемые вызовы. Если чтения не хватает, уточни через `clarify`, если доступен, иначе спроси в чате.

Читай без подтверждения; не меняй Confluence или локальную настройку молча. Удаления страниц в jwu нет — не ищи обхода, предложи правку или ручную работу в вебе.

- `pageId` — число из `…/pages/viewpage.action?pageId=…`. Чтение: `jwu_confluence_page(page_id, with_body=True)` / `confluence page ID --json`; дети `jwu_confluence_children(page_id)` / `confluence children ID --json`; CQL `jwu_confluence_search(cql, limit=…)` / `confluence search CQL --limit N --json`.
- «Внутри X» → родитель X, «рядом с X» → `parent_id` из карточки X. При отсутствии родителя уточни корень и пространство; при наличии родителя пространство берётся у него. Посмотри соседей, следуй их формату. Если Confluence не настроен, `confluence setup` читает настройки; `confluence setup --url URL [--space KEY]` меняет локальные настройки — только по отдельной просьбе и после согласия на точные значения, затем снова `confluence setup` без флагов.
- При правке возьми текущий `body` в storage/XHTML, сохрани чужую вёрстку, макросы и разделы. Тело при передаче заменяется целиком; для правки только заголовка не передавай `body`. Новую страницу можно подготовить как `storage` либо `wiki` (серверная конвертация). Для текста от имени пользователя возьми раздел «Голос пользователя» из `skill_view(name="jwu-pr-comment")`, без переписывания фактов и XHTML агентом.
- Создание: `jwu_confluence_create(title, body, parent_id=…, space=…, fmt=…, dry_run=True)`; CLI `confluence create --title TITLE --file FILE [--parent ID | --space SPACE] --format storage|wiki --json` без `--yes` безопасно останавливается на превью/`confirm_required`. Если заголовок занят, предложи другую страницу или правку существующей.
- Правка: `jwu_confluence_update(page_id, title=…, body=…, fmt=…, message=…, dry_run=True)` / `confluence update ID [--title TITLE] [--file FILE --format storage|wiki] [--message MESSAGE] --json` без `--yes` для безопасного превью. Перед записью проверь текущую версию снова; при расхождении пересобери правку и пересогласуй.
- Покажи человеку точное превью: пространство, путь, родителя/ID, исходную версию, заголовок, формат, комментарий к версии и **полный отправляемый текст** в блоке кода, при правке — что меняется («было → стало»). Только новое явное «да» на этот payload разрешает тот же вызов с `dry_run=False` или `--yes`. После записи перечитай страницу по ID, сверь заголовок, путь, версию и тело (для wiki — результат конвертации), дай `url`. При неясном исходе не повторяй запись вслепую.
