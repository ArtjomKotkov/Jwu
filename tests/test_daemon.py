"""Демон фонового синка: проход по контурам, лок одного экземпляра, файлы служб."""

import json

from typer.testing import CliRunner

from jwu.cli import main as cli
from jwu.core import daemon, workspaces
from jwu.core.models import Delta
from jwu.core.service import SyncResult
from jwu.core.store import Store

runner = CliRunner()


class _FakeService:
    """Сервис-заглушка: sync() отдаёт заданный результат либо бросает."""

    def __init__(self, ws, *, deltas=0, fail=None):
        self.workspace = ws
        self._deltas = deltas
        self._fail = fail
        self.closed = False

    def sync(self):
        if self._fail is not None:
            raise self._fail
        return SyncResult(run_id=1, counts={}, deltas=[Delta(key="K", kind="x")] * self._deltas)

    def close(self):
        self.closed = True


def _registry(tmp_path):
    """Свежая БД уже содержит дефолтный контур «work» (jira + bitbucket)."""
    store = Store(tmp_path / "state.db")
    workspaces.create(store, "gh", provider="github")
    workspaces.create(store, "home", provider="local")
    return store


def test_run_pass_syncs_external_workspaces_and_survives_errors(tmp_path):
    store = _registry(tmp_path)
    made = []
    seen = []

    def factory(ws):
        svc = _FakeService(ws, deltas=2 if ws.slug == "work" else 0,
                           fail=RuntimeError("сеть") if ws.slug == "gh" else None)
        made.append(svc)
        return svc

    report = daemon.run_pass(store, factory=factory, after_sync=lambda svc, res: seen.append(svc.workspace.slug))
    assert report.synced == ["work"]
    assert list(report.failed) == ["gh"] and "сеть" in report.failed["gh"]
    assert report.skipped == ["home"]
    assert report.deltas == 2
    assert seen == ["work"]                      # хук только после успешного синка
    assert all(s.closed for s in made)           # соединения закрыты даже при ошибке
    assert store.get_meta(daemon.LAST_PASS_META)
    assert "1 ок" in store.get_meta(daemon.LAST_PASS_SUMMARY_META)
    store.close()


def test_run_pass_hook_error_does_not_fail_sync(tmp_path):
    store = _registry(tmp_path)

    def boom(svc, res):
        raise ValueError("телеграм лежит")

    report = daemon.run_pass(store, factory=lambda ws: _FakeService(ws), after_sync=boom)
    assert report.synced == ["work", "gh"] and not report.failed
    store.close()


def test_single_instance_lock(tmp_path):
    lock = tmp_path / "d.lock"
    first = daemon.SingleInstance(lock)
    assert first.acquire()
    second = daemon.SingleInstance(lock)
    assert not second.acquire()
    assert second.holder_pid() == first.holder_pid()
    first.release()
    assert second.acquire()
    assert first.holder_pid() is not None  # теперь держит второй
    second.release()
    assert daemon.SingleInstance(lock).holder_pid() is None  # файл остался, лок свободен


def test_run_loop_once_runs_single_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "lock_path", lambda: tmp_path / "d.lock")
    calls = []
    slept = []
    db = tmp_path / "state.db"
    Store(db).close()
    daemon.run_loop(lambda: Store(db), once=True,
                    factory=lambda ws: calls.append(ws.slug) or _FakeService(ws),
                    sleep=slept.append)
    assert calls == ["work"] and slept == []  # один проход по дефолтному контуру, без паузы


def test_render_plist_and_unit_contain_command():
    from pathlib import Path

    tmp_log = Path("/tmp/x.log")
    plist = daemon.render_plist("/usr/local/bin/jwu", 300, log_file=tmp_log,
                                env={"JWU_DB_PATH": "/db"})
    assert "<string>/usr/local/bin/jwu</string>" in plist
    assert "<string>--interval</string>\n        <string>300</string>" in plist
    assert "<key>JWU_DB_PATH</key>" in plist and "<string>/db</string>" in plist
    assert f"<string>{tmp_log}</string>" in plist
    unit = daemon.render_systemd_unit("/usr/bin/jwu", 120, log_file=tmp_log)
    assert "ExecStart=/usr/bin/jwu daemon run --interval 120" in unit
    assert "Restart=always" in unit


def test_install_writes_service_file_and_calls_launchctl(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "is_macos", lambda: True)
    monkeypatch.setattr(daemon, "plist_path", lambda: tmp_path / "agents" / "x.plist")
    monkeypatch.setattr(daemon, "log_path", lambda: tmp_path / "daemon.log")
    monkeypatch.delenv("JWU_DB_PATH", raising=False)
    cmds = []

    def runner_ok(cmd):
        cmds.append(cmd)
        return 0, ""

    msgs = daemon.install(90, jwu_bin="/opt/jwu", runner=runner_ok)
    assert (tmp_path / "agents" / "x.plist").exists()
    assert cmds[-1][:2] == ["launchctl", "bootstrap"]
    assert any("90с" in m for m in msgs)
    # интервал ниже минимума поднимается до минимума
    daemon.install(5, jwu_bin="/opt/jwu", runner=runner_ok)
    assert f"<string>{daemon.MIN_INTERVAL}</string>" in (tmp_path / "agents" / "x.plist").read_text()
    # снятие удаляет файл
    daemon.uninstall(runner=runner_ok)
    assert not (tmp_path / "agents" / "x.plist").exists()


def test_status_reports_last_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "is_macos", lambda: True)
    monkeypatch.setattr(daemon, "plist_path", lambda: tmp_path / "none.plist")
    monkeypatch.setattr(daemon, "lock_path", lambda: tmp_path / "d.lock")
    store = Store(tmp_path / "state.db")
    store.set_meta(daemon.LAST_PASS_META, "2026-09-11T10:00:00+00:00")
    info = daemon.status(store, runner=lambda cmd: (1, "not loaded"))
    assert info["installed"] is False and info["service_loaded"] is False
    assert info["running"] is False and info["last_pass"].startswith("2026-09-11")
    store.close()


def test_cli_daemon_status_json(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "is_macos", lambda: True)
    monkeypatch.setattr(daemon, "plist_path", lambda: tmp_path / "none.plist")
    monkeypatch.setattr(daemon, "lock_path", lambda: tmp_path / "d.lock")
    monkeypatch.setattr(daemon, "_run", lambda cmd: (1, ""))
    monkeypatch.setattr(cli, "_open_store", lambda: Store(tmp_path / "state.db"))
    res = runner.invoke(cli.app, ["daemon", "status", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["installed"] is False and payload["running"] is False
