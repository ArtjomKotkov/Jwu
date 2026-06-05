# jwu

CLI для **Jira Server / Data Center** + **Bitbucket Server** с локальной памятью
(кэш задач/PR, дельты между синками, заметки) — и набор скиллов для Claude Code.

- Задачи по фильтрам: мои активные / упоминания / ждут моего ревью (PR).
- Живой TUI-дашборд с авто-обновлением.
- «Работы (jobs)» — журнал работы над задачей (фазы, баги, тесты, решения).

---

## 1. Установка

Требуется **Python 3.10+**. Ставим как CLI-приложение через [pipx](https://pipx.pypa.io):

```bash
# из git
pipx install git+https://github.com/ArtjomKotkov/jwu.git

# или из локальной копии репозитория
cd jwu && pipx install .
```

Для разработки — poetry: `poetry install`, запуск `poetry run jwu …`.

## 2. Сделать доступным как `jwu`

pipx кладёт бинарь в `~/.local/bin`. Один раз добавь его в PATH и перезапусти шелл:

```bash
pipx ensurepath
```

Проверка:

```bash
jwu --help
```

Обновить после изменений: `pipx install --force .` (из папки репо).

## 3. Конфигурация (любая платформа)

Один визард спрашивает хосты, логин, токен/пароль и путь до БД, всё сохраняет и проверяет связь:

```bash
jwu configure
```

Секреты **обязательно надо задать** (без них jwu не достучится до Jira/Bitbucket) — это делает
`jwu configure`: несекретное он пишет в `~/.config/jwu/config.toml` (**руками не правим**), а
пароли/токены кладёт в системный **keyring**. От платформы зависит лишь то, есть ли keyring-бэкенд:

| Платформа | Хранилище секретов | Что нужно для работы |
|---|---|---|
| macOS | Keychain | бэкенд есть — `jwu configure` сразу пишет секреты |
| Windows | Credential Locker | бэкенд есть — `jwu configure` сразу пишет секреты |
| Linux (десктоп) | Secret Service (GNOME Keyring/KWallet) | `jwu configure` пишет; держать сессию разлоченной |
| Linux (сервер/headless/контейнер) | keyring-бэкенда нет | задать токены через env (см. ниже) |

### Под какими ключами лежат секреты

Обычно их пишет `jwu configure`. Если нужно добавить вручную/для отладки — секреты хранятся под парой `(service, account)`:

| Секрет | service | account | env (приоритет над keyring) |
|---|---|---|---|
| Jira PAT (Bearer) | `jira-pat` | `jira` | `JIRA_TOKEN` |
| Jira пароль (сессия) | `jira-login` | `<твой Jira-логин>` | — |
| nginx Basic-гейт | `jira-proxy-basic` | `<логин гейта>` | — |
| Bitbucket PAT | `bitbucket-pat` | `bitbucket` | `BITBUCKET_TOKEN` |

Добавить вручную — кросс-платформенно через `keyring` CLI (ставится вместе с jwu):

```bash
keyring set jira-pat jira            # спросит токен, ввод скрыт
keyring set bitbucket-pat bitbucket
keyring set jira-login alice         # если входишь паролем (alice = твой логин)
keyring get jira-pat jira            # проверить
```

macOS — то же нативно через Keychain:

```bash
security add-generic-password -U -s jira-pat -a jira -w 'ТОКЕН'
```

Через переменные окружения (работает только для токенов, удобно на серверах):

```bash
export JIRA_TOKEN='…'  BITBUCKET_TOKEN='…'
```

Неинтерактивно (CI/серверы):

```bash
jwu configure --non-interactive \
  --jira-host https://jira.acme.com --jira-user alice --jira-token "$JIRA_TOKEN" \
  --bitbucket-host https://git.acme.com --bitbucket-token "$BITBUCKET_TOKEN"
```

Где нет keyring — отдавай секреты переменными окружения (имеют приоритет):
`JIRA_TOKEN`, `BITBUCKET_TOKEN`. Если перед Jira стоит nginx Basic-гейт — `--gate-user/--gate-password`.

**Путь до БД** переопределяется ключом `[storage].db_path` (через `jwu configure --db-path …`)
или env `JWU_DB_PATH`. Удобно положить БД в облако (iCloud/Dropbox) для синка между машинами —
**но не запускай jwu на двух устройствах одновременно** (риск конфликта файла). jwu сам раз в день
делает локальный бэкап и проверяет целостность.

## 4. Дашборд и авто-синк

```bash
jwu dashboard            # TUI из памяти (быстро, без сети)
jwu dashboard --sync     # сначала синхронизировать всё, потом открыть
jwu dashboard -a         # авто-обновление (см. ниже)
```

Режим `-a` (`--auto-update`):
- локальные вкладки (Анализ/Работы) — из памяти каждые **5с** (`--fast-interval`);
- сетевые таблицы (задачи/PR) — фоновый синк каждые **10 мин** (`--slow-interval`);
- открытый детальный экран — раз в **60с** (`--detail-interval`).

Клавиши: `enter` — детали, `o` — браузер, `y` — скопировать ключ задачи (вкладки «Мои задачи» /
«Упоминания» и карточка задачи), `R` — синк всего, `c` — свернуть панель изменений, `q` — выход.

**Буфер обмена (`y`):** jwu использует [pyperclip](https://pypi.org/project/pyperclip/) — он уже
ставится вместе с пакетом. На **macOS** и **Windows** дополнительно ничего не нужно. На **Linux**
нужна утилита для работы с clipboard в вашей сессии:

| Среда | Пакет |
|---|---|
| X11 | `xclip` или `xsel` |
| Wayland | `wl-clipboard` (команда `wl-copy`) |

Примеры: `sudo apt install xclip` (Debian/Ubuntu, X11), `sudo apt install wl-clipboard` (Wayland).
Без этих утилит `y` покажет ошибку в уведомлении TUI.

Без TUI: `jwu sync` — разовый полный синк.

## 5. Скиллы и субагенты для Claude

В репозитории шипуются и **скиллы** (`src/jwu/skills/<имя>/SKILL.md`), и **дефолтные субагенты**
(`src/jwu/agents/<имя>.md`) — можно взять прямо из git.

**Claude Code** — установить одной командой (копирует скиллы в `~/.claude/skills` и субагентов
в `~/.claude/agents`, обновляя существующие):

```bash
jwu install-claude-skills                   # каталог скиллов — флагом --dest
                                            # каталог агентов — флагом --agents-dest
                                            # пропустить агентов — флагом --skip-agents
```

**Другой агент / не Claude** — скорми содержимое нужного `SKILL.md` из `src/jwu/skills/`
своему инструменту (это обычные markdown-инструкции, без привязки к Claude).

### Вызов скиллов в Claude Code

Подхватываются **автоматически по фразам-триггерам**, либо явно слэш-командой:

| Скилл | Когда / как |
|---|---|
| `/jwu-start-job` | «начни работу по ABC-123», «проанализируй задачу» — анализ + план + создание работы (последняя фаза = ревью) |
| `/jwu-resume-job` | «на чём остановились», после очистки контекста — восстановить состояние работы |
| `/jwu-track-job` | «запиши баг/решение/прогон тестов», «отметь фазу done» — лог прогресса |
| `/jwu-job-review <reviewer-subagent>` | «прогони ревью», «code review этого PR» — запуск явно указанного субагента-ревьювера с контекстом задачи+PR+диффа; результат пишется в работу записями `--kind review` (шапка с якорями `[B*]/[I*]/[M*]`) + детальные `remark`/`bug`/`warning`/`constraint` |
| `/jwu-commit-message` | «напиши коммит», «коммит-месседж» — генерирует текст из текущих изменений (staged + unstaged); префикс `EXAMPLE-1111:` подставляется из имени ветки, нет префикса → без него; печатает текст, `git commit` делает пользователь |
| `/jwu-analyze-day` | «разбери мой день» — план по задачам и PR на сегодня |
| `/jwu-post-analyze-day` | «что я сделал сегодня» — итоги для трекинга времени |

### Дефолтные субагенты

| Субагент | Когда |
|---|---|
| `reviewer-jwu-sample` | Универсальный код-ревьювер по 10-пунктовому чек-листу (баги, безопасность, нейминг, миграции, потеря правок из target, и пр.). Используется `/jwu-job-review` как дефолт, если для проекта нет более специфичного ревьювера. |

Свой проектный ревьювер (напр. `reviewer-myproject-django`) добавляется как обычный субагент
Claude Code в `~/.claude/agents/` и вызывается тем же скиллом: `/jwu-job-review reviewer-myproject-django`.

---

## Команды

| Команда | Что делает |
|---|---|
| `jwu configure` | настройка хостов/логинов/секретов/БД |
| `jwu dashboard` | живой TUI (`--sync`, `-a`) |
| `jwu auth check` | проверка доступа к Jira и Bitbucket |
| `jwu sync` | разовый синк вью + PR, снапшот в память, расчёт дельт |
| `jwu tasks --view mine\|mentions` | список задач (есть `--jql`) |
| `jwu task ABC-123` | полная карточка: описание, комменты, dev-панель |
| `jwu prs --view mine\|review` / `jwu pr <id>` | PR с ревьюерами и статусом конфликта |
| `jwu changes` | дельты с прошлого синка |
| `jwu note KEY "…"` / `jwu notes KEY` | заметки по задаче |
| `jwu job …` | работы: `start`/`add`/`show`/`done` и т.д. |
| `jwu install-claude-skills` | развернуть скиллы в `~/.claude/skills` и дефолтных субагентов в `~/.claude/agents` |

У большинства команд есть `--json` (для интеграции с агентами).
