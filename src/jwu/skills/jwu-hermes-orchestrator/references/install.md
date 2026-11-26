# Как подключить инструкции Jwu в Hermes Desktop

Доменные скиллы **для Hermes** находятся в `.hermes/skills/` доверенного клона Jwu. В этом каталоге `jwu-hermes-orchestrator` и `jwu-hermes-role-samples` уже ссылаются на пакетные скрипты и штатные роли из `src/jwu/skills/`. Остальные инструкции из `src/jwu/skills/` предназначены для других клиентов. Установка CLI через pipx сама по себе не подключает каталог Hermes.

**Внутри клона** достаточно `hermes skills trust /путь/к/Jwu`. **Из другого воркспейса**, в том числе без `.git`, добавь абсолютный путь к `.hermes/skills` этого клона в `skills.external_dirs` используемого профиля. Это меняет источник инструкций, а не рабочую папку Jwu. Сначала прочитай текущий список, сохрани другие элементы; пример для пустого списка:

```bash
hermes config get skills.external_dirs --json
hermes config set skills.external_dirs '["/абсолютный/путь/к/Jwu/.hermes/skills"]'
hermes config get skills.external_dirs --json
```

Не копируй каталог по рабочим папкам. Одноимённый профильный скилл имеет приоритет над внешним каталогом: оставляй такую ссылку, только если она разрешается в тот же файл. В **новом** чате с рабочей папкой реального проекта проверь `skill_view` для `jwu-session-init`, `jwu-start-job`, `jwu-review-queue` и `jwu-hermes-orchestrator`. Сверь `skill_dir` с ожидаемым каталогом. Для частной роли проверь также её точный файл; штатные примеры не заменяют отсутствующую роль проекта.

## До создания чата

Проверь оба компонента **до** запуска `worker_launch.py`. Один не доказывает второй.

1. **Совместимость шлюза.** Нужен локальный шлюз именно профиля Main: `session.create_worker` должен записывать нативную связь worker–parent; `prompt.submit` должен возвращать существующий admission при точном повторе и `admission_conflict` при смене текста для того же `input_id`. Сверь сборку шлюза с проверенной в изолированном прогоне. Запись `orchestrator_session_id` в `session.info`, видимая связь на боковой панели и наличие RPC `session.create_worker` по отдельности этого не доказывают. Если сборка или поведение не подтверждены, не создавай чат.
2. **Совместимость Python payload.** `run_with_gateway.sh` использует отдельный `agent-payload` (`venv/bin/python` и `hermes-agent` из заданного `HERMES_AGENT_PAYLOAD` или найденного приложения). Проверь именно выбранный payload, даже если шлюз запущен из другого checkout. Ниже проверка импортов без подключения к шлюзу. Нужен совместимый read-only reader того же хранилища; удачный импорт — необходимая, но недостаточная проверка.

```bash
S="$JWU_ROOT/.hermes/skills/jwu-hermes-orchestrator/scripts"
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 "$S/run_with_gateway.sh" -c '
from gateway.owner_identity import host_owner_subject
from hermes_cli.gateway_runtime import gateway_profile_home
from persistence_assembly import open_persistence_reader
from persistence.authority.admissions import AdmissionRepository
from persistence.conversation.mailbox import MailboxRepository
assert callable(MailboxRepository.worker_parent) and callable(AdmissionRepository.list)
print("worker link and admission APIs importable")'
```

Текущий штатный Canary не готов к этому маршруту: его bundled Python payload не содержит `persistence.conversation.mailbox`. То, что в другой копии шлюза умеют создать воркера, не исправляет payload, который выбирает wrapper. При любом блокере не создавай чат, не переходи на generic prompt и не меняй runtime или конфиг в ходе запуска.

## Запуск связанного воркера

Только после обеих проверок, из чата Main при заданном `HERMES_SESSION_ID` (`JWU_ROOT` — доверенный клон):

```bash
S="$JWU_ROOT/.hermes/skills/jwu-hermes-orchestrator/scripts"
"$S/run_with_gateway.sh" "$S/worker_launch.py" --create-only --title "$TITLE"
```

По ответу создания и `session.info` сверь точные ID воркера и родителя, проект, папку, модель и провайдер **до передачи поручения**. Отправь бриф в тот же чат (`worker_relay.py send`): скрипт требует проверяемую нативную связь в локальном хранилище шлюза. Если это недоступно, не отправляй бриф и не создавай второй чат.

```bash
"$S/run_with_gateway.sh" "$S/worker_relay.py" send --target "$WORKER_ID" \
  --title "$TITLE" --project "$PROJECT_ID" --message-file "$BRIEF"
```

Проверь точный текст в истории воркера; `queued` ещё не доставка. При неопределённом результате повтори только неизменный бриф в тот же чат: `input_id` один для пары Main–воркер. CLI принимает старый `--key` только для совместимости и игнорирует его. Другой текст для того же `input_id` отклоняет шлюз (`admission_conflict`). `--dry-run` у `worker_launch.py` только печатает запрос к шлюзу (родитель, заголовок, ID запроса) и не может подтвердить ни существование воркера, ни его модель. Ведение работы и её журнал — не часть запуска чата.

Если воркер уже получал ввод по старому маршруту, проверяй существующий чат, не повторяй бриф и не создавай второй чат. Relay не отправит новый бриф при найденной истории, активном ходе или любой записи admission (включая очередь), если нет записи нового `input_id` от того же локального владельца. Проверка прежней активности и отправка не атомарны друг с другом: до подтверждения первого брифа не используй для этого чата старый relay, ручной ввод или другой маршрут параллельно. Повторы нового relay для пары защищены нативной идемпотентностью шлюза.

Для обычных отчётов, вопросов и ответов проверь наличие встроенного `session_mailbox` отдельно в Main и отдельно в чате воркера по списку фактически доступных инструментов (поле `tools` в `session.info` соответствующего чата). Это opt-in набор Hermes; установка Jwu не включает его автоматически. При отсутствии инструмента или ошибке назови блокер без запасного маршрута через relay, промпт, CLI или БД. Архив чата — только вручную в Hermes Desktop.
