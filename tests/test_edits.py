"""Правка и удаление через jwu: только своё, после подтверждения (JWU-53)."""

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from jwu.cli import main as cli
from jwu.core import timechain
from jwu.core.bitbucket import BitbucketClient
from jwu.core.config import Config
from jwu.core.github import GitHubClient
from jwu.core.jira import JiraClient
from jwu.core.service import Service
from jwu.core.store import Store

JIRA = "https://jira.test"
BB = "https://git.test"
GH = "https://api.github.test"
API = f"{JIRA}/rest/api/2"
PR_BASE = f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42"
runner = CliRunner()


def _svc(tmp_path):
    cfg = Config()
    cfg.jira.base_url = JIRA
    cfg.jira.username = "akotkov"
    cfg.bitbucket.base_url = BB
    cfg.bitbucket.project = "PROJ"
    cfg.bitbucket.repo = "repo"
    cfg.sdesk.base_url = JIRA
    cfg.sdesk.project = "SDESK"
    store = Store(tmp_path / "s.db")
    store.use_workspace(store.get_workspace_by_slug("work").id)
    return Service(cfg, JiraClient(JIRA, "tok"), BitbucketClient(BB, "tok"), store)


def _body(route):
    return json.loads(route.calls.last.request.content)


# --- Jira: комменты ------------------------------------------------------------ #

@respx.mock
def test_issue_comment_edit_and_delete_own(tmp_path):
    respx.get(f"{API}/issue/PROJ-1/comment/10").mock(return_value=httpx.Response(200, json={
        "id": "10", "body": "старый", "author": {"name": "akotkov"}}))
    put = respx.put(f"{API}/issue/PROJ-1/comment/10").mock(return_value=httpx.Response(200, json={"id": "10"}))
    delete = respx.delete(f"{API}/issue/PROJ-1/comment/10").mock(return_value=httpx.Response(204))
    svc = _svc(tmp_path)
    try:
        res = svc.issue_comment_update("PROJ-1", "10", "новый")
        assert _body(put) == {"body": "новый"} and res["before"] == "старый"
        assert svc.issue_comment_delete("PROJ-1", "10")["text"] == "старый" and delete.call_count == 1
        with pytest.raises(ValueError, match="Пустой"):
            svc.issue_comment_update("PROJ-1", "10", "  ")
    finally:
        svc.close()


@respx.mock
def test_foreign_comment_refused(tmp_path):
    respx.get(f"{API}/issue/PROJ-1/comment/11").mock(return_value=httpx.Response(200, json={
        "id": "11", "body": "чужой", "author": {"name": "colleague"}}))
    put = respx.put(f"{API}/issue/PROJ-1/comment/11")
    svc = _svc(tmp_path)
    try:
        with pytest.raises(ValueError, match="не твой"):
            svc.issue_comment_update("PROJ-1", "11", "перезапишу")
        assert put.call_count == 0
    finally:
        svc.close()


def test_sdesk_comment_edit_requires_client_facing(tmp_path):
    svc = _svc(tmp_path)
    try:
        with pytest.raises(ValueError, match="КЛИЕНТ"):
            svc.issue_comment_update("SDESK-5", "1", "текст")
        with pytest.raises(ValueError, match="КЛИЕНТ"):
            svc.issue_comment_delete("SDESK-5", "1")
    finally:
        svc.close()


# --- Jira: задача и ворклоги --------------------------------------------------- #

@respx.mock
def test_issue_update_fields(tmp_path):
    put = respx.put(f"{API}/issue/PROJ-1").mock(return_value=httpx.Response(204))
    svc = _svc(tmp_path)
    try:
        res = svc.issue_update("PROJ-1", summary=" Новый заголовок ", assignee="", labels=["stat"], priority="High")
        assert _body(put) == {"fields": {"summary": "Новый заголовок", "assignee": None,
                                         "labels": ["stat"], "priority": {"name": "High"}}}
        assert res["fields"] == ["assignee", "labels", "priority", "summary"]
        with pytest.raises(ValueError, match="Нечего"):
            svc.issue_update("PROJ-1")
    finally:
        svc.close()


def test_no_issue_delete_anywhere():
    from jwu import mcp_server as srv

    assert not hasattr(JiraClient, "delete_issue")
    assert not any("issue_delete" in name for name in dir(srv))


@respx.mock
def test_worklog_edit_converts_started_to_workspace_tz(tmp_path):
    respx.get(f"{API}/issue/PROJ-1/worklog/77").mock(return_value=httpx.Response(200, json={
        "id": "77", "timeSpent": "1h", "started": "2026-09-30T09:00:00.000+0300", "comment": "было",
        "author": {"name": "akotkov"}}))
    put = respx.put(f"{API}/issue/PROJ-1/worklog/77").mock(return_value=httpx.Response(200, json={}))
    svc = _svc(tmp_path)
    try:
        timechain.set_timezone(svc.store, svc.store.workspace_id, "МСК")
        res = svc.worklog_update("PROJ-1", "77", time="90m", started="2026-09-30 10:00", comment="стало")
        assert _body(put) == {"timeSpent": "1h 30m", "started": "2026-09-30T10:00:00.000+0300", "comment": "стало"}
        assert put.calls.last.request.url.params["adjustEstimate"] == "auto"
        assert res["before"]["time"] == "1h"
    finally:
        svc.close()


@respx.mock
def test_worklog_delete_own_only(tmp_path):
    respx.get(f"{API}/issue/PROJ-1/worklog/77").mock(return_value=httpx.Response(200, json={
        "id": "77", "timeSpent": "1h", "author": {"name": "akotkov"}}))
    respx.get(f"{API}/issue/PROJ-1/worklog/78").mock(return_value=httpx.Response(200, json={
        "id": "78", "timeSpent": "2h", "author": {"name": "boss"}}))
    d77 = respx.delete(f"{API}/issue/PROJ-1/worklog/77").mock(return_value=httpx.Response(204))
    d78 = respx.delete(f"{API}/issue/PROJ-1/worklog/78")
    svc = _svc(tmp_path)
    try:
        assert svc.worklog_delete("PROJ-1", "77")["deleted"] and d77.call_count == 1
        with pytest.raises(ValueError, match="не твой"):
            svc.worklog_delete("PROJ-1", "78")
        assert d78.call_count == 0
    finally:
        svc.close()


def test_to_jira_started_needs_tz():
    assert timechain.to_jira_started("2026-09-30T10:00:00+05:00", "") == "2026-09-30T10:00:00.000+0500"
    with pytest.raises(timechain.ChainError, match="пояс"):
        timechain.to_jira_started("2026-09-30 10:00", "")


# --- Bitbucket: коммент, PR, задачи --------------------------------------------- #

@respx.mock
def test_bitbucket_pr_comment_edit(tmp_path):
    respx.get(f"{PR_BASE}/comments/5").mock(return_value=httpx.Response(200, json={
        "id": 5, "version": 2, "text": "было", "author": {"name": "akotkov"}}))
    put = respx.put(f"{PR_BASE}/comments/5").mock(return_value=httpx.Response(200, json={"id": 5}))
    svc = _svc(tmp_path)
    try:
        res = svc.pr_comment_update(None, None, 42, 5, "стало")
        assert _body(put) == {"text": "стало", "version": 2} and res["before"] == "было"
    finally:
        svc.close()


def _bb_pr(author="akotkov"):
    return {"id": 42, "version": 7, "title": "PROJ-1: старое", "description": "было",
            "author": {"user": {"name": author, "displayName": author}},
            "reviewers": [{"user": {"name": "bob", "displayName": "Bob"}, "status": "UNAPPROVED"}],
            "fromRef": {"displayId": "f", "repository": {"slug": "repo", "project": {"key": "PROJ"}}},
            "toRef": {"displayId": "develop", "repository": {"slug": "repo", "project": {"key": "PROJ"}}},
            "state": "OPEN", "links": {"self": [{"href": "x"}]}}


@respx.mock
def test_bitbucket_pr_update_keeps_reviewers(tmp_path):
    respx.get(PR_BASE).mock(return_value=httpx.Response(200, json=_bb_pr()))
    put = respx.put(PR_BASE).mock(return_value=httpx.Response(200, json=_bb_pr()))
    svc = _svc(tmp_path)
    try:
        res = svc.pr_update(None, None, 42, description="стало")
        assert _body(put) == {"version": 7, "title": "PROJ-1: старое", "description": "стало",
                              "reviewers": [{"user": {"name": "bob"}}]}
        assert res["before"]["description"] == "было" and res["after"] == {"description": "стало"}
    finally:
        svc.close()


@respx.mock
def test_bitbucket_foreign_pr_refused(tmp_path):
    respx.get(PR_BASE).mock(return_value=httpx.Response(200, json=_bb_pr(author="colleague")))
    put = respx.put(PR_BASE)
    svc = _svc(tmp_path)
    try:
        with pytest.raises(ValueError, match="не твой"):
            svc.pr_update(None, None, 42, title="моё")
        assert put.call_count == 0
    finally:
        svc.close()


@respx.mock
def test_bitbucket_pr_task_edit_and_delete(tmp_path):
    respx.get(f"{BB}/rest/api/1.0/tasks/9").mock(return_value=httpx.Response(200, json={
        "id": 9, "text": "старое", "state": "OPEN", "author": {"name": "akotkov"}}))
    put = respx.put(f"{BB}/rest/api/1.0/tasks/9").mock(return_value=httpx.Response(200, json={
        "id": 9, "text": "Вынести таймаут в настройки", "state": "OPEN"}))
    delete = respx.delete(f"{BB}/rest/api/1.0/tasks/9").mock(return_value=httpx.Response(204))
    svc = _svc(tmp_path)
    try:
        assert svc.pr_task_update(9, "Вынести таймаут в настройки").text == "Вынести таймаут в настройки"
        assert _body(put) == {"id": 9, "text": "Вынести таймаут в настройки"}
        assert svc.pr_task_delete(9)["text"] == "старое" and delete.call_count == 1
        with pytest.raises(ValueError):
            svc.pr_task_update(9, " ".join(["слово"] * 11))   # лимит 10 слов
    finally:
        svc.close()


# --- GitHub ------------------------------------------------------------------- #

@respx.mock
def test_github_pr_update_and_comment_edit():
    gh = GitHubClient(GH, "tok")
    patch_pr = respx.patch(f"{GH}/repos/o/r/pulls/7").mock(return_value=httpx.Response(200, json={}))
    patch_c = respx.patch(f"{GH}/repos/o/r/issues/comments/3").mock(return_value=httpx.Response(200, json={}))
    try:
        gh.pr_update("o", "r", 7, title="t")
        assert _body(patch_pr) == {"title": "t"}
        gh.pr_comment_update("o", "r", 7, 3, "x", kind="issue")
        assert _body(patch_c) == {"body": "x"}
    finally:
        gh.close()


# --- CLI ---------------------------------------------------------------------- #

@pytest.mark.parametrize("args", [
    ["comment-edit", "PROJ-1", "10", "-m", "x", "--json"],
    ["comment-delete", "PROJ-1", "10", "--json"],
    ["worklog-edit", "PROJ-1", "77", "--time", "1h", "--json"],
    ["worklog-delete", "PROJ-1", "77", "--json"],
    ["issue", "edit", "PROJ-1", "--summary", "x", "--json"],
    ["pr-comment-edit", "42", "5", "-m", "x", "--json"],
    ["pr-edit", "42", "--title", "x", "--json"],
    ["pr-task", "edit", "9", "новый текст", "--json"],
    ["pr-task", "delete", "9", "--json"],
])
def test_cli_edits_require_confirmation(monkeypatch, args):
    called = []
    monkeypatch.setattr(cli, "_service_with_jira", lambda: called.append(1))
    monkeypatch.setattr(cli, "_service_with_prs", lambda: called.append(1))
    res = runner.invoke(cli.app, args)
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["reason"] == "confirm_required" and not called
