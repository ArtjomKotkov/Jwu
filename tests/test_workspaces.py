"""Воркспейсы: миграция старой БД, изоляция данных, резолв активного воркспейса."""

import json
import sqlite3

import pytest

from jwu.core import workspaces
from jwu.core.models import Comment, Issue, PR
from jwu.core.store import DEFAULT_WORKSPACE_SLUG, SCHEMA_VERSION, Store


def _issue(key="PROJ-1", status="Open"):
    return Issue(key=key, summary="S", status=status, comments=[Comment(id="1")])


def _pr(pr_id=1, title="T"):
    return PR(id=pr_id, title=title, author="me", project="P", repository="r")


# --------------------------------------------------------------------------- #
# Миграция
# --------------------------------------------------------------------------- #


def _make_legacy_db(path) -> None:
    """БД в том виде, в каком её оставляла версия jwu до воркспейсов."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
            views TEXT NOT NULL, counts TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE issue_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sync_run_id INTEGER NOT NULL,
            key TEXT NOT NULL, signature TEXT NOT NULL, fields TEXT NOT NULL,
            views TEXT NOT NULL DEFAULT '[]', fetched_at TEXT NOT NULL);
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_key TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT 'claude', text TEXT NOT NULL, ts TEXT NOT NULL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    conn.execute("INSERT INTO sync_runs (started_at, views, counts) VALUES ('t', '[\"mine\"]', '{}')")
    conn.execute(
        "INSERT INTO issue_snapshots (sync_run_id, key, signature, fields, views, fetched_at)"
        " VALUES (1, 'PROJ-1', '{}', ?, '[\"mine\"]', 't')",
        (_issue().model_dump_json(),),
    )
    conn.execute(
        "INSERT INTO jobs (task_key, title, status, created_at, updated_at)"
        " VALUES ('PROJ-1', 'старая работа', 'active', 't', 't')"
    )
    conn.execute("INSERT INTO notes (key, author, text, ts) VALUES ('PROJ-1', 'me', 'n', 't')")
    conn.execute("INSERT INTO meta (key, value) VALUES ('identity', ?)",
                 (json.dumps({"user": "alice"}),))
    conn.execute("INSERT INTO meta (key, value) VALUES ('ui.theme', 'nord')")
    conn.commit()
    conn.close()


def test_legacy_db_migrates_into_work_workspace(tmp_path):
    db = tmp_path / "state.db"
    _make_legacy_db(db)

    store = Store(db)
    try:
        ws = store.get_workspace_by_slug(DEFAULT_WORKSPACE_SLUG)
        assert ws is not None and ws.name == "Работа"
        # существующие данные заведомо рабочие — обе интеграции включены
        assert (ws.jira_enabled, ws.bitbucket_enabled) == (True, True)
        assert store.workspace_id == ws.id
        # данные видны и никуда не делись
        assert [i.key for i in store.latest_issues("mine")] == ["PROJ-1"]
        assert [j.title for j in store.list_jobs()] == ["старая работа"]
        assert len(store.get_notes("PROJ-1")) == 1
        # пер-воркспейсный ключ meta переехал под префикс, глобальный — нет
        assert json.loads(store.get_workspace_meta("identity"))["user"] == "alice"
        assert store.get_meta("identity") is None
        assert store.get_meta("ui.theme") == "nord"
        assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
    finally:
        store.close()


def test_migration_is_idempotent_and_does_not_duplicate_workspace(tmp_path):
    db = tmp_path / "state.db"
    _make_legacy_db(db)
    Store(db).close()
    store = Store(db)
    try:
        assert len(store.list_workspaces()) == 1
        assert [j.title for j in store.list_jobs()] == ["старая работа"]
    finally:
        store.close()


def test_migration_makes_backup_of_non_empty_db(tmp_path):
    db = tmp_path / "state.db"
    backups = tmp_path / "backups"
    _make_legacy_db(db)

    from jwu.core import maintenance

    dest = maintenance.backup_before_migration(db, to_version=2, backups_dir=backups)
    assert dest is not None and dest.exists()
    # копия читается и содержит те же данные
    conn = sqlite3.connect(str(dest))
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    conn.close()


def test_backup_skips_empty_db(tmp_path):
    from jwu.core import maintenance

    db = tmp_path / "state.db"
    db.touch()
    assert maintenance.backup_before_migration(db, to_version=2,
                                               backups_dir=tmp_path / "b") is None


# --------------------------------------------------------------------------- #
# Изоляция данных между воркспейсами
# --------------------------------------------------------------------------- #


@pytest.fixture()
def two_workspaces(tmp_path):
    store = Store(tmp_path / "state.db")
    home = store.create_workspace("home", name="Личное")
    work_id = store.workspace_id
    yield store, work_id, home.id
    store.close()


def test_snapshots_and_deltas_are_isolated_by_workspace(two_workspaces):
    store, work_id, home_id = two_workspaces

    # один и тот же ключ задачи в обоих воркспейсах, но с разными статусами
    store.use_workspace(work_id)
    run = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run, _issue(status="Open"), ["mine"])
    store.finish_sync_run(run, {"tasks:mine": 1})

    store.use_workspace(home_id)
    run_h = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run_h, _issue(status="Done"), ["mine"])
    store.finish_sync_run(run_h, {"tasks:mine": 1})

    assert [i.status for i in store.latest_issues("mine")] == ["Done"]
    store.use_workspace(work_id)
    assert [i.status for i in store.latest_issues("mine")] == ["Open"]

    # первое появление задачи в СВОЁМ воркспейсе — new_issue, а не «сменился статус»
    store.use_workspace(home_id)
    kinds = {d.kind for d in store.compute_changes(run_h)}
    assert kinds == {"new_issue"}


def test_gone_deltas_ignore_other_workspace_runs(two_workspaces):
    store, work_id, home_id = two_workspaces

    store.use_workspace(work_id)
    r1 = store.start_sync_run(["mine"])
    store.save_issue_snapshot(r1, _issue(), ["mine"])
    store.finish_sync_run(r1, {"tasks:mine": 1})

    # прогон чужого воркспейса без этой задачи не должен выглядеть как «задача ушла»
    store.use_workspace(home_id)
    r2 = store.start_sync_run(["mine"])
    store.finish_sync_run(r2, {"tasks:mine": 0})
    assert store.compute_changes(r2) == []

    store.use_workspace(work_id)
    r3 = store.start_sync_run(["mine"])
    store.save_issue_snapshot(r3, _issue(), ["mine"])
    store.finish_sync_run(r3, {"tasks:mine": 1})
    assert [d.kind for d in store.compute_changes(r3)] == []


def test_prs_notes_analyses_jobs_and_pending_are_isolated(two_workspaces):
    store, work_id, home_id = two_workspaces

    store.use_workspace(work_id)
    run = store.start_sync_run(["prs:mine"])
    store.save_pr_snapshot(run, _pr(), ["mine"])
    store.finish_sync_run(run, {"prs:mine": 1})
    store.add_note("PROJ-1", "рабочая заметка")
    store.save_analysis("рабочий анализ", "план")
    store.create_job("PROJ-1", "рабочая работа")
    store.add_pending_changes(run, [])

    store.use_workspace(home_id)
    assert store.latest_prs("mine") == []
    assert store.get_notes("PROJ-1") == []
    assert store.list_analyses() == []
    assert store.list_jobs() == []
    assert store.pending_changes() == []
    assert store.latest_run_id() is None

    store.use_workspace(work_id)
    assert len(store.latest_prs("mine")) == 1
    assert len(store.list_jobs()) == 1


def test_job_of_other_workspace_is_not_writable(two_workspaces):
    store, work_id, home_id = two_workspaces
    store.use_workspace(work_id)
    job = store.create_job("PROJ-1", "рабочая")

    store.use_workspace(home_id)
    assert store.get_job(job.id) is None
    with pytest.raises(ValueError, match="work"):
        store.add_job_record(job.id, "не должно записаться")
    store.delete_job(job.id)  # чужую работу не удаляем молча

    store.use_workspace(work_id)
    assert store.get_job(job.id) is not None


def test_delete_workspace_removes_its_data_only(two_workspaces):
    store, work_id, home_id = two_workspaces
    store.use_workspace(work_id)
    store.create_job("PROJ-1", "рабочая")
    store.use_workspace(home_id)
    job = store.create_job("", "домашняя")
    store.add_job_record(job.id, "запись")

    store.delete_workspace(home_id)
    store.use_workspace(work_id)
    assert [j.title for j in store.list_jobs()] == ["рабочая"]
    assert store.conn.execute("SELECT COUNT(*) FROM job_records").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# Резолв активного воркспейса
# --------------------------------------------------------------------------- #


@pytest.fixture()
def resolve_env(tmp_path, monkeypatch):
    monkeypatch.delenv(workspaces.ENV_VAR, raising=False)
    store = Store(tmp_path / "state.db")
    home = store.create_workspace("home", name="Личное")
    yield store, home
    store.close()


def test_resolve_prefers_explicit_then_env_then_cwd(resolve_env, tmp_path, monkeypatch):
    store, home = resolve_env
    folder = tmp_path / "projects" / "home"
    folder.mkdir(parents=True)
    workspaces.add_path(store, home, folder)

    assert workspaces.resolve(store, explicit="work").workspace.slug == "work"

    monkeypatch.setenv(workspaces.ENV_VAR, "work")
    assert workspaces.resolve(store, cwd=folder).source == "env"
    monkeypatch.delenv(workspaces.ENV_VAR)

    res = workspaces.resolve(store, cwd=folder)
    assert (res.workspace.slug, res.source) == ("home", "cwd")


def test_resolve_falls_back_to_active_then_raises(resolve_env, tmp_path):
    store, home = resolve_env
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    with pytest.raises(workspaces.WorkspaceNotSelected):
        workspaces.resolve(store, cwd=outside)

    workspaces.set_active(store, home)
    res = workspaces.resolve(store, cwd=outside)
    assert (res.workspace.slug, res.source) == ("home", "active")


def test_resolve_single_workspace_needs_no_choice(tmp_path, monkeypatch):
    monkeypatch.delenv(workspaces.ENV_VAR, raising=False)
    store = Store(tmp_path / "state.db")
    try:
        res = workspaces.resolve(store, cwd=tmp_path)
        assert (res.workspace.slug, res.source) == ("work", "only")
    finally:
        store.close()


def test_nested_paths_pick_the_deepest_workspace(resolve_env, tmp_path):
    store, home = resolve_env
    work = store.get_workspace_by_slug("work")
    outer = tmp_path / "dev"
    inner = outer / "toy"
    inner.mkdir(parents=True)
    workspaces.add_path(store, work, outer)
    workspaces.add_path(store, home, inner)

    deep = inner / "src" / "core"
    deep.mkdir(parents=True)

    assert workspaces.resolve(store, cwd=outer).workspace.slug == "work"
    assert workspaces.resolve(store, cwd=inner).workspace.slug == "home"
    # вложенная папка наследует самый глубокий совпавший корень, а не самый внешний
    assert workspaces.resolve(store, cwd=deep).workspace.slug == "home"


def test_path_cannot_belong_to_two_workspaces(resolve_env, tmp_path):
    store, home = resolve_env
    work = store.get_workspace_by_slug("work")
    folder = tmp_path / "shared"
    folder.mkdir()
    workspaces.add_path(store, home, folder)

    _, warn = workspaces.add_path(store, home, folder)
    assert warn and "уже привязана" in warn

    with pytest.raises(workspaces.WorkspaceError, match="home"):
        workspaces.add_path(store, work, folder)


def test_unknown_explicit_workspace_is_an_error(resolve_env):
    store, _ = resolve_env
    with pytest.raises(workspaces.WorkspaceError, match="не найден"):
        workspaces.resolve(store, explicit="nope")


def test_create_validates_slug(resolve_env):
    store, _ = resolve_env
    with pytest.raises(workspaces.WorkspaceError, match="Некорректный slug"):
        workspaces.create(store, "Не Слаг")
    with pytest.raises(workspaces.WorkspaceError, match="уже есть"):
        workspaces.create(store, "home")
