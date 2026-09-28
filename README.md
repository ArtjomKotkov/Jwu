# jwu

CLI + MCP-сервер для **Jira Server / Bitbucket Server / Jenkins** и для **GitHub** с локальной
памятью — и набор скиллов и субагентов для Claude Code. Сделан под работу через удалённые
сессии: синк и уведомления идут в фоне, а всё, что нужно знать по задаче или PR, лежит в
памяти jwu и достаётся одним вызовом.

- **Воркспейсы** — независимые контуры (рабочий, личный): свои папки с тегами, свой провайдер
  задач и PR, свои данные. Определяется по текущей папке.
- **Память**: работы (журнал цикла правок), заметки-контекст по задаче/PR/ветке, правила
  контура, локальные фичи. Синкается между машинами через приватный git.
- **Кэш**: снапшоты задач и PR, дельты между синками (конфликт, красный билд, needs work,
  вернули с тестов…), упоминания.
- Фоновый демон синка, уведомления в Telegram (в обе стороны), дневной анализ, TUI-дашборд.

## Установка и обновление

Нужен Python 3.10+ и [pipx](https://pipx.pypa.io).

```bash
pipx install git+https://github.com/ArtjomKotkov/jwu.git   # или из клона: pipx install .
pipx ensurepath                                            # один раз, потом перезапустить шелл
jwu install-claude-skills                                  # скиллы и субагенты в ~/.claude
claude mcp add --scope user jwu -- ~/.local/bin/jwu-mcp    # MCP-сервер для Claude Code
```

Обновление — **два шага**: `pipx install --force .` и `jwu install-claude-skills`. MCP-сервер
живёт процессом сессии Claude Code: новые инструменты появятся после её перезапуска
(`jwu_version` покажет `version_warning`, пока сессия на старом коде). Скилл `/jwu-update`
делает всё это и напоминает про `jwu backup` перед обновлением.

Разработка: `poetry install`, тесты — `poetry run pytest -q`.

## Воркспейсы

Воркспейс выбирается так: флаг `-W/--workspace` (глобальный, идёт **до** подкоманды) →
`JWU_WORKSPACE` → **текущая папка** (самая глубокая привязка) → `jwu workspace use` →
единственный.

```bash
jwu init . --provider jira --bitbucket --yes   # подключить проект: контур + папки + теги
jwu workspace current                          # что резолвится здесь и почему
jwu workspace add-path ~/dev/backend --tag legacy-бэкенд --tag django
jwu workspace paths --tag legacy-бэкенд
jwu workspace provider github                  # сменить провайдера, память остаётся
```

| provider | задачи | PR | сборки | ключ |
|---|---|---|---|---|
| `local` | локальные фичи (`HOMEJWU-1`) | — | — | `HOMEJWU-1` |
| `jira` | Jira (+ второй инстанс SDESK) | Bitbucket | Jenkins | `PROJ-123` |
| `github` | Issues | PR | Actions | `dndeck#42` |

**Правила контура** — то, что знает о проекте человек, но не код: запреты (`constraint`),
инструкции (`howto`), соглашения (`convention`), грабли (`gotcha`), справка (`info`). Общие
или привязанные к тегу папки. Они сами приезжают агенту в `jwu_workspace_current()` и при
старте работы; запреты скиллы обязаны соблюдать.

```bash
jwu rule add "Не пушить в develop напрямую" --kind constraint
jwu rule add "Как поднять стенд" --kind howto --tag legacy-бэкенд --file -
jwu rules --tag legacy-бэкенд
```

**Пороги «давно»** для дневного анализа — тоже настройка контура:
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

**Переменные окружения перекрывают БД** — так контур поднимается без визарда и keyring:
секреты `JIRA_TOKEN`, `JIRA_PASSWORD`, `JIRA_GATE_PASSWORD`, `SDESK_*`, `BITBUCKET_TOKEN`,
`GITHUB_TOKEN`, `JENKINS_TOKEN`, `TELEGRAM_BOT_TOKEN`; настройки `JWU_JIRA_URL/USER/PROJECT/GATE_USER`,
`JWU_SDESK_URL/PROJECT/USER`, `JWU_BITBUCKET_URL/PROJECT/REPO`, `JWU_GITHUB_API/WEB/OWNER/REPOS/USER`,
`JWU_JENKINS_URL/USER`, `JWU_TELEGRAM_CHAT`; сеть — `JWU_HTTP_TIMEOUT` (30 с), `JWU_HTTP_RETRIES`
(2 повтора GET при таймауте и 5xx).

GitHub: PAT classic со scope `repo` (или fine-grained с чтением Metadata, Issues, Pull requests,
Actions; для репозиториев организации Resource owner должен быть организацией). Без
`--github-owner` выборка `assignee:@me` уедет на весь GitHub.

## Синк, демон и уведомления

`jwu sync` тянет мои задачи, упоминания и PR (с конфликтами, сборками и задачами на
комментах), кладёт снапшот и считает **дельты**: `new_comment`, `status_change`,
`returned_from_testing`, `qa_comment`, `new_pr`, `new_pr_comment`, `new_pr_commit`,
`reviewer_approved`, `reviewer_needs_work`, `new_conflict`, `build_failed`/`build_fixed`,
`new_pr_task`/`pr_task_resolved`, `gone`/`pr_gone`. Дельты копятся до `jwu changes --clear`.

**Демон** делает синк регулярным без открытого дашборда:

```bash
jwu daemon run --once            # один проход руками
jwu daemon install --interval 600   # службой: launchd (macOS) / systemd --user (Linux)
jwu daemon kick                  # внеплановый проход прямо сейчас
jwu daemon status · jwu daemon uninstall
```

Как он работает: один процесс на машину (файловый лок в `~/.local/share/jwu/`), раз в
`--interval` секунд от конца прошлого прохода обходит все контуры с Jira или GitHub,
для каждого открывает сервис, делает `sync`, после синка отправляет уведомления и забирает
ответы боту, закрывает соединения. При старте пишет в настроенные чаты Telegram, что поднялся. Ошибка одного контура
пишется в лог (`~/.local/share/jwu/daemon.log`) и не мешает остальным. Служба стартует при входе и
перезапускается сама; итог последнего прохода — в `jwu daemon status` и `jwu doctor`.

**Telegram.** После **каждого** сетевого синка (руками, из дашборда, из демона, из
day-analyze) важные дельты уходят одним сообщением, сгруппированные по виду, ключи —
ссылками на PR и задачу: конфликт, красный билд, needs work, новая задача в PR, возврат с
тестов, комментарий QA, новые упоминания. Если нового нет — молчит.

Сообщение делится на два блока: **МОЁ** (я исполнитель, мой PR или задача моего PR) и
**УЧАСТВУЮ** (я автор задачи, ревьюер, наблюдаю или меня упомянули). Под каждым ключом —
роль и на ком задача (`👁 наблюдаю · исп. Иванов`); у смены статуса и возврата с тестов —
ещё и кто перевёл (`👤 исполнитель · перевёл Мария · на мне`, из истории изменений Jira).

Бот двусторонний: между проходами демон держит long polling к Telegram (`--poll-interval`,
25 с), поэтому на команду или ответ реагирует сразу.
Команды с клавиатуры: `/sync` — проход сейчас, `/status`, `/stuck` — что застряло по
порогам, `/mentions`, `/seen`, `/help`. Ответ на уведомление становится заметкой-контекстом по
ключу из него, `PROJ-1 текст` — заметкой по `PROJ-1`; бот подтверждает «записал».
Чужие чаты игнорируются.

```bash
jwu configure --non-interactive --telegram-chat <chat_id> --telegram-token "$TELEGRAM_BOT_TOKEN"
jwu notify status · jwu notify test · jwu notify poll
```

Скилл `/jwu-setup-remote` проводит через doctor → daemon → Telegram → memory sync.

## Задачи, PR и память

```bash
jwu tasks --view mine|mentions · jwu task PROJ-1 · jwu attachments PROJ-1 --download
jwu prs --view mine|review · jwu pr 893 --project WEBIM --repo django-chat [--diff] [--download]
jwu builds 893 · jwu build 893              # статусы CI и разбор падения из Jenkins/Actions
jwu branches [PROJ-1]                       # локальные ветки по задаче во всех репозиториях
jwu mentions list --unseen · jwu mentions read --all · jwu mentions archive --older-than 30
jwu action day-analyze --brief              # контекст + промпт для дневной сводки
```

**Работы (jobs)** — журнал одного цикла правок: фазы, баги, прогоны тестов, решения, ревью.
`jwu job add` прикладывает ветку и коммит HEAD (читает `.git`, в git ничего не пишет).
`jwu job handoff <id>` собирает самодостаточный промпт для следующей сессии; `jwu job done`
кладёт выжимку в контекст задачи и PR.

**Заметки-контекст** живут на любом ключе — задаче, PR (`WEBIM/repo#893`), ветке — и
всплывают там, где сущность открывают: `jwu task`, `jwu pr`, дашборд, дневной анализ,
handoff. Вид `status` — одна закреплённая строка «почему висит».

```bash
jwu note WEBIM/repo#893 "ждём ответа по таймауту" --kind status
jwu notes PROJ-1 · jwu note PROJ-1 "порт делаем в 10.7" --kind decision
```

**Задачи на комментах PR** (Bitbucket tasks) — чек-лист правок, который видят и ревьювер, и
Claude: `jwu pr-task list|add|done|reopen`. Текст задачи до 10 слов, запись только с `--yes`.

**Внешняя запись** — только по явному подтверждению (в CLI флаг `--yes`, без него превью):
`jwu comment`, `jwu issue create|link|transition|attach`, `jwu worklog`, `jwu pr-comment`
(общий, ответ в тред `--reply-to`, на строку), `jwu pr-comment-delete` (только свои),
`jwu pr-review approve|needs-work|unapprove`, `jwu pr-create`, `jwu pr-task add|done`.

**Коммент на строку** цепляется к диффу: `--line` — номер строки новой версии (правый номер
в `jwu pr <id> --diff --numbered`), тип строки (ADDED/CONTEXT/REMOVED) jwu берёт из диффа PR.
Строки нет в диффе — ошибка до отправки; удалённая строка — `--side FROM` и номер старой
версии. Если коммент всё же не привязался, jwu скажет об этом сразу после отправки.

## Данные: БД, бэкап, память

БД раз в день бэкапится в `~/.local/share/jwu/backups/` и, если файл больше 256 МБ, чистит
снапшоты старше 30 дней (`jwu db stats|prune [--apply] [--vacuum]|vacuum`).

```bash
jwu backup --out ~/Desktop [--no-secrets]    # tar.gz: БД (VACUUM INTO), config, проектные скиллы/агенты, RESTORE.md
jwu restore jwu-backup-2026-09-11.tar.gz [--force] [--db-only] [--dry-run]
jwu memory export|import [--dry-run]         # память как JSON без секретов и снапшотов
jwu memory sync --repo ~/jwu-memory          # pull → импорт → экспорт → commit → push в СВОЙ приватный git
```

`restore` проверяет контрольные суммы, существующую БД не трогает без `--force`, путь до БД
переписывает под эту машину. Слить две базы без потерь — `memory import`, не `restore`.

## Голос: тексты от твоего имени

Любой внешний текст — коммент или ответ в PR, коммент в Jira, ответ клиенту в SDESK, текст
задачи, 4test, коммит — скиллы jwu не сочиняют сами: они собирают факты и отдают их **агенту
голоса**, показывают тебе результат и отправляют только после «да».

```bash
jwu voice show                 # какой агент пишет, где профиль, сколько текстов в корпусе
jwu voice collect [--reset]    # корпус: твои комменты и описания в PR, комменты в Jira/SDESK, коммиты (без ИИ-коммитов)
jwu voice examples pr_reply    # что агент увидит как примеры для канала
jwu voice agent voice-me       # свой агент голоса для воркспейса («-» — дефолт jwu)
```

- В пакете только безличный агент `voice-writer-sample`. Персональное — локально, в
  `~/.local/share/jwu/voice/<slug>/`: `profile.md` (правила, регистры, примеры; правь руками)
  и `corpus.jsonl` (твои реальные тексты). В память и в репозиторий это не уходит.
- `/jwu-voice-profile` один раз разбирает корпус и сохраняет в профиль раздел «Анализ
  корпуса» — правила по каналам. Дальше агент пишет по профилю, а в примеры корпуса смотрит,
  только когда канала нет в анализе или ты говоришь «не похоже на меня».
- Правишь или отклоняешь черновик — скилл спросит «записать в профиль?» и допишет пару
  «было → стало» в журнал профиля.
- Переписка с ассистентом в корпус не попадает: как ты пишешь Claude — это не твой внешний стиль.
- MCP: `jwu_voice_profile`, `jwu_voice_examples`, `jwu_voice_feedback`, `jwu_voice_collect`,
  `jwu_voice_analysis_save`.

## SSH-стенды (ssh-mcp)

Стенды контура — серверы, где смотрят логи и состояние. jwu хранит их описания в
воркспейсе, а выполнять команды даёт Claude Code через
[ssh-mcp](https://github.com/overklassniy/ssh-mcp) (`go install github.com/overklassniy/ssh-mcp/cmd/ssh-mcp@latest`).

```bash
jwu ssh add test --host test.example.com --user deploy --key ~/.ssh/id_ed25519 \
    --remote-path /var/log/app --tag бэкенд --desc "логи приложения в /var/log/app"
jwu ssh list                  # стенды, вход, политика, состояние интеграции
jwu ssh install [--dry-run]   # конфиг ssh-mcp + MCP ssh-<slug> в каждой папке воркспейса
jwu ssh rm test | jwu ssh config [--print] | jwu ssh uninstall
```

- По умолчанию политика `readonly`: whitelist просмотра (`tail`, `grep`, `journalctl`,
  `systemctl status`, `docker logs` …) и запрет цепочек, перенаправлений, `sudo`, `find -exec`.
  `--policy none` снимает пресет, `--allow`/`--deny` добавляют свои regex.
- MCP регистрируется в скоупе `local` папок воркспейса: стенды рабочего контура не видны в личном.
- Пароль/passphrase (`--password`, `--passphrase`) лежат в секретах воркспейса и в память не
  синкаются. ssh-mcp в режиме конфига читает пароль только из файла, поэтому конфиг
  (`~/.local/share/jwu/ssh/<slug>.toml`) пишется с правами 600. Лучше ключ или `--agent`.
- ssh-mcp не проверяет host key сервера — доверяй только своим стендам.
- Скиллы разбора (`duty-support`, `jwu-qa-triage`, `build-failure`) смотрят логи стенда, когда
  его инструменты есть в сессии. Сами стенды видны через MCP `jwu_ssh_servers`.

## Дашборд

`jwu dashboard [--sync] [-a]` — TUI: вкладки Workspace / Структура / Правила / Мои задачи /
Упоминания / PR: мои / PR: на ревью / Фичи / Работы. В `-a` локальные вкладки обновляются
раз в 5 с, сетевые — раз в 15 мин, открытая карточка — раз в минуту. У PR в ячейке блокеров
`⚠` конфликт, `✗` красная сборка, `☐N` открытые задачи; рядом с заголовком — status-заметка.
Клавиши: `?` легенда, `/` поиск, `b` изменения, `enter` карточка, `o` открыть, `y`/`Y`
копировать, `R` синк всего, `W` воркспейс, `N` создать, `D` удалить, `q` выход.

## Скиллы и субагенты для Claude Code

Ставятся `jwu install-claude-skills`; срабатывают по фразам или слэш-командой.

| Скилл | Когда |
|---|---|
| `/jwu-session-init` | первый шаг сессии: воркспейс, правила, активные работы и открытые фичи — дальше всё через jwu |
| `/jwu-workspace-setup`, `/jwu-setup-remote`, `/jwu-update` | подключить проект; настроить демон, Telegram и память; обновить jwu |
| `/jwu-context` | «что я знаю по задаче / почему висит PR» — контекст без разбора работ, записать статус |
| `/jwu-start-job`, `/jwu-track-job`, `/jwu-resume-job`, `/jwu-wrap-up` | начать работу с планом; вести журнал; подхватить после потери контекста (handoff); закруглить сессию |
| `/jwu-job-review <reviewer>` | ревью СВОИХ локальных правок до коммита субагентом |
| `/jwu-review-pr <id>` | ревью ЧУЖОГО PR: дифф через jwu, замечания/задачи/статус только после «да» |
| `/jwu-review-queue [light\|full] [N]` | очередь ревью: все PR на мне (кроме уже апрувнутых) — ревьюеры пулом, фильтр замечаний (только то, что внёс PR), черновики голосом, сводка md; отправка по одному PR после «да» |
| `/jwu-pr-comment`, `/jwu-pr-task` | ответить в PR или поставить статус; задача-чекбокс на комменте |
| `/jwu-qa-triage <KEY>` | задачу вернули с тестов: вердикт по коду и черновик ответа |
| `/build-failure` | почему упал билд PR (Jenkins / Actions) |
| `/jwu-task-create`, `/jwu-task-comment`, `/jwu-task-status`, `/jwu-task-attach`, `/jwu-task-branches` | запись в Jira только после «да»; в какие ветки доехал фикс |
| `/jwu-analyze-day`, `/jwu-post-analyze-day`, `/jwu-track-time`, `/jwu-4test-message`, `/jwu-commit-message`, `/jwu-prompt-refine`, `/duty-support` | сводка дня; итоги дня и трекинг времени; инструкция QA; коммит-месседж; промпт для другой сессии; дежурство |

Субагенты из поставки: `reviewer-jwu-sample` (ревью по чек-листу), `jenkins-build-analyst`
(разбор сборок), `qa-triage-sample` (возвраты с тестов), `duty-support-sample` (обращения
поддержки). Проектные субагенты в `~/.claude/agents/` главнее образцов; `jwu backup` их
сохраняет.

MCP-инструменты повторяют CLI (`jwu_task`, `jwu_pr`, `jwu_pr_diff`, `jwu_context`,
`jwu_branches`, `jwu_day_context`, `jwu_job_handoff`, …). Пишущие наружу
(`jwu_comment`, `jwu_issue_*`, `jwu_worklog`, `jwu_pr_comment`, `jwu_pr_review`,
`jwu_pr_create`, `jwu_pr_task_*`) скиллы зовут только после явного подтверждения.

## Команды

| Группа | Команды |
|---|---|
| проект | `init`, `workspace list\|create\|use\|current\|show\|provider\|add-path\|remove-path\|tag\|paths\|rename\|delete\|migrate\|thresholds`, `rule add\|list\|show\|edit\|rm`, `ssh add\|list\|rm\|config\|install\|uninstall`, `voice show\|collect\|examples\|agent\|feedback`, `review queue\|agents`, `configure [export\|import]`, `auth check`, `doctor` |
| чтение | `tasks`, `task`, `attachments`, `prs`, `pr [--diff\|--download]`, `builds`, `build`, `branches`, `mentions list`, `changes`, `sync`, `action day-analyze` |
| память | `job start\|add\|link\|status\|done\|cancel\|delete\|show\|handoff`, `jobs`, `note`, `notes`, `feature …`, `features`, `mentions read\|archive`, `memory export\|import\|sync` |
| внешняя запись | `comment`, `issue create\|link\|transition\|transitions\|attach\|similar\|link-types`, `worklog`, `worklogs`, `pr-comment`, `pr-comment-delete`, `pr-review`, `pr-create`, `pr-task list\|add\|done\|reopen` |
| фон и данные | `daemon run\|install\|uninstall\|status\|kick`, `notify status\|test\|poll`, `backup`, `restore`, `db stats\|prune\|vacuum`, `dashboard`, `install-claude-skills` |

У большинства команд есть `--json`.
