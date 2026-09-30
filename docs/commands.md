# Команды

У большинства команд есть `--json`; у всех пишущих наружу — превью без `--yes`.
Воркспейс по умолчанию — по текущей папке, явно — `jwu -W <имя> <команда>`.

| Группа | Команды |
|---|---|
| установка | `install [--for claude\|cursor\|both]`, `install-claude-skills`, `doctor`, `configure [export\|import]`, `auth check` |
| проект | `init`, `workspace list\|create\|use\|current\|show\|provider\|add-path\|remove-path\|tag\|paths\|rename\|delete\|migrate\|thresholds`, `rule add\|list\|show\|edit\|rm` |
| чтение | `tasks`, `task`, `attachments`, `prs`, `pr [--diff [--numbered]\|--download]`, `builds`, `build`, `branches`, `mentions list`, `changes`, `sync`, `action day-analyze`, `review queue\|agents` |
| память | `job start\|add\|link\|status\|done\|cancel\|delete\|show\|handoff`, `jobs`, `note`, `notes`, `feature …`, `features`, `mentions read\|archive`, `memory export\|import\|sync` |
| задачи Jira | `issue create\|edit\|link\|transition\|transitions\|attach\|similar\|link-types`, `comment`, `comment-edit`, `comment-delete` |
| время | `worklog`, `worklog-chain`, `worklog-tz`, `worklog-edit`, `worklog-delete`, `worklogs` |
| PR | `pr-create`, `pr-edit`, `pr-review`, `pr-comment`, `pr-comment-edit`, `pr-comment-delete`, `pr-task list\|add\|done\|reopen\|edit\|delete` |
| интеграции | `confluence setup\|page\|children\|search\|create\|update`, `ssh add\|list\|rm\|config\|install\|uninstall`, `voice show\|collect\|examples\|agent\|analysis\|feedback` |
| фон и данные | `daemon run\|install\|uninstall\|status\|kick`, `notify status\|test\|poll`, `backup`, `restore`, `db stats\|prune\|vacuum`, `dashboard` |

Подробнее по каждой — `jwu <команда> --help`.
