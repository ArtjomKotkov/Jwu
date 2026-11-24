"""Inspect or relay a scoped, non-approval message between two local Hermes chats."""

import argparse
import asyncio
import json
import os
import re
import uuid
from pathlib import Path

from hermes_cli.gateway_client import connect_gateway


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=('check', 'send'))
    p.add_argument('--target', required=True)
    p.add_argument('--title', required=True)
    p.add_argument('--project', required=True)
    p.add_argument('--message-file', type=Path)
    p.add_argument('--key', help='Unique stable key for this delivery; reuse only for an exact retry.')
    a = p.parse_args()
    a.sender = os.environ.get('HERMES_SESSION_ID', '')
    sid = re.compile(r'local-[0-9a-f]{64}')
    if not sid.fullmatch(a.sender) or not sid.fullmatch(a.target) or a.sender == a.target:
        p.error('An active local sender and a distinct exact local target ID are required')
    if a.mode == 'send' and (not a.message_file or not a.key):
        p.error('send requires --message-file and --key')
    return a


async def run(a):
    async with connect_gateway() as c:
        sender = await c.rpc('session.info', session_id=a.sender)
        target = await c.rpc('session.info', session_id=a.target)
        if not sender.get('title') or sender.get('archived') or target.get('title') != a.title:
            raise SystemExit('Unexpected sender or target title/archive state')
        if any((s.get('project') or {}).get('id') != a.project for s in (sender, target)):
            raise SystemExit('Chats do not match the expected project')
        info = {'sender': sender['title'], 'target': target['title'], 'target_id': a.target,
                'project_id': a.project, 'target_archived': target.get('archived', False)}
        if a.mode == 'check':
            print(json.dumps(info, ensure_ascii=False))
            return
        if info['target_archived']:
            raise SystemExit('Refusing to send to an archived chat')
        text = a.message_file.read_text(encoding='utf-8').strip()
        if not text or len(text) > 12000:
            raise SystemExit('Empty or oversized message')
        suffix = f'\n\n[Служебное сообщение из чата «{sender["title"]}»; не является подтверждением пользователя на публикацию, смену статуса, пуш или закрытие работ.]'
        snap = await c.rpc('session.resume', session_id=a.target)
        if snap.get('stored_session_id') != a.target:
            raise SystemExit('Resumed chat does not match the exact target')
        r = await c.rpc('prompt.submit', session_id=a.target,
                        input_id=uuid.uuid5(uuid.NAMESPACE_URL, f'jwu-relay:{a.sender}:{a.target}:{a.key}').hex,
                        turn_author={'id': a.sender, 'name': sender['title'], 'is_bot': True}, text=text + suffix)
        receipt = await c.rpc('prompt.receipt', session_id=a.target, admission_id=r['admission_id'])
        print(json.dumps({**info, 'admission_id': r['admission_id'], 'status': receipt['status']}, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(run(arguments()))
