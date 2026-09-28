"""SSH-стенды воркспейса и конфиг ssh-mcp (JWU-45)."""

import asyncio
import json
import re
import stat

import pytest
import tomli
from typer.testing import CliRunner

from jwu import mcp_server as srv
from jwu.cli import main as cli
from jwu.core import ssh as ssh_mod
from jwu.core.store import Store

runner = CliRunner()


def _server(**kw) -> ssh_mod.SshServer:
    base = dict(name="test", host="test.example.com", username="deploy", private_key="~/.ssh/id_ed25519",
                allowed_remote_paths=["/var/log"], tag="бэкенд", description="логи в /var/log/app")
    base.update(kw)
    return ssh_mod.SshServer(**base)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.db")
    yield s
    s.close()


def test_save_list_remove(store):
    wid = store.get_workspace_by_slug("work").id
    ssh_mod.save_server(store, wid, _server(), passphrase="secret")
    ssh_mod.save_server(store, wid, _server(name="prod", private_key="", agent="env"))
    assert [s.name for s in ssh_mod.list_servers(store, wid)] == ["prod", "test"]
    # секрет — в секретах воркспейса, не в настройках (настройки уезжают в синк памяти)
    assert "secret" not in json.dumps(store.workspace_settings(wid))
    assert store.get_workspace_secret(wid, "ssh.test.passphrase") == "secret"
    ssh_mod.remove_server(store, wid, "test")
    assert [s.name for s in ssh_mod.list_servers(store, wid)] == ["prod"]
    assert store.get_workspace_secret(wid, "ssh.test.passphrase") is None
    with pytest.raises(ssh_mod.SshError):
        ssh_mod.get_server(store, wid, "test")


def test_servers_scoped_by_workspace(store):
    work = store.get_workspace_by_slug("work").id
    home = store.create_workspace("home").id
    ssh_mod.save_server(store, work, _server())
    assert ssh_mod.list_servers(store, home) == []


@pytest.mark.parametrize("kw, msg", [
    (dict(name="Bad Name"), "Имя"),
    (dict(host=""), "host"),
    (dict(port=70000), "Порт"),
    (dict(policy="yolo"), "Политика"),
    (dict(whitelist=["(unclosed"]), "regex"),
    (dict(allowed_remote_paths=["var/log"]), "абсолютным"),
    (dict(private_key=""), "способ входа"),
])
def test_validation(store, kw, msg):
    wid = store.get_workspace_by_slug("work").id
    with pytest.raises(ssh_mod.SshError, match=msg):
        ssh_mod.save_server(store, wid, _server(**kw))


def test_password_counts_as_auth(store):
    wid = store.get_workspace_by_slug("work").id
    ssh_mod.save_server(store, wid, _server(private_key=""), password="pw")
    assert ssh_mod.has_password(store, wid, "test")


def _allowed(server: ssh_mod.SshServer, cmd: str) -> bool:
    wl, bl = server.effective_whitelist(), server.effective_blacklist()
    return (not wl or any(re.search(p, cmd) for p in wl)) and not any(re.search(p, cmd) for p in bl)


@pytest.mark.parametrize("cmd, ok", [
    ("tail -n 200 /var/log/app/error.log", True),
    ("grep -i timeout /var/log/app/app.log | tail -50", True),
    ("journalctl -u app --since today", True),
    ("docker logs --tail 100 app", True),
    ("systemctl status nginx", True),
    ("systemctl restart nginx", False),
    ("rm -rf /var/log", False),
    ("tail x; rm -rf ~", False),
    ("cat a && reboot", False),
    ("cat $(echo /etc/shadow)", False),
    ("cat a > /etc/passwd", False),
    ("cat script | bash", False),
    ("find /var/log -name '*.gz' -delete", False),
    ("ls | xargs rm", False),
    ("sudo tail /var/log/secure", False),
])
def test_readonly_policy(cmd, ok):
    assert _allowed(_server(), cmd) is ok


def test_policy_none_allows_everything_but_deny():
    s = _server(policy="none", blacklist=[r"^rm "])
    assert _allowed(s, "systemctl restart nginx")
    assert not _allowed(s, "rm -rf /tmp/x")


def test_render_config_is_valid_toml(store):
    wid = store.get_workspace_by_slug("work").id
    ssh_mod.save_server(store, wid, _server(description='кавычки " и \\ слэш'), passphrase='p"w')
    ssh_mod.save_server(store, wid, _server(name="prod", private_key="", allowed_remote_paths=[]),
                        password="pa\\ss")
    text = ssh_mod.render_config(ssh_mod.list_servers(store, wid), store.workspace_secrets(wid))
    data = tomli.loads(text)
    by_name = {s["name"]: s for s in data["server"]}
    assert by_name["test"]["passphrase"] == 'p"w'
    assert by_name["prod"]["password"] == "pa\\ss"
    assert by_name["test"]["whitelist"] == ssh_mod.READONLY_WHITELIST
    assert by_name["test"]["allowed_remote_paths"] == ["/var/log"]
    # поля jwu (tag/description/policy) в конфиг ssh-mcp не попадают: он их не знает
    assert "tag" not in by_name["test"] and "policy" not in by_name["test"]


def test_write_config_mode_600(store):
    ws = store.get_workspace_by_slug("work")
    ssh_mod.save_server(store, ws.id, _server())
    path = ssh_mod.write_config(store, ws.id, ws.slug)
    assert path == ssh_mod.config_path("work")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert tomli.loads(path.read_text())["server"][0]["name"] == "test"


def test_install_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(ssh_mod, "find_binary", lambda: "/usr/local/bin/ssh-mcp")
    res = ssh_mod.install_mcp("work", [str(tmp_path)], tmp_path / "c.toml", dry_run=True)
    assert res[0].ok
    assert res[0].message.split() == ["claude", "mcp", "add", "--scope", "local", "ssh-work", "--",
                                      "/usr/local/bin/ssh-mcp", "--config", str(tmp_path / "c.toml")]


def test_install_without_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(ssh_mod, "find_binary", lambda: None)
    with pytest.raises(ssh_mod.SshError, match="go install"):
        ssh_mod.install_mcp("work", [str(tmp_path)], tmp_path / "c.toml")


def test_install_runs_claude_per_path(tmp_path, monkeypatch):
    calls = []

    class Proc:
        returncode, stdout, stderr = 0, "Added stdio MCP server ssh-work", ""

    def fake_run(args, cwd=None, **kw):
        calls.append((args[:3], cwd))
        return Proc()

    monkeypatch.setattr(ssh_mod.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(ssh_mod.subprocess, "run", fake_run)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    res = ssh_mod.install_mcp("work", [str(a), str(b), str(tmp_path / "gone")], tmp_path / "c.toml")
    assert [r.ok for r in res] == [True, True, False]
    assert calls == [(["claude", "mcp", "remove"], str(a)), (["claude", "mcp", "add"], str(a)),
                     (["claude", "mcp", "remove"], str(b)), (["claude", "mcp", "add"], str(b))]


def test_describe_has_no_secrets(store):
    ws = store.get_workspace_by_slug("work")
    ssh_mod.save_server(store, ws.id, _server(private_key=""), password="topsecret")
    info = ssh_mod.describe(store, ws.id, ws.slug)
    assert "topsecret" not in json.dumps(info, ensure_ascii=False)
    assert info["mcp_server"] == "ssh-work" and info["tools_prefix"] == "mcp__ssh-work__"
    assert info["servers"][0]["auth"] == "password" and info["servers"][0]["has_password"]


def test_cli_add_list_config_rm(tmp_path):
    r = runner.invoke(cli.app, ["-W", "work", "ssh", "add", "test", "--host", "h.example.com",
                                "--user", "deploy", "--key", "~/.ssh/id", "--remote-path", "/var/log",
                                "--tag", "бэкенд"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli.app, ["-W", "work", "ssh", "list", "--json"])
    assert json.loads(r.output[r.output.index("{"):])["servers"][0]["host"] == "h.example.com"
    r = runner.invoke(cli.app, ["-W", "work", "ssh", "config"])
    assert r.exit_code == 0, r.output
    assert ssh_mod.config_path("work").exists()
    # правка стенда при выданном конфиге переписывает его сразу
    runner.invoke(cli.app, ["-W", "work", "ssh", "add", "test", "--host", "h2.example.com",
                            "--user", "deploy", "--key", "~/.ssh/id"])
    assert "h2.example.com" in ssh_mod.config_path("work").read_text()
    r = runner.invoke(cli.app, ["-W", "work", "ssh", "rm", "test", "-y"])
    assert r.exit_code == 0, r.output
    assert "h2.example.com" not in ssh_mod.config_path("work").read_text()


def test_cli_add_rejects_no_auth():
    r = runner.invoke(cli.app, ["-W", "work", "ssh", "add", "test", "--host", "h", "--user", "u"])
    assert r.exit_code == 1 and "способ входа" in r.output


def test_mcp_ssh_servers(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.setenv("JWU_DB_PATH", str(db))
    monkeypatch.setattr(srv, "_base_store", None)
    monkeypatch.setattr(srv, "_stores", {})
    seed = Store(db)
    ssh_mod.save_server(seed, seed.get_workspace_by_slug("work").id, _server())
    seed.close()
    try:
        out = asyncio.run(srv.jwu_ssh_servers(workspace="work"))
        assert out["workspace"] == "work" and out["servers"][0]["name"] == "test"
    finally:
        for s in list(srv._stores.values()):
            s.close()
        if srv._base_store is not None:
            srv._base_store.close()


def test_doctor_check_ssh(store, tmp_path, monkeypatch):
    from jwu.core import doctor

    ws = store.get_workspace_by_slug("work")
    assert doctor.check_ssh(store, ws)[0].status == "skip"
    ssh_mod.save_server(store, ws.id, _server())
    monkeypatch.setattr(ssh_mod, "find_binary", lambda: None)
    assert doctor.check_ssh(store, ws)[0].status == "fail"
    monkeypatch.setattr(ssh_mod, "find_binary", lambda: "/bin/ssh-mcp")
    assert doctor.check_ssh(store, ws)[0].status == "warn"  # конфиг не выдан
    ssh_mod.write_config(store, ws.id, ws.slug)
    folder = str(tmp_path / "proj")
    store.add_workspace_path(ws.id, folder)
    ws = store.get_workspace_by_slug("work")
    claude_json = tmp_path / "claude.json"
    monkeypatch.setenv("CLAUDE_CONFIG_PATH", str(claude_json))
    claude_json.write_text(json.dumps({"projects": {}}))
    assert doctor.check_ssh(store, ws)[0].status == "warn"
    claude_json.write_text(json.dumps({"projects": {folder: {"mcpServers": {"ssh-work": {}}}}}))
    assert doctor.check_ssh(store, ws)[0].status == "ok"


def test_detail_rows_full_and_compact():
    s = _server(whitelist=[r"^cat /srv/.*"], blacklist=[r"secret"])
    full = dict(ssh_mod.detail_rows(s, has_pw=True))
    assert full["адрес"] == ["deploy@test.example.com:22"]
    assert full["вход"] == ["ключ ~/.ssh/id_ed25519 + пароль"]
    assert full["whitelist"] == ssh_mod.READONLY_WHITELIST + [r"^cat /srv/.*"]
    assert full["blacklist"][-1] == "secret"
    assert full["SFTP на сервере"] == ["/var/log"] and "SFTP локально" not in full
    compact = dict(ssh_mod.detail_rows(s, compact=True))
    assert compact["whitelist"] == [f"пресет readonly ({len(ssh_mod.READONLY_WHITELIST)})", r"^cat /srv/.*"]
    assert dict(ssh_mod.detail_rows(_server(policy="none")))["whitelist"] == ["— (любые команды)"]


def test_workspace_context_has_brief_servers(store):
    store.use_workspace(store.get_workspace_by_slug("work").id)
    assert "ssh_servers" not in store.workspace_context()
    ssh_mod.save_server(store, store.workspace_id, _server(), password="pw")
    brief = store.workspace_context()["ssh_servers"]
    assert brief == [{"name": "test", "address": "deploy@test.example.com:22",
                      "auth": "ключ ~/.ssh/id_ed25519 + пароль", "policy": "readonly",
                      "tag": "бэкенд", "description": "логи в /var/log/app"}]
    assert "pw" not in json.dumps(brief)


def test_workspace_show_has_ssh_section():
    runner.invoke(cli.app, ["-W", "work", "ssh", "add", "test", "--host", "h.example.com", "--user", "deploy",
                            "--key", "~/.ssh/id", "--deny", "^cat /etc/.*", "--desc", "логи [app]"])
    r = runner.invoke(cli.app, ["-W", "work", "workspace", "show"])
    assert r.exit_code == 0, r.output
    out = r.output
    assert "SSH-стенды" in out and "deploy@h.example.com:22" in out and "ключ ~/.ssh/id" in out
    assert "^journalctl( |$)" in out and "^cat /etc/.*" in out and "логи [app]" in out


def test_dashboard_ssh_head(store):
    from jwu.cli.dashboard import JwuDashboard
    from jwu.core.service import dashboard_from_memory

    store.use_workspace(store.get_workspace_by_slug("work").id)
    ssh_mod.save_server(store, store.workspace_id, _server(whitelist=[r"^cat /srv/.*"]))
    data = dashboard_from_memory(store)
    assert data.ssh_servers and data.ssh_servers[0][0].name == "test"
    app = JwuDashboard.__new__(JwuDashboard)
    app.data = data
    text = "\n".join(app._ssh_head_lines())
    assert "SSH-стенды" in text and "deploy@test.example.com:22" in text
    assert "пресет readonly" in text and "^cat /srv/.*" in text
