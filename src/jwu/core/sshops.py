"""Диагностика SSH-стендов с правилами на конкретное действие и цель."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import shlex
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from .ssh import SshError, SshServer, get_server

if TYPE_CHECKING:
    from .store import Store

ACTIONS = ("logs", "status")
KINDS = ("systemd", "docker")
LEVELS = ("allow", "ask", "grant", "deny")
RULE_PREFIX = "ssh.access."
GRANT_REQUEST_TTL = 900
GRANT_TTL = 600
OUTPUT_LIMIT = 64 * 1024
TIMEOUT = 30

_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,254}")
_USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")


def _validate_operation(action: str, kind: str, target: str) -> None:
    if action not in ACTIONS or kind not in KINDS:
        raise SshError("Действия: logs, status. Типы целей: systemd, docker.")
    if not _TARGET.fullmatch(target):
        raise SshError("Укажи точное имя systemd-юнита или контейнера.")


def _command(action: str, kind: str, target: str, lines: int | None) -> list[str]:
    _validate_operation(action, kind, target)
    if action == "status":
        if lines is not None:
            raise SshError("lines применяется только для logs.")
        if kind == "systemd":
            return ["systemctl", "show", "--no-pager", "--property=ActiveState,SubState,Result", "--", target]
        return ["docker", "inspect", "--type", "container", "--format", "{{.State.Status}}", "--", target]
    lines = 100 if lines is None else lines
    if not 1 <= lines <= 1000:
        raise SshError("lines должно быть от 1 до 1000.")
    if kind == "systemd":
        return ["journalctl", "--no-pager", "--lines", str(lines), "--unit", target]
    return ["docker", "logs", "--tail", str(lines), "--", target]


def _stand_scope(server: SshServer) -> str:
    return json.dumps([server.host, server.port, server.username, server.private_key, server.agent,
                       server.effective_whitelist(), server.effective_blacklist()])


def rules(store: Store, server: SshServer, settings: dict[str, str] | None = None) -> list[dict]:
    if settings is None:
        settings = store.workspace_settings(store.workspace_id)
    prefix = f"{RULE_PREFIX}{server.name}/"
    result = []
    for key, raw in settings.items():
        if not key.startswith(prefix):
            continue
        try:
            rule = json.loads(raw)
            if rule["stand_scope"] == _stand_scope(server) and rule["level"] in LEVELS:
                _validate_operation(rule["action"], rule["kind"], rule["target"])
                if isinstance(rule["revision"], str):
                    result.append(rule)
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(result, key=lambda row: (row["action"], row["kind"], row["target"]))


def describe_rules(store: Store, server: SshServer, settings: dict[str, str] | None = None) -> list[dict]:
    return [{key: row[key] for key in ("action", "kind", "target", "level")}
            for row in rules(store, server, settings)]


def set_rule(store: Store, server: SshServer, action: str, kind: str, target: str, level: str) -> None:
    _validate_operation(action, kind, target)
    if level not in LEVELS:
        raise SshError(f"Допустимые уровни: {', '.join(LEVELS)}.")
    key = f"{RULE_PREFIX}{server.name}/{action}/{kind}/{target}"
    rule = dict(action=action, kind=kind, target=target, level=level,
                stand_scope=_stand_scope(server), revision=uuid.uuid4().hex)
    store.set_workspace_settings(store.workspace_id, {key: json.dumps(rule)})


def _prepare_ssh(server: SshServer, remote_command: str) -> list[str]:
    if not _HOST.fullmatch(server.host):
        try:
            ipaddress.IPv6Address(server.host)
        except ValueError as exc:
            raise SshError("Некорректный адрес SSH-стенда.") from exc
    if not _USER.fullmatch(server.username) or not 1 <= server.port <= 65535:
        raise SshError("Некорректный пользователь или порт SSH-стенда.")
    if not (server.private_key or server.agent):
        raise SshError("Для встроенного SSH у стенда нужен ключ или ssh-agent.")
    cmd = [
        "ssh", "-F", "none", "-T",
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no", "-o", "UpdateHostKeys=no",
        "-o", "ConnectTimeout=10", "-o", "PreferredAuthentications=publickey",
        "-p", str(server.port), "-l", server.username,
    ]
    if server.private_key:
        cmd.extend(["-i", str(Path(server.private_key).expanduser()), "-o", "IdentitiesOnly=yes"])
    else:
        cmd.extend(["-o", "IdentityFile=none"])
    agent = {"env": "SSH_AUTH_SOCK", "": "none"}.get(server.agent, server.agent)
    cmd.extend(["-o", f"IdentityAgent={agent}", "--", server.host, remote_command])
    return cmd


async def _read_output(stream: asyncio.StreamReader) -> tuple[str, bool]:
    output = bytearray()
    truncated = False
    while chunk := await stream.read(8192):
        remaining = OUTPUT_LIMIT - len(output)
        output.extend(chunk[:remaining])
        truncated |= len(chunk) > remaining
    return output.decode("utf-8", errors="replace"), truncated


async def _run_ssh(cmd: list[str]) -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise SshError(f"Не удалось запустить OpenSSH: {exc.strerror or exc}.") from exc

    async def collect() -> dict:
        stdout, stderr, code = await asyncio.gather(_read_output(proc.stdout),
                                                    _read_output(proc.stderr), proc.wait())
        return {"ok": code == 0, "exit_code": code, "stdout": stdout[0], "stderr": stderr[0],
                "truncated": stdout[1] or stderr[1]}

    try:
        return await asyncio.wait_for(collect(), TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise SshError(f"SSH не завершился за {TIMEOUT} с.") from exc
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await asyncio.gather(_read_output(proc.stdout), _read_output(proc.stderr), proc.wait())


def _grant_row(store: Store, grant_id: int):
    return store.conn.execute(
        "SELECT * FROM ssh_grants WHERE workspace_id = ? AND id = ?", (store.workspace_id, grant_id)
    ).fetchone()


def _grant_state(row, now: int) -> str:
    if row["status"] in ("requested", "granted") and row["expires_at"] <= now:
        return "expired"
    return row["status"]


def answer_grant(store: Store, grant_id: int, allow: bool) -> str:
    row = _grant_row(store, grant_id)
    if row is None:
        raise SshError(f"Запроса SSH #{grant_id} в воркспейсе нет.")
    now = int(time.time())
    status = _grant_state(row, now)
    if status != "requested":
        raise SshError(f"Запрос SSH #{grant_id}: {status}.")
    new_status = "granted" if allow else "denied"
    with store.conn:
        updated = store.conn.execute(
            "UPDATE ssh_grants SET status = ?, expires_at = ?"
            " WHERE workspace_id = ? AND id = ? AND status = 'requested' AND expires_at > ?",
            (new_status, now + GRANT_TTL, store.workspace_id, grant_id, now),
        )
        if not updated.rowcount:
            raise SshError(f"Запрос SSH #{grant_id} уже обработан.")
    return new_status


def grant_description(store: Store, grant_id: int) -> str:
    row = _grant_row(store, grant_id)
    if row is None:
        raise SshError(f"Запроса SSH #{grant_id} в воркспейсе нет.")
    return row["description"]


def _authorize_grant(store: Store, fingerprint: str, description: str, grant_id: int | None) -> dict | None:
    now = int(time.time())
    if grant_id is None:
        with store.conn:
            store.conn.execute("DELETE FROM ssh_grants WHERE expires_at < ?", (now - 86400,))
            existing = store.conn.execute(
                "SELECT id FROM ssh_grants WHERE workspace_id = ? AND fingerprint = ?"
                " AND status = 'requested' AND expires_at > ? ORDER BY id DESC LIMIT 1",
                (store.workspace_id, fingerprint, now),
            ).fetchone()
            if existing:
                grant_id = existing["id"]
            else:
                created = store.conn.execute(
                    "INSERT INTO ssh_grants (workspace_id, fingerprint, description, status, expires_at)"
                    " VALUES (?, ?, ?, 'requested', ?)",
                    (store.workspace_id, fingerprint, description, now + GRANT_REQUEST_TTL),
                )
                grant_id = created.lastrowid
        status = "requested"
    else:
        row = _grant_row(store, grant_id)
        if row is None or row["fingerprint"] != fingerprint:
            raise SshError("Запрос допуска не относится к этому действию и стенду.")
        with store.conn:
            used = store.conn.execute(
                "UPDATE ssh_grants SET status = 'used' WHERE id = ? AND workspace_id = ?"
                " AND fingerprint = ? AND status = 'granted' AND expires_at > ?",
                (grant_id, store.workspace_id, fingerprint, now),
            )
        if used.rowcount:
            return None
        status = _grant_state(_grant_row(store, grant_id), now)
    return {"ok": False, "blocked": "grant", "grant_id": grant_id, "status": status}


async def execute(store: Store, server_name: str, action: str, kind: str, target: str,
                  lines: int | None = None, *, user_approved: bool = False,
                  grant_id: int | None = None) -> dict:
    command = shlex.join(_command(action, kind, target, lines))
    server = get_server(store, store.workspace_id, server_name)
    rule = next((row for row in rules(store, server)
                 if (row["action"], row["kind"], row["target"]) == (action, kind, target)), None)
    level = rule["level"] if rule else "deny"
    blocked = {"ok": False, "blocked": "deny", "command": command}
    if level not in ("allow", "ask", "grant"):
        return blocked
    if any(re.search(pattern, command) for pattern in server.effective_blacklist()):
        return {**blocked, "reason": "blacklist стенда"}
    whitelist = server.effective_whitelist()
    if whitelist and not any(re.search(pattern, command) for pattern in whitelist):
        return {**blocked, "reason": "whitelist стенда"}
    cmd = _prepare_ssh(server, command)
    if level == "ask" and not user_approved:
        return {"ok": False, "blocked": "ask", "command": command}
    if level == "grant":
        binding = json.dumps([server.name, rule["stand_scope"], rule["revision"], action, kind, target, command])
        fingerprint = hashlib.sha256(binding.encode()).hexdigest()
        description = f"{server.name} ({server.username}@{server.host}:{server.port}) · {command}"
        pending = _authorize_grant(store, fingerprint, description, grant_id)
        if pending:
            return {**pending, "command": command}
    return {**await _run_ssh(cmd), "command": command}
