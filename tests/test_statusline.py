"""Строка статуса Claude Code: рендер по JSON, цепочка с прежней командой, установка."""

import json

import pytest
from typer.testing import CliRunner

from jwu.cli import main as cli
from jwu.core import statusline as sl
from jwu.core import workspaces
from jwu.core.store import Store

runner = CliRunner()


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


def _strip(text: str) -> str:
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_parse_stdin_tolerates_garbage():
    assert sl.parse_stdin("") == {}
    assert sl.parse_stdin("not json") == {}
    assert sl.parse_stdin("[1, 2]") == {}
    assert sl.parse_stdin('{"cwd": "/x"}') == {"cwd": "/x"}


def test_session_cwd_prefers_json_over_process(tmp_path):
    assert sl.session_cwd({"cwd": "/a"}) == "/a"
    assert sl.session_cwd({"workspace": {"current_dir": "/b"}}) == "/b"
    assert sl.session_cwd({})  # cwd процесса — хоть что-то


def test_claude_parts_pick_model_context_cost():
    parts = _strip(" ".join(sl.claude_parts({
        "model": {"display_name": "Fable 5.1"},
        "context_window": {"used_percentage": 42.6},
        "cost": {"total_cost_usd": 1.234, "total_duration_ms": 65 * 60000},
    })))
    assert parts == "Fable 5.1 ctx 43% $1.23 1ч05"
    assert sl.claude_parts({}) == []
    assert sl.claude_parts({"cost": {"total_cost_usd": 0}}) == []


def test_render_unbound_folder(store, tmp_path):
    line = _strip(sl.render({"cwd": str(tmp_path / "nowhere")}, store, with_claude=False))
    assert line == "jwu      папка не привязана"


def test_render_workspace_job_queue_daemon(store, tmp_path, monkeypatch):
    folder = tmp_path / "pet"
    folder.mkdir()
    ws = workspaces.create(store, "pet", name="Питомец")
    workspaces.add_path(store, ws, folder)
    store.use_workspace(ws.id)
    feature = store.create_feature("Тёмная тема")
    job = store.create_job("", "тема", feature_id=feature.id)
    store.add_job_record(job.id, "Ф1: правка core/statusline.py", kind="phase", status="done")
    store.queue_add("поправить импорт")
    store.queue_add("обновить доку", mode="parallel")
    store.queue_take(mode="parallel", worker="agent")
    monkeypatch.setattr(sl.SingleInstance, "holder_pid", lambda self: 4242)

    lines = _strip(sl.render({"cwd": str(folder), "model": {"display_name": "M"}}, store, with_claude=True)).splitlines()
    assert lines[0] == "M"
    assert lines[1] == "jwu      Питомец (local)   ·   демон ✓"
    assert lines[2] == f"работа   #{job.id} {feature.key} ✓  Тёмная тема"
    assert "statusline.py" not in lines[2]  # текст фазы в строку не попадает
    assert lines[3] == "очередь  ждёт 1  ·  идёт 1  ·  слоты 1/3"

    store.queue_take(mode="main")
    lines = _strip(sl.render({"cwd": str(folder)}, store, with_claude=False)).splitlines()
    assert lines[2] == "очередь  ждёт 0  ·  идёт 2  ·  слоты 1/3   ▶ поправить импорт"
    monkeypatch.setattr(sl.SingleInstance, "holder_pid", lambda self: None)
    assert _strip(sl.render({"cwd": str(folder)}, store, with_claude=False)).splitlines()[0].endswith("демон ✗")


def test_render_truncates_long_title_and_marks_pending_phase(store, tmp_path, monkeypatch):
    folder = tmp_path / "pet"
    folder.mkdir()
    ws = workspaces.create(store, "pet")
    workspaces.add_path(store, ws, folder)
    store.use_workspace(ws.id)
    job = store.create_job("", "Очень длинное название работы, которое точно не влезет в строку")
    store.add_job_record(job.id, "Ф1", kind="phase", status="pending")
    monkeypatch.setattr(sl.SingleInstance, "holder_pid", lambda self: None)
    line = _strip(sl.render({"cwd": str(folder)}, store, with_claude=False)).splitlines()[1]
    assert line == f"работа   #{job.id} #{job.id} …  Очень длинное название работы, которое…"


def test_render_without_job_or_queue(store, tmp_path, monkeypatch):
    folder = tmp_path / "pet"
    folder.mkdir()
    ws = workspaces.create(store, "pet")
    workspaces.add_path(store, ws, folder)
    monkeypatch.setattr(sl.SingleInstance, "holder_pid", lambda self: None)
    lines = _strip(sl.render({"cwd": str(folder)}, store, with_claude=False)).splitlines()
    assert lines[1] == "работа   нет активной" and lines[2] == "очередь  пусто"


def test_run_previous_passes_stdin_and_survives_failures():
    assert sl.run_previous("cat", '{"a":1}') == '{"a":1}'
    assert sl.run_previous("echo $FORCE_COLOR", "") == "1"  # цвета в подпроцессе не гасятся
    assert sl.run_previous("", "x") == ""
    assert sl.run_previous("exit 3", "x") == ""
    assert sl.run_previous("definitely-not-a-command-xyz", "x") == ""


def test_install_keeps_previous_command_and_uninstall_restores(store, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"theme": "dark", "statusLine": {"type": "command", "command": "node hud.js"}}))
    note = sl.install(store, path, command="jwu statusline")
    assert "прежняя" in note
    data = json.loads(path.read_text())
    assert data["statusLine"] == {"type": "command", "command": "jwu statusline"}
    assert data["theme"] == "dark"
    assert sl.previous_command(store) == "node hud.js"
    assert sl.is_installed(path)
    # повторная установка обновляет команду, но не затирает запомненную нашей же
    sl.install(store, path)
    assert sl.previous_command(store) == "node hud.js"
    assert sl.is_ours(json.loads(path.read_text())["statusLine"]["command"])
    assert sl.our_command().endswith("jwu statusline")

    assert "прежней" in sl.uninstall(store, path)
    assert json.loads(path.read_text())["statusLine"]["command"] == "node hud.js"
    assert sl.previous_command(store) == ""


def test_install_without_chain_and_uninstall_removes_key(store, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"statusLine": {"type": "command", "command": "node hud.js"}}))
    sl.install(store, path, chain=False)
    assert sl.previous_command(store) == ""
    assert "убрана" in sl.uninstall(store, path)
    assert "statusLine" not in json.loads(path.read_text())


def test_install_on_missing_settings_file(store, tmp_path):
    path = tmp_path / "nested" / "settings.json"
    sl.install(store, path, command="jwu statusline")
    assert json.loads(path.read_text())["statusLine"]["command"] == "jwu statusline"
    with pytest.raises(ValueError):
        path.write_text("{broken")
        sl.current_command(path)


def _patch_cli(monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    monkeypatch.setattr(cli, "_open_store", lambda: Store(db))
    monkeypatch.setattr(cli, "_store", lambda: Store(db))
    monkeypatch.setattr(cli, "_WORKSPACE_ARG", None)
    return db


def test_statusline_cli_chains_previous_and_never_fails(monkeypatch, tmp_path):
    db = _patch_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(sl.SingleInstance, "holder_pid", lambda self: None)
    with Store(db) as s:
        s.set_meta(sl.PREV_META_KEY, "echo HUD")
    res = runner.invoke(cli.app, ["statusline"], input=json.dumps({"cwd": str(tmp_path)}), color=True)
    assert res.exit_code == 0, res.output
    assert "\x1b[" in res.stdout  # ANSI-коды доходят до Claude Code, а не вырезаются click
    lines = _strip(res.stdout).splitlines()
    assert lines[0] == "HUD" and lines[1] == "jwu      папка не привязана"

    res = runner.invoke(cli.app, ["statusline", "--no-chain"], input="garbage")
    assert res.exit_code == 0 and _strip(res.stdout).strip() == "jwu      папка не привязана"


def test_statusline_cli_install_uninstall(monkeypatch, tmp_path):
    _patch_cli(monkeypatch, tmp_path)
    path = tmp_path / "claude" / "settings.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(path.parent))
    path.parent.mkdir()
    path.write_text(json.dumps({"statusLine": {"type": "command", "command": "node hud.js"}}))

    res = runner.invoke(cli.app, ["statusline", "install", "--yes"])
    assert res.exit_code == 0, res.output
    assert sl.is_ours(json.loads(path.read_text())["statusLine"]["command"])
    assert runner.invoke(cli.app, ["statusline", "install"]).exit_code == 0  # повтор без вопросов

    res = runner.invoke(cli.app, ["statusline", "uninstall"])
    assert res.exit_code == 0, res.output
    assert json.loads(path.read_text())["statusLine"]["command"] == "node hud.js"
    assert "нечего убирать" in runner.invoke(cli.app, ["statusline", "uninstall"]).output
