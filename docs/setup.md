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

### Cursor и Codex

```bash
jwu install --for cursor     # только Cursor
jwu install --for codex      # только Codex
jwu install --for both       # Claude Code и Cursor
jwu install --for all        # Claude Code, Cursor и Codex
```

Для Cursor (2.4+) jwu кладёт те же скиллы в `~/.cursor/skills`, субагентов — в
`~/.cursor/agents` (без поля `tools`: там имена инструментов Claude Code) и добавляет сервер
`jwu` в `~/.cursor/mcp.json` — остальные серверы в файле не трогаются. После установки
перезапусти Cursor.

Для Codex скиллы ложатся в `~/.codex/skills` (или `$CODEX_HOME/skills`), сервер — секцией
`[mcp_servers.jwu]` в `~/.codex/config.toml` с путём до `jwu-mcp`; остальные секции файла
не меняются. Субагенты — TOML-файлами в `~/.codex/agents` (имя, описание, инструкции); поля
Claude Code вроде списка инструментов Codex не знает, их там нет. Codex зовёт субагента, когда
скилл или ты просите его по имени. `jwu doctor` проверяет все три: скиллы и субагентов (нет или
устарели), запись MCP и что путь до `jwu-mcp` существует.

### Обновление

```bash
jwu backup --out ~/Desktop     # на всякий случай
pipx install --force <источник>
jwu install [--for …]
```

MCP-сервер живёт процессом сессии агента: новые инструменты появятся после её перезапуска
(`jwu_version` покажет `version_warning`, пока сессия на старом коде). Скилл `/jwu-update`
делает всё это сам. Демон перезапускается: `jwu daemon install` (или через launchd/systemd).

### Строка статуса Claude Code

`jwu statusline install` прописывает `jwu statusline` в `~/.claude/settings.json` →
`statusLine`: под строкой ввода появляются воркспейс, активная работа с последней фазой,
очередь задач и живость демона. Если строка статуса уже была (например, плагин claude-hud),
прежняя команда запоминается и печатается первой строкой, jwu — второй (`--no-chain` —
только своя). Только локальная БД, без сети. Вернуть как было — `jwu statusline uninstall`;
посмотреть без Claude Code — `jwu statusline preview`.

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
| `jira` | Jira (+ второй инстанс Support) | Bitbucket | Jenkins | `ACME-123` |
| `github` | Issues | PR | Actions | `shop#42` |

**Правила контура** — то, что знает о проекте человек, но не код: запреты (`constraint`),
инструкции (`howto`), тестовое окружение (`testenv`), соглашения (`convention`), грабли (`gotcha`),
справка (`info`). Общие
или привязанные к тегу папки. Сами приезжают агенту при старте сессии и работы; запреты
скиллы обязаны соблюдать.

```bash
jwu rule add "Не пушить в develop напрямую" --kind constraint
jwu rule add "Стенд — по инструкции backend-stand" --kind testenv --tag backend
jwu guide add backend-stand --title "Локальный стенд backend" --file stand.md --tag backend
jwu guide add playwright --title "Playwright" --file pw.md --global   # общая для всех воркспейсов
jwu ssh add dev-stand --host … --user root --key ~/.ssh/… --desc "логи, что можно"   # удалённые серверы
jwu rules --tag backend
```

**Рабочие копии и место.** Работа, которая правит код, заводит git worktree на каждый
репозиторий в общей папке воркспейса, с номером работы в имени. Стенд работы поднимается
по правилу `testenv`, всё созданное помечается `jwu.job=<id>` (compose — `-p jwu-job<id>`)
и убирается при закрытии работы. Перед копией и стендом проверяется свободное место; мало —
работа стоп, демон предупреждает в Telegram, `doctor` и строка статуса — красным.

```bash
jwu workspace jobs-dir ~/jwu-jobs/work       # где лежат рабочие копии работ
jwu disk [--min 20]                          # свободно, docker, неубранное у закрытых работ
jwu job worktree <id> <репозиторий>          # копия для работы (обычно делает скилл)
jwu job ports <id> db redis web             # свободные порты стенду работы (не пересекутся)
jwu job cleanup <id> [--dry-run] [--force]   # убрать стенд, docker, кеш, порты и копии
jwu job done <id> --summary "что сделано" --cleanup
```

**Пороги «давно»** для дневного анализа:
`jwu workspace thresholds --stale-pr 14 --approval-wait 7 --review-wait 3 --testing 5`.

## Доступы

```bash
jwu configure                    # визард: хосты, логины, токены, Telegram, путь до БД
jwu configure --non-interactive --jira-host … --jira-user … --jira-token "$JIRA_TOKEN" \
  --bitbucket-host … --bitbucket-token "$BITBUCKET_TOKEN"
jwu auth check                   # Jira, Support, Bitbucket/GitHub, Jenkins, Confluence и Zephyr — что настроено
jwu doctor [--offline]           # всё разом: БД, папки, конфиг, доступы, демон, MCP, скиллы, версия
```

Настройки и секреты лежат в БД (`~/.local/share/jwu/state.db`, права 600). **Не держи БД в
облачной папке** — `configure --db-path` туда откажет без `--force`, `doctor` считает это ошибкой.

**Пароли — не значением аргумента**: он виден в `ps` и истории шелла, так что
`--jira-password`, `--gate-password`, `--support-password` и `--support-gate-password` принимают только
`-` (прочитать из stdin) или `?` (спросить скрыто). Без флага `--non-interactive` берёт пароль из
переменной окружения, а визард спрашивает сам.

```bash
printf '%s\n' "$PW" | jwu configure --non-interactive --jira-password -
```

**Переменные окружения перекрывают БД** — так контур поднимается без визарда: секреты
`JIRA_TOKEN`, `JIRA_PASSWORD`, `JIRA_GATE_PASSWORD`, `SUPPORT_*`, `BITBUCKET_TOKEN`,
`GITHUB_TOKEN`, `JENKINS_TOKEN`, `TELEGRAM_BOT_TOKEN`; настройки `JWU_JIRA_URL/USER/PROJECT/GATE_USER`,
`JWU_SUPPORT_URL/PROJECT/USER`, `JWU_BITBUCKET_URL/PROJECT/REPO`, `JWU_GITHUB_API/WEB/OWNER/REPOS/USER`,
`JWU_JENKINS_URL/USER`, `JWU_TELEGRAM_CHAT`; сеть — `JWU_HTTP_TIMEOUT` (30 с на ответ),
`JWU_HTTP_CONNECT_TIMEOUT` (5 с на соединение — недоступный хост не держит минутами),
`JWU_HTTP_RETRIES` (2 повтора GET при таймауте и 5xx).

GitHub: PAT classic со scope `repo` (или fine-grained с чтением Metadata, Issues, Pull requests,
Actions; для репозиториев организации Resource owner — организация). Без `--github-owner`
выборка `assignee:@me` уедет на весь GitHub.

## Данные: БД, бэкап, память

БД раз в день бэкапится в `~/.local/share/jwu/backups/` и, если файл больше 256 МБ, чистит
снапшоты старше 30 дней (`jwu db stats|prune [--apply] [--vacuum]|vacuum`).

```bash
jwu backup [--out ~/Desktop] [--no-secrets]  # tar.gz: БД, config, проектные скиллы/агенты, RESTORE.md;
                                             # без --out — в <данные jwu>/backups/ (не в текущую папку)
jwu restore jwu-backup-<дата>.tar.gz [--force] [--db-only] [--dry-run]
jwu memory export|import [--dry-run]         # память как JSON без секретов и снапшотов
jwu memory sync --repo ~/jwu-memory          # pull → импорт → экспорт → commit → push в СВОЙ приватный git
```

`restore` проверяет контрольные суммы, существующую БД не трогает без `--force`, путь до БД
переписывает под эту машину. Слить две базы без потерь — `memory import`, не `restore`.
