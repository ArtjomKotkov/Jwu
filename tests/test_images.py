"""Картинки в тексте: Jira (!name.png! ↔ вложения задачи) и Bitbucket (attachment: в PR)."""

import json

import httpx
import respx
from typer.testing import CliRunner

from jwu.cli import main as cli
from jwu.core.bitbucket import BitbucketClient
from jwu.core.config import Config
from jwu.core.jira import JiraClient
from jwu.core.models import Issue, PR, inline_image_refs, pr_attachment_refs
from jwu.core.service import Service
from jwu.core.store import Store

from .fixtures import jira_issue_raw

BB = "https://git.test"
JIRA = "https://jira.test"
PR_BASE = f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42"
runner = CliRunner()


# --- Jira ------------------------------------------------------------------- #

def test_inline_image_refs_parses_markup_variants():
    text = ("см. !screen one.png! и !Image_2_MSYlg4X.PNG|thumbnail! ещё раз !screen one.png!"
            " и !diagram.svg|width=600,height=200! но не !not-image.txt! и не !broken")
    assert inline_image_refs(text) == ["screen one.png", "Image_2_MSYlg4X.PNG", "diagram.svg"]
    assert inline_image_refs("") == []


def test_issue_links_images_to_comments_and_description():
    raw = jira_issue_raw(comments=[
        {"id": 11, "body": "вот баг: !bug.png!"},
        {"id": 12, "body": "и ещё !bug.png|thumbnail! плюс !log.png!"},
    ])
    raw["fields"]["description"] = "Экран: !main.png!"
    raw["fields"]["attachment"] = [
        {"id": "1", "filename": "bug.png", "mimeType": "image/png", "size": 10, "content": f"{JIRA}/a/1"},
        {"id": "2", "filename": "main.png", "mimeType": "image/png", "size": 10, "content": f"{JIRA}/a/2"},
        {"id": "3", "filename": "orphan.png", "mimeType": "image/png", "size": 10, "content": f"{JIRA}/a/3"},
    ]
    issue = Issue.from_jira(raw)
    assert issue.description_images == ["main.png"]
    assert [c.images for c in issue.comments] == [["bug.png"], ["bug.png", "log.png"]]
    by_name = {a.filename: a.referenced_by for a in issue.attachments}
    assert by_name == {"bug.png": ["11", "12"], "main.png": ["description"], "orphan.png": []}
    # в дампе для MCP связь видна
    assert issue.attachments[0].model_dump()["referenced_by"] == ["11", "12"]


# --- Bitbucket ------------------------------------------------------------------- #

def test_pr_attachment_refs_dedupes_preview_and_link():
    text = ("Скачет поле:\n[![image.png](attachment:101/626b19958d%2Fimage.png)](attachment:101/626b19958d%2Fimage.png)"
            "\nи ещё [![image.png](attachment:101/4343269f55%2Fimage.png)](attachment:101/4343269f55%2Fimage.png)"
            " и лог [log.txt](attachment:101/abc%2Flog.txt)")
    refs = pr_attachment_refs(text, comment_id="95983")
    assert [(a.name, a.path, a.comment_id, a.kind) for a in refs] == [
        ("image.png", "626b19958d/image.png", "95983", "image"),
        ("image.png", "4343269f55/image.png", "95983", "image"),
        ("log.txt", "abc/log.txt", "95983", "log"),
    ]
    assert PR.from_bitbucket({"id": 1, "description": "[![a.png](attachment:1/x%2Fa.png)](attachment:1/x%2Fa.png)"}).attachments[0].path == "x/a.png"


@respx.mock
def test_client_fills_urls_and_downloads(tmp_path):
    bb = BitbucketClient(BB, "tok")
    respx.get(f"{PR_BASE}").mock(return_value=httpx.Response(200, json={
        "id": 42, "title": "t", "description": "![s.png](attachment:101/h1%2Fs.png)",
        "fromRef": {"repository": {"slug": "repo", "project": {"key": "PROJ"}}},
        "toRef": {"repository": {"slug": "repo", "project": {"key": "PROJ"}}},
    }))
    respx.get(f"{PR_BASE}/merge").mock(return_value=httpx.Response(200, json={"canMerge": True, "conflicted": False}))
    file_route = respx.get(f"{BB}/projects/PROJ/repos/repo/attachments/h1/s.png").mock(
        return_value=httpx.Response(200, content=b"PNGDATA", headers={"content-type": "image/png"}))
    try:
        pr = bb.pr("PROJ", "repo", 42)
        att = pr.attachments[0]
        assert att.url == f"{BB}/projects/PROJ/repos/repo/attachments/h1/s.png"
        path = bb.download_attachment(att.url, tmp_path / "s.png")
        assert path.read_bytes() == b"PNGDATA"
        assert file_route.calls.last.request.headers["accept"] == "*/*"
    finally:
        bb.close()


def _service(tmp_path):
    cfg = Config()
    cfg.jira.base_url = JIRA
    cfg.bitbucket.base_url = BB
    cfg.bitbucket.project = "PROJ"
    cfg.bitbucket.repo = "repo"
    return Service(cfg, JiraClient(JIRA, "tok"), BitbucketClient(BB, "tok"), Store(tmp_path / "state.db"))


def _activities_with_images():
    return {"isLastPage": True, "values": [
        {"action": "COMMENTED", "comment": {
            "id": 7, "text": "баг [![image.png](attachment:101/aaa%2Fimage.png)](attachment:101/aaa%2Fimage.png)",
            "author": {"displayName": "Bob"}, "createdDate": 1, "comments": []}},
        {"action": "COMMENTED", "comment": {
            "id": 8, "text": "ещё [![image.png](attachment:101/bbb%2Fimage.png)](attachment:101/bbb%2Fimage.png)",
            "author": {"displayName": "Bob"}, "createdDate": 2, "comments": []}},
    ]}


@respx.mock
def test_service_downloads_pr_attachments_with_comment_anchor(tmp_path):
    respx.get(f"{PR_BASE}").mock(return_value=httpx.Response(200, json={
        "id": 42, "title": "t", "description": "![d.png](attachment:101/ddd%2Fd.png)",
        "fromRef": {"repository": {"slug": "repo", "project": {"key": "PROJ"}}},
        "toRef": {"repository": {"slug": "repo", "project": {"key": "PROJ"}}},
    }))
    respx.get(f"{PR_BASE}/merge").mock(return_value=httpx.Response(200, json={"canMerge": True, "conflicted": False}))
    respx.get(f"{PR_BASE}/activities").mock(return_value=httpx.Response(200, json=_activities_with_images()))
    respx.get(f"{PR_BASE}/commits").mock(return_value=httpx.Response(200, json={"values": []}))
    for h in ("ddd/d.png", "aaa/image.png", "bbb/image.png"):
        respx.get(f"{BB}/projects/PROJ/repos/repo/attachments/{h}").mock(
            return_value=httpx.Response(200, content=h.encode()))
    svc = _service(tmp_path)
    try:
        got = svc.download_pr_attachments(None, None, 42, dest=tmp_path / "out")
        names = sorted(p.name for _, p in got)
        assert names == ["aaa-image.png", "bbb-image.png", "d.png"] or names == ["bbb-image.png", "d.png", "image.png"]
        anchors = {p.name: a.comment_id for a, p in got}
        assert anchors["d.png"] == "" and set(anchors.values()) == {"", "7", "8"}
        assert (tmp_path / "out" / "d.png").read_bytes() == b"ddd/d.png"
    finally:
        svc.close()


@respx.mock
def test_cli_pr_lists_attachments(monkeypatch, tmp_path):
    respx.get(f"{PR_BASE}").mock(return_value=httpx.Response(200, json={
        "id": 42, "title": "t", "description": "",
        "fromRef": {"repository": {"slug": "repo", "project": {"key": "PROJ"}}},
        "toRef": {"repository": {"slug": "repo", "project": {"key": "PROJ"}}},
    }))
    respx.get(f"{PR_BASE}/merge").mock(return_value=httpx.Response(200, json={"canMerge": True, "conflicted": False}))
    respx.get(f"{PR_BASE}/activities").mock(return_value=httpx.Response(200, json=_activities_with_images()))
    respx.get(f"{PR_BASE}/commits").mock(return_value=httpx.Response(200, json={"values": []}))
    svc = _service(tmp_path)
    monkeypatch.setattr(cli, "_service_with_prs", lambda: svc)
    res = runner.invoke(cli.app, ["pr", "42", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert [a["comment_id"] for a in payload["attachments"]] == ["7", "8"]
    assert payload["attachments"][0]["url"].endswith("/attachments/aaa/image.png")
