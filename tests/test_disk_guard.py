"""Порог места перед рабочей копией: мягкий обходится с разрешения, жёсткий — нет."""

from collections import namedtuple

import pytest

from jwu.core import jobspace
from jwu.core.store import Store

Usage = namedtuple("Usage", "total used free")
GB = 1024 ** 3


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "s.db")
    st.use_workspace(st.get_workspace_by_slug("work").id)
    yield st
    st.close()


@pytest.fixture
def free(monkeypatch):
    state = {"gb": 100.0}
    monkeypatch.setattr(jobspace.shutil, "disk_usage",
                        lambda _p: Usage(500 * GB, 0, int(state["gb"] * GB)))
    return state


def _require(store, **kw):
    return jobspace.require_space(store, store.workspace_id, "/", runner=lambda _c: (1, ""), **kw)


def test_defaults_and_settings(store):
    ws = store.workspace_id
    assert jobspace.min_free_gb(store, ws) == 20.0
    assert jobspace.hard_min_free_gb(store, ws) == 14.0
    jobspace.set_hard_min_free_gb(store, ws, 10)
    assert jobspace.hard_min_free_gb(store, ws) == 10.0
    jobspace.set_min_free_gb(store, ws, 8)          # жёсткий не выше мягкого
    assert jobspace.hard_min_free_gb(store, ws) == 8.0


def test_enough_space_passes(store, free):
    assert _require(store).ok


def test_soft_limit_needs_permission(store, free):
    free["gb"] = 16
    with pytest.raises(jobspace.JobSpaceError, match="low_disk_ok"):
        _require(store)
    report = _require(store, low_disk_ok=True)
    assert not report.ok and report.hard_ok
    assert report.to_dict()["hard_ok"] is True


def test_hard_limit_never_bypassed(store, free):
    free["gb"] = 12
    with pytest.raises(jobspace.JobSpaceError, match="обойти нельзя"):
        _require(store, low_disk_ok=True)
    assert "КРИТИЧНО" in jobspace.check_space(store, store.workspace_id, "/", with_docker=False).summary()


def test_worktree_note_marks_low_disk():
    res = {"ref": "/w/app-job1", "meta": {"branch": "b"}}
    assert "малом месте" not in jobspace.worktree_note(res)
    note = jobspace.worktree_note({**res, "low_disk": "свободно 16.0 из 500 ГБ"})
    assert "с разрешения пользователя" in note and "16.0" in note
