---
name: build-failure
description: Use when the user wants to understand why a CI build failed for a PR/branch (Jenkins) — phrases like «почему упал билд», «разбери сборку», «красный билд», «что упало в jenkins», «build failure», «упали тесты ветки в CI». Collects facts ONLY via jwu (`jwu builds`/`jwu build`, which reach Bitbucket+Jenkins and do NOT need Jira) and dispatches the `jenkins-build-analyst` subagent to produce a structured root-cause verdict (наш баг vs регресс из целевой ветки vs инфра) and a fix.
---

# build-failure: разбор падения CI-сборки

## Когда применять
Пользователь спрашивает, почему красный билд / что упало в Jenkins по PR или ветке
(«почему упал билд», «разбери сборку 159», «упали тесты ветки в CI», «build failure»).

## Что делает
Единый источник данных — **jwu** (у него есть доступ в Bitbucket+Jenkins; команды сборок
не зависят от Jira). Анализ делегируется субагенту **jenkins-build-analyst**.

> **MCP-first.** Read-данные бери MCP-инструментами jwu — они предпочтительнее bash:
> `jwu_builds(pr_id, project, repo)` вместо `jwu builds …`, `jwu_build(pr_id, project, repo, url)`
> вместо `jwu build …`. Команды `jwu builds/build … --json` в bash — **фолбэк**, если
> MCP-сервер jwu не подключён.

## Шаги
1. **Собери вводные** из запроса: номер PR (обязательно, если нет прямого URL сборки),
   опц. `project`/`repo` Bitbucket, опц. прямой `build_url`, целевую ветку (по умолчанию
   `develop`), и путь к локальному репозиторию (для вердикта «наш баг vs регресс»).
   Если номера PR/URL нет — спроси у пользователя, не угадывай.

2. **Быстрая проверка статуса** (можно прямо здесь, до субагента):
   `jwu builds <PR> [--project P --repo R] --json`
   - всё `SUCCESSFUL` → билд зелёный, сообщи и закончи;
   - `INPROGRESS` → сборка ещё идёт (тесты в CI бывают длинными) — предложи вернуться позже
     или разобрать предыдущую упавшую по `--url`.

3. **Делегируй разбор** субагенту `jenkins-build-analyst` через Agent: передай PR,
   project/repo, build_url (если есть), target-ветку и путь к репозиторию. Субагент сам
   зовёт `jwu build … --json`, парсит падения и возвращает структурированный вердикт.
   - Если уже запускал субагента в этой сессии — продолжай его через SendMessage, не плоди нового.

4. **Покажи пользователю** итог субагента: какая сборка, что упало, причина, вердикт
   (наш баг PR / регресс из target / инфра), как чинить.

## Заметки
- `jwu build/builds` требуют jwu с поддержкой сборок. Если команд нет — скажи, что нужно
  обновить jwu, и не выдумывай данные.
- Токен Jenkins живёт в keyring jwu — ни скилл, ни субагент его не трогают, всё через jwu.
- Только чтение и анализ: ни правок кода, ни коммитов/пушей без отдельного запроса.
