# Hermes worker handoff (orchestrator)

Checklist for assigning jwu todo numbers to separate Desktop chats. Domain rules (approvals, journals) stay in **jwu-orchestrator** / **jwu-todo**; this file is Hermes-only mechanics.

## Main: identify the assignment without taking it

Use the exact KEY/PR or resolve the selected number against the board session the user saw. Never reinterpret it on a refreshed board. Keep the user's grouping. Main may read Jwu to disambiguate, but never creates or changes a job, claim, owner, journal or closure. A failed launch or pending delivery leaves nothing for Main to reserve, release or close.

## Launch (Hermes gateway)

| Step | Action |
|------|--------|
| Preflight | Before creating a chat, verify the local gateway build and the Python payload selected by `scripts/run_with_gateway.sh` independently (see [install.md](install.md#до-создания-чата)). Do not proceed on the current stock Canary or an unverified build. |
| Create | `scripts/worker_launch.py --create-only --title …` from orchestrator (`HERMES_SESSION_ID` set) |
| Model | Hermes `session.create_worker` inherits the model, provider and workspace from the parent. Jwu does not reconstruct the launch settings. |
| Verify | The creation snapshot must describe the exact active worker and confirm its parent before sending. Check model/provider, project and cwd, then use `worker_relay.py send` on that exact chat for the first brief only. Relay requires the native worker link from the local gateway’s read-only store, not just display metadata. On an uncertain retry, resend only the identical text: the native pair fixes the input ID; changed text conflicts at the gateway. Do not reuse this route for a chat already started by a legacy sender, or mix manual/legacy input with the first relay. |
| List | The worker appears below its parent in the Desktop project tree before its first message. |

## Debugging failed worker turns

- For Composer via CLIProxyAPI, correlate `incomplete tool result batch` with dropped-call warnings. Matching IDs in the rewritten request are not enough: `auth-cursor` expects a result for each originally announced ID. A protocol fix must acknowledge suppressed IDs without re-executing side-effecting calls.
- For missing `clarify` buttons, inspect the call arguments and delivery result (`outcome`, `notice`, `error`). A valid call can submit even when batched with other tools; submission does not establish what the UI rendered before it.
- Timestamp owner-resolution errors against archiving. An archived row observed later cannot explain a warning captured before archive.

## Title uniqueness

Day-prefix worker titles; archived title reuse resurrects old sessions.

## Shell hygiene

After gateway scripts, `unset PYTHONPATH` before `jwu` CLI in the same shell.

## Short brief

Example: «Разбери WEBIM/django-chat#918 из раздела review. Только черновик» (последняя фраза — только если так попросил пользователь). Do not add the Main board session/row, a job ID, a plan or an inferred restriction.

## Worker: entry and accounting after delivery

1. Run `jwu-session-init` in the assigned workdir; verify `source=cwd`, workspace, rules and the relevant domain skill. Find the exact assigned key and section on the worker’s own board snapshot and verify ownership. On changed identity or a live competing claim, stop rather than opening a duplicate.
2. For tracked work, resume the matching assignment or take the verified free item yourself and read back its job and anchor. For inspection only, do not claim or create a job. Check live PR state yourself: a conflicted review remains a review; a conflict blocks a move to testing.
3. Keep approvals (`clarify` in the worker chat), journal, results and closure with the worker. Check `session_mailbox` availability separately in Main and worker. Use it for ordinary reports, questions and replies with the directly linked chat. It does not start a turn or answer `clarify`. Put routing fields outside `text`; no fallback if mail is unavailable.
4. There is no trusted automatic archive route. Leave the worker chat open. After explicit consent, tell the user to use Archive on that exact worker chat in Hermes Desktop. Do not pass archive requests via prompt or mailbox; client-supplied author fields are not authentication.
