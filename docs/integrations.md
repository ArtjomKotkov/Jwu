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

`jwu job push <id>` (в MCP — `jwu_job_push`) пушит ветку рабочей копии работы, и только её:
без force, не в защищённые ветки (настройка воркспейса `push.protected`, маски через
запятую, по умолчанию `develop,master,main,release*,stable*`), не при незакоммиченном и не
когда ветка на origin ушла вперёд или разошлась. Поток работы пушит только так: можно
разрешить агенту этот инструмент, не разрешая голый `git push`.

## SSH-стенды (ssh-mcp)

Стенды — серверы, где смотрят логи и состояние. jwu хранит их описания в воркспейсе, а
выполнять команды даёт агенту через [ssh-mcp](https://github.com/overklassniy/ssh-mcp)
(`go install github.com/overklassniy/ssh-mcp/cmd/ssh-mcp@latest`).

```bash
jwu ssh add test --host test.example.com --user deploy --key ~/.ssh/id_ed25519 \
    --remote-path /var/log/app --tag backend --desc "логи приложения в /var/log/app"
jwu ssh list                  # стенды, вход, политика, состояние интеграции
jwu ssh install [--dry-run]   # конфиг ssh-mcp + MCP ssh-<воркспейс> в папках воркспейса
jwu ssh rm test | jwu ssh config [--print] | jwu ssh uninstall
```

- По умолчанию политика `readonly`: разрешён просмотр (`tail`, `grep`, `journalctl`,
  `systemctl status`, `docker logs` …), запрещены цепочки, перенаправления, `sudo`,
  `find -exec`. `--policy none` снимает пресет, `--allow`/`--deny` добавляют свои regex.
- MCP регистрируется в папках воркспейса: стенды рабочего контура не видны в личном.
- Пароль и passphrase лежат в секретах воркспейса и в синк памяти не уходят. Конфиг ssh-mcp
  (`~/.local/share/jwu/ssh/<воркспейс>.toml`) пишется с правами 600. Лучше ключ или `--agent`.
- ssh-mcp не проверяет host key сервера — подключай только свои стенды.
