# Команды

У большинства команд есть `--json`; у всех пишущих наружу — превью без `--yes`.
Воркспейс по умолчанию — по текущей папке, явно — `jwu -W <имя> <команда>`.

| Группа | Команды |
|---|---|
| установка | `install [--for claude\|cursor\|both]`, `install-claude-skills`, `doctor`, `configure [export\|import]`, `auth check`, `statusline [install\|uninstall\|preview]`, `duty [rooms\|room\|on\|off\|personal\|check]` |
| проект | `init`, `workspace jobs-dir`, `workspace list\|create\|use\|current\|show\|provider\|add-path\|remove-path\|tag\|paths\|rename\|delete\|migrate\|thresholds`, `rule add\|list\|show\|edit\|rm`, `guide [show\|add\|rm]` |
| чтение | `todo [номера] [-g simple\|fixes\|todo\|review] [-s сессия]`, `tasks`, `task`, `attachments`, `prs`, `pr [--diff [--numbered]\|--download]`, `video-frames`, `builds`, `build`, `branches`, `mentions list`, `changes`, `sync`, `action day-analyze`, `review queue\|agents` |
| память | `job start\|add\|link\|status\|done [--summary] [--cleanup]\|reopen\|cancel\|delete\|show\|handoff\|worktree\|push\|ports\|resource\|resources\|cleanup`, `disk`, `jobs`, `note`, `notes`, `feature …`, `features`, `queue add\|list\|show\|take\|done\|fail\|cancel\|release\|edit\|move\|clear\|slots`, `mentions read\|archive`, `memory export\|import\|sync` |
| задачи Jira | `issue create\|edit\|link\|transition\|transitions\|attach\|similar\|link-types`, `comment`, `comment-edit`, `comment-delete` |
| время | `worklog`, `worklog-chain`, `worklog-tz`, `worklog-edit`, `worklog-delete`, `worklogs` |
| PR | `pr-create`, `pr-edit`, `pr-review`, `pr-comment`, `pr-comment-edit`, `pr-comment-delete`, `pr-task list\|add\|done\|reopen\|edit\|delete` |
| интеграции | `confluence setup\|page\|children\|search\|create\|update`, `zephyr setup\|projects\|fields\|folders\|case\|search\|for-issue\|create\|update\|link\|folder-create\|rules`, `ssh add\|list\|rm\|config\|install\|uninstall`, `voice show\|collect\|examples\|agent\|analysis\|feedback` |
| фон и данные | `daemon run\|install\|uninstall\|status\|kick`, `notify status\|test\|poll`, `backup`, `restore`, `db stats\|prune\|vacuum`, `dashboard` |

Подробнее по каждой — `jwu <команда> --help`.
