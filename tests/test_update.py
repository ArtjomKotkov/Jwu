"""jwu update: версии, проверка релиза, план и прогон обновления, предложение в боте."""

import json
from datetime import date

import httpx
import pytest
import respx

from jwu.core import update as upd
from jwu.core.store import Store

API = "https://api.github.com/repos/ArtjomKotkov/Jwu/releases/latest"


def test_version_order():
    order = ["2.14.2", "2.15.0", "3.0.0.dev1", "3.0.0.dev2", "3.0.0rc1", "3.0.0", "3.0.1", "v3.1"]
    assert sorted(reversed(order), key=upd.version_key) == order
    assert upd.is_newer("3.0.0", "3.0.0.dev7") and not upd.is_newer("2.15.0", "3.0.0.dev1")


@respx.mock
def test_check_compares_with_latest_release():
    respx.get(API).mock(return_value=httpx.Response(200, json={
        "tag_name": "v3.0.0", "html_url": "https://github.com/x/r", "body": "- новое"}))
    info = upd.check(current="2.15.0")
    assert info["newer"] and info["latest"] == "3.0.0" and info["tag"] == "v3.0.0" and info["notes"] == "- новое"
    assert not upd.check(current="3.0.0")["newer"]


def _pipx_json(source, injected=()):
    return json.dumps({"venvs": {"jwu": {"metadata": {
        "main_package": {"package_or_url": source, "pip_args": []},
        "injected_packages": {n: {"package_or_url": src, "pip_args": ["--editable"] if ed else []}
                              for n, src, ed in injected}}}}})


def test_plan_from_release_reinjects_addons_and_keeps_daemon_interval(tmp_path, monkeypatch):
    calls = []
    addon_dir = tmp_path / "addons"
    addon_dir.mkdir()

    def runner(cmd):
        calls.append(cmd)
        if cmd[:2] == ["pipx", "list"]:
            return 0, _pipx_json("jwu", [("my-addons", str(addon_dir), True)])
        return 0, ""

    monkeypatch.setattr(upd, "daemon_interval", lambda: 600)
    inst = upd.current_install(runner)
    assert not inst.from_folder and inst.injected[0]["editable"]
    rel = upd.Release("3.0.0", "v3.0.0")
    steps = upd.plan(rel, inst)
    assert steps[0] == ["pipx", "install", "--force", "git+https://github.com/ArtjomKotkov/Jwu.git@v3.0.0"]
    assert steps[1] == ["pipx", "inject", "--force", "jwu", "-e", str(addon_dir)]
    assert steps[2][1:] == ["install"] and steps[3][1:] == ["daemon", "install", "--interval", "600"]
    res = upd.run(rel, runner=runner, log=lambda m: None, inst=inst)
    assert res["ok"] and res["addons"] == ["my-addons"] and calls[-1][1:] == ["--help"]


def test_plan_from_folder_and_stops_on_failed_step(tmp_path, monkeypatch):
    monkeypatch.setattr(upd, "daemon_interval", lambda: None)
    inst = upd.Install(source=str(tmp_path))
    assert upd.plan(None, inst)[0] == ["pipx", "install", "--force", str(tmp_path)]
    assert len(upd.plan(None, inst)) == 2                      # демона нет — не ставим

    def runner(cmd):
        return (1, "boom") if cmd[:2] == ["pipx", "install"] else (0, "")

    with pytest.raises(upd.UpdateError, match="boom"):
        upd.run(None, runner=runner, log=lambda m: None, inst=inst)


def test_daemon_interval_read_from_plist(tmp_path, monkeypatch):
    from jwu.core import daemon

    plist = tmp_path / "x.plist"
    plist.write_text(daemon.render_plist("/bin/jwu", 777))
    monkeypatch.setattr(daemon, "is_macos", lambda: True)
    monkeypatch.setattr(daemon, "plist_path", lambda: plist)
    assert upd.daemon_interval() == 777
    plist.unlink()
    assert upd.daemon_interval() is None


@respx.mock
def test_offer_once_a_day_once_per_version_and_snooze(tmp_path):
    route = respx.get(API).mock(return_value=httpx.Response(200, json={"tag_name": "v9.0.0"}))
    store = Store(tmp_path / "s.db")
    d1, d2 = date(2026, 11, 1), date(2026, 11, 2)
    assert upd.due_offer(store, today=d1)["latest"] == "9.0.0"
    assert upd.due_offer(store, today=d1) is None and route.call_count == 1    # раз в день
    assert upd.due_offer(store, today=d2) is None                              # про 9.0.0 уже сказали
    upd.snooze(store, today=d2)
    assert upd.due_offer(store, today=date(2026, 11, 4)) is None               # отложено на 3 дня
    assert upd.due_offer(store, today=date(2026, 11, 5))["latest"] == "9.0.0"  # срок вышел — снова
    assert "9.0.0" in upd.offer_text({"latest": "9.0.0", "current": "1.0", "notes": "", "url": ""})
    store.close()


def test_bot_buttons_snooze_and_start(tmp_path, monkeypatch):
    store = Store(tmp_path / "s.db")
    text, _ = upd.handle_callback(store, "upd:snooze")
    assert "напомню" in text and store.get_meta(upd.SNOOZE_META)
    monkeypatch.setattr(upd, "start_detached", lambda **kw: 4242)
    assert "4242" in upd.handle_callback(store, "upd:go")[0]
    store.close()
