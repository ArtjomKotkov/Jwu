"""Create a Desktop worker chat inheriting model/provider from the orchestrator session."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from hermes_cli.gateway_client import connect_gateway

SID = re.compile(r"local-[0-9a-f]{64}")


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--title", required=True, help="Unique worker chat title (day-prefix recommended).")
    p.add_argument("--brief-file", type=Path, help="Initial worker brief; omit with --create-only.")
    p.add_argument("--parent", help="Orchestrator session id (default: HERMES_SESSION_ID).")
    p.add_argument("--create-only", action="store_true", help="Create session without prompt.submit.")
    p.add_argument("--dry-run", action="store_true", help="Print create payload only.")
    a = p.parse_args()
    a.parent = (a.parent or os.environ.get("HERMES_SESSION_ID", "")).strip()
    if not SID.fullmatch(a.parent):
        p.error("--parent or HERMES_SESSION_ID must be a local gateway session id")
    if not a.create_only and not a.dry_run and (not a.brief_file or not a.brief_file.is_file()):
        p.error("--brief-file is required unless --create-only or --dry-run")
    return a


def project_id(info: dict):
    project = info.get("project")
    return project.get("id") if isinstance(project, dict) else None


def real_cwd(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        return None
    try:
        path = Path(value).resolve(strict=True)
        return str(path) if path.is_dir() else None
    except (OSError, ValueError, RuntimeError):
        return None


def inherit_launch_fields(parent_info: dict) -> dict:
    """Model/provider live on session.info; launch_request often omits model."""
    lr = parent_info.get("launch_request") or {}
    model = parent_info.get("model")
    provider = parent_info.get("provider")
    if not isinstance(model, str) or not model.strip():
        raise SystemExit("Orchestrator session.info has no model; select one in this chat first")
    if not isinstance(provider, str) or provider.strip().lower() in {"", "auto"}:
        raise SystemExit("Orchestrator session.info has no concrete provider; select one in this chat first")
    # Project lookup is lexical in Hermes. Preserve its spelling for the launch, but
    # compare resolved directories when checking the worker.
    cwd = parent_info.get("cwd")
    payload = {
        "source": lr.get("source") or "gui",
        "cwd": cwd,
        "row_cwd": cwd,
        "cwd_explicit": True,
        "model": model,
        "provider": provider,
    }
    # The create contract calls this `reasoning`; `fast` is a projection, not a launch flag.
    if reasoning := parent_info.get("reasoning_effort", lr.get("reasoning")):
        payload["reasoning"] = reasoning
    tier = parent_info.get("service_tier", lr.get("service_tier"))
    if tier is not None:  # An empty tier explicitly pins Normal in Hermes.
        payload["service_tier"] = tier
    return payload


def expected_worker_id(profile_id: str, request_id: str) -> str:
    # Canonical local IDs bind the profile, native control-socket subject and create request
    # (Hermes persistence.authority.bindings.local_session_id). A titled create may instead
    # return an existing chat, ignoring the request_id and giving no reuse flag.
    identity = json.dumps([profile_id, f"uid:{os.getuid()}", request_id], separators=(",", ":"))
    return "local-" + hashlib.sha256(identity.encode()).hexdigest()


def exact_snapshot(snapshot: dict, session_id: str) -> bool:
    return (isinstance(snapshot, dict) and snapshot.get("session_id") == session_id
            and snapshot.get("stored_session_id") == session_id)


def verify_worker_info(info: object, expected: dict):
    if not isinstance(info, dict):
        raise SystemExit("Gateway did not return worker session metadata")
    actual = {**info, "project": project_id(info), "cwd": real_cwd(info.get("cwd"))}
    for field, want in expected.items():
        got = actual.get(field)
        if got != want:
            raise SystemExit(f"Worker {field} mismatch: expected {want!r}, got {got!r}")
    if info.get("archived") is not False:
        raise SystemExit("Worker is archived or its archived status is unknown")


async def run(a):
    title = " ".join(a.title.split())
    if not title:
        raise SystemExit("Empty worker title")
    text = ""
    if not a.create_only and not a.dry_run:
        text = a.brief_file.read_text(encoding="utf-8").strip()
        if not text:
            raise SystemExit("Empty brief")
    async with connect_gateway() as c:
        parent = await c.rpc("session.info", session_id=a.parent)
        if not isinstance(parent, dict) or parent.get("archived") is not False:
            raise SystemExit("Orchestrator is archived or its archived status is unknown")
        project = project_id(parent)
        if not isinstance(project, str) or not project.strip():
            raise SystemExit("Orchestrator session.info has no project id; open a Desktop project first")
        profile_id = parent.get("profile_id")
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise SystemExit("Orchestrator session.info has no profile_id")
        base = inherit_launch_fields(parent)
        cwd = real_cwd(base["cwd"])
        if not cwd:
            raise SystemExit("Orchestrator has no valid cwd; open the project workspace first")
        base["title"] = title
        base["request_id"] = uuid.uuid4().hex
        if a.dry_run:
            print(json.dumps(base, ensure_ascii=False, indent=2))
            return
        worker_id = expected_worker_id(profile_id, base["request_id"])
        if worker_id == a.parent:
            raise SystemExit("Refusing to use the orchestrator as the new worker")
        created = await c.rpc("session.create", **base)
        if not exact_snapshot(created, worker_id):
            raise SystemExit("Gateway did not return the new worker for this request; use a unique title")
        expected = {key: base[key] for key in ("title", "model", "provider")}
        expected.update(cwd=cwd, project=project, profile_id=profile_id)
        info = await c.rpc("session.info", session_id=worker_id)
        verify_worker_info(info, expected)
        out = {
            "session_id": worker_id,
            "title": info.get("title"),
            "model": info.get("model"),
            "provider": info.get("provider"),
            "project_id": project,
            "archived": info.get("archived"),
            "cwd": info.get("cwd"),
        }
        if a.create_only:
            print(json.dumps(out, ensure_ascii=False))
            return
        resumed = await c.rpc("session.resume", session_id=worker_id, omit_messages=True)
        if not exact_snapshot(resumed, worker_id):
            raise SystemExit("Gateway did not resume the exact worker; refusing to submit the brief")
        verify_worker_info(resumed.get("info"), expected)
        r = await c.rpc(
            "prompt.submit",
            session_id=worker_id,
            input_id=uuid.uuid4().hex,
            text=text,
        )
        receipt = await c.rpc("prompt.receipt", session_id=worker_id, admission_id=r["admission_id"])
        out["admission_id"] = r["admission_id"]
        out["admission_status"] = receipt.get("status")
        print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(run(arguments()))
