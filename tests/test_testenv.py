import json

import pytest

from jwu.core import jobspace, testenv
from jwu.core.store import Store


class Docker:
    def __init__(self):
        self.containers = {}
        self.networks = {}
        self.daemon = "docker-a"
        self.next_number = 1
        self.fail_service = None

    def __call__(self, command):
        _, action, *args = command
        if action == "network":
            subcommand, *args = args
            if subcommand == "create":
                network = f"{len(self.networks) + 1:012x}" + "1" * 52
                self.networks[network] = {
                    args[index + 1] for index, arg in enumerate(args) if arg == "--label"
                }
                return 0, network
            if subcommand == "rm":
                del self.networks[args[-1]]
                return 0, ""
            filters = [args[index + 1][6:] for index, arg in enumerate(args) if arg == "--filter"]
            return 0, "\n".join(network for network, labels in self.networks.items()
                                if all(label in labels for label in filters))
        if action == "ps":
            filters = [args[index + 1][6:] for index, arg in enumerate(args) if arg == "--filter"]
            rows = [
                (container, next(label.split("=", 1)[1] for label in labels
                                 if label.startswith(f"{testenv.SERVICE_LABEL}=")))
                for container, (labels, _, _) in self.containers.items()
                if all(label in labels for label in filters)
            ]
            if "--format" in args:
                return 0, "\n".join(f"{container} {service}" for container, service in rows)
            return 0, "\n".join(container for container, _ in rows)
        if action == "run":
            assert args[args.index("--network") + 1] in self.networks
            labels = {args[index + 1] for index, arg in enumerate(args) if arg == "--label"}
            service = next(label.split("=", 1)[1] for label in labels
                           if label.startswith(f"{testenv.SERVICE_LABEL}="))
            _, _, port = args[args.index("--publish") + 1].split(":")
            container = f"{self.next_number:012x}" + "0" * 52
            self.containers[container] = (labels, port, str(40000 + self.next_number))
            self.next_number += 1
            if service == self.fail_service:
                return 1, "ошибка после создания контейнера"
            return 0, container
        if action == "inspect":
            _, port, host_port = self.containers[args[-1]]
            return 0, json.dumps({f"{port}/tcp": [{"HostIp": testenv.HOST, "HostPort": host_port}]})
        if action == "rm":
            assert "-fv" in args
            del self.containers[args[-1]]
        return 0, "PONG" if action == "exec" else ""


def test_retry_removes_partial_start_and_keeps_other_jobs(tmp_path, monkeypatch):
    docker = Docker()
    monkeypatch.setattr(jobspace, "require_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(testenv, "_local_runner", lambda runner: (runner, docker.daemon))
    monkeypatch.setattr(testenv, "_ready", lambda *args: None)
    with Store(tmp_path / "state.db") as store:
        first, second = [store.create_job("", title).id for title in ("one", "two")]
        other = testenv.manage(store, second, "up", runner=docker)
        docker.fail_service = "redis"
        with pytest.raises(jobspace.JobSpaceError, match="после создания"):
            testenv.manage(store, first, "up", runner=docker)
        assert store.job_resources(first) == []
        assert len(docker.containers) == 2
        assert len(docker.networks) == 1

        docker.fail_service = None
        started = testenv.manage(store, first, "up", runner=docker)
        assert started == testenv.manage(store, first, "up", runner=docker)
        assert set(started["ports"].values()).isdisjoint(other["ports"].values())
        assert len(store.job_resources(first)) == 3

        dry_run = jobspace.cleanup(store, first, dry_run=True, runner=docker)
        assert len(dry_run.removed) == 6
        assert len(docker.containers) == 4
        assert len(docker.networks) == 2
        assert not jobspace.cleanup(store, first, runner=docker).errors
        assert store.job_resources(first) == []
        assert len(docker.containers) == 2
        assert len(docker.networks) == 1
        assert testenv.manage(store, second, "up", runner=docker) == other


def test_failed_optional_service_preserves_running_pair(tmp_path, monkeypatch):
    docker = Docker()
    monkeypatch.setattr(jobspace, "require_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(testenv, "_local_runner", lambda runner: (runner, docker.daemon))
    monkeypatch.setattr(testenv, "_ready", lambda *args: None)
    with Store(tmp_path / "state.db") as store:
        job_id = store.create_job("", "test").id
        started = testenv.manage(store, job_id, "up", runner=docker)
        docker.fail_service = "s3"
        with pytest.raises(jobspace.JobSpaceError, match="после создания"):
            testenv.manage(store, job_id, "up", minio=True, runner=docker)
        assert len(docker.containers) == 2
        assert testenv.manage(store, job_id, "up", runner=docker) == started
        testenv.manage(store, job_id, "down", runner=docker)
        assert testenv.manage(store, job_id, "down", runner=docker)["removed"] == []
        assert not store.job_resources(job_id)


def test_different_daemon_cannot_clear_stand_resources(tmp_path, monkeypatch):
    docker = Docker()
    monkeypatch.setattr(jobspace, "require_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(testenv, "_local_runner", lambda runner: (runner, docker.daemon))
    monkeypatch.setattr(testenv, "_ready", lambda *args: None)
    with Store(tmp_path / "state.db") as store:
        job_id = store.create_job("", "test").id
        testenv.manage(store, job_id, "up", runner=docker)
        original = store.job_resources(job_id)
        docker.daemon = "docker-b"
        with pytest.raises(jobspace.JobSpaceError, match="другой Docker-демон"):
            testenv.manage(store, job_id, "down", runner=docker)
        assert jobspace.cleanup(store, job_id, runner=docker).errors
        assert store.job_resources(job_id) == original
        assert len(docker.containers) == 2
        docker.daemon = "docker-a"
        testenv.manage(store, job_id, "down", runner=docker)
        assert not store.job_resources(job_id)
        assert not docker.containers
        assert not docker.networks
