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
конвертирует сам Confluence). Занятый заголовок в пространстве — отказ до записи.

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
