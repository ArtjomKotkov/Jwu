"""Аддоны: поиск, зависимости, вкл/выкл, хуки бота, демона и установки."""

import types

import pytest
from typer.testing import CliRunner

from jwu.core import addons


class _EP:
    def __init__(self, name, module=None, error=None, version="1.0"):
        self.name, self._module, self._error = name, module, error
        self.dist = types.SimpleNamespace(version=version)
        self.loaded = False

    def load(self):
        self.loaded = True
        if self._error:
            raise self._error
        return self._module


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


@pytest.fixture
def eps(monkeypatch):
    """Подменить найденные entry points; кэш аддонов — заново на каждый тест."""
    found: list[_EP] = []
    monkeypatch.setattr(addons, "_entry_points", lambda: found)
    monkeypatch.setattr(addons, "_cache", None)
    yield found
    addons._cache = None


def test_requires_blocks_chain_when_dependency_missing_or_disabled(eps):
    eps += [_EP("chat", _mod("chat")),
            _EP("work", _mod("work", REQUIRES=["chat"])),
            _EP("extra", _mod("extra", REQUIRES=["work"])),
            _EP("orphan", _mod("orphan", REQUIRES=["nope"]))]
    assert [a.name for a in addons.active()] == ["chat", "work", "extra"]
    assert "nope — он не установлен" in addons.get("orphan").error

    addons.set_enabled("chat", False)
    by = {a.name: a for a in addons.load(refresh=True)}
    assert by["chat"].state == addons.DISABLED and by["chat"].module is None
    assert by["work"].state == addons.BLOCKED and "chat — он выключен" in by["work"].error
    assert by["extra"].state == addons.BLOCKED and "work — он не подключён" in by["extra"].error
    assert addons.active() == []
    assert addons.dependents("chat") == ["work"]


def test_disabled_addon_is_not_even_imported(eps):
    ep = _EP("heavy", _mod("heavy"))
    eps.append(ep)
    addons.set_enabled("heavy", False)
    assert addons.get("heavy").state == addons.DISABLED and not ep.loaded
    addons.set_enabled("heavy", True)
    assert addons.load(refresh=True)[0].active and ep.loaded


def test_broken_addon_hooks_are_not_called(eps):
    eps += [_EP("bad", error=ImportError("нет зависимости")),
            _EP("fine", _mod("fine", REQUIRES=["bad"], bot_commands=lambda: [("x", "y")]))]
    assert addons.get("bad").state == addons.BROKEN and "нет зависимости" in addons.get("bad").error
    assert addons.get("fine").call("bot_commands", default="nope") == "nope"


def test_cli_list_enable_disable(eps):
    from jwu.cli.main import app

    addons._cache = None                                   # импорт CLI уже прочитал аддоны
    eps += [_EP("chat", _mod("chat")), _EP("work", _mod("work", REQUIRES=["chat"]))]
    run = CliRunner().invoke
    out = run(app, ["addons", "list"]).output
    assert "chat" in out and "work" in out and "включён" in out
    r = run(app, ["addons", "disable", "chat"])
    assert r.exit_code == 1 and "work" in r.output          # от него зависят — без --force нельзя
    assert addons.disabled() == set()
    r = run(app, ["addons", "disable", "chat", "--force"])
    assert r.exit_code == 0 and addons.disabled() == {"chat"}
    assert addons.get("work").state == addons.BLOCKED
    assert "включён" in run(app, ["addons", "enable", "chat"]).output and addons.disabled() == set()
    assert run(app, ["addons", "enable", "nope"]).exit_code == 1


def test_bot_routes_addon_commands_and_callbacks(eps, tmp_path):
    from jwu.core import bot, notify
    from jwu.core.config import Config
    from jwu.core.store import Store

    seen = []
    eps.append(_EP("chat", _mod(
        "chat",
        bot_commands=lambda: [("ping", "Проверка связи")],
        bot_command=lambda ctx, cmd, text: seen.append((cmd, text)) or ("pong", {"inline_keyboard": []}),
        callback_prefixes=lambda: ["chat:"],
        bot_callback=lambda ctx, data: (f"нажато {data}", {}),
    )))
    assert ("/ping", "Проверка связи") in bot.addon_commands()
    cmds = [c["command"] for c in notify.all_commands()]
    assert "ping" in cmds and cmds[-1] == "help"

    store = Store(tmp_path / "s.db")
    ws = store.get_workspace_by_slug("work")
    store.use_workspace(ws.id)
    cfg = Config()
    cfg.telegram.chat_id = "42"
    sent = []
    sender = types.SimpleNamespace(chat_id="42", role="main",
                                   send=lambda text, **kw: sent.append(text) or [1],
                                   edit=lambda mid, text, **kw: sent.append(text),
                                   answer_callback=lambda *a, **kw: None)
    upd = {"message": {"message_id": 5, "chat": {"id": 42}, "text": "/ping раз"}}
    res = bot.handle_update(upd, store=store, cfg=cfg, sender=sender, ws=ws, login="me")
    assert res["reply"] == "pong" and seen == [("/ping", "/ping раз")]
    upd = {"callback_query": {"id": "1", "data": "chat:go", "message": {"message_id": 9, "chat": {"id": 42}}}}
    res = bot.handle_update(upd, store=store, cfg=cfg, sender=sender, ws=ws, login="me")
    assert res["reply"] == "нажато chat:go"
    store.close()


def test_daemon_runs_addon_tasks_on_their_schedule(eps):
    from jwu.core import daemon

    calls = []
    eps.append(_EP("chat", _mod("chat", daemon_tasks=lambda: [
        ("poll", 0, lambda store: calls.append("poll")),
        ("slow", 100, lambda store: calls.append("slow")),
        ("boom", 0, lambda store: 1 / 0),
    ])))
    tasks = [t for t in daemon.periodic_tasks() if t[0].startswith("chat:")]
    last: dict = {}
    ran = daemon.run_due_tasks(None, tasks, last, now=1000)
    assert ran == ["chat:poll", "chat:slow"]                 # упавшая не мешает остальным
    daemon.run_due_tasks(None, tasks, last, now=1050)
    assert calls == ["poll", "slow", "poll"]                 # slow — не раньше чем через 100 с
    daemon.run_due_tasks(None, tasks, last, now=1101)
    assert calls[-2:] == ["poll", "slow"]


def test_install_puts_addon_agents_next_to_bundled(eps, tmp_path):
    from jwu import skills_install

    src = tmp_path / "agents"
    src.mkdir()
    (src / "chat-helper.md").write_text("---\nname: chat-helper\ndescription: x\n---\nтело\n")
    eps.append(_EP("chat", _mod("chat", agents_dir=lambda: src)))
    dest = tmp_path / "out"
    res = dict(skills_install.install_agents(dest))
    assert res["chat-helper"] == "добавлен" and (dest / "chat-helper.md").read_text().endswith("тело\n")
