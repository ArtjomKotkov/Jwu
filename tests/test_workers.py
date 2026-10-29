"""Воркеры оркестратора (core.workers): запуск, учёт, отчёты, остановка, строка статуса, уборка."""

import signal

import pytest

from jwu.core import statusline, sweep, workers
from jwu.core.store import Store


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(workers, "logs_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(workers, "dated", lambda name, today=None: " ".join(name.split()))   # дата — в test_dated_name
    store = Store(tmp_path / "s.db")
    ws = store.get_workspace_by_slug("work")
    store.use_workspace(ws.id)
    spawned = []

    def spawn(cmd, cwd, env_):
        spawned.append({"cmd": cmd, "cwd": cwd, "env": env_})
        return 4242

    yield store, ws, spawned, spawn
    store.close()


def test_start_records_worker_and_spawns_clean_session(env, tmp_path, monkeypatch):
    store, ws, spawned, spawn = env
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    monkeypatch.setenv("CLAUDECODE", "1")
    workers.set_orchestrator(store, "sid-1", "Оркестратор")
    job = store.create_job("PROJ-1", "выгрузка")
    info = workers.start(store, "  PROJ-1 ·  выгрузка ", "1. PROJ-1 — jwu-start-job", cwd=str(tmp_path),
                         jobs=[job.id], workspace=ws.slug, spawn=spawn, claude="/bin/claude")
    name = workers.dated("PROJ-1 · выгрузка")
    assert info["name"] == name and info["pgid"] == 4242 and info["jobs"] == [job.id]
    assert info["orchestrator"] == "sid-1" and info["state"] == "starting"
    call = spawned[0]
    assert call["cwd"] == str(tmp_path)
    # метка дочерней сессии и id родителя не наследуются, имя на доске — имя воркера
    assert not any(k.startswith("CLAUDE_CODE_") or k == "CLAUDECODE" for k in call["env"])
    assert call["env"]["JWU_SESSION_NAME"] == name
    shell = call["cmd"][-1]
    assert call["cmd"][:2] == ["sh", "-c"] and shell.startswith("tail -f /dev/null | script -q")
    assert "--remote-control" in shell and f"'{name}'" in shell
    assert f"Ты — воркер jwu «{name}»" in shell and "«Оркестратор»" in shell
    assert "1. PROJ-1 — jwu-start-job" in shell
    [w] = workers.list_workers(store, alive=lambda pgid: True)
    assert w["name"] == name and w["state"] == "starting"


def test_start_validates_name_task_and_folder(env, tmp_path):
    store, ws, spawned, spawn = env
    with pytest.raises(workers.WorkerError, match="имя"):
        workers.start(store, " ", "задание", cwd=str(tmp_path), spawn=spawn, claude="c")
    with pytest.raises(workers.WorkerError, match="длиннее"):
        workers.start(store, "x" * 61, "задание", cwd=str(tmp_path), spawn=spawn, claude="c")
    with pytest.raises(workers.WorkerError, match="Пустое"):
        workers.start(store, "w", "  ", cwd=str(tmp_path), spawn=spawn, claude="c")
    with pytest.raises(workers.WorkerError, match="Папки"):
        workers.start(store, "w", "задание", cwd=str(tmp_path / "nope"), spawn=spawn, claude="c")
    assert spawned == []


def test_live_name_is_taken(env, tmp_path, monkeypatch):
    store, ws, spawned, spawn = env
    workers.start(store, "w", "задание", cwd=str(tmp_path), spawn=spawn, claude="c")
    monkeypatch.setattr(workers, "_alive", lambda pgid: True)
    with pytest.raises(workers.WorkerError, match="уже работает"):
        workers.start(store, "w", "ещё", cwd=str(tmp_path), spawn=spawn, claude="c")
    monkeypatch.setattr(workers, "_alive", lambda pgid: False)   # упал — имя можно занять снова
    workers.start(store, "w", "ещё", cwd=str(tmp_path), spawn=spawn, claude="c")


def test_report_and_derived_states(env, tmp_path):
    store, ws, spawned, spawn = env
    for name in ("a", "b", "c", "d"):
        workers.start(store, name, "задание", cwd=str(tmp_path), spawn=spawn, claude="c")
    workers.report(store, "a", "working", "фазы идут\nвторая строка", jobs=[7])
    workers.report(store, "b", "waiting", "согласовать план")
    workers.report(store, "c", "done", "PR создан")
    with pytest.raises(workers.WorkerError, match="не бывает"):
        workers.report(store, "a", "sleeping")
    with pytest.raises(workers.WorkerError, match="нет"):
        workers.report(store, "zzz", "working")
    alive = {"a": True, "b": True, "c": False, "d": False}
    by_pg = {}
    for w in workers.list_workers(store, alive=lambda pg: True):
        by_pg[w["name"]] = w
    items = workers.list_workers(store, alive=lambda pg: True)
    assert by_pg["a"]["note"] == "фазы идут" and by_pg["a"]["jobs"] == [7]
    # «d» не отчитывался и процесса нет → упал; «c» готов — процесс и не нужен
    raw = {w["name"]: w for w in items}
    states = {n: workers._view({**raw[n], "state": raw[n]["state"]}, lambda pg, n=n: alive[n])["state"]
              for n in raw}
    assert states == {"a": "working", "b": "waiting", "c": "done", "d": "dead"}
    assert [w["name"] for w in items][0] == "b"           # ждущий пользователя — первым
    assert workers.summary(items) == {"working": 1, "waiting": 1, "done": 1, "starting": 1}


def test_silent_when_no_report_for_long(env, tmp_path):
    store, ws, spawned, spawn = env
    info = workers.start(store, "w", "задание", cwd=str(tmp_path), spawn=spawn, claude="c")
    info["state"], info["updated"] = "working", "2020-01-01T00:00:00+00:00"
    workers._write(store, info)
    assert workers.get(store, "w", alive=lambda pg: True)["state"] == "silent"


def test_stop_refuses_open_jobs_then_force_pauses(env, tmp_path):
    store, ws, spawned, spawn = env
    job = store.create_job("PROJ-1", "выгрузка")
    workers.start(store, "w", "задание", cwd=str(tmp_path), jobs=[job.id], workspace=ws.slug,
                  spawn=spawn, claude="c")
    killed = []
    state = {"alive": True}

    def kill(pgid, sig):
        killed.append((pgid, sig))
        state["alive"] = False

    with pytest.raises(workers.WorkerError, match="открыты работы #"):
        workers.stop(store, "w", kill=kill, alive=lambda pg: state["alive"], sleep=lambda s: None)
    assert killed == []
    res = workers.stop(store, "w", force=True, kill=kill, alive=lambda pg: state["alive"], sleep=lambda s: None)
    assert killed == [(4242, signal.SIGTERM)]
    assert res["paused_jobs"] == [job.id] and store.get_job(job.id).status == "paused"
    assert workers.list_workers(store) == []


def test_stop_done_worker_without_force(env, tmp_path):
    store, ws, spawned, spawn = env
    job = store.create_job("PROJ-1", "выгрузка")
    workers.start(store, "w", "задание", cwd=str(tmp_path), jobs=[job.id], workspace=ws.slug,
                  spawn=spawn, claude="c")
    store.set_job_status(job.id, "done")
    res = workers.stop(store, "w", kill=lambda pg, sig: None, alive=lambda pg: False, sleep=lambda s: None)
    assert res == {"name": "w", "stopped": True, "paused_jobs": []}


def test_session_url_from_log_with_escapes(tmp_path):
    log = tmp_path / "w.log"
    log.write_text("\x1b[2m·\x1b[0m Connecting\n\x1b]8;;https://claude.ai/code/session_011abc?from=cli\x07link\x1b]8;;\x07\n"
                   "Continue at https://claude.ai/code/session_022Def\n")
    assert workers.session_url(str(log)) == "https://claude.ai/code/session_022Def"
    assert workers.session_url(str(tmp_path / "none.log")) == ""


def test_statusline_shows_workers_only_in_orchestrator(env, tmp_path, monkeypatch):
    store, ws, spawned, spawn = env
    monkeypatch.setattr(workers, "_alive", lambda pgid: True)
    workers.set_orchestrator(store, "sid-1", "Оркестратор")
    assert statusline.workers_lines(store, "sid-1")[0].endswith("нет — «возьми … воркеру»\x1b[0m")
    workers.start(store, "PROJ-1 · выгрузка", "задание", cwd=str(tmp_path), spawn=spawn, claude="c")
    workers.start(store, "Сборки · PR 12–14", "задание", cwd=str(tmp_path), spawn=spawn, claude="c")
    workers.report(store, "PROJ-1 · выгрузка", "waiting", "согласовать план")
    workers.report(store, "Сборки · PR 12–14", "working", "сборка 2/3")
    assert statusline.workers_lines(store, "other-session") == []
    assert statusline.workers_lines(store, "") == []
    lines = statusline.workers_lines(store, "sid-1")
    plain = [workers._ANSI.sub("", ln) for ln in lines]
    assert plain[0].startswith("воркеры  2") and "⏳ 1 ждут тебя" in plain[0] and "⚙ 1 работают" in plain[0]
    assert plain[1].startswith("  ⏳ PROJ-1 · выгрузка") and "ждёт: согласовать план" in plain[1]
    assert plain[2].startswith("  ⚙ Сборки · PR 12–14") and "сборка 2/3" in plain[2]
    text = statusline.render({"session_id": "sid-1", "cwd": str(tmp_path)}, store, with_claude=False)
    assert "PROJ-1 · выгрузка" in text


def test_statusline_install_sets_refresh_interval(tmp_path):
    store = Store(tmp_path / "s.db")
    path = tmp_path / "settings.json"
    statusline.install(store, path, command="jwu statusline")
    import json

    assert json.loads(path.read_text())["statusLine"]["refreshInterval"] == statusline.REFRESH_SECONDS
    store.close()


def test_sweep_finds_and_stops_dead_workers(env, tmp_path, monkeypatch):
    store, ws, spawned, spawn = env
    job = store.create_job("PROJ-1", "выгрузка")
    workers.start(store, "w", "задание", cwd=str(tmp_path), jobs=[job.id], workspace=ws.slug,
                  spawn=spawn, claude="c")
    monkeypatch.setattr(workers, "_alive", lambda pgid: False)
    monkeypatch.setattr(sweep.jobspace, "jobs_dir", lambda store, wid: None)
    runner = lambda cmd: (1, "")  # noqa: E731 — docker нет
    plan = sweep.scan(store, runner=runner)
    assert plan["dead_workers"] == [{"session": "w", "jobs": [job.id], "seen_ago": plan["dead_workers"][0]["seen_ago"]}]
    res = sweep.apply(store, ["dead_workers"], runner=runner, plan=plan)
    assert res["done"]["dead_workers"] == [{"session": "w", "released_jobs": [job.id]}]
    assert store.get_job(job.id).status == "paused" and workers.list_workers(store) == []


def test_tree_finds_children_of_root(monkeypatch):
    class R:
        stdout = "  10     1\n  11    10\n  12    11\n  13     1\n"

    monkeypatch.setattr(workers.subprocess, "run", lambda *a, **k: R())
    assert sorted(workers._tree(10)) == [10, 11, 12]


def test_clear_orchestrator(env):
    store = env[0]
    workers.set_orchestrator(store, "sid-1", "Оркестратор")
    assert workers.clear_orchestrator(store)["name"] == "Оркестратор"
    assert workers.orchestrator(store) == {} and statusline.workers_lines(store, "sid-1") == []


def test_dated_name():
    from datetime import datetime

    day = datetime(2026, 10, 29)
    assert workers.dated("Ревью · PR 12", day) == "29.10 · Ревью · PR 12"
    assert workers.dated("29.10 · Ревью · PR 12", day) == "29.10 · Ревью · PR 12"   # второй раз не добавляет
    assert workers.dated("  ", day) == ""
