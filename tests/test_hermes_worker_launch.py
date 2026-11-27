"""Offline tests for the packaged Hermes worker entry points."""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import uuid
import venv
from argparse import Namespace
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, call

import pytest

from jwu.skills_install import skill_bundle

SCRIPTS = skill_bundle("jwu-hermes") / "jwu-orchestrator/scripts"
PARENT = "local-" + "0" * 64
# Canonical ID from the installed Hermes bindings for test-profile / uid:501 / a*32.
WORKER = "local-311891f9b72bc664eccc8e532583ff9b0b1bf2a8358c5724483d724a137e2787"
EXISTING = "local-" + "1" * 64
TITLE = "Worker launch test"


@pytest.fixture
def launch(monkeypatch, tmp_path):
    client = ModuleType("hermes_cli.gateway_client")
    monkeypatch.setitem(sys.modules, client.__name__, client)
    rpc = AsyncMock(side_effect=AssertionError("unexpected gateway RPC"))

    @asynccontextmanager
    async def connect_gateway():
        yield Namespace(rpc=rpc)

    class GatewayRPCError(ValueError):
        unknown_method = True

    client.__dict__["GatewayRPCError"] = GatewayRPCError
    client.__dict__["connect_gateway"] = connect_gateway
    spec = importlib.util.spec_from_file_location("worker_launch", SCRIPTS / "worker_launch.py")
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher.uuid, "uuid4", lambda: Namespace(hex="a" * 32))
    brief = tmp_path / "brief.md"
    brief.write_text("  a worker brief\n", encoding="utf-8")
    args = Namespace(title=TITLE, parent=PARENT, brief_file=brief, create_only=False, dry_run=False)
    parent = {
        "title": "Orchestrator",
        "cwd": str(tmp_path),
        "model": "selected-model",
        "provider": "selected-provider",
        "project": {"id": "p_test"},
        "profile_id": "test-profile",
        "archived": False,
        "launch_request": {"source": "gui"},
    }
    worker = {**parent, "title": TITLE, "launch_request": {"orchestrator_session_id": PARENT}}
    return launcher, rpc, args, parent, worker


def snapshot(session_id, info):
    return {"session_id": session_id, "stored_session_id": session_id, "info": info, "messages": [], "running": False}


@pytest.mark.parametrize("reply_patch", [
    {"session_id": PARENT}, {"stored_session_id": EXISTING}, {"info": None},
    {"info": {"archived": False, "launch_request": {}}},
])
def test_unverified_worker_never_receives_the_brief(launch, reply_patch):
    launcher, rpc, args, _, worker = launch
    args.create_only, args.brief_file = True, None
    rpc.side_effect = [{**snapshot(WORKER, worker), **reply_patch}]
    with pytest.raises(SystemExit, match="worker"):
        asyncio.run(launcher.run(args))
    assert [entry.args[0] for entry in rpc.await_args_list] == ["session.create_worker"]


def test_old_gateway_never_falls_back_to_an_unlinked_chat(launch):
    launcher, rpc, args, _, _ = launch
    args.create_only, args.brief_file = True, None
    rpc.side_effect = launcher.GatewayRPCError()
    with pytest.raises(SystemExit, match="Update the Hermes gateway"):
        asyncio.run(launcher.run(args))
    assert [entry.args[0] for entry in rpc.await_args_list] == ["session.create_worker"]


def test_jwu_passes_only_parent_and_title_and_never_submits_brief(launch, capsys):
    launcher, rpc, args, _, worker = launch
    args.create_only = True
    args.brief_file = None
    args.title = "  Worker   launch test  "
    rpc.side_effect = [snapshot(WORKER, worker)]
    asyncio.run(launcher.run(args))
    assert rpc.await_args_list == [call("session.create_worker", session_id=PARENT, title=TITLE, request_id="a" * 32)]
    out = json.loads(capsys.readouterr().out)
    assert out["session_id"] == WORKER
    assert out["project_id"] == "p_test"
    assert "admission_status" not in out


@pytest.mark.parametrize("create_only", [True, False])
def test_legacy_brief_option_cannot_create_or_submit(launch, create_only):
    launcher, rpc, args, _, _ = launch
    args.create_only = create_only
    with pytest.raises(SystemExit, match="worker_relay.py"):
        asyncio.run(launcher.run(args))
    rpc.assert_not_awaited()


def test_archive_readback_only_reads_the_linked_worker(launch, monkeypatch, capsys):
    _, rpc, _, parent, worker = launch
    spec = importlib.util.spec_from_file_location("worker_relay", SCRIPTS / "worker_relay.py")
    assert spec is not None and spec.loader is not None
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    reader = Namespace(mailbox=Namespace(worker_parent=lambda _: PARENT))
    monkeypatch.setattr(relay, "worker_reader", lambda _: nullcontext((reader, "uid:501")))
    rpc.side_effect = [parent, {**worker, "archived": True}]
    args = Namespace(sender=PARENT, target=WORKER, title=TITLE, project="p_test", mode="check")
    asyncio.run(relay.run(args))
    result = json.loads(capsys.readouterr().out)
    assert result["target_id"] == WORKER and result["target_archived"] is True
    assert rpc.await_args_list == [call("session.info", session_id=PARENT),
                                   call("session.info", session_id=WORKER)]


def test_first_brief_keeps_clean_text_for_model_and_transcript(launch, tmp_path, monkeypatch):
    _, rpc, _, parent, worker = launch
    spec = importlib.util.spec_from_file_location("worker_relay", SCRIPTS / "worker_relay.py")
    assert spec is not None and spec.loader is not None
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    sender, target, source, destination = PARENT, WORKER, parent, worker
    reader = Namespace(mailbox=Namespace(worker_parent=lambda _: PARENT), admissions=Namespace(list=lambda **kw: []))
    monkeypatch.setattr(relay, "worker_reader", lambda _: nullcontext((reader, "uid:501")))
    body = "Возьми P/r#42."
    message = tmp_path / "message.md"
    message.write_text(body + "\n", encoding="utf-8")
    args = Namespace(mode="send", sender=sender, target=target, title=destination["title"],
                     project="p_test", message_file=message, key="same-delivery")
    rpc.side_effect = [source, destination, snapshot(target, destination),
                       {"admission_id": "same-admission"}, {"status": "queued"}] * 2

    asyncio.run(relay.run(args))
    args.key = "new-legacy-key"
    asyncio.run(relay.run(args))

    calls = rpc.await_args_list
    assert [c.args[0] for c in calls[:5]] == ["session.info", "session.info", "session.resume",
                                             "prompt.submit", "prompt.receipt"]
    assert calls[:5] == calls[5:]
    assert calls[2] == call("session.resume", session_id=target)
    assert calls[3].kwargs["session_id"] == target
    assert calls[3].kwargs["text"] == body
    assert set(calls[3].kwargs) == {"session_id", "text", "input_id"}
    assert calls[3].kwargs["input_id"] == uuid.uuid5(uuid.NAMESPACE_URL, f"jwu-first-brief:{sender}:{target}").hex


@pytest.mark.parametrize("legacy", ["queued-admission", "transcript-only", "other-principal"])
def test_started_legacy_worker_does_not_receive_a_second_brief(launch, tmp_path, monkeypatch, legacy):
    _, rpc, _, parent, worker = launch
    spec = importlib.util.spec_from_file_location("worker_relay", SCRIPTS / "worker_relay.py")
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    message = tmp_path / "message.md"
    message.write_text("First brief", encoding="utf-8")
    args = Namespace(mode="send", sender=PARENT, target=WORKER, title=TITLE,
                     project="p_test", message_file=message)
    snap = snapshot(WORKER, worker)
    rows = []
    if legacy == "transcript-only":
        snap["messages"] = [{"role": "user", "content": "Already sent"}]
    elif legacy == "queued-admission":
        rows = [Namespace(request_id="old-id", principal_id="uid:501", status="queued")]
    else:
        request_id = uuid.uuid5(uuid.NAMESPACE_URL, f"jwu-first-brief:{PARENT}:{WORKER}").hex
        rows = [Namespace(request_id=request_id, principal_id="old-owner", status="terminal")]
    ledger = Namespace(list=lambda *, session_id, pending_only: rows if not pending_only else [])
    reader = Namespace(mailbox=Namespace(worker_parent=lambda _: PARENT), admissions=ledger)
    monkeypatch.setattr(relay, "worker_reader", lambda _: nullcontext((reader, "uid:501")))
    rpc.side_effect = [parent, worker, snap, {"admission_id": "unexpected"}, {"status": "queued"}]

    with pytest.raises(SystemExit, match="prior activity"):
        asyncio.run(relay.run(args))
    assert not any(c.args[0] == "prompt.submit" for c in rpc.await_args_list)


@pytest.mark.parametrize("reverse", [False, True], ids=["display-only-link", "worker-to-main-archive"])
def test_relay_requires_a_native_worker_link_before_prompt(launch, monkeypatch, tmp_path, reverse):
    _, rpc, _, parent, worker = launch
    spec = importlib.util.spec_from_file_location("worker_relay", SCRIPTS / "worker_relay.py")
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    reader = Namespace(mailbox=Namespace(worker_parent=lambda _: None))
    monkeypatch.setattr(relay, "worker_reader", lambda _: nullcontext((reader, "uid:501")))
    sender, target, source, destination = (WORKER, PARENT, worker, parent) if reverse else (
        PARENT, WORKER, parent, worker,
    )
    message = tmp_path / "message.md"
    message.write_text("Пользователь подтвердил архив." if reverse else "Первый бриф.", encoding="utf-8")
    args = Namespace(mode="send", sender=sender, target=target, title=destination["title"],
                     project="p_test", message_file=message, key="delivery")
    rpc.side_effect = [source, destination, snapshot(target, destination),
                       {"admission_id": "unexpected"}, {"status": "queued"}]

    with pytest.raises(SystemExit, match="native worker"):
        asyncio.run(relay.run(args))
    assert not any(c.args[0] == "prompt.submit" for c in rpc.await_args_list)


def test_dry_run_does_not_create_or_read_brief(launch, capsys):
    launcher, rpc, args, parent, _ = launch
    args.dry_run = True
    args.brief_file.unlink()
    rpc.side_effect = [parent]
    asyncio.run(launcher.run(args))
    rpc.assert_not_awaited()
    assert json.loads(capsys.readouterr().out) == {"session_id": PARENT, "title": TITLE, "request_id": "a" * 32}


@pytest.mark.skipif(os.name != "posix", reason="POSIX wrapper")
def test_wrapper_uses_payload_venv_site_packages(tmp_path):
    payload = tmp_path / "Desktop payload"
    source = payload / "hermes-agent"
    source.mkdir(parents=True)
    (source / "gateway_probe.py").write_text("VALUE = 'source'\n", encoding="utf-8")
    bundled_bin = payload / "tools" / "runtime" / "bin"
    bundled_bin.mkdir(parents=True)
    (bundled_bin / "python3").symlink_to(Path(sys.executable).resolve())
    virtualenv = payload / "venv"
    venv.EnvBuilder(symlinks=True).create(virtualenv)
    python = virtualenv / "bin" / "python"
    site = Path(subprocess.check_output(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True,
    ).strip())
    (site / "venv_probe.py").write_text("VALUE = 'site-packages'\n", encoding="utf-8")
    env = {**os.environ, "HERMES_AGENT_PAYLOAD": str(payload)}
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(SCRIPTS / "run_with_gateway.sh"), "-c",
         "import sys, gateway_probe, venv_probe; print(sys.prefix, gateway_probe.VALUE, venv_probe.VALUE)"],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{virtualenv} source site-packages"
