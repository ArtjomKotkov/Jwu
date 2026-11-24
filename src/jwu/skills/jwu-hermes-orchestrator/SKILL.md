---
name: jwu-hermes-orchestrator
description: Use with jwu-orchestrator for Hermes Desktop handoffs (launch, relay, archive).
version: 1.0.0
metadata:
  hermes:
    tags: [jwu, orchestrator, hermes, gateway]
    related_skills: [jwu-orchestrator, hermes-agent]
---

# jwu: механика Hermes для оркестратора

Переносимый пакет в репозитории **Jwu** (`.hermes/skills/jwu-hermes-orchestrator/`). Доменные правила — в **jwu-orchestrator**; здесь только gateway: создание воркер-чатов, отчёты между чатами, архивирование.

## Когда грузить

Вместе с **jwu-orchestrator**, когда нужно создать Hermes-чат воркера, отправить callback в оркестратор или архивировать воркера после закрытых jobs.

## Скрипты (каноническая копия в git)

| Скрипт | Назначение |
|--------|------------|
| `scripts/worker_launch.py` | `session.create` + бриф; наследует **`model`/`provider` из `session.info` оркестратора** |
| `scripts/worker_relay.py` | Служебное сообщение worker → orchestrator (`check` / `send`) |
| `scripts/worker_archive.py` | Архив воркера после проверки jwu jobs |
| `scripts/run_with_gateway.sh` | Bundled python + `PYTHONPATH` для macOS Desktop |

Путь к корню скилла: ищи `.hermes/skills/jwu-hermes-orchestrator` от каталога клонированного Jwu (`JWU_REPO` в brief) или от текущего workdir вверх по дереву. Не полагайся на `~/.hermes/skills/…` — на новой машине достаточно симлинка из [install.md](references/install.md).

## Запуск воркера

1. Оркестратор открыт в нужном Desktop-проекте, модель выставлена в UI (`session.info` содержит `model`).
2. Уникальный `title` (префикс даты).
3. Бриф на диск в `.tmp-work/` воркспейса.
4. Из чата оркестратора (есть `HERMES_SESSION_ID`):

```bash
ROOT="<абсолютный путь к Jwu>/.hermes/skills/jwu-hermes-orchestrator"
"$ROOT/scripts/run_with_gateway.sh" "$ROOT/scripts/worker_launch.py" \
  --title "…" --brief-file "…"
```

5. Проверь JSON: `model`, `project_id`, `admission_status`. `queued` ≠ доставка в транскрипт.
6. После скрипта — `unset PYTHONPATH` перед `jwu` в том же shell.

Чеклист batch-handoff: [hermes-worker-handoff.md](references/hermes-worker-handoff.md).

## Перенос на другую машину

См. [references/install.md](references/install.md): pipx jwu + симлинк этого скилла в `~/.hermes/skills/productivity/`.

## Не делать

- Не копировать только `launch_request` в `session.create` — там часто нет `model`.
- Не заменять интерактивный чат воркера на `delegate_task` без явной просьбы.
- Не использовать `worker_relay` как согласие пользователя на публикацию или пуш.
- Не заканчивать отчёт оркестратора или воркера вариантами текстом — только **`clarify`** (см. jwu-orchestrator § «Меню в Hermes Desktop»).
