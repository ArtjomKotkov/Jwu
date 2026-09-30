# Установка и настройка

## Установка

Нужны Python 3.10+ и [pipx](https://pipx.pypa.io).

```bash
pipx install git+<адрес этого репозитория>   # или из клона: pipx install .
jwu install                                  # скиллы, субагенты и MCP-сервер для Claude Code
jwu init ~/code/my-project --provider jira    # подключить проект: воркспейс + папка
```

`jwu install` ставит:

- скиллы в `~/.claude/skills`, субагентов в `~/.claude/agents`;
- MCP-сервер `jwu` в Claude Code (`claude mcp add --scope user jwu -- jwu-mcp`), если CLI
  `claude` есть в PATH; иначе подсказывает команду.

Первый раз после `pipx install` может понадобиться `pipx ensurepath` и перезапуск шелла.

### Cursor

```bash
jwu install --for cursor     # только Cursor
jwu install --for both       # Claude Code и Cursor
```

Для Cursor (2.4+) jwu кладёт те же скиллы в `~/.cursor/skills`, субагентов — в
`~/.cursor/agents` (без поля `tools`: там имена инструментов Claude Code) и добавляет сервер
`jwu` в `~/.cursor/mcp.json` — остальные серверы в файле не трогаются. После установки
перезапусти Cursor. `jwu doctor` проверяет и Claude Code, и Cursor.

### Обновление

```bash
jwu backup --out ~/Desktop     # на всякий случай
pipx install --force <источник>
jwu install [--for …]
```

MCP-сервер живёт процессом сессии агента: новые инструменты появятся после её перезапуска
(`jwu_version` покажет `version_warning`, пока сессия на старом коде). Скилл `/jwu-update`
делает всё это сам. Демон перезапускается: `jwu daemon install` (или через launchd/systemd).

### Разработка

`poetry install`, тесты — `poetry run pytest -q`. Скрины для README —
`python scripts/screenshots.py` (на выдуманных данных).

## Воркспейсы

Воркспейс — отдельный контур работы (рабочий, личный): свои папки с тегами, свой провайдер
задач и PR, свои данные. Выбирается так: флаг `-W/--workspace` (идёт **до** подкоманды) →
`JWU_WORKSPACE` → **текущая папка** (самая глубокая привязка) → `jwu workspace use` →
единственный.

```bash
jwu init . --provider jira --bitbucket --yes   # подключить проект: контур + папки + теги
jwu workspace current                          # что резолвится здесь и почему
jwu workspace add-path ~/code/shop-api --tag backend
jwu workspace paths --tag backend
jwu workspace provider github                  # сменить провайдера, память остаётся
```

| provider | задачи | PR | сборки | ключ |
|---|---|---|---|---|
| `local` | локальные фичи | — | — | `HOME-1` |
| `jira` | Jira (+ второй инстанс SDESK) | Bitbucket | Jenkins | `ACME-123` |
| `github` | Issues | PR | Actions | `shop#42` |

**Правила контура** — то, что знает о проекте человек, но не код: запреты (`constraint`),
инструкции (`howto`), соглашения (`convention`), грабли (`gotcha`), справка (`info`). Общие
или привязанные к тегу папки. Сами приезжают агенту при старте сессии и работы; запреты
скиллы обязаны соблюдать.

```bash
jwu rule add "Не пушить в develop напрямую" --kind constraint
jwu rule add "Как поднять стенд" --kind howto --tag backend --file -
jwu rules --tag backend
```

**Пороги «давно»** для дневного анализа:
`jwu workspace thresholds --stale-pr 14 --approval-wait 7 --review-wait 3 --testing 5`.

## Доступы

```bash
jwu configure                    # визард: хосты, логины, токены, Telegram, путь до БД
jwu configure --non-interactive --jira-host … --jira-user … --jira-token "$JIRA_TOKEN" \
  --bitbucket-host … --bitbucket-token "$BITBUCKET_TOKEN"
jwu auth check
jwu doctor [--offline]           # всё разом: БД, папки, конфиг, доступы, демон, MCP, скиллы, версия
```

Настройки и секреты лежат в БД (`~/.local/share/jwu/state.db`, права 600). **Не держи БД в
облачной папке** — `configure --db-path` туда откажет без `--force`, `doctor` считает это ошибкой.

**Переменные окружения перекрывают БД** — так контур поднимается без визарда: секреты
`JIRA_TOKEN`, `JIRA_PASSWORD`, `JIRA_GATE_PASSWORD`, `SDESK_*`, `BITBUCKET_TOKEN`,
`GITHUB_TOKEN`, `JENKINS_TOKEN`, `TELEGRAM_BOT_TOKEN`; настройки `JWU_JIRA_URL/USER/PROJECT/GATE_USER`,
`JWU_SDESK_URL/PROJECT/USER`, `JWU_BITBUCKET_URL/PROJECT/REPO`, `JWU_GITHUB_API/WEB/OWNER/REPOS/USER`,
`JWU_JENKINS_URL/USER`, `JWU_TELEGRAM_CHAT`; сеть — `JWU_HTTP_TIMEOUT` (30 с), `JWU_HTTP_RETRIES`
(2 повтора GET при таймауте и 5xx).

GitHub: PAT classic со scope `repo` (или fine-grained с чтением Metadata, Issues, Pull requests,
Actions; для репозиториев организации Resource owner — организация). Без `--github-owner`
выборка `assignee:@me` уедет на весь GitHub.

## Данные: БД, бэкап, память

БД раз в день бэкапится в `~/.local/share/jwu/backups/` и, если файл больше 256 МБ, чистит
снапшоты старше 30 дней (`jwu db stats|prune [--apply] [--vacuum]|vacuum`).

```bash
jwu backup --out ~/Desktop [--no-secrets]    # tar.gz: БД, config, проектные скиллы/агенты, RESTORE.md
jwu restore jwu-backup-<дата>.tar.gz [--force] [--db-only] [--dry-run]
jwu memory export|import [--dry-run]         # память как JSON без секретов и снапшотов
jwu memory sync --repo ~/jwu-memory          # pull → импорт → экспорт → commit → push в СВОЙ приватный git
```

`restore` проверяет контрольные суммы, существующую БД не трогает без `--force`, путь до БД
переписывает под эту машину. Слить две базы без потерь — `memory import`, не `restore`.
