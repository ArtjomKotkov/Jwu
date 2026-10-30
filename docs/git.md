# Политика git

Агент работает с git через jwu: инструменты MCP `jwu_git_*` и `jwu_job_push`. Перед каждой
пишущей операцией jwu сверяется с **политикой воркспейса**, и решает код, а не инструкция:
если нельзя, git не запускается, а агент получает ответ, что делать дальше. Голый `git` из
Bash мимо jwu режет хук Claude Code `jwu-git-guard`.

## Уровни

| уровень | что значит |
|---|---|
| `allow` | сразу |
| `ask` | только после «да» пользователя в меню (`user_approved=True`) |
| `grant` | «да» в чате мало: нужен **допуск** — кнопка в Telegram-боте или `jwu git grant` в терминале пользователя. Одноразовый, живёт `grant.ttl`, привязан к HEAD |
| `deny` | никогда |

## Операции

`commit`, `switch` (переключить/создать ветку), `sync` (подтянуть целевую ветку merge или
rebase), `stash`, `discard` (выкинуть несохранённое), `history` (amend, reset — только
незапушенное), `push`, `force_push` (только `--force-with-lease`), `delete_branch`, `tag`.
Чтение (`status`, `diff`, `log`, `show`, `fetch`…) политикой не ограничивается.

## Как решается

Два уровня: **воркспейс** задаёт общее, **репозиторий** уточняет. Репозиторий узнаётся по
`origin` (`project/repo`), а не по папке — основная папка, рабочие копии работ и любой клон
живут по одной политике. Порядок, первое совпадение выигрывает:

1. правила репозитория;
2. правила воркспейса;
3. уровень операции у репозитория;
4. уровень операции у воркспейса.

Правило совпадает по `branch` (маски), `key` (префикс задачи в имени ветки, `*` — любой
ключ) и `where` (`job` — рабочая копия работы, `main` — основная папка репозитория, `any`).

## Встроенная политика

```toml
[git]
guard = true             # хук: пишущий git — только через jwu_git_*
sync = "merge"           # как подтягивать целевую ветку: merge | rebase
backup_days = 14         # сколько хранить refs/jwu/backup/*

[git.grant]
ttl = 600                # секунд
via = ["telegram", "cli"]
bind = "head"            # допуск на конкретный HEAD

[git.ops]
commit = "allow"
switch = "allow"
sync = "allow"
stash = "allow"
discard = "ask"
history = "ask"
push = "ask"
force_push = "ask"
delete_branch = "ask"
tag = "grant"

[[git.rule]]
name = "прод-ветки"
match = { branch = ["master", "main"] }
ops = { push = "deny", force_push = "deny", commit = "deny", history = "deny", sync = "deny" }

[[git.rule]]
name = "общие ветки"
match = { branch = ["develop", "release*", "stable*"] }
ops = { push = "grant", force_push = "deny", commit = "ask", history = "deny", delete_branch = "deny" }

[[git.rule]]
name = "основная папка"
match = { where = "main" }
ops = { commit = "ask", sync = "ask", history = "deny" }
```

Старая настройка `push.protected` при первом обращении превращается в правило «общие ветки».

## Пример: свои ветки без вопросов, чужие — нельзя, у одного репозитория rebase

```toml
[[git.repo]]
remote = "proj/app"
target = "develop"
sync = "rebase"
  [[git.repo.rule]]
  match = { key = ["APP"], where = "job" }
  ops = { push = "allow" }
  [[git.repo.rule]]
  match = { key = ["*"] }
  ops = { push = "deny" }

[[git.repo]]
remote = "proj/legacy"
ops = { push = "deny", commit = "deny" }   # только читать
```

## Команды

| что | команда |
|---|---|
| итоговая политика | `jwu git policy show [--toml]` |
| записать встроенную и найти репозитории папок | `jwu git policy init` |
| править целиком в редакторе | `jwu git policy edit` |
| перенос, бэкап | `jwu git policy export [файл]`, `jwu git policy import <файл>` |
| что сработает | `jwu git policy check push --branch release-5.2 [--repo proj/app]` |
| репозитории | `jwu git repo list`, `jwu git repo set proj/app --target develop --sync rebase --op push=grant`, `jwu git repo rm proj/app` |
| правила | `jwu git rule add [--repo proj/app] --branch 'release*' --key APP --where job --op push=grant [--at 1]`, `jwu git rule rm <N>`, `jwu git rule move <N> <куда>` |
| допуск | `jwu git grants` — ждущие запросы; `jwu git grant [N] [--deny]`, или заранее `jwu git grant --repo proj/app --branch develop --op push` |
| журнал | `jwu git log` — разрешено, заблокировано, допуски |
| хук | `jwu git hook install\|uninstall\|status` (ставится `jwu install`; не нужен — `jwu install --no-git-guard`) |

Политика и правила лежат в базе jwu (`git_policy`, `git_repos`, `git_rules`) и переезжают с
памятью (`jwu memory export/import`; при импорте чужая копия не заменяет уже настроенную).
Допуски и журнал — только на этой машине.

## Что видит агент

Отказ — ответ, а не ошибка:

```json
{"ok": false, "blocked": "grant", "op": "push", "rule": "воркспейс · общие ветки",
 "reason": "пуш в proj/app:release-5.2 — нужен допуск пользователя, «да» в чате не хватает",
 "ask_user": "Нужен допуск: кнопка в Telegram-боте (запрос отправлен) или в своём терминале: `! jwu git grant 7`",
 "retry": "после допуска повтори тот же вызов", "grant_id": 7}
```

Каждое разрешение и отказ — в `jwu git log` и в журнале работы, если папка — её рабочая копия.

## Страховки

- Перед `sync`, `discard`, `history`, удалением ветки и откатом jwu снимает backup-ссылку
  `refs/jwu/backup/<время>-<операция>` (вместе с несохранёнными правками); `jwu_git_undo`
  возвращает к ней.
- Агент не может поменять политику и выдать себе допуск: у MCP таких инструментов нет, а хук
  режет из Bash агента `jwu git policy edit/init/import`, `jwu git grant`, `jwu git repo/rule …`,
  `jwu workspace setting git.*`, `jwu job push` и `sqlite3` по базе jwu. `jwu git grant`
  работает только в терминале (TTY).
- Хук разбирает команду целиком: цепочки, `$(…)`, `sh -c`, `eval`, `env`/`VAR=…`/`timeout`,
  `git -C`, `-c`. Вне папок воркспейсов jwu хук git не трогает.

Это страховка от случайности и самодеятельности агента, а не песочница: окончательный замок —
права веток на сервере git.
