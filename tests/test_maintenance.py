import sqlite3
from datetime import date

import pytest

from jwu.core.config import ConfigError
from jwu.core.maintenance import ensure_db_available, run_daily_maintenance


def _make_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.commit()
    con.close()


def test_backup_created_and_daily_noop(tmp_path):
    db = tmp_path / "state.db"
    _make_db(db)
    bdir = tmp_path / "backups"

    msgs = run_daily_maintenance(db, backups_dir=bdir)
    baks = list(bdir.glob("state.db.bak-*"))
    assert len(baks) == 1
    assert any("бэкап" in m for m in msgs)

    # повторный вызов в тот же день — ничего не делает
    msgs2 = run_daily_maintenance(db, backups_dir=bdir)
    assert msgs2 == []
    assert len(list(bdir.glob("state.db.bak-*"))) == 1


def test_prune_keeps_only_n(tmp_path):
    db = tmp_path / "state.db"
    _make_db(db)
    bdir = tmp_path / "backups"
    bdir.mkdir()
    for d in ("2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04"):
        (bdir / f"state.db.bak-{d}").write_text("x")

    run_daily_maintenance(db, backups_dir=bdir, keep=2)
    baks = sorted(p.name for p in bdir.glob("state.db.bak-*"))
    assert len(baks) == 2          # старые подрезаны
    assert baks[-1].endswith(__import__("datetime").date.today().isoformat())  # сегодняшний есть


def test_corrupt_db_not_backed_up(tmp_path):
    db = tmp_path / "state.db"
    db.write_bytes(b"this is not a sqlite database at all")
    bdir = tmp_path / "backups"

    msgs = run_daily_maintenance(db, backups_dir=bdir)
    assert any("поврежден" in m.lower() or "integrity" in m.lower() for m in msgs)
    assert list(bdir.glob("state.db.bak-*")) == []  # битую БД не бэкапим


def test_missing_db_is_noop(tmp_path):
    msgs = run_daily_maintenance(tmp_path / "nope.db", backups_dir=tmp_path / "b")
    assert msgs == []


def test_ensure_available_ok_when_exists(tmp_path):
    db = tmp_path / "state.db"
    _make_db(db)
    ensure_db_available(db)  # не бросает


def test_ensure_available_ok_when_absent_no_placeholder(tmp_path):
    ensure_db_available(tmp_path / "state.db")  # первый запуск — не бросает


def test_ensure_available_raises_on_icloud_placeholder(tmp_path):
    db = tmp_path / "state.db"
    (tmp_path / ".state.db.icloud").write_text("")  # iCloud выгрузил файл
    with pytest.raises(ConfigError):
        ensure_db_available(db)


# --- ежедневная чистка снапшотов ------------------------------------------ #


def _store_with_old_snapshots(db, runs=6):
    """Store с историей снапшотов, состаренной на 60 дней."""
    from datetime import datetime, timedelta, timezone

    from jwu.core.models import Issue
    from jwu.core.store import Store

    store = Store(db)
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    for n in range(runs):
        run = store.start_sync_run(["mine"])
        store.save_issue_snapshot(run, Issue(key="PROJ-1", summary="s" * 500), ["mine"])
        store.finish_sync_run(run, {"tasks:mine": 1})
        store.conn.execute("UPDATE sync_runs SET started_at = ? WHERE id = ?", (old, run))
        store.conn.execute(
            "UPDATE issue_snapshots SET fetched_at = ? WHERE sync_run_id = ?", (old, run))
    store.conn.commit()
    return store


def test_daily_prune_skips_small_db(tmp_path):
    """Маленькую базу не трогаем вовсе — необратимая операция без нужды не нужна."""
    from jwu.core.maintenance import run_daily_prune

    db = tmp_path / "state.db"
    store = _store_with_old_snapshots(db)
    bdir = tmp_path / "backups"
    bdir.mkdir()
    (bdir / f"state.db.bak-{date.today().isoformat()}").write_text("x")
    before = store.conn.execute("SELECT COUNT(*) FROM issue_snapshots").fetchone()[0]

    assert run_daily_prune(store, db, backups_dir=bdir) == []
    assert store.conn.execute(
        "SELECT COUNT(*) FROM issue_snapshots").fetchone()[0] == before
    store.close()


def test_daily_prune_refuses_without_todays_backup(tmp_path):
    """Нет сегодняшнего бэкапа — не чистим: откатываться было бы некуда."""
    from jwu.core.maintenance import run_daily_prune

    db = tmp_path / "state.db"
    store = _store_with_old_snapshots(db)
    bdir = tmp_path / "backups"
    bdir.mkdir()
    before = store.conn.execute("SELECT COUNT(*) FROM issue_snapshots").fetchone()[0]

    msgs = run_daily_prune(store, db, backups_dir=bdir, min_bytes=0)
    assert msgs and "бэкапа" in msgs[0]
    assert store.conn.execute(
        "SELECT COUNT(*) FROM issue_snapshots").fetchone()[0] == before
    store.close()


def test_daily_prune_runs_once_a_day(tmp_path):
    from jwu.core.maintenance import run_daily_prune

    db = tmp_path / "state.db"
    store = _store_with_old_snapshots(db)
    bdir = tmp_path / "backups"
    bdir.mkdir()
    (bdir / f"state.db.bak-{date.today().isoformat()}").write_text("x")

    msgs = run_daily_prune(store, db, backups_dir=bdir, min_bytes=0)
    assert msgs and "чистка снапшотов" in msgs[0]
    assert store.conn.execute("SELECT COUNT(*) FROM issue_snapshots").fetchone()[0] == 1

    # второй вызов в тот же день — молча ничего не делает
    assert run_daily_prune(store, db, backups_dir=bdir, min_bytes=0) == []
    store.close()
