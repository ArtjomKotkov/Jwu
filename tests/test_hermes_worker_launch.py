"""Offline tests for the packaged Hermes worker entry points."""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import venv
from argparse import Namespace
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, call

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "src/jwu/skills/jwu-hermes-orchestrator/scripts"
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

    client.__dict__["connect_gateway"] = connect_gateway
    spec = importlib.util.spec_from_file_location("worker_launch", SCRIPTS / "worker_launch.py")
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher.os, "getuid", lambda: 501, raising=False)
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
    worker = {**parent, "title": TITLE}
    return launcher, rpc, args, parent, worker


def snapshot(session_id, info):
    return {"session_id": session_id, "stored_session_id": session_id, "info": info}


@pytest.mark.parametrize("reused_id", [PARENT, EXISTING])
def test_title_reuse_cannot_receive_brief(launch, reused_id):
    launcher, rpc, args, parent, worker = launch
    # A titled create can return a wholly matching existing chat, not a reuse flag.
    rpc.side_effect = [parent, snapshot(reused_id, worker), worker, snapshot(reused_id, worker),
                       {"admission_id": "admission"}, {"status": "queued"}]
    with pytest.raises(SystemExit, match="new worker"):
        asyncio.run(launcher.run(args))
    assert [entry.args[0] for entry in rpc.await_args_list] == ["session.info", "session.create"]


@pytest.mark.parametrize("field,bad_value", [
    ("project", {"id": "p_other"}), ("project", None), ("cwd", "."), ("title", "Other"),
    ("model", "other-model"), ("provider", "other-provider"), ("provider", None),
    ("archived", True), ("archived", None), ("profile_id", "other-profile"),
])
def test_worker_readback_mismatch_stops_before_resume(launch, field, bad_value):
    launcher, rpc, args, parent, worker = launch
    rpc.side_effect = [parent, snapshot(WORKER, worker), {**worker, field: bad_value},
                       snapshot(WORKER, worker), {"admission_id": "admission"}, {"status": "queued"}]
    with pytest.raises(SystemExit, match=field):
        asyncio.run(launcher.run(args))
    assert [entry.args[0] for entry in rpc.await_args_list] == ["session.info", "session.create", "session.info"]


@pytest.mark.parametrize("field,bad_value", [("project", None), ("provider", None), ("cwd", ".")])
def test_unverified_parent_cannot_create_worker(launch, field, bad_value):
    launcher, rpc, args, parent, worker = launch
    rpc.side_effect = [{**parent, field: bad_value}, snapshot(WORKER, worker), worker,
                       snapshot(WORKER, worker), {"admission_id": "admission"}, {"status": "queued"}]
    with pytest.raises(SystemExit, match=field):
        asyncio.run(launcher.run(args))
    assert rpc.await_args_list == [call("session.info", session_id=PARENT)]


def test_create_only_uses_gateway_fields_and_accepts_symlinked_cwd(launch, tmp_path, capsys):
    launcher, rpc, args, parent, worker = launch
    real_cwd = tmp_path / "project"
    real_cwd.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_cwd)
    parent.update(cwd=str(alias), reasoning_effort="high", fast=True, service_tier="priority",
                  launch_request={"source": "gui", "model": "stale-model", "provider": "stale-provider",
                                  "reasoning": "low", "cwd_explicit": False, "fast": True})
    worker["cwd"] = str(real_cwd)
    rpc.side_effect = [parent, snapshot(WORKER, worker), worker]
    args.create_only = True
    asyncio.run(launcher.run(args))
    assert rpc.await_args_list == [
        call("session.info", session_id=PARENT),
        call("session.create", source="gui", title=TITLE, request_id="a" * 32, cwd=str(alias),
             row_cwd=str(alias), cwd_explicit=True, model="selected-model", provider="selected-provider",
             reasoning="high", service_tier="priority"),
        call("session.info", session_id=WORKER),
    ]
    assert json.loads(capsys.readouterr().out)["session_id"] == WORKER


@pytest.mark.parametrize("changed", [
    {"session_id": EXISTING}, {"stored_session_id": EXISTING}, {"session_id": None}, {"info": None},
])
def test_resume_must_return_the_exact_session_snapshot(launch, changed):
    launcher, rpc, args, parent, worker = launch
    rpc.side_effect = [parent, snapshot(WORKER, worker), worker, {**snapshot(WORKER, worker), **changed},
                       {"admission_id": "admission"}, {"status": "queued"}]
    with pytest.raises(SystemExit):
        asyncio.run(launcher.run(args))
    assert [entry.args[0] for entry in rpc.await_args_list] == [
        "session.info", "session.create", "session.info", "session.resume",
    ]


def test_resume_rechecks_metadata_before_prompt(launch):
    launcher, rpc, args, parent, worker = launch
    rpc.side_effect = [parent, snapshot(WORKER, worker), worker,
                       snapshot(WORKER, {**worker, "provider": "changed-provider"}),
                       {"admission_id": "admission"}, {"status": "queued"}]
    with pytest.raises(SystemExit, match="provider"):
        asyncio.run(launcher.run(args))
    assert rpc.await_args_list[-1].args == ("session.resume",)


def test_submits_brief_to_verified_worker_and_reports_receipt(launch, capsys):
    launcher, rpc, args, parent, worker = launch
    rpc.side_effect = [parent, snapshot(WORKER, worker), worker, snapshot(WORKER, worker),
                       {"admission_id": "admission"}, {"status": "queued"}]
    asyncio.run(launcher.run(args))
    assert rpc.await_args_list[-3:] == [
        call("session.resume", session_id=WORKER, omit_messages=True),
        call("prompt.submit", session_id=WORKER, input_id="a" * 32, text="a worker brief"),
        call("prompt.receipt", session_id=WORKER, admission_id="admission"),
    ]
    assert json.loads(capsys.readouterr().out) == {
        "session_id": WORKER, "title": TITLE, "project_id": "p_test", "cwd": worker["cwd"],
        "model": worker["model"], "provider": worker["provider"], "archived": False,
        "admission_id": "admission", "admission_status": "queued",
    }


def test_empty_brief_is_rejected_before_create(launch):
    launcher, rpc, args, parent, worker = launch
    args.brief_file.write_text(" \n", encoding="utf-8")
    rpc.side_effect = [parent, snapshot(WORKER, worker), worker]
    with pytest.raises(SystemExit, match="Empty brief"):
        asyncio.run(launcher.run(args))
    assert not any(entry.args[0] == "session.create" for entry in rpc.await_args_list)


def test_launch_normalizes_title_before_create(launch):
    launcher, rpc, args, parent, worker = launch
    args.title = "  Worker   launch test  "
    rpc.side_effect = [parent, snapshot(WORKER, worker), worker, snapshot(WORKER, worker),
                       {"admission_id": "admission"}, {"status": "queued"}]
    asyncio.run(launcher.run(args))
    assert rpc.await_args_list[1].kwargs["title"] == TITLE


def test_archive_rejects_an_omitted_paused_job(launch, monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("worker_archive", SCRIPTS / "worker_archive.py")
    assert spec is not None and spec.loader is not None
    archive = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(archive)
    done = {"id": 1, "status": "done", "records": [{"kind": "summary", "text": WORKER}]}
    paused = {"id": 2, "status": "paused", "records": [{"kind": "note", "text": WORKER}]}

    def check_output(argv, *, cwd):
        assert cwd == str(tmp_path)
        if argv[1:3] == ["workspace", "current"]:
            return json.dumps({"slug": "jwu", "source": "cwd"})
        command = argv[3:]
        if command[:2] == ["job", "show"]:
            return json.dumps(done)
        return json.dumps([] if "active" in command else [done, paused])

    monkeypatch.setattr(archive.subprocess, "check_output", check_output)
    with pytest.raises(SystemExit, match="#2"):
        archive.check_jobs(Namespace(workspace="jwu", workdir=str(tmp_path), job=[1], target=WORKER))


def test_dry_run_does_not_create_or_read_brief(launch, capsys):
    launcher, rpc, args, parent, _ = launch
    args.dry_run = True
    args.brief_file.unlink()
    rpc.side_effect = [parent]
    asyncio.run(launcher.run(args))
    assert rpc.await_args_list == [call("session.info", session_id=PARENT)]
    assert json.loads(capsys.readouterr().out)["title"] == TITLE


def test_explicit_normal_service_tier_is_not_replaced_by_profile_default(launch, capsys):
    launcher, rpc, args, parent, _ = launch
    args.dry_run = True
    rpc.side_effect = [{**parent, "service_tier": ""}]
    asyncio.run(launcher.run(args))
    assert json.loads(capsys.readouterr().out)["service_tier"] == ""


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
