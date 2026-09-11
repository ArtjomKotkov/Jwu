"""Дифф PR, комментарий/ответ в PR и статус ревью (JWU-23/24/25) у Bitbucket и GitHub."""

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from jwu.cli import main as cli
from jwu.core.bitbucket import BitbucketClient, BitbucketError, render_unified_diff
from jwu.core.config import Config
from jwu.core.github import GitHubClient, GitHubError
from jwu.core.jira import JiraClient
from jwu.core.service import Service
from jwu.core.store import Store

BB = "https://git.test"
JIRA = "https://jira.test"
GH = "https://api.github.test"
PR_BASE = f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42"
runner = CliRunner()


def _bb_diff_json():
    return {"truncated": False, "diffs": [
        {"source": {"toString": "a.py"}, "destination": {"toString": "a.py"}, "truncated": False,
         "hunks": [{"sourceLine": 1, "sourceSpan": 2, "destinationLine": 1, "destinationSpan": 3,
                    "truncated": False, "segments": [
                        {"type": "CONTEXT", "lines": [{"line": "import os"}]},
                        {"type": "REMOVED", "lines": [{"line": "x = 1"}]},
                        {"type": "ADDED", "lines": [{"line": "x = 2"}, {"line": "y = 3"}]},
                    ]}]},
        {"source": None, "destination": {"toString": "new.txt"}, "hunks": [
            {"sourceLine": 0, "sourceSpan": 0, "destinationLine": 1, "destinationSpan": 1,
             "segments": [{"type": "ADDED", "lines": [{"line": "hello"}]}]}]},
        {"source": {"toString": "img.png"}, "destination": {"toString": "img.png"}, "binary": True, "hunks": []},
    ]}


def test_render_unified_diff():
    text = render_unified_diff(_bb_diff_json())
    assert "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n import os\n-x = 1\n+x = 2\n+y = 3\n" in text
    assert "diff --git a/new.txt b/new.txt\nnew file\n--- /dev/null\n+++ b/new.txt" in text
    assert "Binary files differ" in text
    assert render_unified_diff({"diffs": []}) == ""


@respx.mock
def test_bitbucket_diff_review_and_reply():
    bb = BitbucketClient(BB, "tok")
    respx.get(f"{PR_BASE}/diff").mock(return_value=httpx.Response(200, json=_bb_diff_json()))
    one = respx.get(f"{PR_BASE}/diff/a.py").mock(return_value=httpx.Response(200, json={"diffs": _bb_diff_json()["diffs"][:1]}))
    review = respx.put(f"{PR_BASE}/participants/akotkov").mock(return_value=httpx.Response(200, json={"status": "APPROVED"}))
    try:
        assert "+++ b/new.txt" in bb.pr_diff("PROJ", "repo", 42)
        assert "new.txt" not in bb.pr_diff("PROJ", "repo", 42, path="a.py") and one.call_count == 1
        assert bb.pr_review("PROJ", "repo", 42, "akotkov", "approved")["status"] == "APPROVED"
        assert json.loads(review.calls.last.request.content) == {
            "user": {"name": "akotkov"}, "approved": True, "status": "APPROVED"}
        with pytest.raises(BitbucketError):
            bb.pr_review("PROJ", "repo", 42, "akotkov", "DONE")
    finally:
        bb.close()


_GH_DIFF = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
            "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-p\n+q\n")


@respx.mock
def test_github_diff_comment_and_review():
    gh = GitHubClient(GH, "tok")
    diff_route = respx.get(f"{GH}/repos/o/r/pulls/7").mock(return_value=httpx.Response(200, text=_GH_DIFF))
    reply = respx.post(f"{GH}/repos/o/r/pulls/7/comments/99/replies").mock(return_value=httpx.Response(201, json={"id": 100}))
    general = respx.post(f"{GH}/repos/o/r/issues/7/comments").mock(return_value=httpx.Response(201, json={"id": 101}))
    reviews = respx.post(f"{GH}/repos/o/r/pulls/7/reviews").mock(return_value=httpx.Response(200, json={"id": 5, "state": "APPROVED"}))
    try:
        assert gh.pr_diff("o", "r", 7) == _GH_DIFF
        assert diff_route.calls.last.request.headers["accept"] == "application/vnd.github.diff"
        only_b = gh.pr_diff("o", "r", 7, path="b.py")
        assert "b/b.py" in only_b and "a/a.py" not in only_b
        assert gh.pr_comment_add("o", "r", 7, "поправил", parent_id=99)["id"] == 100
        assert json.loads(reply.calls.last.request.content) == {"body": "поправил"}
        assert gh.pr_comment_add("o", "r", 7, "общий")["id"] == 101
        assert gh.pr_review("o", "r", 7, "me", "APPROVED")["state"] == "APPROVED"
        assert json.loads(reviews.calls.last.request.content) == {"event": "APPROVE"}
        with pytest.raises(GitHubError, match="текст"):
            gh.pr_review("o", "r", 7, "me", "NEEDS_WORK")
        with pytest.raises(GitHubError):
            gh.pr_review("o", "r", 7, "me", "UNAPPROVED")
    finally:
        gh.close()


def _service(tmp_path):
    cfg = Config()
    cfg.jira.base_url = JIRA
    cfg.jira.username = "akotkov"
    cfg.bitbucket.base_url = BB
    cfg.bitbucket.project = "PROJ"
    cfg.bitbucket.repo = "repo"
    return Service(cfg, JiraClient(JIRA, "tok"), BitbucketClient(BB, "tok"), Store(tmp_path / "s.db"))


@respx.mock
def test_service_review_uses_my_login_and_truncates_diff(tmp_path):
    respx.get(f"{JIRA}/rest/api/2/myself").mock(return_value=httpx.Response(200, json={"name": "akotkov", "displayName": "A"}))
    review = respx.put(f"{PR_BASE}/participants/akotkov").mock(return_value=httpx.Response(200, json={"status": "NEEDS_WORK"}))
    respx.get(f"{PR_BASE}/diff").mock(return_value=httpx.Response(200, json=_bb_diff_json()))
    svc = _service(tmp_path)
    try:
        svc.pr_review(None, None, 42, "needs-work")
        assert json.loads(review.calls.last.request.content)["status"] == "NEEDS_WORK"
        with pytest.raises(ValueError):
            svc.pr_review(None, None, 42, "meh")
        short = svc.pr_diff(None, None, 42, max_chars=40)
        assert "обрезано jwu" in short and len(short.split("\\ ...")[0]) <= 40
        with pytest.raises(ValueError):
            svc.pr_comment_add(None, None, 42, "   ")
    finally:
        svc.close()


def test_cli_pr_comment_and_review_require_confirmation(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "_service_with_prs", lambda: called.append(1))
    res = runner.invoke(cli.app, ["pr-comment", "42", "-m", "поправил", "--reply-to", "9", "--json"])
    assert res.exit_code == 0 and json.loads(res.stdout)["reason"] == "confirm_required" and not called
    res = runner.invoke(cli.app, ["pr-comment", "42", "-m", "x", "--dry-run"])
    assert res.exit_code == 0 and "ничего не отправлено" in res.output and not called
    res = runner.invoke(cli.app, ["pr-review", "42", "needs-work", "-m", "лишний запрос"])
    assert res.exit_code == 1 and "NEEDS_WORK" in res.output and not called
    res = runner.invoke(cli.app, ["pr-review", "42", "lgtm", "--yes"])
    assert res.exit_code == 1 and not called
