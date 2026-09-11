"""Реестр локальных веток по задаче (JWU-35): чтение git, без записи."""

import asyncio
import json
import subprocess

import pytest
from typer.testing import CliRunner

from jwu import mcp_server as srv
from jwu.cli import main as cli
from jwu.core import branches as br
from jwu.core import workspaces
from jwu.core.store import Store

runner = CliRunner()


def _git_ok():
    try:
        return subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


pytestmark = pytest.mark.skipif(not _git_ok(), reason="нужен git")


def _run(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _repo(tmp_path, name="app"):
    """Клон с origin: ветки PROJ-1-fix (ahead 1), PROJ-2-feat (без апстрима), develop."""
    origin = tmp_path / f"{name}-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    root = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(origin), str(root)], check=True, capture_output=True)
    _run(root, "config", "user.email", "t@t")
    _run(root, "config", "user.name", "t")
    (root / "a.txt").write_text("1")
    _run(root, "add", "a.txt")
    _run(root, "commit", "-q", "-m", "init")
    _run(root, "branch", "-M", "develop")
    _run(root, "push", "-q", "-u", "origin", "develop")
    _run(root, "checkout", "-q", "-b", "PROJ-1-fix")
    _run(root, "push", "-q", "-u", "origin", "PROJ-1-fix")
    (root / "a.txt").write_text("2")
    _run(root, "commit", "-q", "-am", "fix")                  # ahead 1
    _run(root, "checkout", "-q", "-b", "PROJ-2-feat")          # без апстрима
    (root / "b.txt").write_text("x")                           # грязное дерево
    return root


def test_collect_finds_task_branches_with_state(tmp_path):
    root = _repo(tmp_path)
    items = br.collect({str(root): "app"})
    by = {b.branch: b for b in items}
    assert set(by) == {"PROJ-1-fix", "PROJ-2-feat"}           # develop отфильтрован
    assert by["PROJ-1-fix"].ahead == 1 and by["PROJ-1-fix"].upstream == "origin/PROJ-1-fix"
    assert by["PROJ-1-fix"].task_key == "PROJ-1" and not by["PROJ-1-fix"].current
    assert by["PROJ-2-feat"].current and by["PROJ-2-feat"].dirty == 1 and by["PROJ-2-feat"].upstream == ""
    assert "незакоммичено 1" in br.summary_line(by["PROJ-2-feat"]) and "без апстрима" in br.summary_line(by["PROJ-2-feat"])
    assert "↑1" in br.summary_line(by["PROJ-1-fix"])
    # по ключу и --all
    assert [b.branch for b in br.collect({str(root): "app"}, key="proj-1")] == ["PROJ-1-fix"]
    assert "develop" in {b.branch for b in br.collect({str(root): "app"}, all_branches=True)}


def test_worktree_detected(tmp_path):
    root = _repo(tmp_path)
    wt = tmp_path / "wt-proj1"
    _run(root, "worktree", "add", "-q", str(wt), "PROJ-1-fix")
    items = {b.branch: b for b in br.collect({str(root): "app"})}
    assert items["PROJ-1-fix"].worktree == str(wt) and items["PROJ-1-fix"].dirty == 0
    assert f"worktree {wt}" in br.summary_line(items["PROJ-1-fix"])


def test_cli_and_mcp_branches(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    db = tmp_path / "state.db"
    store = Store(db)
    ws = store.get_workspace_by_slug("work")
    workspaces.add_path(store, ws, tmp_path)   # папка с репозиторием внутри
    store.use_workspace(ws.id)
    store.add_note("PROJ-1-fix", "ждём ревью", kind="status")
    store.close()

    def scoped():
        s = Store(db)
        s.use_workspace(s.get_workspace_by_slug("work").id)
        return s

    monkeypatch.setattr(cli, "_store", scoped)
    res = runner.invoke(cli.app, ["branches", "PROJ-1", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload[0]["branch"] == "PROJ-1-fix" and payload[0]["note"] == "ждём ревью"
    res = runner.invoke(cli.app, ["branches"])
    assert "PROJ-2-feat" in res.output and "📍 ждём ревью" in res.output

    monkeypatch.setenv("JWU_DB_PATH", str(db))
    monkeypatch.setattr(srv, "_base_store", None)
    monkeypatch.setattr(srv, "_stores", {})
    try:
        items = asyncio.run(srv.jwu_branches(key="PROJ-2", workspace="work"))
        assert items[0]["branch"] == "PROJ-2-feat" and items[0]["dirty"] == 1
        ctx = asyncio.run(srv.jwu_context("PROJ-1", workspace="work"))
        assert ctx["branches"][0]["branch"] == "PROJ-1-fix"
    finally:
        for s in list(srv._stores.values()):
            s.close()
        if srv._base_store is not None:
            srv._base_store.close()
