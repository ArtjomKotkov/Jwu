"""MCP-сервер: резолв воркспейса, кэш сервисов, отказы без Jira.

Инструменты — обычные корутины, поэтому зовём их напрямую через asyncio.run.
"""

import asyncio

import pytest

from jwu import mcp_server as srv
from jwu.core import workspaces
from jwu.core.store import Store


@pytest.fixture(autouse=True)
def fresh_server(tmp_path, monkeypatch):
    """Свежая БД и пустые кэши на каждый тест (сервер живёт процессом)."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("JWU_DB_PATH", str(db))
    monkeypatch.setattr(srv, "_base_store", None)
    monkeypatch.setattr(srv, "_full", {})
    monkeypatch.setattr(srv, "_builds", {})
    monkeypatch.setattr(srv, "_stores", {})
    yield db
    for store in list(srv._stores.values()):
        store.close()
    if srv._base_store is not None:
        srv._base_store.close()


def _run(coro):
    return asyncio.run(coro)


def test_workspace_current_reports_source_and_integrations(fresh_server, tmp_path, monkeypatch):
    store = Store(fresh_server)
    home = workspaces.create(store, "home", name="Личное")
    folder = tmp_path / "pet"
    folder.mkdir()
    workspaces.add_path(store, home, folder)
    store.close()
    monkeypatch.chdir(folder)

    payload = _run(srv.jwu_workspace_current())
    assert payload["slug"] == "home"
    assert payload["source"] == "cwd"
    assert payload["jira_enabled"] is False


def test_workspace_current_without_match_reports_error(fresh_server, tmp_path, monkeypatch):
    store = Store(fresh_server)
    workspaces.create(store, "home")
    store.close()
    monkeypatch.chdir(tmp_path)

    payload = _run(srv.jwu_workspace_current())
    assert payload["workspace"] is None
    assert "воркспейс" in payload["error"].lower()


def test_workspaces_lists_counts(fresh_server):
    store = Store(fresh_server)
    home = workspaces.create(store, "home")
    store.use_workspace(home.id)
    store.create_job("", "домашняя")
    store.create_feature("Тёмная тема")
    store.close()

    items = {w["slug"]: w for w in _run(srv.jwu_workspaces())}
    assert items["home"]["jobs"] == 1
    assert items["home"]["features"] == 1
    assert items["work"]["jobs"] == 0


def test_tools_are_scoped_by_workspace_argument(fresh_server):
    store = Store(fresh_server)
    workspaces.create(store, "home")
    store.close()

    _run(srv.jwu_job_start(task_key="PROJ-1", title="рабочая", workspace="work"))
    _run(srv.jwu_job_start(title="домашняя", workspace="home"))

    work_jobs = _run(srv.jwu_jobs(workspace="work"))
    home_jobs = _run(srv.jwu_jobs(workspace="home"))
    assert [j["title"] for j in work_jobs] == ["рабочая"]
    assert [j["title"] for j in home_jobs] == ["домашняя"]
    assert home_jobs[0]["anchor"].startswith("#")  # работа без якоря


def test_store_cache_is_per_workspace(fresh_server):
    store = Store(fresh_server)
    workspaces.create(store, "home")
    store.close()

    first = srv._store_only("work")
    again = srv._store_only("work")
    other = srv._store_only("home")
    assert first is again          # один воркспейс — одно соединение
    assert first is not other      # разные воркспейсы не делят скоуп
    assert other.workspace_id != first.workspace_id


def test_features_flow_and_job_anchor(fresh_server):
    store = Store(fresh_server)
    workspaces.create(store, "home")
    store.close()

    feature = _run(srv.jwu_feature_add("Тёмная тема", workspace="home"))
    assert feature["key"] == "HOME-1"

    job = _run(srv.jwu_job_start(feature="HOME-1", title="тема", workspace="home"))
    assert job["anchor"] == "HOME-1"

    by_feature = _run(srv.jwu_jobs(feature="HOME-1", workspace="home"))
    assert [j["id"] for j in by_feature] == [job["id"]]

    _run(srv.jwu_feature_status("HOME-1", "done", workspace="home"))
    assert _run(srv.jwu_features(status="done", workspace="home"))[0]["key"] == "HOME-1"
    assert _run(srv.jwu_features(status="open", workspace="home")) == []


def test_jira_tools_refuse_without_jira(fresh_server, monkeypatch):
    store = Store(fresh_server)
    workspaces.create(store, "home")
    store.close()
    # сервис без интеграций строится без единого обращения к кредам
    with pytest.raises(ValueError, match="Jira не подключена"):
        _run(srv.jwu_task("PROJ-1", workspace="home"))


def test_job_start_validates_anchors(fresh_server):
    with pytest.raises(ValueError, match="что-то одно"):
        _run(srv.jwu_job_start(task_key="PROJ-1", feature="X-1", workspace="work"))
    with pytest.raises(ValueError, match="title"):
        _run(srv.jwu_job_start(workspace="work"))
    with pytest.raises(ValueError, match="не найдена"):
        _run(srv.jwu_job_start(feature="NOPE-1", workspace="work"))


def test_bad_feature_status_is_rejected(fresh_server):
    with pytest.raises(ValueError, match="Недопустимый статус"):
        _run(srv.jwu_features(status="лол", workspace="work"))


def test_workspace_create_and_paths_via_mcp(fresh_server, tmp_path, monkeypatch):
    """Создание воркспейса и привязка папок доступны агенту без похода в bash."""
    folder = tmp_path / "pet"
    folder.mkdir()

    ws = _run(srv.jwu_workspace_create(
        "home-jwu", name="Личное", paths=[str(folder)]))
    assert ws["slug"] == "home-jwu"
    assert ws["jira_enabled"] is False          # интеграции объявляются явно
    assert ws["paths"] == [str(folder.resolve())]

    # по привязанной папке воркспейс определяется сам
    monkeypatch.chdir(folder)
    assert _run(srv.jwu_workspace_current())["slug"] == "home-jwu"

    other = tmp_path / "second"
    other.mkdir()
    payload = _run(srv.jwu_workspace_add_path(str(other), label="второй репозиторий"))
    assert set(payload["paths"]) == {str(folder.resolve()), str(other.resolve())}

    payload = _run(srv.jwu_workspace_remove_path(str(other)))
    assert payload["paths"] == [str(folder.resolve())]


def test_workspace_create_rejects_duplicates_and_bad_slug(fresh_server):
    _run(srv.jwu_workspace_create("home"))
    with pytest.raises(ValueError, match="уже есть"):
        _run(srv.jwu_workspace_create("home"))
    with pytest.raises(ValueError, match="Некорректный slug"):
        _run(srv.jwu_workspace_create("Не Слаг"))


def test_add_path_refuses_foreign_folder(fresh_server, tmp_path):
    folder = tmp_path / "shared"
    folder.mkdir()
    _run(srv.jwu_workspace_create("home", paths=[str(folder)]))
    with pytest.raises(ValueError, match="уже принадлежит"):
        _run(srv.jwu_workspace_add_path(str(folder), workspace="work"))


def test_workspace_use_switches_default(fresh_server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _run(srv.jwu_workspace_create("home"))
    # два воркспейса, папка не привязана — без активного резолв невозможен
    assert _run(srv.jwu_workspace_current())["workspace"] is None

    _run(srv.jwu_workspace_use("home"))
    current = _run(srv.jwu_workspace_current())
    assert (current["slug"], current["source"]) == ("home", "active")

    with pytest.raises(ValueError, match="не найден"):
        _run(srv.jwu_workspace_use("nope"))


def test_remove_path_reports_when_not_bound(fresh_server, tmp_path):
    with pytest.raises(ValueError, match="не привязана"):
        _run(srv.jwu_workspace_remove_path(str(tmp_path), workspace="work"))


def test_tags_via_mcp_answer_where_the_code_lives(fresh_server, tmp_path):
    """Агент помечает папки и потом находит их по тегу — «где legacy, где новая версия»."""
    legacy = tmp_path / "old-backend"
    fresh = tmp_path / "new-backend"
    legacy.mkdir()
    fresh.mkdir()

    _run(srv.jwu_workspace_create("dev", paths=[str(legacy)]))
    _run(srv.jwu_workspace_add_path(str(fresh), tags=["новая-версия"], workspace="dev"))
    _run(srv.jwu_workspace_tag(str(legacy), add=["legacy-бэкенд", "django"], workspace="dev"))

    found = _run(srv.jwu_workspace_paths(tag="legacy-бэкенд", workspace="dev"))
    assert [p["path"] for p in found["paths"]] == [str(legacy.resolve())]
    assert found["paths"][0]["tags"] == ["django", "legacy-бэкенд"]

    everything = _run(srv.jwu_workspace_paths(workspace="dev"))
    assert everything["known_tags"] == {"django": 1, "legacy-бэкенд": 1, "новая-версия": 1}
    assert len(everything["paths"]) == 2


def test_tag_replace_and_remove_via_mcp(fresh_server, tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    _run(srv.jwu_workspace_create("dev", paths=[str(folder)]))
    _run(srv.jwu_workspace_tag(str(folder), add=["a", "b"], workspace="dev"))

    assert _run(srv.jwu_workspace_tag(str(folder), remove=["a"], workspace="dev"))["tags"] == ["b"]
    replaced = _run(srv.jwu_workspace_tag(str(folder), replace=["фронт"], workspace="dev"))
    assert replaced["tags"] == ["фронт"]


def test_tagging_unbound_folder_is_refused(fresh_server, tmp_path):
    with pytest.raises(ValueError, match="не привязана"):
        _run(srv.jwu_workspace_tag(str(tmp_path / "nope"), add=["x"], workspace="work"))
