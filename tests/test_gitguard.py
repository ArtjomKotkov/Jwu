"""Хук git-guard: разбор Bash-команд и отказ пишущему git мимо jwu."""

import io
import json
import subprocess

import pytest

from jwu.core import gitguard as gg
from jwu.core import gitpolicy as gp
from jwu.core.store import Store


@pytest.fixture
def env(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@git.example:proj/app.git"], check=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    subprocess.run(["git", "init", "-q", str(outside)], check=True)
    store = Store(tmp_path / "s.db")
    ws = store.get_workspace_by_slug("work")
    store.add_workspace_path(ws.id, str(repo))
    yield store, str(repo), str(outside), str(tmp_path / "s.db")
    store.close()


@pytest.mark.parametrize("cmd", [
    "git status", "git diff --stat", "git log -5 --oneline", "git show HEAD", "git branch -a",
    "git branch --show-current", "git branch --list 'PROJ-*'", "git remote -v", "git fetch origin",
    "git config --get user.name", "git stash list", "git tag -l", "git worktree list", "git rev-parse HEAD",
    "ls && git status | head", "echo $(git rev-parse --abbrev-ref HEAD)", "git -C . log",
])
def test_reads_allowed(env, cmd):
    store, repo, _, db = env
    assert gg.check(cmd, repo, store, [db]).allow, cmd


@pytest.mark.parametrize("cmd, sub", [
    ("git push", "push"), ("git commit -m x", "commit"), ("git add .", "add"),
    ("cd /x && git push origin develop", "push"), ("git -C . push", "push"),
    ("env GIT_X=1 git push", "push"), ("FOO=1 git commit -am x", "commit"),
    ("sh -c 'git push --force'", "push"), ('bash -lc "git reset --hard"', "reset"),
    ("eval git rebase develop", "rebase"), ("echo $(git push)", "push"), ("echo `git push`", "push"),
    ("git -c user.name=x commit -m y", "commit"), ("git branch new-branch", "branch"), ("git branch -D x", "branch"),
    ("git checkout -- a.txt", "checkout"), ("git clean -fdx", "clean"), ("git stash", "stash"),
    ("git tag v1", "tag"), ("git pull", "pull"), ("timeout 30 git push", "push"), ("nohup git push &", "push"),
    ("git status; git push", "push"), ("git status\ngit push", "push"), ("true || git merge x", "merge"),
    ("git config user.name x", "config"), ("git someunknownalias", "someunknownalias"),
])
def test_writes_blocked_in_workspace(env, cmd, sub):
    store, repo, _, db = env
    v = gg.check(cmd, repo, store, [db])
    assert not v.allow, cmd
    assert f"git {sub}" in v.reason and "jwu" in v.reason


def test_git_C_into_workspace_from_outside(env):
    store, repo, outside, db = env
    assert gg.check("git push", outside, store, [db]).allow           # вне воркспейса — не наше
    assert not gg.check(f"git -C {repo} push", outside, store, [db]).allow


def test_guard_off_for_repo(env):
    store, repo, _, db = env
    store.use_workspace(store.get_workspace_by_slug("work").id)
    pol = gp.ensure(store)
    pol.repos.append(gp.Repo(remote="proj/app", guard=False))
    gp.save(store, pol)
    assert gg.check("git push", repo, store, [db]).allow


@pytest.mark.parametrize("cmd", [
    "jwu git grant 3", "jwu git policy edit", "jwu git policy init", "jwu -W work git rule add --op push=allow",
    "jwu git repo set proj/app --op push=allow", "jwu job push 5 -y", "jwu workspace setting git.x 1",
    "sqlite3 {db} 'update git_policy set guard=0'",
])
def test_policy_changes_blocked_everywhere(env, cmd):
    store, _, outside, db = env
    assert not gg.check(cmd.format(db=db), outside, store, [db]).allow, cmd


@pytest.mark.parametrize("cmd", ["jwu git policy show", "jwu git policy check push --branch x", "jwu git log",
                                 "jwu git repo list", "jwu todo", "sqlite3 other.db .tables"])
def test_jwu_reads_allowed(env, cmd):
    store, _, outside, db = env
    assert gg.check(cmd, outside, store, [db]).allow, cmd


def test_main_outputs_deny_json(env, monkeypatch, capsys):
    store, repo, _, db = env
    monkeypatch.setenv("JWU_DB_PATH", db)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": repo})))
    gg.main()
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "ls -la"}, "cwd": repo})))
    gg.main()
    assert capsys.readouterr().out == ""


def test_install_uninstall_keeps_foreign_hooks(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "other-hook"}]}]}}))
    gg.install(path, command="/bin/jwu-git-guard")
    gg.install(path, command="/bin/jwu-git-guard")                 # повтор не дублирует
    data = json.loads(path.read_text())
    cmds = [h["command"] for e in data["hooks"]["PreToolUse"] for h in e["hooks"]]
    assert cmds == ["other-hook", "/bin/jwu-git-guard"] and gg.installed(path)
    gg.uninstall(path)
    data = json.loads(path.read_text())
    assert [h["command"] for e in data["hooks"]["PreToolUse"] for h in e["hooks"]] == ["other-hook"]
    assert not gg.installed(path)
