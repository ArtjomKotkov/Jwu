"""Confluence: чтение, создание и правка страниц; удаления нет (JWU-54)."""

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from jwu import mcp_server as srv
from jwu.cli import main as cli
from jwu.core import confluence as conf_mod
from jwu.core.confluence import ConfluenceClient, ConfluenceError
from jwu.core.config import Config
from jwu.core.jira import JiraClient
from jwu.core.service import Service
from jwu.core.store import Store

CONF = "https://conf.test"
API = f"{CONF}/rest/api"
runner = CliRunner()


def _page(pid="100", title="WhatsApp Gupshup", version=3, body="<p>текст</p>"):
    return {"id": pid, "title": title, "type": "page",
            "space": {"key": "WEBIMTEST"}, "version": {"number": version, "by": {"displayName": "Artyom"}},
            "ancestors": [{"id": "1", "title": "Home"}, {"id": "50", "title": "Каналы"}],
            "body": {"storage": {"value": body}}, "_links": {"webui": f"/pages/viewpage.action?pageId={pid}"}}


def _login_ok():
    return respx.post(f"{CONF}/dologin.action").mock(return_value=httpx.Response(302, headers={"location": "/"}))


def _client():
    return ConfluenceClient(CONF, proxy_basic=("gate", "gp"), session_login=("akotkov", "pw"))


@respx.mock
def test_login_with_gate_and_read_page():
    login = _login_ok()
    route = respx.get(f"{API}/content/100").mock(return_value=httpx.Response(200, json=_page()))
    c = _client()
    try:
        page = c.page("100")
    finally:
        c.close()
    assert page == {"id": "100", "title": "WhatsApp Gupshup", "space": "WEBIMTEST", "version": 3,
                    "updated_by": "Artyom", "parent_id": "50", "path": ["Home", "Каналы"],
                    "url": f"{CONF}/pages/viewpage.action?pageId=100", "body": "<p>текст</p>"}
    form = login.calls.last.request.content.decode()
    assert "os_username=akotkov" in form and "os_password=pw" in form
    assert route.calls.last.request.headers["authorization"].startswith("Basic ")   # гейт nginx
    assert route.calls.last.request.url.params["expand"] == "space,version,ancestors,body.storage"


@respx.mock
def test_bad_login_is_explicit():
    respx.post(f"{CONF}/dologin.action").mock(return_value=httpx.Response(
        302, headers={"location": "/login.action?os_destination=%2F&permissionViolation=true"}))
    c = _client()
    try:
        with pytest.raises(ConfluenceError, match="войти"):
            c.page("100")
    finally:
        c.close()


@respx.mock
def test_expired_session_relogin_once():
    login = _login_ok()
    respx.get(f"{API}/content/100").mock(side_effect=[httpx.Response(401), httpx.Response(200, json=_page())])
    c = _client()
    try:
        assert c.page("100")["title"] == "WhatsApp Gupshup"
    finally:
        c.close()
    assert login.call_count == 2


@respx.mock
def test_children_and_search():
    _login_ok()
    respx.get(f"{API}/content/50/child/page").mock(return_value=httpx.Response(200, json={"results": [
        {"id": "100", "title": "WhatsApp Gupshup", "version": {"number": 3}, "_links": {"webui": "/x"}}]}))
    respx.get(f"{API}/content/search").mock(return_value=httpx.Response(200, json={"results": [
        {"id": "100", "title": "WhatsApp Gupshup", "space": {"key": "WEBIMTEST"}, "_links": {}}]}))
    c = _client()
    try:
        assert c.children("50") == [{"id": "100", "title": "WhatsApp Gupshup", "version": 3, "url": f"{CONF}/x"}]
        assert c.search('title ~ "gupshup"')[0]["space"] == "WEBIMTEST"
    finally:
        c.close()


def test_no_delete_anywhere():
    """Удаления страниц нет ни в клиенте, ни в сервисе, ни в MCP, ни в CLI."""
    assert not [n for n in dir(ConfluenceClient) if "delete" in n.lower() or "remove" in n.lower()]
    assert not [n for n in dir(Service) if n.startswith("confluence") and "delete" in n]
    assert not [n for n in dir(srv) if n.startswith("jwu_confluence") and "delete" in n]
    names = {c.name for c in cli.confluence_app.registered_commands}
    assert names == {"setup", "page", "children", "search", "create", "update"}


# --- сервис: создание и правка ------------------------------------------------ #

def _svc(tmp_path):
    cfg = Config()
    cfg.jira.base_url = "https://jira.test"
    cfg.jira.username = "akotkov"
    store = Store(tmp_path / "s.db")
    store.use_workspace(store.get_workspace_by_slug("work").id)
    store.set_workspace_settings(store.workspace_id, {conf_mod.BASE_URL_SETTING: CONF})
    svc = Service(cfg, JiraClient("https://jira.test", "tok"), None, store)
    svc._confluence = _client()
    return svc


@respx.mock
def test_create_under_parent_dry_run_then_write(tmp_path):
    _login_ok()
    respx.get(f"{API}/content/50").mock(return_value=httpx.Response(200, json=_page("50", "Каналы")))
    respx.get(f"{API}/content").mock(return_value=httpx.Response(200, json={"results": []}))
    post = respx.post(f"{API}/content").mock(return_value=httpx.Response(200, json={
        "id": "200", "title": "LINE", "version": {"number": 1}, "_links": {"webui": "/pages/viewpage.action?pageId=200"}}))
    svc = _svc(tmp_path)
    try:
        preview = svc.confluence_create(title=" LINE ", body="<p>гайд</p>", parent_id="50")
        assert preview["dry_run"] and preview["space"] == "WEBIMTEST" and preview["parent"]["title"] == "Каналы"
        assert post.call_count == 0
        res = svc.confluence_create(title="LINE", body="<p>гайд</p>", parent_id="50", dry_run=False)
        assert json.loads(post.calls.last.request.content) == {
            "type": "page", "title": "LINE", "space": {"key": "WEBIMTEST"},
            "body": {"storage": {"value": "<p>гайд</p>", "representation": "storage"}},
            "ancestors": [{"id": "50"}]}
        assert res["id"] == "200" and res["url"].endswith("pageId=200")
    finally:
        svc.close()


@respx.mock
def test_create_refuses_taken_title(tmp_path):
    _login_ok()
    respx.get(f"{API}/content/50").mock(return_value=httpx.Response(200, json=_page("50", "Каналы")))
    respx.get(f"{API}/content").mock(return_value=httpx.Response(200, json={"results": [{"id": "100", "title": "WhatsApp Gupshup"}]}))
    post = respx.post(f"{API}/content")
    svc = _svc(tmp_path)
    try:
        with pytest.raises(ValueError, match="уже есть страница"):
            svc.confluence_create(title="WhatsApp Gupshup", body="<p>x</p>", parent_id="50", dry_run=False)
        assert post.call_count == 0
    finally:
        svc.close()


@respx.mock
def test_update_bumps_version_and_keeps_body_when_only_title(tmp_path):
    _login_ok()
    respx.get(f"{API}/content/100").mock(return_value=httpx.Response(200, json=_page(version=3, body="<p>старое</p>")))
    put = respx.put(f"{API}/content/100").mock(return_value=httpx.Response(200, json={
        "id": "100", "title": "WhatsApp (Gupshup)", "version": {"number": 4}, "_links": {}}))
    svc = _svc(tmp_path)
    try:
        preview = svc.confluence_update("100", title="WhatsApp (Gupshup)")
        assert preview["dry_run"] and preview["version"] == 3 and not preview["body_changed"] and put.call_count == 0
        res = svc.confluence_update("100", title="WhatsApp (Gupshup)", message="уточнил", dry_run=False)
        assert json.loads(put.calls.last.request.content) == {
            "id": "100", "type": "page", "title": "WhatsApp (Gupshup)",
            "version": {"number": 4, "message": "уточнил"},
            "body": {"storage": {"value": "<p>старое</p>", "representation": "storage"}}}
        assert res["version"] == 4
    finally:
        svc.close()


@respx.mock
def test_wiki_body_converted_before_write(tmp_path):
    _login_ok()
    respx.get(f"{API}/content/100").mock(return_value=httpx.Response(200, json=_page()))
    conv = respx.post(f"{API}/contentbody/convert/storage").mock(return_value=httpx.Response(200, json={"value": "<h2>Шаги</h2>"}))
    put = respx.put(f"{API}/content/100").mock(return_value=httpx.Response(200, json={"id": "100", "version": {"number": 4}}))
    svc = _svc(tmp_path)
    try:
        svc.confluence_update("100", body="h2. Шаги", fmt="wiki", dry_run=False)
        assert json.loads(conv.calls.last.request.content) == {"value": "h2. Шаги", "representation": "wiki"}
        assert json.loads(put.calls.last.request.content)["body"]["storage"]["value"] == "<h2>Шаги</h2>"
    finally:
        svc.close()


def test_not_configured(tmp_path):
    cfg = Config()
    store = Store(tmp_path / "s.db")
    svc = Service(cfg, None, None, store)
    try:
        with pytest.raises(ValueError, match="не настроен"):
            svc.confluence()
    finally:
        svc.close()


# --- CLI ---------------------------------------------------------------------- #

def test_cli_setup_and_create_requires_confirmation(tmp_path, monkeypatch):
    r = runner.invoke(cli.app, ["-W", "work", "confluence", "setup", "--url", "https://conf.test/", "--space", "WEBIMTEST"])
    assert r.exit_code == 0 and "https://conf.test" in r.output and "WEBIMTEST" in r.output
    calls = []

    class Svc:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def confluence_create(self, **kw):
            calls.append(kw["dry_run"])
            return {"dry_run": True, "space": "WEBIMTEST", "title": kw["title"], "format": "storage",
                    "parent": {"id": "50", "title": "Каналы", "path": []}, "body_chars": 5}

    monkeypatch.setattr(cli, "_service", lambda: Svc())
    body = tmp_path / "p.html"
    body.write_text("<p>x</p>")
    r = runner.invoke(cli.app, ["confluence", "create", "--title", "LINE", "-F", str(body), "--parent", "50", "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout)["reason"] == "confirm_required"
    assert calls == [True]                      # без --yes только превью
