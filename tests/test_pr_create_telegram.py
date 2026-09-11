"""jwu pr-create (JWU-41), двусторонний Telegram (JWU-43), status-заметки в дашборде (JWU-42)."""

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from jwu.cli import main as cli
from jwu.core import daemon, notify
from jwu.core.bitbucket import BitbucketClient
from jwu.core.config import Config
from jwu.core.github import GitHubClient
from jwu.core.jira import JiraClient
from jwu.core.models import PR
from jwu.core.service import Service, dashboard_from_memory
from jwu.core.store import Store

BB = "https://git.test"
JIRA = "https://jira.test"
GH = "https://api.github.test"
TG = "https://api.telegram.org"
runner = CliRunner()


def _pr_raw(pr_id=77, title="PROJ-1: фикс"):
    return {"id": pr_id, "title": title, "state": "OPEN",
            "fromRef": {"displayId": "PROJ-1-fix", "repository": {"slug": "repo", "project": {"key": "PROJ"}}},
            "toRef": {"displayId": "develop", "repository": {"slug": "repo", "project": {"key": "PROJ"}}},
            "links": {"self": [{"href": f"{BB}/projects/PROJ/repos/repo/pull-requests/{pr_id}"}]}}


@respx.mock
def test_bitbucket_and_github_pr_create():
    bb = BitbucketClient(BB, "tok")
    create = respx.post(f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests").mock(
        return_value=httpx.Response(201, json=_pr_raw()))
    try:
        pull = bb.pr_create("PROJ", "repo", source="PROJ-1-fix", target="develop", title="PROJ-1: фикс",
                            description="d", reviewers=["bob", ""])
        assert pull.id == 77 and pull.url.endswith("/77")
        body = json.loads(create.calls.last.request.content)
        assert body["fromRef"]["id"] == "refs/heads/PROJ-1-fix" and body["toRef"]["id"] == "refs/heads/develop"
        assert body["reviewers"] == [{"user": {"name": "bob"}}]
    finally:
        bb.close()

    gh = GitHubClient(GH, "tok")
    pulls = respx.post(f"{GH}/repos/o/r/pulls").mock(return_value=httpx.Response(201, json={
        "number": 5, "title": "t", "state": "open", "html_url": "u",
        "head": {"ref": "f", "sha": "abc", "repo": {"name": "r", "owner": {"login": "o"}}},
        "base": {"ref": "main", "repo": {"name": "r", "owner": {"login": "o"}}}}))
    rev = respx.post(f"{GH}/repos/o/r/pulls/5/requested_reviewers").mock(return_value=httpx.Response(201, json={}))
    try:
        pull = gh.pr_create("o", "r", source="f", target="main", title="t", reviewers=["ann"])
        assert pull.id == 5 and json.loads(pulls.calls.last.request.content)["head"] == "f"
        assert json.loads(rev.calls.last.request.content) == {"reviewers": ["ann"]}
    finally:
        gh.close()


def _service(tmp_path, chat_id=""):
    cfg = Config()
    cfg.jira.base_url = JIRA
    cfg.bitbucket.base_url = BB
    cfg.bitbucket.project = "PROJ"
    cfg.bitbucket.repo = "repo"
    cfg.telegram.chat_id = chat_id
    return Service(cfg, JiraClient(JIRA, "tok"), BitbucketClient(BB, "tok"), Store(tmp_path / "s.db"))


def test_pr_draft_from_job_and_cli_preview(tmp_path, monkeypatch):
    svc = _service(tmp_path)
    try:
        job = svc.store.create_job("PROJ-1", "починить экспорт")
        svc.store.add_job_record(job.id, "разбор", kind="phase", status="done")
        title, body = svc.pr_draft_from_job(job.id)
        assert title == "PROJ-1: починить экспорт" and "разбор" in body
        with pytest.raises(ValueError):
            svc.pr_create(None, None, source="", target="develop", title="x")
        monkeypatch.setattr(cli, "_service_with_prs", lambda: svc)
        res = runner.invoke(cli.app, ["pr-create", "--from", "PROJ-1-fix", "--to", "develop",
                                      "--from-job", str(job.id), "--json"])
        assert res.exit_code == 0, res.output
        payload = json.loads(res.stdout)
        assert payload["reason"] == "confirm_required" and payload["title"] == "PROJ-1: починить экспорт"
        res = runner.invoke(cli.app, ["pr-create", "--from", "b", "--to", "develop", "--title", "t", "--dry-run"])
        assert res.exit_code == 0 and "ничего не создано" in res.output
    finally:
        svc.close()


def test_parse_incoming_reply_and_prefixed():
    reply = {"update_id": 5, "message": {"chat": {"id": 42}, "text": "поправил, жду ревью",
             "reply_to_message": {"text": "jwu · work\n⚠️ конфликт: PROJ/repo#893 — появился"}}}
    assert notify.parse_incoming(reply, chat_id="42") == ("PROJ/repo#893", "поправил, жду ревью")
    prefixed = {"update_id": 6, "message": {"chat": {"id": 42}, "text": "PROJ-1: ждём ответа Eugeny"}}
    assert notify.parse_incoming(prefixed, chat_id="42") == ("PROJ-1", "ждём ответа Eugeny")
    assert notify.parse_incoming({"message": {"chat": {"id": 1}, "text": "PROJ-1 x"}}, chat_id="42") is None
    assert notify.parse_incoming({"message": {"chat": {"id": 42}, "text": "просто текст"}}, chat_id="42") is None
    assert notify.parse_incoming({"message": {"chat": {"id": 42}, "text": "PROJ-1"}}, chat_id="42") is None


@respx.mock
def test_poll_replies_writes_notes_and_advances_offset(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    updates = respx.get(f"{TG}/bottok/getUpdates")
    updates.side_effect = [
        httpx.Response(200, json={"ok": True, "result": [
            {"update_id": 10, "message": {"chat": {"id": 42}, "text": "PROJ-1 ждём QA"}},
            {"update_id": 11, "message": {"chat": {"id": 42}, "text": "мимо"}},
            {"update_id": 12, "message": {"chat": {"id": 7}, "text": "PROJ-2 чужой чат"}},
        ]}),
        httpx.Response(200, json={"ok": True, "result": []}),
    ]
    respx.post(f"{TG}/bottok/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    svc = _service(tmp_path, chat_id="42")
    try:
        written = svc.poll_telegram_replies()
        assert [w["key"] for w in written if w["kind"] == "note"] == ["PROJ-1"]
        notes = svc.store.get_notes("PROJ-1")
        assert notes[0].author == "telegram" and notes[0].text == "ждём QA"
        assert svc.store.get_workspace_meta(notify.OFFSET_META) == "12"
        assert svc.poll_telegram_replies() == []
        assert updates.calls.last.request.url.params["offset"] == "13"
    finally:
        svc.close()
    quiet = _service(tmp_path / "q") if (tmp_path / "q").mkdir() is None else None
    try:
        assert quiet.poll_telegram_replies() == []       # уведомления не настроены — тишина
    finally:
        quiet.close()


def test_daemon_default_hook_polls(tmp_path, monkeypatch):
    class _Svc:
        workspace = None
        polled = 0

        def poll_telegram_replies(self):
            self.polled += 1
            return [{"key": "PROJ-1"}]

    svc = _Svc()
    from jwu.core.service import SyncResult

    daemon.default_after_sync(svc, SyncResult(run_id=1, counts={}, deltas=[]))
    assert svc.polled == 1


def test_dashboard_data_carries_status_notes(tmp_path):
    store = Store(tmp_path / "s.db")
    try:
        store.use_workspace(store.get_workspace_by_slug("work").id)
        run = store.start_sync_run(["prs:mine"])
        store.save_pr_snapshot(run, PR(id=42, project="PROJ", repository="repo", title="T"), ["mine"])
        store.add_note("PROJ/repo#42", "ждём Eugeny", kind="status")
        data = dashboard_from_memory(store, "me")
        assert data.status_notes == {"PROJ/repo#42": "ждём Eugeny"}
    finally:
        store.close()
