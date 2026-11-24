"""Archive one already-authorized, idle Hermes worker after checking its jwu jobs."""

import argparse
import asyncio
import json
import os
import re
import subprocess
import uuid

from hermes_cli.gateway_client import connect_gateway


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target', required=True)
    p.add_argument('--title', required=True)
    p.add_argument('--project', required=True)
    p.add_argument('--workspace', required=True)
    p.add_argument('--workdir', required=True)
    p.add_argument('--job', type=int, action='append', required=True)
    a = p.parse_args()
    a.parent = os.environ.get('HERMES_SESSION_ID', '')
    pattern = re.compile(r'local-[0-9a-f]{64}')
    if not pattern.fullmatch(a.parent) or not pattern.fullmatch(a.target) or a.parent == a.target:
        p.error('Require distinct exact local worker and orchestrator IDs')
    return a


def check_jobs(a):
    def jwu(*args):
        return json.loads(subprocess.check_output(['jwu', '--workspace', a.workspace, *args], cwd=a.workdir))

    w = json.loads(subprocess.check_output(['jwu', 'workspace', 'current', '--json'], cwd=a.workdir))
    if w.get('slug') != a.workspace or w.get('source') != 'cwd':
        raise SystemExit('Working directory does not resolve to the expected jwu workspace')
    for n in a.job:
        j = jwu('job', 'show', str(n), '--json')
        records = j.get('records', [])
        if (j.get('id') != n or j.get('status') != 'done'
                or not any(r.get('kind') == 'summary' and r.get('text', '').strip() for r in records)
                or not any(a.target in r.get('text', '') for r in records)):
            raise SystemExit(f'Job #{n} is not linked, done and summarized')
    for j in jwu('jobs', '--json'):
        records = j.get('records', [])
        if j.get('status') not in {'done', 'cancelled'} and any(
                a.target in r.get('text', '') for r in records):
            raise SystemExit(f"Unfinished jwu job #{j.get('id')} still points to this worker")


async def run(a):
    async with connect_gateway() as c:
        target = await c.rpc('session.info', session_id=a.target)
        parent = await c.rpc('session.info', session_id=a.parent)
        if target.get('title') != a.title or parent.get('archived') or any(
                (s.get('project') or {}).get('id') != a.project for s in (target, parent)):
            raise SystemExit('Unexpected worker or orchestrator')
        if not target.get('archived'):
            s = await c.rpc('session.resume', session_id=a.target)
            if s.get('stored_session_id') != a.target or any(s.get(k) for k in ('running', 'pending', 'prompts', 'open_requests')):
                raise SystemExit('Worker is running or has a pending request; not archived')
            await c.rpc('session.mutate', session_id=a.target, request_id=uuid.uuid4().hex,
                        expected_revision=s['revision'], operation='archive', payload={'archived': True})
        target = await c.rpc('session.info', session_id=a.target)
        parent = await c.rpc('session.info', session_id=a.parent)
        if not target.get('archived') or parent.get('archived'):
            raise SystemExit('Archive read-back did not match expectations')
        print(json.dumps({'worker': target['title'], 'worker_archived': True,
                          'orchestrator': parent['title'], 'orchestrator_archived': False,
                          'done_jobs': a.job}, ensure_ascii=False))


if __name__ == '__main__':
    args = arguments()
    check_jobs(args)
    asyncio.run(run(args))
