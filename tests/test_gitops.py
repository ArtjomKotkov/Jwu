"""Git-операции под политикой (core.gitops) — на настоящем временном репозитории с bare origin."""

import subprocess

import pytest

from jwu.core import gitops as go
from jwu.core import gitpolicy as gp
from jwu.core.store import Store


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "proj" / "app.git"
    origin.mkdir(parents=True)
    sh(origin, "init", "-q", "--bare", "-b", "develop")
    work = tmp_path / "work"
    sh(tmp_path, "clone", "-q", str(origin), str(work))
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        sh(work, "config", k, v)
    (work / "a.txt").write_text("1\n")
    sh(work, "add", "-A")
    sh(work, "commit", "-qm", "init")
    sh(work, "push", "-q", "origin", "develop")
    sh(work, "remote", "set-head", "origin", "develop")
    return work


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.db")
    s.use_workspace(s.get_workspace_by_slug("work").id)
    yield s
    s.close()


def as_job(store, path):
    job = store.create_job("PROJ-1", "x")
    store.conn.execute(
        "INSERT INTO job_resources (workspace_id, job_id, kind, ref, meta, created_at) VALUES (?, ?, 'worktree', ?, '{}', 'now')",
        (store.workspace_id, job.id, str(path)))
    store.conn.commit()
    return job


def test_context_knows_repo_where_and_target(store, repo):
    ctx = go.context(store, str(repo))
    assert ctx.remote == "proj/app" and ctx.branch == "develop" and ctx.where == "main" and ctx.job_id is None
    assert gp.load(store).repo("proj/app").target == "develop"      # репозиторий заведён сам
    job = as_job(store, repo)
    ctx = go.context(store, str(repo))
    assert ctx.where == "job" and ctx.job_id == job.id


def test_commit_allowed_in_job_copy_blocked_on_develop(store, repo):
    as_job(store, repo)
    ctx = go.context(store, str(repo))
    (repo / "a.txt").write_text("2\n")
    with pytest.raises(go.Blocked) as e:                 # develop — «общие ветки»: commit = ask
        go.commit(ctx, "fix")
    assert e.value.level == "ask" and "user_approved" in e.value.retry
    assert sh(repo, "diff", "--cached") == ""            # отказ ничего не трогает
    go.switch(ctx, "PROJ-1-fix", create=True)
    res = go.commit(ctx, "PROJ-1: fix")
    assert res["committed"] and sh(repo, "log", "-1", "--format=%s") == "PROJ-1: fix"
    recs = [r.text for r in store.get_job(ctx.job_id).records]
    assert any("⛔ политика" in t for t in recs) and any("коммит" in t for t in recs)


def test_main_folder_commit_needs_yes(store, repo):
    ctx = go.context(store, str(repo))
    sh(repo, "switch", "-qc", "PROJ-2-x")
    ctx.refresh()
    (repo / "b.txt").write_text("b")
    with pytest.raises(go.Blocked):
        go.commit(ctx, "b")
    assert go.commit(ctx, "b", user_approved=True)["committed"]


def test_push_ask_then_approved(store, repo):
    as_job(store, repo)
    ctx = go.context(store, str(repo))
    go.switch(ctx, "PROJ-1-fix", create=True)
    (repo / "a.txt").write_text("3\n")
    go.commit(ctx, "PROJ-1: three")
    plan = go.push(ctx, dry_run=True)
    assert plan["new_branch"] and len(plan["commits"]) == 1 and not plan["pushed"]
    with pytest.raises(go.Blocked) as e:
        go.push(ctx)
    assert e.value.level == "ask"
    assert go.push(ctx, user_approved=True)["pushed"]
    assert sh(repo, "ls-remote", "origin", "PROJ-1-fix")


def test_push_to_develop_needs_grant_not_just_yes(store, repo):
    as_job(store, repo)
    ctx = go.context(store, str(repo))
    sent = []
    ctx.notify = lambda st, req: sent.append(req)
    (repo / "a.txt").write_text("4\n")
    go.commit(ctx, "four", user_approved=True)
    with pytest.raises(go.Blocked) as e:
        go.push(ctx, user_approved=True)                 # «да» не хватает
    b = e.value.to_dict()
    assert b["blocked"] == "grant" and b["grant_id"] and "jwu git grant" in b["ask_user"] and len(sent) == 1
    with pytest.raises(go.Blocked):                      # повтор — тот же запрос, без второго уведомления
        go.push(ctx, user_approved=True)
    assert len(sent) == 1
    gp.answer_grant(store, b["grant_id"], allow=True, via="cli")
    assert go.push(ctx, user_approved=True)["pushed"]
    (repo / "a.txt").write_text("5\n")
    go.commit(ctx, "five", user_approved=True)
    ctx.refresh()
    with pytest.raises(go.Blocked):                      # допуск одноразовый и на тот HEAD
        go.push(ctx, user_approved=True)


def test_push_master_denied(store, repo):
    as_job(store, repo)
    ctx = go.context(store, str(repo))
    go.switch(ctx, "master", create=True)
    (repo / "a.txt").write_text("m\n")
    with pytest.raises(go.Blocked) as e:                 # и коммит в master запрещён
        go.commit(ctx, "m", user_approved=True)
    assert e.value.level == "deny"
    sh(repo, "commit", "-qam", "m")                      # закоммитили мимо jwu — пуш всё равно нет
    ctx.refresh()
    with pytest.raises(go.Blocked) as e:
        go.push(ctx, user_approved=True)
    assert e.value.level == "deny" and "прод-ветки" in e.value.rule


def test_sync_backup_and_undo(store, repo, tmp_path):
    as_job(store, repo)
    ctx = go.context(store, str(repo))
    go.switch(ctx, "PROJ-1-fix", create=True)
    (repo / "mine.txt").write_text("m")
    go.commit(ctx, "mine")
    other = tmp_path / "other"
    sh(tmp_path, "clone", "-q", "-b", "develop", str(tmp_path / "proj" / "app.git"), str(other))
    for k, v in (("user.email", "o@o"), ("user.name", "o")):
        sh(other, "config", k, v)
    (other / "theirs.txt").write_text("t")
    sh(other, "add", "-A")
    sh(other, "commit", "-qm", "theirs")
    sh(other, "push", "-q", "origin", "develop")
    before = ctx.head
    res = go.sync(ctx)
    assert res["ok"] and res["mode"] == "merge" and (repo / "theirs.txt").exists()
    assert res["backup"].startswith(go.BACKUP_PREFIX)
    with pytest.raises(go.Blocked):
        go.undo(ctx, res["backup"])
    go.undo(ctx, res["backup"], user_approved=True)
    assert sh(repo, "rev-parse", "HEAD") == before and not (repo / "theirs.txt").exists()


def test_sync_conflict_then_abort(store, repo, tmp_path):
    as_job(store, repo)
    ctx = go.context(store, str(repo))
    go.switch(ctx, "PROJ-1-fix", create=True)
    (repo / "a.txt").write_text("mine\n")
    go.commit(ctx, "mine")
    other = tmp_path / "other"
    sh(tmp_path, "clone", "-q", "-b", "develop", str(tmp_path / "proj" / "app.git"), str(other))
    for k, v in (("user.email", "o@o"), ("user.name", "o")):
        sh(other, "config", k, v)
    (other / "a.txt").write_text("theirs\n")
    sh(other, "commit", "-qam", "theirs")
    sh(other, "push", "-q", "origin", "develop")
    res = go.sync(ctx)
    assert not res["ok"] and res["conflicts"] == ["a.txt"]
    assert go.status(ctx)["in_progress"] == "merge"
    go.sync_abort(ctx)
    assert go.status(ctx)["in_progress"] == ""


def test_discard_backup_restores_dirty(store, repo):
    as_job(store, repo)
    ctx = go.context(store, str(repo))
    go.switch(ctx, "PROJ-1-fix", create=True)
    (repo / "a.txt").write_text("dirty\n")
    with pytest.raises(go.Blocked):
        go.discard(ctx)
    res = go.discard(ctx, user_approved=True)
    assert (repo / "a.txt").read_text() == "1\n"
    go.undo(ctx, res["backup"], user_approved=True)
    assert (repo / "a.txt").read_text() == "dirty\n"


def test_history_refuses_pushed(store, repo):
    as_job(store, repo)
    ctx = go.context(store, str(repo))
    go.switch(ctx, "PROJ-1-fix", create=True)
    (repo / "a.txt").write_text("x\n")
    go.commit(ctx, "x")
    assert go.reset_to(ctx, "HEAD~1", user_approved=True)["ok"]
    assert sh(repo, "status", "--porcelain")                 # правки остались в дереве
    go.commit(ctx, "x again")
    go.push(ctx, user_approved=True)
    with pytest.raises(go.GitOpError, match="origin"):
        go.amend(ctx, "y", user_approved=True)
    with pytest.raises(go.GitOpError, match="origin"):
        go.reset_to(ctx, "HEAD~1", user_approved=True)


def test_status_and_log(store, repo):
    ctx = go.context(store, str(repo))
    st = go.status(ctx)
    assert st["branch"] == "develop" and st["target"] == "develop" and st["guard"] is True
    assert go.log(ctx, n=5)["commits"][0]["subject"] == "init"
