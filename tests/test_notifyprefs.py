"""Настройки уведомлений: группы событий, «Участвую», упоминания, тихие часы со сводкой."""

from datetime import datetime, time

import pytest

from jwu.core import notify
from jwu.core import notifyprefs as np
from jwu.core.models import Delta, Mention
from jwu.core.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.db")
    s.use_workspace(s.get_workspace_by_slug("work").id)
    yield s
    s.close()


class Tg:
    chat_id = "42"

    def __init__(self):
        self.sent = []

    def send(self, text, **kw):
        self.sent.append((text, kw))
        return [len(self.sent)]


def test_defaults_send_everything(store):
    p = np.load(store)
    assert p.kinds() == list(notify.DEFAULT_KINDS) and p.on("mentions") and p.on("participate")
    assert p.quiet is None and not p.is_quiet()


def test_groups_and_kinds_off(store):
    p = np.toggle(np.load(store), "my_prs")
    p.kinds_off = {"status_change"}
    p = np.save(store, p)
    kinds = p.kinds()
    assert "reviewer_approved" not in kinds and "new_pr_commit" not in kinds and "status_change" not in kinds
    assert "build_failed" in kinds and "new_issue" in kinds
    with pytest.raises(np.PrefsError, match="Нет группы"):
        np.toggle(p, "nonsense")


@pytest.mark.parametrize("raw, now, quiet", [
    ("22:00-09:00", datetime(2026, 11, 2, 23, 30), True),
    ("22:00-09:00", datetime(2026, 11, 2, 8, 59), True),
    ("22:00-09:00", datetime(2026, 11, 2, 9, 0), False),
    ("13:00-14:00", datetime(2026, 11, 2, 13, 30), True),
    ("13:00-14:00", datetime(2026, 11, 2, 15, 0), False),
])
def test_quiet_window(raw, now, quiet):
    assert np.Prefs(quiet=np.parse_quiet(raw)).is_quiet(now) is quiet


def test_quiet_days_and_parse_errors():
    sunday = datetime(2026, 11, 1, 12, 0)
    assert np.Prefs(quiet_days={"sun"}).is_quiet(sunday)
    assert not np.Prefs(quiet_days={"sat"}).is_quiet(sunday)
    assert np.parse_quiet("off") is None and np.parse_quiet("9:00-18:30") == (time(9), time(18, 30))
    for bad in ("22-09", "25:00-09:00", "10:00-10:00"):
        with pytest.raises(np.PrefsError):
            np.parse_quiet(bad)


def test_deliver_holds_in_quiet_hours_then_flushes_once(store):
    p = np.Prefs(quiet=np.parse_quiet("22:00-09:00"))
    tg = Tg()
    night, morning = datetime(2026, 11, 2, 23, 0), datetime(2026, 11, 3, 9, 5)
    assert np.deliver(store, tg, "конфликт", prefs=p, now=night) == "held"
    assert np.deliver(store, tg, "апрув", prefs=p, now=night) == "held"
    assert tg.sent == [] and len(np.held(store)) == 2
    assert np.flush(store, tg, prefs=p, now=night) == 0                     # ещё тихо
    assert np.flush(store, tg, prefs=p, now=morning) == 2
    text, kw = tg.sent[0]
    assert "Пока было тихо" in text and "конфликт" in text and "апрув" in text and kw.get("silent")
    assert np.held(store) == [] and np.flush(store, tg, prefs=p, now=morning) == 0
    # днём: сначала накопленное, потом новое
    np.deliver(store, tg, "ночное", prefs=p, now=night)
    np.deliver(store, tg, "дневное", prefs=p, now=morning)
    assert "ночное" in tg.sent[-2][0] and tg.sent[-1][0] == "дневное"


def test_filter_note_participate_and_mentions():
    note = notify.Notification(
        workspace="w",
        deltas=[Delta(kind="new_comment", key="A-1"), Delta(kind="new_comment", key="B-2")],
        mentions=[Mention(task_key="A-1", author="x", body="@me"), Mention(task_key="B-2", author="y", body="@me")],
        roles={"A-1": notify.Role(label="моя", mine=True), "B-2": notify.Role(label="участвую", mine=False)})
    np.filter_note(note, np.Prefs(off={"participate"}))
    assert [d.key for d in note.deltas] == ["A-1"] and [m.task_key for m in note.mentions] == ["A-1"]
    np.filter_note(note, np.Prefs(off={"mentions"}))
    assert note.mentions == []


def test_bot_menu_toggles_and_quiet_presets(store):
    ws_id = store.workspace_id
    text, kb = np.workspaces_menu(store)
    assert any(b["callback_data"] == f"np:{ws_id}:show" for row in kb["inline_keyboard"] for b in row)
    text, kb = np.handle_callback(store, f"np:{ws_id}:t:qa")
    assert not np.load(store, ws_id).on("qa") and "🔕 🧪 QA" in str(kb)
    text, kb = np.handle_callback(store, f"np:{ws_id}:q:22:00-09:00")
    assert np.load(store, ws_id).quiet == (time(22), time(9)) and "22:00–09:00" in text
    np.handle_callback(store, f"np:{ws_id}:w")
    assert {"sat", "sun"} <= np.load(store, ws_id).quiet_days
    np.handle_callback(store, f"np:{ws_id}:q:off")
    assert np.load(store, ws_id).quiet is None
    assert np.handle_callback(store, "np:zzz")[0] == "Кнопка устарела."


def test_cli_settings(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from jwu.cli import main as cli

    monkeypatch.setenv("JWU_DB_PATH", str(tmp_path / "c.db"))
    run = lambda *a: CliRunner().invoke(cli.app, ["-W", "work", "notify", "settings", *a])  # noqa: E731
    r = run("--off", "tasks", "--kind-off", "new_pr_commit", "--quiet", "23:00-08:00", "--quiet-days", "sun")
    assert r.exit_code == 0, r.output
    out = " ".join(run().output.split())
    assert "🔕 📋 Задачи" in out and "new_pr_commit" in out and "23:00–08:00" in out and "вс — весь день" in out
    assert run("--quiet", "25:00-01:00").exit_code == 2
    assert run("--kind-off", "nope").exit_code == 2
    assert run("--on", "tasks", "--quiet", "off").exit_code == 0
    assert "✅ 📋 Задачи" in " ".join(run().output.split())
