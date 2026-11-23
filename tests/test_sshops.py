"""Проверка политики встроенного SSH через обычные MCP и CLI."""

import asyncio
import sys
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from jwu import mcp_server as srv
from jwu.cli import main as cli
from jwu.core import ssh, sshops
from jwu.core.store import Store


@pytest.fixture
def stand(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setenv("JWU_DB_PATH", str(path))
    monkeypatch.setenv("JWU_WORKSPACE", "work")
    monkeypatch.setattr(srv, "_base_store", None)
    monkeypatch.setattr(srv, "_stores", {})
    with Store(path) as store:
        server = ssh.SshServer("test", "test.example", "deploy", agent="env")
        ssh.save_server(store, store.workspace_id, server)
        yield store, server
    for cached in srv._stores.values():
        cached.close()
    if srv._base_store:
        srv._base_store.close()


@pytest.fixture
def commands(monkeypatch):
    seen = []

    async def run(argv):
        seen.append(argv)
        return {"ok": True, "stdout": "sample"}

    monkeypatch.setattr(sshops, "_run_ssh", run)
    return seen


def _call(**kwargs):
    args = dict(server="test", action="logs", kind="docker", target="api", workspace="work")
    args.update(kwargs)
    return asyncio.run(srv.jwu_ssh(**args))


def test_ssh_mcp_policy_and_exact_targets(stand, commands):
    store, server = stand
    assert "jwu_ssh" in {tool.name for tool in asyncio.run(srv.mcp.list_tools())}
    assert _call(user_approved=True)["blocked"] == "deny"
    sshops.set_rule(store, server, "logs", "docker", "api", "allow")
    assert _call(action="status")["blocked"] == "deny"
    assert _call(target="other")["blocked"] == "deny"
    assert _call(kind="systemd")["blocked"] == "deny"
    assert "error" in _call(target="api; date")
    assert "error" in _call(lines=1001)
    assert not commands
    result = _call(lines=2)
    assert result["stdout"] == "sample"
    assert commands[0][-3:] == ["--", "test.example", "docker logs --tail 2 -- api"]
    assert "StrictHostKeyChecking=yes" in commands[0]
    assert "ForwardAgent=no" in commands[0]
    assert "IdentityAgent=SSH_AUTH_SOCK" in commands[0]
    info = asyncio.run(srv.jwu_ssh_servers(workspace="work"))
    assert info["default_access"] == "deny"
    assert info["servers"][0]["access"] == [
        {"action": "logs", "kind": "docker", "target": "api", "level": "allow"}
    ]


def test_ssh_ask_and_legacy_blacklist(stand, commands):
    store, server = stand
    sshops.set_rule(store, server, "status", "systemd", "api.service", "ask")
    args = dict(action="status", kind="systemd", target="api.service")
    assert _call(**args)["blocked"] == "ask"
    assert not commands
    assert _call(**args, user_approved=True)["ok"]
    assert commands[0][-1].startswith("systemctl show ")
    ssh.save_server(store, store.workspace_id, replace(server, blacklist=[r"^systemctl show"]))
    assert _call(**args, user_approved=True)["blocked"] == "deny"
    assert len(commands) == 1
    restricted = ssh.get_server(store, store.workspace_id, server.name)
    sshops.set_rule(store, restricted, "status", "systemd", "api.service", "allow")
    assert _call(**args)["reason"] == "blacklist стенда"
    ssh.save_server(store, store.workspace_id, server)
    assert _call(**args)["blocked"] == "deny"
    assert len(commands) == 1


def test_ssh_grant_cli_binding_and_single_use(stand, commands, monkeypatch):
    store, server = stand
    runner = CliRunner()
    policy = ["-W", "work", "ssh", "policy", "test", "logs", "docker", "api", "grant"]
    assert runner.invoke(cli.app, policy).exit_code == 2
    with monkeypatch.context() as terminal:
        terminal.setattr("click.testing._NamedTextIOWrapper.isatty", lambda self: True)
        assert runner.invoke(cli.app, policy).exit_code == 0
    pending = _call(user_approved=True, lines=9)
    assert pending["blocked"] == "grant"
    grant_id = pending["grant_id"]
    assert _call(lines=9)["grant_id"] == grant_id
    assert _call(lines=9, grant_id=grant_id)["status"] == "requested"
    assert not commands
    grant = ["-W", "work", "ssh", "grant", str(grant_id)]
    assert runner.invoke(cli.app, grant, input="y\n").exit_code == 2
    with monkeypatch.context() as terminal:
        terminal.setattr("click.testing._NamedTextIOWrapper.isatty", lambda self: True)
        out = runner.invoke(cli.app, grant, input="y\n")
    assert out.exit_code == 0, out.output
    assert "docker logs --tail 9 -- api" in out.output
    assert "error" in _call(lines=10, grant_id=grant_id)
    other = store.create_workspace("home")
    ssh.save_server(store, other.id, server)
    store.use_workspace(other.id)
    sshops.set_rule(store, server, "logs", "docker", "api", "grant")
    assert "error" in _call(lines=9, grant_id=grant_id, workspace="home")
    assert not commands
    assert _call(lines=9, grant_id=grant_id)["ok"]
    assert _call(lines=9, grant_id=grant_id)["status"] == "used"
    assert len(commands) == 1


def test_ssh_grant_expiration_denial_and_rule_revision(stand, commands, monkeypatch):
    store, server = stand
    now = 100
    monkeypatch.setattr(sshops.time, "time", lambda: now)
    sshops.set_rule(store, server, "logs", "docker", "api", "grant")
    denied = _call()["grant_id"]
    sshops.answer_grant(store, denied, allow=False)
    assert _call(grant_id=denied)["status"] == "denied"
    expired = _call()["grant_id"]
    now += sshops.GRANT_REQUEST_TTL
    assert _call(grant_id=expired)["status"] == "expired"
    with pytest.raises(ssh.SshError, match="expired"):
        sshops.answer_grant(store, expired, allow=True)
    changed = _call()["grant_id"]
    sshops.answer_grant(store, changed, allow=True)
    sshops.set_rule(store, server, "logs", "docker", "api", "grant")
    assert "error" in _call(grant_id=changed)
    assert not commands


def test_ssh_policy_endpoint_change_and_removal(stand, commands):
    store, server = stand
    sshops.set_rule(store, server, "logs", "docker", "api", "allow")
    ssh.save_server(store, store.workspace_id, replace(server, host="different.example"))
    assert _call()["blocked"] == "deny"
    ssh.remove_server(store, store.workspace_id, server.name)
    ssh.save_server(store, store.workspace_id, server)
    assert _call()["blocked"] == "deny"
    assert not commands


def test_ssh_requires_session_workspace_and_clears_removed_grants(stand, commands, tmp_path, monkeypatch):
    store, server = stand
    sshops.set_rule(store, server, "logs", "docker", "api", "grant")
    pending = _call()["grant_id"]
    monkeypatch.delenv("JWU_WORKSPACE")
    monkeypatch.chdir(tmp_path)
    assert "error" in _call(grant_id=pending)
    store.add_workspace_path(store.workspace_id, str(tmp_path))
    assert _call(grant_id=pending)["status"] == "requested"
    store.delete_workspace(store.workspace_id)
    assert not store.conn.execute("SELECT 1 FROM ssh_grants WHERE id = ?", (pending,)).fetchone()
    assert not commands


def test_ssh_subprocess_output_limit_and_deadline(monkeypatch):
    result = asyncio.run(sshops._run_ssh([
        sys.executable, "-c", "import sys; print('x' * 99999); print('error', file=sys.stderr); sys.exit(9)"
    ]))
    assert result["exit_code"] == 9
    assert not result["ok"]
    assert result["truncated"]
    assert len(result["stdout"]) == sshops.OUTPUT_LIMIT
    assert result["stderr"] == "error\n"
    monkeypatch.setattr(sshops, "TIMEOUT", 0.2)
    with pytest.raises(ssh.SshError, match="за 0.2 с"):
        asyncio.run(sshops._run_ssh([
            sys.executable, "-c", "import time; print('x' * 99999); time.sleep(3)"
        ]))
