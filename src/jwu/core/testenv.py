"""Доверенный локальный профиль зависимостей django-chat для работы jwu."""

from __future__ import annotations

import json
import os
import socket
import struct
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from . import jobspace

PROFILE = "django-chat"
KIND = "testenv"
STAND_LABEL = "jwu.testenv"
SERVICE_LABEL = "jwu.testenv.service"
HOST = "127.0.0.1"


@dataclass(frozen=True)
class Service:
    image: str
    port: int
    env: tuple[str, ...] = ()
    args: tuple[str, ...] = ()


SERVICES = {
    "db": Service(
        "postgres:17-alpine", 5432,
        env=("POSTGRES_USER=postgres", "POSTGRES_PASSWORD=postgres", "POSTGRES_DB=postgres"),
    ),
    "redis": Service("redis:7.4-alpine", 6379),
    "s3": Service(
        "pgsty/minio:latest", 9000,
        env=("MINIO_ROOT_USER=minioadmin", "MINIO_ROOT_PASSWORD=minioadmin"),
        args=("server", "/data"),
    ),
}


@contextmanager
def lock(store, uid):
    """Сериализовать операции над одним стендом в CLI и MCP."""
    import fcntl

    if store.path == ":memory:":
        yield
        return
    path = Path(store.path).resolve().with_name(f".jwu-testenv-{uid}.lock")
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _docker(runner, *args):
    code, output = runner(["docker", *args])
    if code:
        raise jobspace.JobSpaceError(f"docker {args[0]}: {output[-600:]}")
    return output


def _local_runner(runner):
    endpoint = os.environ.get("DOCKER_HOST")
    if not endpoint or os.environ.get("DOCKER_CONTEXT"):
        endpoint = _docker(runner, "context", "inspect", "--format", '{{(index .Endpoints "docker").Host}}')
    if not endpoint.startswith("unix:///") or "\n" in endpoint:
        raise jobspace.JobSpaceError("Для локального стенда нужен Docker через локальный Unix-сокет")

    def local(command):
        return runner(["docker", "--host", endpoint, *command[1:]])

    daemon = _docker(local, "info", "--format", "{{.ID}}")
    if not daemon:
        raise jobspace.JobSpaceError("Docker не вернул идентификатор демона")
    return local, daemon


def _check_daemon(resources, daemon):
    if any(r["kind"] == KIND and r["meta"]["daemon"] != daemon for r in resources):
        raise jobspace.JobSpaceError("Выбран другой Docker-демон. Верните контекст, в котором запущен стенд.")


def _networks(uid, runner):
    return _docker(
        runner, "network", "ls", "-q", "--no-trunc",
        "--filter", f"label={jobspace.label(uid)}",
        "--filter", f"label={STAND_LABEL}={PROFILE}",
    ).splitlines()



def _containers(uid, runner):
    output = _docker(
        runner, "ps", "-a", "--no-trunc",
        "--filter", f"label={jobspace.label(uid)}",
        "--filter", f"label={STAND_LABEL}={PROFILE}",
        "--format", '{{.ID}} {{.Label "jwu.testenv.service"}}',
    )
    return [tuple(line.split(maxsplit=1)) for line in output.splitlines()]


def _port(container, service, runner):
    output = _docker(
        runner, "inspect", "--format", "{{json .NetworkSettings.Ports}}", container,
    )
    mappings = (json.loads(output) or {}).get(f"{service.port}/tcp") or []
    for mapping in mappings:
        if mapping["HostIp"] == HOST:
            return int(mapping["HostPort"])
    raise jobspace.JobSpaceError(f"Контейнер {container[:12]} не опубликовал порт на {HOST}")


def _responds(name, port):
    if name == "s3":
        with urlopen(f"http://{HOST}:{port}/minio/health/ready", timeout=2) as response:
            return response.status == 200
    with socket.create_connection((HOST, port), timeout=2) as sock:
        if name == "db":
            # PostgreSQL SSLRequest: ответ S или N приходит от готового сервера.
            sock.sendall(struct.pack("!II", 8, 80877103))
            return sock.recv(1) in (b"S", b"N")
        sock.sendall(b"*1\r\n$4\r\nPING\r\n")
        with sock.makefile("rb") as reply:
            return reply.readline() == b"+PONG\r\n"


def _ready(name, port):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if _responds(name, port):
                return
        except (URLError, OSError):
            pass
        time.sleep(0.5)
    raise jobspace.JobSpaceError(f"Сервис {name} не готов на {HOST}:{port} за 60 секунд")


def _remove(containers, runner, report):
    for container, name in containers:
        try:
            if not report.dry_run:
                _docker(runner, "rm", "-fv", container)
        except jobspace.JobSpaceError as exc:
            report.errors.append(f"{name}: {exc}")
        else:
            report.removed.append(f"стенд {PROFILE}: {name} ({container[:12]})")


def _resources(store, job_id):
    resources = store.job_resources(job_id)
    return [
        r for r in resources
        if r["kind"] == KIND or (r["kind"] == "port" and r["meta"].get("testenv") == PROFILE)
    ]


def _remove_networks(networks, runner, report):
    for network in networks:
        try:
            if not report.dry_run:
                _docker(runner, "network", "rm", network)
        except jobspace.JobSpaceError as exc:
            report.errors.append(str(exc))
        else:
            report.removed.append(f"сеть стенда {network[:12]}")


def cleanup_locked(store, job_id, uid, runner, report):
    """Удалить сервисы профиля и их анонимные тома, затем снять резервации."""
    resources = _resources(store, job_id)
    if not resources:
        return {}
    try:
        runner, daemon = _local_runner(runner)
        _check_daemon(resources, daemon)
        containers = _containers(uid, runner)
        networks = _networks(uid, runner)
    except jobspace.JobSpaceError as exc:
        report.errors.append(str(exc))
        return {}
    _remove(containers, runner, report)
    if not report.errors:
        _remove_networks(networks, runner, report)
    if not report.errors:
        for resource in resources:
            if not report.dry_run:
                store.mark_job_resource_removed(resource["id"])
            report.removed.append(f"{resource['kind']} {resource['ref']}")
    return {
        "container": {container[:12] for container, _ in containers},
        "network": {network[:12] for network in networks},
    }


def manage(store, job_id, action, *, minio=False, low_disk_ok=False, runner=jobspace._run):
    """Поднять профиль или убрать только его ресурсы."""
    uid = store.job_uid(job_id)
    if uid is None:
        raise jobspace.JobSpaceError(f"Работа #{job_id} не найдена")
    if action not in ("up", "down"):
        raise jobspace.JobSpaceError("Действие стенда: up или down")
    with lock(store, uid):
        if action == "down":
            report = jobspace.CleanupReport()
            cleanup_locked(store, job_id, uid, runner, report)
            if report.errors:
                raise jobspace.JobSpaceError("; ".join(report.errors))
            return {"profile": PROFILE, **report.to_dict()}
        job = store.get_job(job_id)
        if job is None or job.uid != uid or job.status != "active":
            raise jobspace.JobSpaceError(f"Работа #{job_id} должна быть активной")
        return _up(store, job_id, uid, minio, low_disk_ok, runner)


def _up(store, job_id, uid, minio, low_disk_ok, runner):
    raw_runner = runner
    runner, daemon = _local_runner(raw_runner)
    _check_daemon(_resources(store, job_id), daemon)
    existing = _containers(uid, runner)
    networks = _networks(uid, runner)
    by_name = {name: container for container, name in existing}
    required = ["db", "redis"]
    if minio or "s3" in by_name:
        required.append("s3")
    if not networks or any(name not in by_name for name in required):
        jobspace.require_space(store, store.workspace_id, low_disk_ok=low_disk_ok, runner=runner)
    store.add_job_resource(job_id, KIND, PROFILE, {"daemon": daemon})
    ports = {}
    try:
        network = networks[0] if networks else _docker(
            runner, "network", "create", "--label", jobspace.label(uid),
            "--label", f"{STAND_LABEL}={PROFILE}", f"{jobspace.compose_project(uid)}-testenv",
        )
        for name in required:
            service = SERVICES[name]
            container = by_name.get(name)
            if container:
                _docker(runner, "start", container)
            else:
                env = [item for value in service.env for item in ("--env", value)]
                container = _docker(
                    runner, "run", "--detach",
                    "--name", f"{jobspace.compose_project(uid)}-testenv-{name}",
                    "--network", network,
                    "--label", jobspace.label(uid),
                    "--label", f"{STAND_LABEL}={PROFILE}",
                    "--label", f"{SERVICE_LABEL}={name}",
                    "--publish", f"{HOST}::{service.port}", *env, service.image, *service.args,
                ).splitlines()[-1]
            ports[name] = _port(container, service, runner)
            _ready(name, ports[name])
        resources = _resources(store, job_id)
        for resource in resources:
            if resource["kind"] != "port":
                continue
            if resource["ref"] != str(ports.get(resource["meta"]["name"])):
                store.mark_job_resource_removed(resource["id"])
        for name, port in ports.items():
            store.add_job_resource(job_id, "port", str(port), {"name": name, "testenv": PROFILE})
    except (Exception, KeyboardInterrupt) as exc:
        report = jobspace.CleanupReport()
        if existing:
            try:
                created = [item for item in _containers(uid, runner) if item not in existing]
                _remove(created, runner, report)
            except jobspace.JobSpaceError as cleanup_error:
                report.errors.append(str(cleanup_error))
        else:
            cleanup_locked(store, job_id, uid, raw_runner, report)
        if report.errors:
            detail = "; ".join(report.errors)
            raise jobspace.JobSpaceError(f"{exc}. Повторите down для уборки: {detail}") from exc
        raise
    return {"profile": PROFILE, "host": HOST, "ports": ports}
