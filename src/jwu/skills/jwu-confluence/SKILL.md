---
name: jwu-confluence
description: Use when the user (or another agent) needs to read, create or edit Confluence pages through jwu — «создай статью в конфлюенсе», «заведи страницу рядом с …», «поправь инструкцию в Confluence», «что написано на странице …», «найди страницу про …», ссылки вида conf.…/pages/viewpage.action?pageId=…, «/jwu-confluence». Read freely; create and update only after the user's explicit «да». Deleting pages is NOT possible in jwu at all — by design.
---

# jwu: страницы Confluence

## Overview

Чтение, создание и правка страниц Confluence контура. Доступ — тот же, что у Jira
(гейт + логин). **Удаления страниц в jwu нет ни в каком виде** — просят удалить: скажи,
что это только руками в вебе, и предложи вместо этого поправить страницу.

> **MCP-first.** Чтение: `jwu_confluence_page(page_id)`, `jwu_confluence_children(page_id)`,
> `jwu_confluence_search(cql)`. Запись: `jwu_confluence_create(...)`, `jwu_confluence_update(...)`
> — оба с `dry_run=True` по умолчанию. Bash-фолбэк: `jwu confluence page|children|search|create|update`.
> Не настроен (ошибка «Confluence не настроен») — `jwu confluence setup --url https://conf… [--space KEY]`.

## Где страница

- `pageId` — число из адреса `…/pages/viewpage.action?pageId=149359387`.
- **«Рядом со страницей X»** — дочерняя того же родителя: `jwu_confluence_page(X)` →
  `parent_id`, и создаёшь под ним. **«Внутри X»** — `parent_id = X`.
- Пространство берётся у родителя; без родителя — `space` или настройка контура.
- Перед созданием посмотри соседей (`jwu_confluence_children(parent_id)`) — как они
  названы и устроены; новую страницу делай в том же духе.

## Формат текста

- Confluence хранит страницы в **storage** (XHTML: `<p>`, `<h2>`, `<ul><li>`, `<table>`,
  макросы `<ac:structured-macro …>`). Правишь существующую — бери её `body` из
  `jwu_confluence_page` и меняй его: так сохранятся макросы и вёрстка.
- Новую можно писать **вики-разметкой** (`fmt="wiki"`: `h2. Заголовок`, `* пункт`,
  `||шапка||`, `{code}`) — jwu сконвертирует её сервером Confluence.
- Текст от имени пользователя — через агента голоса (как в остальных пишущих скиллах).

## Запись

1. **Создать:** `jwu_confluence_create(title, body, parent_id, fmt, dry_run=True)` → покажи
   превью: где появится (путь), заголовок, объём. Заголовок уже занят в пространстве —
   jwu откажет: предложи править существующую или другой заголовок.
2. **Править:** прочитай страницу, подготовь новый текст ЦЕЛИКОМ (замена полная) →
   `jwu_confluence_update(page_id, title, body, message, dry_run=True)` → покажи, что меняется
   (версия, заголовок, что правишь в тексте — словами).
3. Только после явного «да» — тот же вызов с `dry_run=False`. Отдай ссылку `url`.

## Чего не делать

- Удалять страницы — такого инструмента нет, обходных путей не ищи.
- Писать без «да» пользователя, в том числе когда просит другой агент: превью всегда видит человек.
- Затирать чужие макросы и вёрстку, переписывая страницу с нуля при точечной правке.
