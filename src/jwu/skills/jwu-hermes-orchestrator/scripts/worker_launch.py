"""Ask Hermes to create a linked worker chat without starting a turn."""

import argparse
import asyncio
import json
import os
import re
import uuid
from pathlib import Path

from hermes_cli.gateway_client import GatewayRPCError, connect_gateway

SID = re.compile(r"local-[0-9a-f]{64}")


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--title", required=True, help="Unique worker chat title (day-prefix recommended).")
    p.add_argument("--brief-file", type=Path, help="Retired; use worker_relay.py to deliver the first brief.")
    p.add_argument("--parent", help="Orchestrator session id (default: HERMES_SESSION_ID).")
    p.add_argument("--create-only", action="store_true", help="Create session without prompt.submit.")
    p.add_argument("--dry-run", action="store_true", help="Print the Hermes request without sending it.")
    a = p.parse_args()
    a.parent = (a.parent or os.environ.get("HERMES_SESSION_ID", "")).strip()
    if not SID.fullmatch(a.parent):
        p.error("--parent or HERMES_SESSION_ID must be a local gateway session id")
    return a


def verified_worker(snapshot, sid, parent):
    if not isinstance(snapshot, dict) or snapshot.get("session_id") != sid or snapshot.get("stored_session_id") != sid:
        raise SystemExit("Hermes did not return the exact worker chat")
    info = snapshot.get("info") or {}
    if (info.get("archived") is not False
            or (info.get("launch_request") or {}).get("orchestrator_session_id") != parent):
        raise SystemExit("Hermes did not confirm an active worker linked to the orchestrator")
    return info


async def run(a):
    if not a.dry_run and (not a.create_only or a.brief_file is not None):
        raise SystemExit("Use --create-only, then worker_relay.py to deliver the first brief")
    title = " ".join(a.title.split())
    if not title:
        raise SystemExit("Empty worker title")
    request = {"session_id": a.parent, "title": title, "request_id": uuid.uuid4().hex}
    if a.dry_run:
        print(json.dumps(request, ensure_ascii=False, indent=2))
        return
    async with connect_gateway() as c:
        try:
            created = await c.rpc("session.create_worker", **request)
        except GatewayRPCError as exc:
            if exc.unknown_method:
                raise SystemExit("Update the Hermes gateway: this version cannot create linked workers") from None
            raise
        worker_id = created.get("session_id") if isinstance(created, dict) else None
        if not isinstance(worker_id, str) or not SID.fullmatch(worker_id) or worker_id == a.parent:
            raise SystemExit("Hermes did not return a separate worker chat")
        info = verified_worker(created, worker_id, a.parent)
        out = {
            "session_id": worker_id,
            "title": info.get("title"),
            "model": info.get("model"),
            "provider": info.get("provider"),
            "project_id": (info.get("project") or {}).get("id"),
            "archived": info.get("archived"),
            "cwd": info.get("cwd"),
        }
        print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(run(arguments()))
