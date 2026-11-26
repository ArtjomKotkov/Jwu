"""Inspect a native worker link or relay the orchestrator's first brief."""

import argparse
import asyncio
import json
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

from hermes_cli.gateway_client import connect_gateway


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=('check', 'send'))
    p.add_argument('--target', required=True)
    p.add_argument('--title', required=True)
    p.add_argument('--project', required=True)
    p.add_argument('--message-file', type=Path)
    p.add_argument('--key', help='Legacy option, ignored. The native Main–worker pair identifies the brief.')
    a = p.parse_args()
    a.sender = os.environ.get('HERMES_SESSION_ID', '')
    sid = re.compile(r'local-[0-9a-f]{64}')
    if not sid.fullmatch(a.sender) or not sid.fullmatch(a.target) or a.sender == a.target:
        p.error('An active local sender and a distinct exact local target ID are required')
    if a.mode == 'send' and not a.message_file:
        p.error('send requires --message-file')
    return a


@contextmanager
def worker_reader(profile):
    # session.info and projects.tree can both echo display-only launch attribution.
    # Use only the local owner's native link, which is written by session.create_worker.
    if os.environ.get('HERMES_TUI_GATEWAY_URL', '').strip():
        raise SystemExit('Cannot verify a native worker link for an explicit gateway URL')
    try:
        from gateway.owner_identity import host_owner_subject
        from hermes_cli.gateway_runtime import gateway_profile_home
        from hermes_constants import get_hermes_home
        from persistence_assembly import open_persistence_reader
        from persistence.core.errors import PersistenceError
    except ImportError as exc:
        raise SystemExit('Python payload does not expose native worker and admission storage APIs') from exc
    home = gateway_profile_home(get_hermes_home())
    if profile != str(home):
        raise SystemExit('Cannot verify a native worker link outside the local gateway profile')
    try:
        with open_persistence_reader(home) as reader:
            yield reader, host_owner_subject()
    except (AttributeError, PersistenceError) as exc:
        raise SystemExit(f'Cannot read the native worker link or admission history: {type(exc).__name__}') from exc


async def run(a):
    async with connect_gateway() as c:
        sender = await c.rpc('session.info', session_id=a.sender)
        target = await c.rpc('session.info', session_id=a.target)
        if not sender.get('title') or sender.get('archived') is not False or target.get('title') != a.title:
            raise SystemExit('Unexpected sender or target title/archive state')
        if any((s.get('project') or {}).get('id') != a.project for s in (sender, target)):
            raise SystemExit('Chats do not match the expected project')
        profile = sender.get('profile_id')
        if profile != target.get('profile_id'):
            raise SystemExit('Target is not a native worker of this sender; no brief sent')
        info = {'sender': sender['title'], 'target': target['title'], 'target_id': a.target,
                'project_id': a.project, 'target_archived': target.get('archived', False)}
        with worker_reader(profile) as (reader, principal):
            if reader.mailbox.worker_parent(a.target) != a.sender:
                raise SystemExit('Target is not a native worker of this sender; no brief sent')
            if a.mode == 'check':
                print(json.dumps(info, ensure_ascii=False))
                return
            if target.get('archived') is not False:
                raise SystemExit('Refusing to send unless the target chat is open')
            text = a.message_file.read_text(encoding='utf-8').strip()
            if not text or len(text) > 12000:
                raise SystemExit('Empty or oversized message')
            snap = await c.rpc('session.resume', session_id=a.target)
            if snap.get('stored_session_id') != a.target:
                raise SystemExit('Resumed chat does not match the exact target')
            input_id = uuid.uuid5(uuid.NAMESPACE_URL, f'jwu-first-brief:{a.sender}:{a.target}').hex
            # Native dedup is scoped to (principal, target, input). Let it check the payload on replay.
            # Otherwise even a legacy queued/terminal admission with no transcript blocks this first send.
            admissions = reader.admissions.list(session_id=a.target, pending_only=False)
            replay = any(row.request_id == input_id and row.principal_id == principal for row in admissions)
            if not replay and (admissions or snap.get('messages') != [] or snap.get('running') is not False):
                raise SystemExit('Worker has prior activity without this first-brief admission; no brief sent')
        r = await c.rpc('prompt.submit', session_id=a.target, input_id=input_id, text=text)
        receipt = await c.rpc('prompt.receipt', session_id=a.target, admission_id=r['admission_id'])
        print(json.dumps({**info, 'admission_id': r['admission_id'], 'status': receipt['status']}, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(run(arguments()))
