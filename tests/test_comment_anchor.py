"""Inline-комменты PR цепляются к строкам диффа (JWU-46)."""

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from jwu.cli import main as cli
from jwu.core import diffmap
from jwu.core.bitbucket import BitbucketClient
from jwu.core.config import Config
from jwu.core.github import GitHubClient
from jwu.core.jira import JiraClient
from jwu.core.service import Service
from jwu.core.store import Store

BB = "https://git.test"
JIRA = "https://jira.test"
GH = "https://api.github.test"
PR_BASE = f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42"
runner = CliRunner()

DIFF = (
    "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
    "@@ -10,4 +10,5 @@\n"
    " def f():\n"        # old 10 / new 10
    "-    x = 1\n"        # old 11
    "+    x = 2\n"        # new 11
    "+    y = 3\n"        # new 12
    "     return x\n"    # old 12 / new 13
    " \n"                # old 13 / new 14
    "@@ -40,2 +41,2 @@\n"
    " a = 1\n"           # old 40 / new 41
    "-b = 2\n"           # old 41
    "+b = 3\n"           # new 42
    "diff --git a/gone.py b/gone.py\ndeleted file\n--- a/gone.py\n+++ /dev/null\n"
    "@@ -1,1 +0,0 @@\n-bye\n"
)


# --- diffmap --------------------------------------------------------------- #

@pytest.mark.parametrize("line, side, expected", [
    (11, None, ("ADDED", "TO")),
    (12, "TO", ("ADDED", "TO")),
    (10, None, ("CONTEXT", "TO")),
    (13, None, ("CONTEXT", "TO")),
    (11, "FROM", ("REMOVED", "FROM")),
    (42, None, ("ADDED", "TO")),
])
def test_locate_by_diff(line, side, expected):
    assert diffmap.locate(DIFF, "a.py", line, side=side) == expected


def test_locate_line_outside_diff_names_ranges():
    with pytest.raises(diffmap.DiffLineError, match=r"Строки 30 .*10–14, 41–42"):
        diffmap.locate(DIFF, "a.py", 30)


def test_locate_unknown_file_lists_files():
    with pytest.raises(diffmap.DiffLineError, match="Файла b.py нет в диффе PR. Файлы PR: a.py, gone.py"):
        diffmap.locate(DIFF, "b.py", 1)


def test_locate_explicit_type_is_checked():
    assert diffmap.locate(DIFF, "a.py", 11, line_type="removed") == ("REMOVED", "FROM")
    with pytest.raises(diffmap.DiffLineError, match="CONTEXT, а не ADDED"):
        diffmap.locate(DIFF, "a.py", 10, line_type="ADDED")
    with pytest.raises(diffmap.DiffLineError, match="line_type"):
        diffmap.locate(DIFF, "a.py", 10, line_type="MOVED")


def test_deleted_file_addressed_by_old_path():
    assert diffmap.locate(DIFF, "gone.py", 1, side="FROM") == ("REMOVED", "FROM")


def test_numbered_diff():
    text = diffmap.numbered(DIFF)
    assert "=== a.py" in text
    assert "   11       │-    x = 1" in text
    assert "         11 │+    x = 2" in text
    assert "   12    13 │     return x" in text
    assert "        ⋮" in text  # разрыв между хунками


# --- сервис: Bitbucket ------------------------------------------------------ #

def _bb_file_diff(path="a.py"):
    return {"diffs": [{"source": {"toString": path}, "destination": {"toString": path}, "hunks": [
        {"sourceLine": 10, "sourceSpan": 2, "destinationLine": 10, "destinationSpan": 3, "segments": [
            {"type": "CONTEXT", "lines": [{"line": "def f():", "source": 10, "destination": 10}]},
            {"type": "REMOVED", "lines": [{"line": "    x = 1", "source": 11, "destination": 11}]},
            {"type": "ADDED", "lines": [{"line": "    x = 2", "source": 11, "destination": 11},
                                        {"line": "    y = 3", "source": 11, "destination": 12}]},
        ]}]}]}


def _activity(cid, *, anchored=True):
    act = {"action": "COMMENTED",
           "comment": {"id": cid, "text": "t", "author": {"name": "akotkov", "displayName": "A"}},
           "commentAnchor": {"path": "a.py", "line": 11, "lineType": "ADDED", "fileType": "TO"}}
    if anchored:
        act["diff"] = _bb_file_diff()["diffs"][0]
    return act


def _service(tmp_path):
    cfg = Config()
    cfg.jira.base_url = JIRA
    cfg.jira.username = "akotkov"
    cfg.bitbucket.base_url = BB
    cfg.bitbucket.project = "PROJ"
    cfg.bitbucket.repo = "repo"
    return Service(cfg, JiraClient(JIRA, "tok"), BitbucketClient(BB, "tok"), Store(tmp_path / "s.db"))


@respx.mock
def test_bitbucket_inline_uses_added_line_type(tmp_path):
    diff = respx.get(f"{PR_BASE}/diff/a.py").mock(return_value=httpx.Response(200, json=_bb_file_diff()))
    post = respx.post(f"{PR_BASE}/comments").mock(return_value=httpx.Response(201, json={"id": 501}))
    respx.get(f"{PR_BASE}/activities").mock(return_value=httpx.Response(200, json={
        "values": [_activity(501)], "isLastPage": True}))
    svc = _service(tmp_path)
    try:
        res = svc.pr_comment_add(None, None, 42, "замечание", path="a.py", line=11)
        assert json.loads(post.calls.last.request.content)["anchor"] == {
            "path": "a.py", "lineType": "ADDED", "fileType": "TO", "line": 11}
        assert diff.calls.last.request.url.params["contextLines"] == "10"
        assert res["anchored"] is True and res["line_type"] == "ADDED" and "warning" not in res
    finally:
        svc.close()


@respx.mock
def test_bitbucket_inline_reports_unanchored(tmp_path):
    respx.get(f"{PR_BASE}/diff/a.py").mock(return_value=httpx.Response(200, json=_bb_file_diff()))
    respx.post(f"{PR_BASE}/comments").mock(return_value=httpx.Response(201, json={"id": 502}))
    respx.get(f"{PR_BASE}/activities").mock(return_value=httpx.Response(200, json={
        "values": [_activity(502, anchored=False)], "isLastPage": True}))
    svc = _service(tmp_path)
    try:
        res = svc.pr_comment_add(None, None, 42, "замечание", path="a.py", line=10)
        assert res["line_type"] == "CONTEXT"
        assert res["anchored"] is False and "не привязался" in res["warning"]
    finally:
        svc.close()


@respx.mock
def test_bitbucket_inline_outside_diff_sends_nothing(tmp_path):
    respx.get(f"{PR_BASE}/diff/a.py").mock(return_value=httpx.Response(200, json=_bb_file_diff()))
    post = respx.post(f"{PR_BASE}/comments").mock(return_value=httpx.Response(201, json={"id": 1}))
    svc = _service(tmp_path)
    try:
        with pytest.raises(diffmap.DiffLineError, match="Строки 99"):
            svc.pr_comment_add(None, None, 42, "замечание", path="a.py", line=99)
        assert post.call_count == 0
    finally:
        svc.close()


@respx.mock
def test_reply_and_general_skip_diff(tmp_path):
    diff = respx.get(f"{PR_BASE}/diff/a.py")
    post = respx.post(f"{PR_BASE}/comments").mock(return_value=httpx.Response(201, json={"id": 7}))
    svc = _service(tmp_path)
    try:
        svc.pr_comment_add(None, None, 42, "поправил", parent_id=5)
        assert json.loads(post.calls.last.request.content) == {"text": "поправил", "parent": {"id": 5}}
        svc.pr_comment_add(None, None, 42, "общий")
        assert diff.call_count == 0
    finally:
        svc.close()


@respx.mock
def test_pr_task_inline_comment_is_anchored(tmp_path):
    respx.get(f"{PR_BASE}/diff/a.py").mock(return_value=httpx.Response(200, json=_bb_file_diff()))
    post = respx.post(f"{PR_BASE}/comments").mock(return_value=httpx.Response(201, json={"id": 503}))
    respx.get(f"{PR_BASE}/activities").mock(return_value=httpx.Response(200, json={
        "values": [_activity(503)], "isLastPage": True}))
    respx.post(f"{BB}/rest/api/1.0/tasks").mock(return_value=httpx.Response(201, json={
        "id": 9, "text": "поправить", "state": "OPEN", "anchor": {"id": 503}}))
    svc = _service(tmp_path)
    try:
        svc.pr_task_add(None, None, 42, "поправить", path="a.py", line=12)
        assert json.loads(post.calls.last.request.content)["anchor"]["lineType"] == "ADDED"
    finally:
        svc.close()


@respx.mock
def test_delete_own_comment_only(tmp_path):
    respx.get(f"{JIRA}/rest/api/2/myself").mock(return_value=httpx.Response(200, json={"name": "akotkov"}))
    respx.get(f"{PR_BASE}/comments/501").mock(return_value=httpx.Response(200, json={
        "id": 501, "version": 3, "text": "мой", "author": {"name": "akotkov"}}))
    respx.get(f"{PR_BASE}/comments/600").mock(return_value=httpx.Response(200, json={
        "id": 600, "version": 0, "text": "чужой", "author": {"name": "colleague"}}))
    delete = respx.delete(f"{PR_BASE}/comments/501").mock(return_value=httpx.Response(204))
    other = respx.delete(f"{PR_BASE}/comments/600").mock(return_value=httpx.Response(204))
    svc = _service(tmp_path)
    try:
        assert svc.pr_comment_delete(None, None, 42, 501) == {"id": "501", "deleted": True, "text": "мой"}
        assert delete.calls.last.request.url.params["version"] == "3"
        with pytest.raises(ValueError, match="не твой"):
            svc.pr_comment_delete(None, None, 42, 600)
        assert other.call_count == 0
    finally:
        svc.close()


# --- GitHub ------------------------------------------------------------------ #

_GH_DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n ctx\n-x\n+y\n"


def _gh_service(tmp_path):
    cfg = Config()
    cfg.github.token = "tok"
    cfg.github.owner = "o"
    cfg.github.repos = ["r"]
    gh = GitHubClient(GH, "tok")
    svc = Service(cfg, gh, gh, Store(tmp_path / "s.db"))
    svc.default_pr_ref = lambda: ("o", "r")
    return svc


@respx.mock
def test_github_inline_side_from_diff(tmp_path):
    respx.get(f"{GH}/repos/o/r/pulls/7").mock(side_effect=lambda req: httpx.Response(
        200, text=_GH_DIFF) if req.headers.get("accept") == "application/vnd.github.diff"
        else httpx.Response(200, json={"head": {"sha": "abc"}}))
    respx.get(f"{GH}/repos/o/r/pulls/7/commits").mock(return_value=httpx.Response(200, json=[{"sha": "abc"}]))
    post = respx.post(f"{GH}/repos/o/r/pulls/7/comments").mock(return_value=httpx.Response(
        201, json={"id": 11, "line": 2}))
    svc = _gh_service(tmp_path)
    try:
        res = svc.pr_comment_add(None, None, 7, "удалил зря", path="a.py", line=2, side="FROM")
        body = json.loads(post.calls.last.request.content)
        assert body["side"] == "LEFT" and body["line"] == 2
        assert res["anchored"] is True and res["line_type"] == "REMOVED"
        with pytest.raises(diffmap.DiffLineError):
            svc.pr_comment_add(None, None, 7, "мимо", path="a.py", line=50)
    finally:
        svc.close()


# --- CLI --------------------------------------------------------------------- #

def test_cli_delete_requires_confirmation(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "_service_with_prs", lambda: called.append(1))
    res = runner.invoke(cli.app, ["pr-comment-delete", "42", "501", "--json"])
    assert res.exit_code == 0 and json.loads(res.stdout)["reason"] == "confirm_required" and not called


def test_cli_pr_comment_line_type_choice():
    res = runner.invoke(cli.app, ["pr-comment", "42", "-m", "x", "--path", "a.py", "--line", "3",
                                  "--line-type", "MOVED", "--dry-run"])
    assert res.exit_code == 2
