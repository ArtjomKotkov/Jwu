"""Настройки уведомлений: группы и события, «Участвую», упоминания, тихие часы (общие и свои)."""

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
    assert p.quiet.empty and not p.own_quiet and not p.is_quiet()


def test_groups_and_single_kinds(store):
    p = np.toggle(np.load(store), "my_prs")
    p.kinds_off = {"status_change"}
    p = np.save(store, p)
    kinds = p.kinds()
    assert "reviewer_approved" not in kinds and "status_change" not in kinds and "build_failed" in kinds
    # событие в выключенной группе: включается оно одно, остальные в группе — нет
    p = np.save(store, np.toggle_kind(p, "reviewer_approved"))
    assert p.on("my_prs") and p.kind_on("reviewer_approved") and not p.kind_on("new_pr_commit")
    with pytest.raises(np.PrefsError):
        np.toggle(p, "nonsense")


@pytest.mark.parametrize("raw, expect", [
    ("10-16, 22-08", [(time(10), time(16)), (time(22), time(8))]),
    ("22:30 - 07:45", [(time(22, 30), time(7, 45))]),
    ("22-8;13-14", [(time(22), time(8)), (time(13), time(14))]),
    ("off", []),
])
def test_parse_ranges(raw, expect):
    assert np.parse_ranges(raw) == expect


@pytest.mark.parametrize("bad", ["22", "25-08", "10-10", "abc"])
def test_parse_ranges_errors(bad):
    with pytest.raises(np.PrefsError):
        np.parse_ranges(bad)


@pytest.mark.parametrize("now, quiet", [
    (datetime(2026, 11, 2, 23, 30), True), (datetime(2026, 11, 2, 7, 59), True),
    (datetime(2026, 11, 2, 8, 0), False), (datetime(2026, 11, 2, 12, 0), True),
    (datetime(2026, 11, 2, 16, 0), False),
])
def test_two_ranges(now, quiet):
    q = np.Quiet(ranges=np.parse_ranges("10-16, 22-08"))
    assert q.active(now) is quiet


def test_global_quiet_and_workspace_override(store):
    np.save_global(store, np.Quiet(ranges=np.parse_ranges("22-08"), days={"sun"}))
    p = np.load(store)
    assert not p.own_quiet and p.quiet.days == {"sun"} and "с 22:00 до 08:00" in p.quiet_label()
    p.quiet, p.own_quiet = np.Quiet(), True                  # у воркспейса — без тишины
    p = np.save(store, p)
    assert p.own_quiet and p.quiet.empty
    p.own_quiet = False
    assert np.save(store, p).quiet.days == {"sun"}           # снова как в общих


def test_deliver_holds_then_flushes_once(store):
    p = np.Prefs(quiet=np.Quiet(ranges=np.parse_ranges("22-09")))
    tg = Tg()
    night, morning = datetime(2026, 11, 2, 23, 0), datetime(2026, 11, 3, 9, 5)
    assert np.deliver(store, tg, "конфликт", prefs=p, now=night) == "held"
    assert np.deliver(store, tg, "апрув", prefs=p, now=night) == "held"
    assert tg.sent == [] and np.flush(store, tg, prefs=p, now=night) == 0
    assert np.flush(store, tg, prefs=p, now=morning) == 2
    text, kw = tg.sent[0]
    assert "Пока было тихо" in text and "2 уведомления" in text and "апрув" in text and kw.get("silent")
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


def test_bot_menus(store):
    ws = store.workspace_id
    text, kb = np.handle_callback(store, "np:home")
    assert "🌐 <b>Общие</b>" in text and "np:g:quiet" in str(kb) and f"np:{ws}:show" in str(kb)
    text, kb = np.handle_callback(store, f"np:{ws}:grp:my_prs")
    assert "Мои PR" in text and f"np:{ws}:k:new_pr_commit" in str(kb)
    np.handle_callback(store, f"np:{ws}:k:new_pr_commit")
    assert not np.load(store, ws).kind_on("new_pr_commit")
    text, kb = np.handle_callback(store, f"np:{ws}:ga:my_prs")
    assert not np.load(store, ws).on("my_prs") and "Включить всю группу" in str(kb)
    text, _ = np.handle_callback(store, f"np:{ws}:show")
    assert "Мои PR · выкл" in str(_)
    text, kb = np.handle_callback(store, "np:g:qp:2200-0800")
    assert np.load_global(store).ranges == [(time(22), time(8))] and "● с 22 до 08" in str(kb)
    np.handle_callback(store, "np:g:qw")
    assert {"sat", "sun"} <= np.load_global(store).days
    np.handle_callback(store, f"np:{ws}:qoff")
    assert np.load(store, ws).own_quiet and np.load(store, ws).quiet.empty
    np.handle_callback(store, f"np:{ws}:qinh")
    assert not np.load(store, ws).own_quiet
    assert np.handle_callback(store, "np:999:show")[0] == "Кнопка устарела."


def test_bot_text_input_for_ranges(store):
    ws = store.workspace_id
    assert np.take_text_input(store, "10-16, 22-08") is None              # не ждём — не наше
    text, _ = np.handle_callback(store, f"np:{ws}:qt")
    assert "Пришли часы тишины" in text
    reply, _ = np.take_text_input(store, "25-08")
    assert reply.startswith("⚠️")                                         # ошибка — ждём дальше
    reply, _ = np.take_text_input(store, "10-16, 22-08")
    assert reply.startswith("✅ Сохранил") and np.load(store, ws).quiet.ranges == np.parse_ranges("10-16,22-08")
    assert np.take_text_input(store, "PROJ-1 заметка") is None             # больше не ждём


def test_cli_settings(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from jwu.cli import main as cli

    monkeypatch.setenv("JWU_DB_PATH", str(tmp_path / "c.db"))
    run = lambda *a: CliRunner().invoke(cli.app, ["-W", "work", "notify", "settings", *a])  # noqa: E731
    assert run("--global", "--quiet", "22-08", "--quiet-days", "sun").exit_code == 0
    r = run("--off", "tasks", "--kind-off", "new_pr_commit")
    out = " ".join(r.output.split())
    assert r.exit_code == 0 and "🔕 📋 Задачи" in out and "(как в общих)" in out and "с 22:00 до 08:00" in out
    out = " ".join(run("--quiet", "10-16, 22-08").output.split())
    assert "с 10:00 до 16:00; с 22:00 до 08:00" in out and "(как в общих)" not in out
    assert run("--quiet", "25-01").exit_code == 2
    assert run("--kind-off", "nope").exit_code == 2
    assert "(как в общих)" in " ".join(run("--inherit").output.split())
