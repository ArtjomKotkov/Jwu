"""Очередь задач сессии: порядок, режимы, слоты, CLI и MCP."""

import asyncio
import json

import pytest
from typer.testing import CliRunner

from jwu import mcp_server as srv
from jwu.cli import main as cli
from jwu.core import workspaces
from jwu.core.store import QueueBusy, Store

runner = CliRunner()


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


def test_items_are_appended_in_order(store):
    a = store.queue_add("первое")
    b = store.queue_add("второе", mode="parallel")
    assert [i.id for i in store.queue_list()] == [a.id, b.id]
    assert [i.position for i in store.queue_list()] == [1, 2]
    assert b.mode == "parallel" and b.status == "queued"


def test_add_rejects_empty_text_and_bad_mode(store):
    with pytest.raises(ValueError):
        store.queue_add("   ")
    with pytest.raises(ValueError):
        store.queue_add("x", mode="async")


def test_main_thread_takes_one_item_at_a_time(store):
    a = store.queue_add("первое")
    store.queue_add("второе")
    taken = store.queue_take(mode="main")
    assert taken.id == a.id and taken.status == "running" and taken.worker == "main"
    with pytest.raises(QueueBusy):
        store.queue_take(mode="main")
    store.queue_finish(a.id, "done", result="сделано")
    assert store.queue_take(mode="main").text == "второе"


def test_parallel_slots_limit_and_setting(store):
    for n in range(4):
        store.queue_add(f"p{n}", mode="parallel")
    assert store.queue_slots() == 3
    taken = [store.queue_take(mode="parallel", worker=f"a{n}") for n in range(3)]
    assert [t.worker for t in taken] == ["a0", "a1", "a2"]
    with pytest.raises(QueueBusy):
        store.queue_take(mode="parallel")
    summary = store.queue_counts()
    assert summary["slots_busy"] == 3 and summary["slots_free"] == 0
    store.set_queue_slots(5)
    assert store.queue_slots() == 5
    assert store.queue_take(mode="parallel").text == "p3"
    with pytest.raises(ValueError):
        store.set_queue_slots(0)


def test_take_without_mode_respects_item_mode(store):
    store.queue_add("главный")
    store.queue_add("параллельный", mode="parallel")
    first = store.queue_take()
    assert first.text == "главный"
    # основной поток занят, но следующий по порядку — параллельный, и слот есть
    second = store.queue_take()
    assert second.text == "параллельный" and second.worker == "parallel"
    assert store.queue_take() is None


def test_release_returns_item_to_queue(store):
    a = store.queue_add("x")
    store.queue_take(mode="main", worker="main")
    released = store.queue_release(a.id)
    assert released.status == "queued" and released.worker == "" and released.started_at == ""
    with pytest.raises(ValueError):
        store.queue_release(a.id)


def test_finish_requires_open_item_and_valid_status(store):
    a = store.queue_add("x")
    with pytest.raises(ValueError):
        store.queue_finish(a.id, "running")
    done = store.queue_finish(a.id, "failed", result="не вышло")
    assert done.status == "failed" and done.result == "не вышло" and done.finished_at
    with pytest.raises(ValueError):
        store.queue_finish(a.id, "done")
    with pytest.raises(ValueError):
        store.queue_finish(999, "done")


def test_move_reorders_waiting_items_only(store):
    a = store.queue_add("a")
    b = store.queue_add("b")
    c = store.queue_add("c")
    store.queue_take(mode="main")  # a выполняется
    assert [i.id for i in store.queue_move(c.id, 1)] == [c.id, b.id]
    assert [i.id for i in store.queue_move(c.id, 99)] == [b.id, c.id]
    with pytest.raises(ValueError):
        store.queue_move(a.id, 1)
    # новые пункты по-прежнему встают в конец
    d = store.queue_add("d")
    assert [i.id for i in store.queue_list(status="queued")] == [b.id, c.id, d.id]


def test_update_text_mode_and_job(store):
    job = store.create_job("", "работа")
    a = store.queue_add("x")
    item = store.queue_update(a.id, text="y", mode="parallel", job_id=job.id)
    assert (item.text, item.mode, item.job_id) == ("y", "parallel", job.id)
    store.queue_take(mode="parallel")
    with pytest.raises(ValueError):
        store.queue_update(a.id, mode="main")
    assert store.queue_update(a.id, mode="parallel").mode == "parallel"
    with pytest.raises(ValueError):
        store.queue_add("z", job_id=12345)


def test_clear_removes_closed_by_default(store):
    a = store.queue_add("a")
    store.queue_add("b")
    store.queue_take(mode="main")
    store.queue_finish(a.id, "done")
    assert store.queue_clear() == 1
    assert [i.text for i in store.queue_list()] == ["b"]
    assert store.queue_clear(statuses=("queued",)) == 1
    with pytest.raises(ValueError):
        store.queue_clear(statuses=("nope",))


def test_queue_is_isolated_between_workspaces(store):
    home = workspaces.create(store, "home", name="Дом")
    store.queue_add("рабочее")
    store.use_workspace(home.id)
    assert store.queue_list() == []
    store.queue_add("домашнее")
    assert [i.text for i in store.queue_list()] == ["домашнее"]
    assert store.queue_counts()["counts"]["queued"] == 1


def test_schema_version_covers_queue(store):
    assert int(store.get_meta("schema_version")) >= 11


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _patch_cli(monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    monkeypatch.setattr(cli, "_open_store", lambda: Store(db))
    monkeypatch.setattr(cli, "_store", lambda: Store(db))
    monkeypatch.setattr(cli, "_WORKSPACE_ARG", None)


def test_queue_cli_roundtrip(monkeypatch, tmp_path):
    _patch_cli(monkeypatch, tmp_path)

    res = runner.invoke(cli.app, ["queue", "add", "починить тест", "--json"])
    assert res.exit_code == 0, res.output
    first = json.loads(res.stdout)["id"]
    res = runner.invoke(cli.app, ["queue", "add", "обновить доку", "--mode", "parallel", "--json"])
    second = json.loads(res.stdout)["id"]

    res = runner.invoke(cli.app, ["queue", "list", "--json"])
    payload = json.loads(res.stdout)
    assert [i["id"] for i in payload["items"]] == [first, second]
    assert payload["slots"] == 3

    res = runner.invoke(cli.app, ["queue", "take", "--mode", "parallel", "-w", "agent-docs", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["worker"] == "agent-docs"
    assert runner.invoke(cli.app, ["queue", "take", "--mode", "parallel"]).exit_code == 2

    res = runner.invoke(cli.app, ["queue", "add", "прогнать линтер", "--json"])
    third = json.loads(res.stdout)["id"]
    assert runner.invoke(cli.app, ["queue", "take", "--mode", "main"]).exit_code == 0
    # второй main-пункт есть, но основной поток занят первым — код 3
    assert runner.invoke(cli.app, ["queue", "take", "--mode", "main"]).exit_code == 3

    assert runner.invoke(cli.app, ["queue", "done", str(second), "-r", "готово"]).exit_code == 0
    assert runner.invoke(cli.app, ["queue", "release", str(first)]).exit_code == 0
    res = runner.invoke(cli.app, ["queue", "show", str(first), "--json"])
    assert json.loads(res.stdout)["status"] == "queued"

    assert runner.invoke(cli.app, ["queue", "slots", "--set", "1"]).exit_code == 0
    res = runner.invoke(cli.app, ["queue", "list", "--json"])
    assert json.loads(res.stdout)["slots"] == 1

    res = runner.invoke(cli.app, ["queue", "clear"])
    assert "1" in res.output
    res = runner.invoke(cli.app, ["queue", "list", "--all", "--json"])
    assert [i["id"] for i in json.loads(res.stdout)["items"]] == [first, third]

    res = runner.invoke(cli.app, ["queue"])
    assert res.exit_code == 0 and "починить тест" in res.output


def test_queue_cli_move_and_missing_item(monkeypatch, tmp_path):
    _patch_cli(monkeypatch, tmp_path)
    ids = []
    for text in ("a", "b", "c"):
        res = runner.invoke(cli.app, ["queue", "add", text, "--json"])
        ids.append(json.loads(res.stdout)["id"])
    res = runner.invoke(cli.app, ["queue", "move", str(ids[2]), "--to", "1", "--json"])
    assert [i["id"] for i in json.loads(res.stdout)] == [ids[2], ids[0], ids[1]]
    assert runner.invoke(cli.app, ["queue", "show", "999"]).exit_code == 1


# --------------------------------------------------------------------------- #
# MCP
# --------------------------------------------------------------------------- #


@pytest.fixture()
def mcp_db(tmp_path, monkeypatch):
    db = tmp_path / "mcp.db"
    monkeypatch.setenv("JWU_DB_PATH", str(db))
    monkeypatch.setattr(srv, "_base_store", None)
    monkeypatch.setattr(srv, "_full", {})
    monkeypatch.setattr(srv, "_builds", {})
    monkeypatch.setattr(srv, "_stores", {})
    yield db
    for s in list(srv._stores.values()):
        s.close()
    if srv._base_store is not None:
        srv._base_store.close()


def _run(coro):
    return asyncio.run(coro)


def test_mcp_queue_flow(mcp_db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    added = _run(srv.jwu_queue_add("починить тест"))
    assert added["status"] == "queued" and "workspace" in added
    par = _run(srv.jwu_queue_add("обновить доку", mode="parallel"))
    _run(srv.jwu_queue_add("прогнать линтер"))

    taken = _run(srv.jwu_queue_take(mode="main"))
    assert taken["item"]["id"] == added["id"] and taken["busy"] is False
    busy = _run(srv.jwu_queue_take(mode="main"))
    assert busy["item"] is None and busy["busy"] is True

    listing = _run(srv.jwu_queue())
    assert listing["main_running"] == added["id"]
    assert [i["id"] for i in listing["items"]][:2] == [added["id"], par["id"]]

    finished = _run(srv.jwu_queue_finish(added["id"], "done", result="ок"))
    assert finished["status"] == "done"
    released = _run(srv.jwu_queue_take(mode="parallel", worker="agent"))
    assert released["item"]["worker"] == "agent"
    back = _run(srv.jwu_queue_finish(par["id"], "queued"))
    assert back["status"] == "queued"

    edited = _run(srv.jwu_queue_edit(par["id"], text="обновить README", position=1))
    assert edited["text"] == "обновить README"
    assert _run(srv.jwu_queue_slots(2))["slots"] == 2
    assert _run(srv.jwu_queue_clear())["removed"] == 1
    nxt = _run(srv.jwu_queue_take(mode="main"))
    assert nxt["item"]["text"] == "прогнать линтер"
    _run(srv.jwu_queue_finish(nxt["item"]["id"], "cancelled"))
    empty = _run(srv.jwu_queue_take(mode="main"))
    assert empty["item"] is None and empty["busy"] is False

    with pytest.raises(ValueError):
        _run(srv.jwu_queue_add("x", mode="weird"))
    with pytest.raises(ValueError):
        _run(srv.jwu_queue_finish(par["id"], "running"))
