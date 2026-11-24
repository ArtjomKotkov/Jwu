# Hermes worker handoff (orchestrator)

Checklist for assigning jwu todo numbers to separate Desktop chats. Domain rules (approvals, journals) stay in **jwu-orchestrator** / **jwu-todo**; this file is Hermes-only mechanics.

## Reserve (jwu)

| Step | Action |
|------|--------|
| Workdir | Must resolve `jwu workspace current` with `source=cwd` and expected slug |
| Snapshot | `jwu todo --refresh --json` — свежий merge/CI; capture `session`, `key`, `problems`, `group` per number |
| PR verify | Для PR-пунктов: `jwu pr <id> --project … --repo …` — if `conflicted` → worker skill **conflict** (jwu-start/continue-job), not «move» / jira transition |
| MCP host | `~/.local/pipx/venvs/jwu/bin/python` + `jwu-mcp` stdio, cwd = workdir |
| Take | `jwu_todo_take`: `numbers`, `keys`, `session` from snapshot, `for_worker` = planned chat title |
| Read-back | Each take returns `job_id`; keep number → job_id map for the user report |

## Launch (Hermes gateway)

| Step | Action |
|------|--------|
| Python | Run `scripts/run_with_gateway.sh` or set bundled python + `PYTHONPATH` (see references/install.md) |
| Create | `scripts/worker_launch.py --title … --brief-file …` from orchestrator (`HERMES_SESSION_ID` set) |
| Model | Script reads `model` + `provider` from orchestrator `session.info`; never rely on `launch_request` alone |
| Verify | `session.info`: `project.id`, cwd, **not archived**, model matches parent |
| List | `hermes sessions list --workspace <workdir>` — title visible among non-archived |

## Debugging failed worker turns

- For Composer via CLIProxyAPI, correlate `incomplete tool result batch` with dropped-call warnings. Matching IDs in the rewritten request are not enough: `auth-cursor` expects a result for each originally announced ID. A protocol fix must acknowledge suppressed IDs without re-executing side-effecting calls.
- For missing `clarify` buttons, inspect the call arguments and delivery result (`outcome`, `notice`, `error`). A valid call can submit even when batched with other tools; submission does not establish what the UI rendered before it.
- Timestamp owner-resolution errors against archiving. An archived row observed later cannot explain a warning captured before archive.

## Title uniqueness

Day-prefix worker titles; archived title reuse resurrects old sessions.

## Shell hygiene

After gateway scripts, `unset PYTHONPATH` before `jwu` CLI in the same shell.

## Brief content (minimum)

- jwu-session-init + workspace rules
- Job id and anchor
- Primary project skill path under `<Jwu>/.hermes/skills/`
- Return orchestrator session id; archive-self rule
- Decisions only in worker chat; choices via **`clarify`**, never numbered prose only
- Brief must remind: jwu-session-init § «Меню в Hermes Desktop» and § «Простой русский»; Hermes workers may load **jwu-workspace-operations** (§ Russian copy, `references/clarify-menu-russian.md`)
