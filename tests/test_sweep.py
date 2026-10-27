"""Уборка за всеми сессиями: находит оставшееся, открытое не трогает, несохранённое — только с force."""

import json
import os
import subprocess
import time

import pytest

from jwu.core import jobspace, sweep, todo
from jwu.core.store import Store


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def no_docker(cmd, cwd=None):
    if cmd[0] == "docker":
        return 1, "no docker"
    return jobspace._run(cmd, cwd)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "temp_root", lambda: tmp_path / "tmpjwu")
    monkeypatch.setattr(jobspace, "require_space", lambda *a, **k: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    (repo / "a").write_text("1")
    git("add", ".", cwd=repo)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "i", cwd=repo)
    git("init", "--bare", "-b", "main", str(tmp_path / "remote.git"), cwd=tmp_path)
    git("remote", "add", "origin", str(tmp_path / "remote.git"), cwd=repo)
    git("push", "origin", "main", cwd=repo)
    store = Store(tmp_path / "s.db")
    ws = store.get_workspace_by_slug("work")
    store.use_workspace(ws.id)
    jobspace.set_jobs_dir(store, ws.id, str(tmp_path / "jobs"))
    yield store, repo, tmp_path
    store.close()


def _tree(repo, path, branch):
    git("worktree", "add", "-b", branch, str(path), cwd=repo)


def test_scan_finds_orphans_and_keeps_open_and_unsaved(env):
    store, repo, tmp = env
    open_job = store.create_job("APP-1", "открытая")
    closed = store.create_job("APP-2", "закрытая")
    store.set_job_status(closed.id, "done")
    jobs = tmp / "jobs"
    _tree(repo, jobs / f"repo-job{open_job.id}-APP-1", "b1")       # открытая — не трогаем
    _tree(repo, jobs / f"repo-job{closed.id}-APP-2", "b2")         # закрытая — убрать
    _tree(repo, jobs / "repo-job999-APP-9", "b9")                  # работы нет — убрать, но тут незакоммиченное
    (jobs / "repo-job999-APP-9" / "x").write_text("dirty")
    old = tmp / "tmpjwu" / "APP-5"
    old.mkdir(parents=True)
    os.utime(old, (time.time() - 3 * 86400,) * 2)
    (tmp / "tmpjwu" / "APP-6").mkdir()                              # свежая — не трогаем

    plan = sweep.scan(store, runner=no_docker)
    assert [i["job_id"] for i in plan["orphan_worktrees"]] == [closed.id]
    assert [k["job_id"] for k in plan["kept"]] == [999]
    assert [i["path"] for i in plan["temp"]] == [str(old)]

    res = sweep.apply(store, sweep.GROUPS, runner=no_docker, plan=plan)
    assert not res["errors"]
    assert not (jobs / f"repo-job{closed.id}-APP-2").exists() and not old.exists()
    assert (jobs / f"repo-job{open_job.id}-APP-1").exists() and (jobs / "repo-job999-APP-9").exists()

    res = sweep.apply(store, ["orphan_worktrees"], force=True, runner=no_docker)
    assert not (jobs / "repo-job999-APP-9").exists()


def test_dead_session_released_only_after_long_silence(env):
    store, _, _ = env
    job = store.create_job("APP-3", "в работе")
    store.add_job_record(job.id, f"{todo.SESSION_MARK} «Old-Owl»", kind="note")
    long_ago = "2020-01-01T00:00:00+00:00"
    store.set_meta(todo.SESSION_META + "Old-Owl", json.dumps({"seen": long_ago, "pid": 0}))
    from datetime import datetime, timezone
    store.set_meta(todo.SESSION_META + "Just-Now", json.dumps({"seen": datetime.now(timezone.utc).isoformat(), "pid": 0}))

    plan = sweep.scan(store, runner=no_docker)
    assert [(s["session"], s["jobs"]) for s in plan["dead_sessions"]] == [("Old-Owl", [job.id])]
    sweep.apply(store, ["dead_sessions"], runner=no_docker, plan=plan)
    assert store.get_job(job.id).status == "paused"
    assert "Just-Now" in todo.sessions(store)
