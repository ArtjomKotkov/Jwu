import sqlite3

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
