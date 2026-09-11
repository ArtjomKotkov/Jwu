"""Задачи на комментах PR (Bitbucket tasks): модель, клиент, дельты, сервис, CLI, MCP."""

import asyncio
import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from jwu import mcp_server as srv
from jwu.cli import main as cli
from jwu.core import workspaces
from jwu.core.bitbucket import BitbucketClient
from jwu.core.config import Config
from jwu.core.jira import JiraClient
from jwu.core.models import PR, PRTask, check_task_text
from jwu.core.service import Service
from jwu.core.store import Store

from .fixtures import bitbucket_dashboard_raw, bitbucket_merge_raw, bitbucket_pr_raw

BB = "https://git.test"
JIRA = "https://jira.test"
API = f"{BB}/rest/api/1.0"
PR_BASE = f"{API}/projects/PROJ/repos/repo/pull-requests/42"
runner = CliRunner()


def _task_raw(task_id=7, text="Убрать лишний запрос", state="OPEN", comment_id=100):
    return {"id": task_id, "text": text, "state": state,
            "author": {"name": "alice", "displayName": "Alice"}, "createdDate": 1700000000000,
            "anchor": {"id": comment_id, "type": "COMMENT"}}


# --- модель ------------------------------------------------------------------ #

def test_check_task_text_limits():
    assert check_task_text("  Убрать   лишний запрос ") == "Убрать лишний запрос"
    with pytest.raises(ValueError):
        check_task_text("   ")
    with pytest.raises(ValueError, match="10"):
        check_task_text("раз два три четыре пять шесть семь восемь девять десять одиннадцать")
    assert check_task_text("раз два три четыре пять шесть семь восемь девять десять")


def test_pr_task_from_bitbucket():
    t = PRTask.from_bitbucket(_task_raw(state="resolved"))
    assert (t.id, t.text, t.state, t.author, t.comment_id) == (7, "Убрать лишний запрос", "RESOLVED", "Alice", "100")
    assert t.resolved


# --- клиент ------------------------------------------------------------------ #

@respx.mock
def test_client_tasks_create_resolve_and_comment():
    bb = BitbucketClient(BB, "tok")
    respx.get(f"{PR_BASE}/tasks").mock(return_value=httpx.Response(200, json={
        "values": [_task_raw(), _task_raw(8, "Тест на пустой фильтр", "RESOLVED")], "isLastPage": True}))
    respx.get(f"{PR_BASE}/tasks/count").mock(return_value=httpx.Response(200, json={"open": 1, "resolved": 1}))
    create = respx.post(f"{API}/tasks").mock(return_value=httpx.Response(201, json=_task_raw(9, "Новая")))
    resolve = respx.put(f"{API}/tasks/9").mock(return_value=httpx.Response(200, json=_task_raw(9, "Новая", "RESOLVED")))
    comment = respx.post(f"{PR_BASE}/comments").mock(return_value=httpx.Response(201, json={"id": 555, "text": "x"}))
    try:
        tasks = bb.pr_tasks("PROJ", "repo", 42)
        assert [t.id for t in tasks] == [7, 8] and tasks[1].resolved
        assert bb.pr_task_count("PROJ", "repo", 42) == (1, 1)

        created = bb.task_create(100, "Новая")
        assert created.id == 9
        assert json.loads(create.calls.last.request.content) == {
            "anchor": {"id": 100, "type": "COMMENT"}, "text": "Новая"}

        assert bb.task_set_state(9, "RESOLVED").resolved
        assert json.loads(resolve.calls.last.request.content) == {"id": 9, "state": "RESOLVED"}

        raw = bb.pr_comment_add("PROJ", "repo", 42, "заметка", path="src/x.py", line=12)
        assert raw["id"] == 555
        body = json.loads(comment.calls.last.request.content)
        assert body["anchor"] == {"path": "src/x.py", "lineType": "CONTEXT", "fileType": "TO", "line": 12}
        bb.pr_comment_add("PROJ", "repo", 42, "ответ", parent_id=100)
        assert json.loads(comment.calls.last.request.content)["parent"] == {"id": 100}
    finally:
        bb.close()


# --- дельты ------------------------------------------------------------------ #

def test_task_deltas_on_new_and_resolved(tmp_path):
    store = Store(tmp_path / "s.db")
    try:
        def pr(open_n, resolved_n):
            return PR(id=1, project="P", repository="r", tasks_open=open_n, tasks_resolved=resolved_n)

        r1 = store.start_sync_run(["prs:mine"]); store.save_pr_snapshot(r1, pr(0, 0), ["mine"]); store.compute_changes(r1)
        r2 = store.start_sync_run(["prs:mine"]); store.save_pr_snapshot(r2, pr(2, 0), ["mine"])
        d2 = store.compute_changes(r2)
        assert [d.kind for d in d2] == ["new_pr_task"] and "+2" in d2[0].detail
        r3 = store.start_sync_run(["prs:mine"]); store.save_pr_snapshot(r3, pr(1, 1), ["mine"])
        assert [d.kind for d in store.compute_changes(r3)] == ["pr_task_resolved"]
        r4 = store.start_sync_run(["prs:mine"]); store.save_pr_snapshot(r4, pr(1, 1), ["mine"])
        assert store.compute_changes(r4) == []
    finally:
        store.close()


# --- сервис ------------------------------------------------------------------ #

def _service(tmp_path):
    cfg = Config()
    cfg.jira.base_url = JIRA
    cfg.bitbucket.base_url = BB
    cfg.bitbucket.project = "PROJ"
    cfg.bitbucket.repo = "repo"
    return Service(cfg, JiraClient(JIRA, "tok"), BitbucketClient(BB, "tok"), Store(tmp_path / "state.db"))


@respx.mock
def test_service_add_task_on_own_comment_and_validation(tmp_path):
    comment = respx.post(f"{PR_BASE}/comments").mock(return_value=httpx.Response(201, json={"id": 555}))
    create = respx.post(f"{API}/tasks").mock(return_value=httpx.Response(201, json=_task_raw(9, "Проверить кэш", comment_id=555)))
    svc = _service(tmp_path)
    try:
        with pytest.raises(ValueError):
            svc.pr_task_add(None, None, 42, "")  # текст обязателен
        assert comment.call_count == 0 and create.call_count == 0

        task = svc.pr_task_add(None, None, 42, "Проверить  кэш")
        assert task.id == 9 and task.comment_id == "555"
        assert json.loads(comment.calls.last.request.content) == {"text": "Проверить кэш"}
        assert json.loads(create.calls.last.request.content)["anchor"]["id"] == 555

        # на чужой коммент — без своего коммента
        svc.pr_task_add(None, None, 42, "Ещё одна", comment_id=100)
        assert comment.call_count == 1
        with pytest.raises(ValueError):
            svc.pr_task_set_state([9], "DONE")
    finally:
        svc.close()


@respx.mock
def test_sync_counts_tasks_and_state_shows_open(tmp_path):
    respx.get(f"{API}/dashboard/pull-requests").mock(
        return_value=httpx.Response(200, json=bitbucket_dashboard_raw([bitbucket_pr_raw(pr_id=42)])))
    respx.get(f"{PR_BASE}/merge").mock(return_value=httpx.Response(200, json=bitbucket_merge_raw()))
    respx.get(f"{PR_BASE}/commits").mock(return_value=httpx.Response(200, json={"values": []}))
    respx.get(f"{PR_BASE}/tasks/count").mock(return_value=httpx.Response(200, json={"open": 3, "resolved": 1}))
    svc = _service(tmp_path)
    try:
        svc.sync_section("prs_mine")
        pr = svc.store.latest_prs("mine")[0]
        assert (pr.tasks_open, pr.tasks_resolved) == (3, 1)
        assert cli._pr_state(pr) == "открытые задачи: 3"
        assert "задач: 3 открытых / 1 закрытых" in cli._pr_line(pr)
    finally:
        svc.close()


def test_github_workspace_refuses_tasks(tmp_path):
    from jwu.core.github import GitHubClient

    cfg = Config()
    gh = GitHubClient("https://api.github.test", "tok")
    svc = Service(cfg, gh, gh, Store(tmp_path / "s.db"))
    try:
        with pytest.raises(ValueError, match="Bitbucket"):
            svc.pr_tasks("o", "r", 1)
    finally:
        svc.close()


# --- CLI ---------------------------------------------------------------------- #

def test_cli_pr_task_add_requires_yes_and_short_text(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "_service_with_prs", lambda: called.append(1))
    res = runner.invoke(cli.app, ["pr-task", "add", "42", "Поправить фильтр"])
    assert res.exit_code == 1 and "Превью" in res.output and not called
    res = runner.invoke(cli.app, ["pr-task", "add", "42", " ".join(["слово"] * 11), "--yes"])
    assert res.exit_code == 1 and "лимит" in res.output and not called
    res = runner.invoke(cli.app, ["pr-task", "done", "7", "8"])
    assert res.exit_code == 1 and "#7, #8" in res.output and not called


# --- MCP ---------------------------------------------------------------------- #

@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.setenv("JWU_DB_PATH", str(db))
    monkeypatch.setattr(srv, "_base_store", None)
    monkeypatch.setattr(srv, "_full", {})
    monkeypatch.setattr(srv, "_builds", {})
    monkeypatch.setattr(srv, "_stores", {})
    yield db
    for store in list(srv._stores.values()):
        store.close()
    if srv._base_store is not None:
        srv._base_store.close()


def test_mcp_task_add_rejects_long_text_before_network(mcp_env, tmp_path, monkeypatch):
    store = Store(mcp_env)
    ws = workspaces.create(store, "gh", provider="github")
    folder = tmp_path / "proj"
    folder.mkdir()
    workspaces.add_path(store, ws, folder)
    store.close()
    monkeypatch.chdir(folder)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    with pytest.raises(ValueError, match="Bitbucket"):
        asyncio.run(srv.jwu_pr_task_add(1, "Коротко"))
