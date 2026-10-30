"""Уборка рабочих копий под политикой: jwu_git_worktree_remove, хук, sweep."""

import subprocess
import time

import pytest

from jwu.core import gitguard as gg
from jwu.core import gitops as go
from jwu.core import gitpolicy as gp
from jwu.core import sweep
from jwu.core.store import Store


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(go, "patch_dir", lambda: tmp_path / "backups")
    origin = tmp_path / "proj" / "app.git"
    origin.mkdir(parents=True)
    sh(origin, "init", "-q", "--bare", "-b", "main")
    repo = tmp_path / "app"
    sh(tmp_path, "clone", "-q", str(origin), str(repo))
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        sh(repo, "config", k, v)
    (repo / "a.txt").write_text("1\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-qm", "init")
    sh(repo, "push", "-q", "origin", "main")
    store = Store(tmp_path / "s.db")
    ws = store.get_workspace_by_slug("work")
    store.use_workspace(ws.id)
    store.add_workspace_path(ws.id, str(repo))
    yield store, repo, tmp_path
    store.close()


def agent_tree(repo, name="agent-abc"):
    """Как harness: копия в .claude/worktrees на ветке worktree-<имя>, правки — в индексе."""
    path = repo / ".claude" / "worktrees" / name
    sh(repo, "worktree", "add", "-q", "-b", f"worktree-{name}", str(path))
    (path / "a.txt").write_text("changed\n")
    (path / "new.txt").write_text("new\n")
    sh(path, "add", "-A")
    (path / "loose.txt").write_text("untracked\n")          # и неотслеживаемое — тоже в бэкап
    return path


def test_policy_knows_worktree_op(env):
    store = env[0]
    pol = gp.ensure(store)
    assert "worktree" in gp.OPS and pol.ops["worktree"] == "ask"
    pol.rules.insert(0, gp.Rule(name="main владельца", branch=["main"], ops={"worktree": "allow"}))
    pol = gp.save(store, pol)
    assert gp.decide(pol, "worktree", branch="main", where="main").level == "allow"
    assert gp.decide(pol, "worktree", branch="dev", where="main").level == "ask"


def test_remove_agent_tree_with_staged_changes(env):
    store, repo, tmp = env
    path = agent_tree(repo)
    with pytest.raises(go.Blocked) as e:
        go.worktree_remove(store, str(path))
    assert e.value.level == "ask" and path.exists()
    res = go.worktree_remove(store, str(path), user_approved=True)
    assert res["ok"] and not path.exists() and res["branch"] == "worktree-agent-abc" and res["branch_deleted"]
    assert "worktree-agent-abc" not in sh(repo, "branch", "--list")
    assert "agent-abc" not in sh(repo, "worktree", "list")
    b = res["backup"]
    assert b["files"] == 3 and b["ref"].startswith(go.BACKUP_PREFIX) and "-worktree-agent-abc" in b["ref"]
    assert sh(repo, "rev-parse", "--verify", b["ref"])
    # патч накладывается на main — правки агента не потеряны
    sh(repo, "apply", "--index", b["patch"])
    assert (repo / "a.txt").read_text() == "changed\n" and (repo / "loose.txt").exists()
    # undo по бэкапу копии не сбрасывает main, а подсказывает, как вернуть
    ctx = go.context(store, str(repo))
    with pytest.raises(go.GitOpError, match="бэкап убранной копии"):
        go.undo(ctx, b["ref"], user_approved=True)


def test_allowed_by_rule_without_yes(env):
    store, repo, _ = env
    pol = gp.ensure(store)
    pol.rules.insert(0, gp.Rule(name="main владельца", branch=["main"], ops={"worktree": "allow"}))
    gp.save(store, pol)
    path = agent_tree(repo, "agent-x")
    assert go.worktree_remove(store, str(path))["ok"] and not path.exists()


def test_refuses_outside_allowed_places_and_non_worktrees(env):
    store, repo, tmp = env
    other = tmp / "elsewhere"
    sh(repo, "worktree", "add", "-q", "-b", "side", str(other))
    with pytest.raises(go.GitOpError, match="только копии"):
        go.worktree_remove(store, str(other), user_approved=True)
    assert other.exists()
    plain = repo / ".claude" / "worktrees" / "not-a-tree"
    plain.mkdir(parents=True)
    with pytest.raises(go.GitOpError, match="не рабочая копия"):
        go.worktree_remove(store, str(plain), user_approved=True)


def test_job_branch_needs_its_own_decision(env):
    from jwu.core import jobspace

    store, repo, tmp = env
    jobspace.set_jobs_dir(store, store.workspace_id, str(tmp / "jobs"))
    path = tmp / "jobs" / "app-job7"
    sh(repo, "worktree", "add", "-q", "-b", "PROJ-7-fix", str(path))
    pol = gp.ensure(store)
    pol.ops["worktree"] = "allow"
    gp.save(store, pol)
    res = go.worktree_remove(store, str(path))                 # копия — allow, ветка задачи — ask
    assert res["ok"] and not path.exists() and not res["branch_deleted"]
    assert res["branch_blocked"]["blocked"] == "ask" and "PROJ-7-fix" in sh(repo, "branch", "--list")


def test_prune_from_repo_root(env):
    import shutil

    store, repo, _ = env
    path = agent_tree(repo, "agent-gone")
    shutil.rmtree(path)
    res = go.worktree_remove(store, str(repo), user_approved=True)
    assert res["ok"] and any("agent-gone" in p for p in res["pruned"])


def test_branch_delete_explains_busy_branch(env):
    store, repo, _ = env
    agent_tree(repo, "agent-busy")
    ctx = go.context(store, str(repo))
    with pytest.raises(go.GitOpError, match="jwu_git_worktree_remove"):
        go.delete_branch(ctx, "worktree-agent-busy", user_approved=True)


@pytest.mark.parametrize("cmd", ["git worktree remove --force .claude/worktrees/agent-a", "git worktree prune",
                                 "git -C . worktree remove x"])
def test_hook_sends_worktree_to_tool(env, cmd):
    store, repo, tmp = env
    v = gg.check(cmd, str(repo), store, [str(tmp / "s.db")])
    assert not v.allow and "jwu_git_worktree_remove" in v.reason


def test_hook_lets_list_and_ignores_heredoc_data(env):
    store, repo, tmp = env
    db = [str(tmp / "s.db")]
    assert gg.check("git worktree list --porcelain", str(repo), store, db).allow
    assert gg.check("cat > f.py <<'EOF'\ngit push\ngit worktree remove x\nEOF\necho ok", str(repo), store, db).allow
    assert gg.check("python3 - <<'PY'\ngit commit -m x\nPY", str(repo), store, db).allow
    assert not gg.check("bash <<EOF\ngit push\nEOF", str(repo), store, db).allow          # тело читает shell
    assert not gg.check("cat <<EOF\n$(git push)\nEOF", str(repo), store, db).allow       # подстановка выполнится
    assert gg.check("cat <<'EOF'\n$(git push)\nEOF", str(repo), store, db).allow          # в кавычках — данные
    assert not gg.check("cat > f <<'EOF'\nx\nEOF\ngit push", str(repo), store, db).allow  # после тела — команда


def test_hook_ignores_non_bash_tools(env, monkeypatch, capsys):
    import io
    import json

    store, repo, tmp = env
    monkeypatch.setenv("JWU_DB_PATH", str(tmp / "s.db"))
    for tool in ("EnterWorktree", "ExitWorktree", "Agent"):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
            {"tool_name": tool, "tool_input": {"command": "git worktree add x"}, "cwd": str(repo)})))
        gg.main()
    assert capsys.readouterr().out == ""


def test_sweep_finds_idle_agent_trees_and_removes_with_backup(env):
    store, repo, tmp = env
    path = agent_tree(repo, "agent-old")
    fresh = agent_tree(repo, "agent-fresh")
    runner = lambda cmd: (1, "") if cmd[0] == "docker" else go._run(cmd)  # noqa: E731
    later = time.time() + 3 * 3600
    plan = sweep.scan(store, runner=runner, now=later)
    found = [i["path"] for i in plan["orphan_worktrees"] if i.get("kind") == "agent"]
    assert str(path) in found and str(fresh) in found                   # обе «старые» через 3 часа
    plan_now = sweep.scan(store, runner=runner)
    assert not [i for i in plan_now["orphan_worktrees"] if i.get("kind") == "agent"]   # сейчас — живые
    plan["orphan_worktrees"] = [i for i in plan["orphan_worktrees"] if i["path"] == str(path)]
    res = sweep.apply(store, ["orphan_worktrees"], runner=runner, plan=plan)
    assert not path.exists() and fresh.exists() and res["done"]["orphan_worktrees"][0].startswith(str(path))
    assert any("-worktree-agent-old" in r for r in sh(repo, "for-each-ref", "refs/jwu/backup/").splitlines())


def test_cli_rule_with_worktree_op(env, monkeypatch):
    from typer.testing import CliRunner

    from jwu.cli import main as cli

    store, repo, tmp = env
    monkeypatch.setenv("JWU_DB_PATH", str(tmp / "s.db"))
    run = lambda *a: CliRunner().invoke(cli.app, ["-W", "work", "git", *a])  # noqa: E731
    r = run("rule", "add", "--repo", "dndeck/dndeck", "--branch", "main", "--at", "1", "--name", "main владельца",
            "--op", "commit=allow", "--op", "push=ask", "--op", "force_push=deny", "--op", "history=ask",
            "--op", "sync=ask", "--op", "discard=ask", "--op", "delete_branch=allow", "--op", "worktree=allow")
    assert "нет" in r.output or r.exit_code == 2                    # репозитория в политике ещё нет
    assert run("repo", "set", "dndeck/dndeck").exit_code == 0
    r = run("rule", "add", "--repo", "dndeck/dndeck", "--branch", "main", "--at", "1", "--name", "main владельца",
            "--op", "commit=allow", "--op", "push=ask", "--op", "force_push=deny", "--op", "history=ask",
            "--op", "sync=ask", "--op", "discard=ask", "--op", "delete_branch=allow", "--op", "worktree=allow")
    assert r.exit_code == 0, r.output
    out = " ".join(run("policy", "show").output.split())
    assert "main владельца" in out and "worktree allow" in out
    r = run("policy", "check", "worktree", "--repo", "dndeck/dndeck", "--branch", "main", "--where", "main")
    text = " ".join(r.output.split())
    assert "allow" in text and "main владельца" in text
