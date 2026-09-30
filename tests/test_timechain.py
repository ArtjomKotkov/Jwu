"""Ворклоги цепочкой: начало, пояс, последовательность, пересечения (JWU-52)."""

import json
from datetime import date

import pytest
from typer.testing import CliRunner

from jwu.cli import main as cli
from jwu.core import timechain
from jwu.core.store import Store

runner = CliRunner()
DAY = date(2026, 9, 30)


@pytest.mark.parametrize("text, sec", [
    ("1h 30m", 5400), ("45m", 2700), ("1.5h", 5400), ("1,5h", 5400), ("2h", 7200), ("1d", 8 * 3600),
])
def test_parse_duration(text, sec):
    assert timechain.parse_duration(text) == sec


@pytest.mark.parametrize("bad", ["", "полчаса", "1x", "0m", "1h и ещё"])
def test_parse_duration_rejects(bad):
    with pytest.raises(timechain.ChainError):
        timechain.parse_duration(bad)


def test_fmt_duration():
    assert timechain.fmt_duration(5400) == "1h 30m" and timechain.fmt_duration(2700) == "45m"
    assert timechain.fmt_duration(7200) == "2h"


def test_resolve_tz_aliases():
    assert timechain.resolve_tz("МСК").key == "Europe/Moscow"
    assert timechain.resolve_tz("Asia/Yekaterinburg").key == "Asia/Yekaterinburg"
    with pytest.raises(timechain.ChainError, match="пояс"):
        timechain.resolve_tz("Марс")


def test_plan_is_contiguous_from_start():
    chain = timechain.plan([
        {"key": "A-1", "time": "1h 30m", "comment": "фикс"},
        {"key": "B-2", "time": "45m", "comment": "Ревью"},
        {"key": "A-1", "time": "15m"},
    ], start="10:00", day=DAY, tz_name="МСК")
    assert [(c.key, c.start_local, c.end_local, c.time) for c in chain] == [
        ("A-1", "10:00", "11:30", "1h 30m"),
        ("B-2", "11:30", "12:15", "45m"),
        ("A-1", "12:15", "12:30", "15m"),
    ]
    assert chain[0].started == "2026-09-30T10:00:00.000+0300"   # смещение МСК — в самой строке
    assert chain[1].started == "2026-09-30T11:30:00.000+0300"


def test_plan_other_tz_and_midnight():
    chain = timechain.plan([{"key": "A-1", "time": "2h"}], start="23:00", day=DAY, tz_name="Asia/Yekaterinburg")
    assert chain[0].started.endswith("+0500") and chain[0].end_local == "01:00 (+1д)"


@pytest.mark.parametrize("start", ["25:00", "10:61", "утро", ""])
def test_plan_bad_start(start):
    with pytest.raises(timechain.ChainError):
        timechain.plan([{"key": "A-1", "time": "1h"}], start=start, day=DAY, tz_name="UTC")


def test_overlaps_and_latest_end():
    chain = timechain.plan([{"key": "A-1", "time": "2h"}], start="10:00", day=DAY, tz_name="МСК")
    existing = {
        "C-3": [{"time": "1h", "seconds": 3600, "comment": "утро", "started": "2026-09-30T09:00:00.000+0300"},
                {"time": "1h", "seconds": 3600, "comment": "пересечение", "started": "2026-09-30T11:00:00.000+0300"}],
        "D-4": [{"time": "30m", "seconds": 1800, "comment": "в UTC", "started": "2026-09-30T09:30:00.000+0000"}],
    }
    hits = timechain.overlaps(chain, existing)
    # 09:00–10:00 МСК стык, не пересечение; 11:00–12:00 пересекается; 09:30 UTC = 12:30 МСК — нет
    assert [(h["key"], h["from"], h["to"]) for h in hits] == [("C-3", "11:00", "12:00")]
    assert timechain.latest_end(existing, "МСК") == "13:00"


def test_timezone_setting(tmp_path):
    store = Store(tmp_path / "s.db")
    try:
        wid = store.get_workspace_by_slug("work").id
        assert timechain.get_timezone(store, wid) == ""
        assert timechain.set_timezone(store, wid, "мск") == "Europe/Moscow"
        assert timechain.get_timezone(store, wid) == "Europe/Moscow"
    finally:
        store.close()


class _Svc:
    """Кусок Service: worklog_chain поверх фейковой Jira."""

    def __init__(self, store, fail_on=None):
        from jwu.core.service import Service

        self.store = store
        self.tasks_client = object()
        self.logged = []
        self.fail_on = fail_on
        self.worklog_chain = Service.worklog_chain.__get__(self)

    def _require_tasks(self):
        return self.tasks_client

    def my_worklog_keys_on(self, day):
        return ["C-3"]

    def my_worklogs_on(self, keys, day):
        return {"C-3": [{"time": "1h", "seconds": 3600, "comment": "", "started": "2026-09-30T09:00:00.000+0300"}]}

    def add_worklog(self, key, time, *, comment=None, started=None):
        if key == self.fail_on:
            raise RuntimeError("403: нет прав")
        self.logged.append((key, time, started, comment))
        return {}


def test_service_chain_dry_run_then_write(tmp_path):
    store = Store(tmp_path / "s.db")
    store.use_workspace(store.get_workspace_by_slug("work").id)
    try:
        svc = _Svc(store)
        with pytest.raises(timechain.ChainError, match="пояс"):
            svc.worklog_chain([{"key": "A-1", "time": "1h"}], start="10:00", day="2026-09-30")
        timechain.set_timezone(store, store.workspace_id, "МСК")
        items = [{"key": "A-1", "time": "1h", "comment": "фикс"}, {"key": "B-2", "time": "30m"}]
        plan = svc.worklog_chain(items, start="10:00", day="2026-09-30")
        assert svc.logged == [] and plan["total"] == "1h 30m" and plan["existing_ends_at"] == "10:00"
        res = svc.worklog_chain(items, start="10:00", day="2026-09-30", dry_run=False)
        assert svc.logged == [("A-1", "1h", "2026-09-30T10:00:00.000+0300", "фикс"),
                              ("B-2", "30m", "2026-09-30T11:00:00.000+0300", None)]
        assert res["written"][1] == {"key": "B-2", "from": "11:00", "to": "11:30"} and res["failed"] is None
    finally:
        store.close()


def test_service_chain_stops_on_first_failure(tmp_path):
    store = Store(tmp_path / "s.db")
    store.use_workspace(store.get_workspace_by_slug("work").id)
    try:
        timechain.set_timezone(store, store.workspace_id, "МСК")
        svc = _Svc(store, fail_on="B-2")
        res = svc.worklog_chain([{"key": "A-1", "time": "1h"}, {"key": "B-2", "time": "1h"},
                                 {"key": "C-3", "time": "1h"}], start="10:00", day="2026-09-30", dry_run=False)
        assert [w["key"] for w in res["written"]] == ["A-1"]
        assert res["failed"]["key"] == "B-2" and "403" in res["failed"]["error"]
        assert [k for k, *_ in svc.logged] == ["A-1"]            # C-3 не трекнут — цепочка не рвётся молча
    finally:
        store.close()


def test_cli_worklog_tz_and_chain_requires_confirmation(monkeypatch):
    r = runner.invoke(cli.app, ["-W", "work", "worklog-tz", "МСК"])
    assert r.exit_code == 0 and "Europe/Moscow" in r.output
    calls = []

    class Svc:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def worklog_chain(self, items, **kw):
            calls.append((items, kw))
            return {"timezone": "Europe/Moscow", "day": kw["day"], "total": "1h", "overlaps": [],
                    "existing_ends_at": "", "written": [], "failed": None,
                    "items": [{"key": "A-1", "start_local": "10:00", "end_local": "11:00", "time": "1h",
                               "comment": "фикс"}]}

    monkeypatch.setattr(cli, "_service_with_jira", lambda: Svc())
    r = runner.invoke(cli.app, ["worklog-chain", "--start", "10:00", "--item", "A-1|1h|фикс", "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout)["reason"] == "confirm_required"
    assert all(kw["dry_run"] for _, kw in calls)                     # без --yes ничего не пишется
    assert calls[0][0] == [{"key": "A-1", "time": "1h", "comment": "фикс"}]
    r = runner.invoke(cli.app, ["worklog-chain", "--start", "10:00", "--item", "без-разделителя"])
    assert r.exit_code == 1
