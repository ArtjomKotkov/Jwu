"""Пуш работы: только своя ветка копии, без force, под политикой git, не при грязной копии."""

import subprocess

import pytest

from jwu.core import gitops, gitpolicy, jobspace
from jwu.core.store import Store


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def env(tmp_path):
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-b", "develop", str(remote), cwd=tmp_path)
    repo = tmp_path / "repo"
    git("clone", str(remote), str(repo), cwd=tmp_path)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git("config", k, v, cwd=repo)
    (repo / "a.txt").write_text("1")
    git("add", ".", cwd=repo)
    git("commit", "-m", "init", cwd=repo)
    git("push", "origin", "HEAD:develop", cwd=repo)
    store = Store(tmp_path / "s.db")
    ws = store.get_workspace_by_slug("work")
    store.use_workspace(ws.id)
    jobspace.set_jobs_dir(store, ws.id, str(tmp_path / "jobs"))
    job = store.create_job("APP-1", "Сделать задачу")
    job = store.get_job(job if isinstance(job, int) else job.id)
    yield store, job, repo, remote
    store.close()


def _commit(path, name):
    (path / name).write_text(name)
    git("add", ".", cwd=path)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", name, cwd=path)


def test_push_new_branch_then_nothing(env, monkeypatch):
    store, job, repo, remote = env
    monkeypatch.setattr(jobspace, "require_space", lambda *a, **k: None)
    tree = jobspace.add_worktree(store, job, str(repo), branch="APP-1-fix", base="develop")
    from pathlib import Path
    wt = Path(tree["ref"])
    _commit(wt, "b.txt")
    plan = jobspace.push_worktree(store, job, dry_run=True)
    assert plan["new_branch"] and len(plan["commits"]) == 1 and not plan["pushed"]
    with pytest.raises(gitops.Blocked):                  # push по умолчанию — ask
        jobspace.push_worktree(store, job)
    res = jobspace.push_worktree(store, job, user_approved=True)
    assert res["pushed"] and "APP-1-fix" in git("branch", "--list", "APP-1-fix", cwd=remote)
    assert jobspace.push_worktree(store, job, user_approved=True)["commits"] == []
    (wt / "c.txt").write_text("dirty")
    with pytest.raises(jobspace.JobSpaceError, match="незакоммиченное"):
        jobspace.push_worktree(store, job, user_approved=True)


def test_refuses_protected_and_diverged(env, monkeypatch, tmp_path):
    store, job, repo, remote = env
    monkeypatch.setattr(jobspace, "require_space", lambda *a, **k: None)
    from pathlib import Path
    tree = jobspace.add_worktree(store, job, str(repo), branch="APP-1-fix", base="develop")
    wt = Path(tree["ref"])
    _commit(wt, "b.txt")
    jobspace.push_worktree(store, job, user_approved=True)
    other = tmp_path / "other"
    git("clone", "-b", "APP-1-fix", str(remote), str(other), cwd=tmp_path)
    _commit(other, "x.txt")
    git("push", "origin", "APP-1-fix", cwd=other)
    _commit(wt, "y.txt")
    with pytest.raises(jobspace.JobSpaceError, match="разошлась"):
        jobspace.push_worktree(store, job, user_approved=True)
    git("pull", "--no-rebase", "-q", "origin", "APP-1-fix", cwd=wt)
    pol = gitpolicy.ensure(store)
    pol.rules.insert(0, gitpolicy.Rule(name="наши", key=["APP"], ops={"push": "deny"}))
    gitpolicy.save(store, pol)
    with pytest.raises(gitops.Blocked) as e:
        jobspace.push_worktree(store, job, user_approved=True)
    assert e.value.level == "deny" and "наши" in e.value.rule


def test_no_worktree(env):
    store, job, _, _ = env
    with pytest.raises(jobspace.JobSpaceError, match="нет рабочей копии"):
        jobspace.push_worktree(store, job)


def test_default_policy_covers_main_branches(env):
    store = env[0]
    pol = gitpolicy.ensure(store)
    for b in ("develop", "master", "main", "release/10.8", "release-10.8"):
        assert gitpolicy.decide(pol, "push", branch=b).level in ("grant", "deny"), b
    assert gitpolicy.decide(pol, "push", branch="APP-1-fix", where="job").level == "ask"
