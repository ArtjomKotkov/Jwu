# Интеграции: Confluence и SSH-стенды

## Confluence

Чтение, создание и правка страниц. **Удаления нет** — ни в CLI, ни в MCP.

```bash
jwu confluence setup --url https://conf.example.com [--space KEY]   # креды — как у Jira контура
jwu confluence page 123456 [--body]           # заголовок, путь, версия, адрес, текст (storage)
jwu confluence children 123456                # дочерние страницы
jwu confluence search 'space = KEY and title ~ "Оплата"'
jwu confluence create --title "Оплата" -F page.html --parent 123456 [--format wiki] [--yes]
jwu confluence update 123457 [--title …] [-F page.html] [-m "что поменял"] [--yes]
```

Без `--yes` — превью: где появится страница или что меняется в версии. Доступ тот же, что у
Jira (гейт + вход логином Jira). Текст — storage (XHTML) либо вики-разметка (`--format wiki`,
конвертирует сам Confluence). Занятый заголовок в пространстве — отказ до записи. Без
`--parent` страница создаётся в пространстве по умолчанию (`setup --space`); не задано —
`jwu doctor` предупредит.

## Zephyr Scale

Тест-кейсы в Jira (Server/DC): чтение, создание, правка, привязка к задачам, папки. **Удаления
нет.** Доступ — тот же, что у Jira контура. Кейсы живут в проекте кейсов (настройка), задачи —
в любом проекте: кейс привязывается к задаче из другого проекта.

```bash
jwu zephyr setup --project KEY [--agent my-tester]   # проект кейсов; свой агент-тестировщик
jwu zephyr projects                                  # где включён Zephyr
jwu zephyr fields                                    # статусы, приоритеты, свои поля проекта
jwu zephyr folders                                   # дерево папок
jwu zephyr for-issue APP-123                         # кейсы задачи и куда класть новые
jwu zephyr case KEY-T1 | search 'projectKey = "KEY" AND folder = "/smoke"'
jwu zephyr create case.json | update KEY-T1 patch.json | link KEY-T1 APP-123 | folder-create /Раздел
jwu zephyr rules docs/test-case-rules.md             # регламент тест-кейсов одной командой
```

Запись без `--yes` показывает, что уйдёт, и спрашивает. Папку для нового кейса jwu
подсказывает по уже существующим кейсам задачи. Регламент хранится как инструкция воркспейса
`zephyr-testcase-rules`: скилл `/jwu-zephyr-testcase` и агент читают её сами, так что для
обновления правил не нужно править скиллы в Claude Code, Cursor и Codex.

## Пуш работы

`jwu job push <id>` (в MCP — `jwu_git_push(job_id=…)`) пушит ветку рабочей копии работы, и только её:
без force, не при незакоммиченном и не когда ветка на origin ушла вперёд или разошлась, — и
под политикой git воркспейса: в ветки задач — с «да», в общие (develop, release*) — только по
допуску пользователя, в master/main — никогда. Остальной пишущий git агент делает через
`jwu_git_*`, голый git из Bash режет хук. Подробно — [git.md](git.md).

## SSH-стенды

`jwu_ssh` в основном MCP jwu читает логи и статус systemd-юнитов и Docker-контейнеров.
Стенды и настроенные права показывает `jwu_ssh_servers`. Для нового инструмента
установи свежий jwu и перезапусти клиент с основным MCP jwu. Рабочий контур
определяется по папке сессии или по `JWU_WORKSPACE` при запуске MCP.

```bash
jwu ssh add test --host test.example.com --user deploy --key ~/.ssh/id_ed25519
jwu ssh policy test logs systemd api.service allow
jwu ssh policy test status docker api ask
jwu ssh policy test logs docker api grant
jwu ssh policy test
```

Правило действует на одну комбинацию стенда, действия, типа цели и точного имени.
Правила SSH настраиваются из своего терминала и хранятся локально на этой машине.
По умолчанию действует `deny`. `allow` выполняется сразу, `ask` требует подтверждения
в чате, а `grant` — одноразового допуска через `jwu -W <воркспейс> ssh grant <номер>`.
Допуск действует 10 минут и привязан к параметрам исходного запроса. Встроенный
инструмент также учитывает whitelist и blacklist стенда. После смены адреса, способа
входа или списков команд права для новой конфигурации нужно задать заново.
SSH-политика действует в MCP jwu. Для изоляции агента с доступом к shell нужны
ограничения клиента на запись в БД jwu и прямой SSH. Хук Claude Code блокирует
вызовы CLI для выдачи допусков и редактирования политики.

Встроенное подключение использует OpenSSH, ключ или ssh-agent и проверяет ключ
сервера по `known_hosts`. Для ключа с passphrase загрузи ключ в агент. Стенды с входом
по паролю и операции SFTP по-прежнему доступны через отдельный ssh-mcp. Его политика
настраивается флагами `jwu ssh add --policy/--allow/--deny`, её следует учитывать, если
он зарегистрирован у клиента вместе с основным MCP jwu.

```bash
jwu ssh list
jwu ssh rm test
jwu ssh install [--dry-run]   # прежняя интеграция ssh-mcp для Claude Code
jwu ssh config [--print]
jwu ssh uninstall
```

- В прежней интеграции `readonly` включает пресет просмотра, а `--policy none` снимает его.
- Пароль и passphrase хранятся в секретах воркспейса. Конфиг ssh-mcp
  (`~/.local/share/jwu/ssh/<воркспейс>.toml`) сохраняется с правами 600.
- ssh-mcp не проверяет ключ сервера.
