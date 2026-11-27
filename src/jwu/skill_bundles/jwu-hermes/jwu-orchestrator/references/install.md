# Как подключить скиллы Jwu в нашей сборке Hermes

Штатный Hermes не целевой клиент. После установки пакета выполни `jwu install --for jwu-hermes` для нужного профиля. Команда копирует только комплект в `$HERMES_HOME/skills/jwu-hermes` (по умолчанию `~/.hermes/skills/jwu-hermes`), не настраивает MCP, инструменты, агентов или хуки. Для разработки в доверенном клоне `.hermes/skills/jwu-hermes` указывает на тот же исходный комплект `src/jwu/skill_bundles/jwu-hermes`; внутри клона достаточно доверия к проекту (`hermes skills trust /путь/к/Jwu`, с согласия владельца).

В новом чате проверь `skill_view(name="jwu-hermes/jwu-orchestrator")` и `skill_dir`: из доверенного проекта загружается его версия, вне него — установленная. Используй точный путь `jwu-hermes/<имя>`, особенно если в старой категории `skills/jwu` есть одноимённые скиллы. Установка старую категорию и частные роли не трогает. Не подключай старый клон через `skills.external_dirs` как запасной источник.

## До создания чата

Проверь оба компонента **до** запуска `worker_launch.py`. Один не доказывает второй.

1. **Совместимость шлюза.** Нужен локальный шлюз именно профиля оркестратора: `session.create_worker` должен записывать нативную связь worker–parent; `prompt.submit` должен возвращать существующий admission при точном повторе и `admission_conflict` при смене текста для того же `input_id`. Сверь сборку шлюза с проверенной в изолированном прогоне. Запись `orchestrator_session_id` в `session.info`, видимая связь на боковой панели и наличие RPC `session.create_worker` по отдельности этого не доказывают. Если сборка или поведение не подтверждены, не создавай чат.
2. **Совместимость Python payload.** `SKILL_DIR` ниже — абсолютный `skill_dir` из точной загрузки `jwu-hermes/jwu-orchestrator`. `run_with_gateway.sh` использует отдельный `agent-payload` (`venv/bin/python` и `hermes-agent` из заданного `HERMES_AGENT_PAYLOAD` или найденного приложения). Проверь именно выбранный payload, даже если шлюз запущен из другого checkout. Ниже проверка импортов без подключения к шлюзу. Нужен совместимый read-only reader того же хранилища; удачный импорт — необходимая, но недостаточная проверка.

```bash
S="$SKILL_DIR/scripts"
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 "$S/run_with_gateway.sh" -c '
from gateway.owner_identity import host_owner_subject
from hermes_cli.gateway_runtime import gateway_profile_home
from persistence_assembly import open_persistence_reader
from persistence.authority.admissions import AdmissionRepository
from persistence.conversation.mailbox import MailboxRepository
assert callable(MailboxRepository.worker_parent) and callable(AdmissionRepository.list)
print("worker link and admission APIs importable")'
```

Не считай Canary совместимым по названию канала. Например, если выбранный payload не содержит `persistence.conversation.mailbox`, рабочий шлюз из другого checkout этого не исправит. Если любая проверка не прошла, назови причину и остановись до создания чата: не меняй комплект или настройки во время запуска и не подменяй бриф обычным сообщением.

## Запуск связанного воркера

Только после обеих проверок, из чата оркестратора при заданном `HERMES_SESSION_ID`:

```bash
S="$SKILL_DIR/scripts"
"$S/run_with_gateway.sh" "$S/worker_launch.py" --create-only --title "$TITLE"
```

По ответу создания и `session.info` сверь точные ID воркера и родителя, проект, папку, модель и провайдер **до передачи поручения**. Отправь бриф в тот же чат (`worker_relay.py send`): скрипт требует проверяемую нативную связь в локальном хранилище шлюза. Если это недоступно, не отправляй бриф и не создавай второй чат.

```bash
"$S/run_with_gateway.sh" "$S/worker_relay.py" send --target "$WORKER_ID" \
  --title "$TITLE" --project "$PROJECT_ID" --message-file "$BRIEF"
```

Проверь точный текст в истории воркера; `queued` ещё не доставка. При неопределённом результате повтори только неизменный бриф в тот же чат: `input_id` один для пары оркестратор–воркер. CLI принимает старый `--key` только для совместимости и игнорирует его. Другой текст для того же `input_id` отклоняет шлюз (`admission_conflict`). `--dry-run` у `worker_launch.py` только печатает запрос к шлюзу (родитель, заголовок, ID запроса) и не может подтвердить ни существование воркера, ни его модель. Ведение работы и её журнал — не часть запуска чата.

Если воркер уже получал ввод по старому маршруту, проверяй существующий чат, не повторяй бриф и не создавай второй чат. Relay не отправит новый бриф при найденной истории, активном ходе или любой записи admission (включая очередь), если нет записи нового `input_id` от того же локального владельца. Проверка прежней активности и отправка не атомарны друг с другом: до подтверждения первого брифа не используй для этого чата старый relay, ручной ввод или другой маршрут параллельно. Повторы нового relay для пары защищены нативной идемпотентностью шлюза.

Перед запуском проверь, что используемый профиль предоставляет `session_mailbox` с действиями из [протокола](hermes-worker-handoff.md#отчёт-и-архивирование). Проверь инструмент отдельно в чате оркестратора и, после создания, в связанном чате воркера (`tools` в `session.info`, если поле доступно). Это набор инструментов Hermes, не MCP Jwu. При отсутствии или ошибке назови блокер без запасного маршрута; relay предназначен только для первого брифа, его `check` — только чтение.
