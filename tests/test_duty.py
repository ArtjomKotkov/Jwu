"""Режим дежурства: клиент Matrix, настройка кнопками, тумблер /duty, проверка комнаты демоном."""

import json

import httpx
import pytest
import respx

from jwu.core import bot, daemon, duty, matrix, notify, workspaces
from jwu.core.config import Config
from jwu.core.store import Store

HS = "https://mx.test"
TG = "https://api.telegram.org"
ME = "@me:mx.test"
ROOM_A = "!a:mx.test"
ROOM_B = "!b:mx.test"


@pytest.fixture(autouse=True)
def _no_mcp_config(tmp_path, monkeypatch):
    # реальный конфиг matrix-mcp на машине разработчика не должен влиять на тесты
    monkeypatch.setenv("JWU_MATRIX_MCP_CONFIG", str(tmp_path / "nope.json"))
    monkeypatch.delenv("MATRIX_TOKEN", raising=False)


def _env(tmp_path):
    store = Store(tmp_path / "s.db")
    ws = store.get_workspace_by_slug("work")
    store.use_workspace(ws.id)
    cfg = Config()
    cfg.telegram.chat_id = "42"
    cfg.matrix.homeserver = HS
    store.set_workspace_secret(ws.id, "matrix.token", "mtok")
    from jwu.core import secrets
    cfg.secrets = secrets.DbSecrets(store, ws.id)
    return store, ws, cfg


DM = "!dm:mx.test"
OTHER = "!other:mx.test"


def _mock_matrix(rooms=None, direct=None):
    """Homeserver: я, комнаты, /sync (без since — точка отсчёта s0 и m.direct; s0 → rooms; дальше пусто)."""
    api = f"{HS}/_matrix/client/v3"
    respx.get(f"{api}/account/whoami").mock(return_value=httpx.Response(200, json={"user_id": ME}))
    respx.get(f"{api}/joined_rooms").mock(return_value=httpx.Response(200, json={"joined_rooms": [ROOM_B, ROOM_A]}))
    respx.get(url__regex=rf"{api}/rooms/.*a%3Amx\.test/state/m\.room\.name").mock(
        return_value=httpx.Response(200, json={"name": "Поддержка продаж"}))
    respx.get(url__regex=rf"{api}/rooms/.*b%3Amx\.test/state/m\.room\.name").mock(
        return_value=httpx.Response(200, json={"name": "Болталка"}))
    respx.get(url__regex=rf"{api}/rooms/.*other%3Amx\.test/state/m\.room\.name").mock(
        return_value=httpx.Response(200, json={"name": "Релизы"}))
    respx.get(url__regex=rf"{api}/profile/.*/displayname").mock(
        side_effect=lambda req: httpx.Response(200, json={"displayname": {
            "%40me%3Amx.test": "Я Сам", "%40ivan%3Amx.test": "Иван Петров"}.get(
                req.url.raw_path.decode().split("/")[-2], "")}))
    direct = direct if direct is not None else {"@olga:mx.test": [DM]}

    def sync(req):
        since = req.url.params.get("since")
        if not since:
            return httpx.Response(200, json={"next_batch": "s0", "account_data": {"events": [
                {"type": "m.direct", "content": direct}]}})
        if since == "s0":
            join = {rid: {"timeline": {"events": evs, "limited": False},
                          "unread_notifications": {"notification_count": unread}}
                    for rid, (evs, unread) in (rooms or {}).items()}
            return httpx.Response(200, json={"next_batch": "s1", "rooms": {"join": join}})
        return httpx.Response(200, json={"next_batch": since})

    return respx.get(f"{api}/sync").mock(side_effect=sync)


def _mock_events(extra):
    api = f"{HS}/_matrix/client/v3"
    for eid, ev in extra.items():
        respx.get(url__regex=rf"{api}/rooms/[^/]+/event/{eid.replace('$', '%24')}$").mock(
            return_value=httpx.Response(200, json=ev))


def _ev(eid, sender, body, *, thread=None, reply=None, mentions=None):
    content = {"msgtype": "m.text", "body": body}
    rel = {}
    if thread:
        rel = {"rel_type": "m.thread", "event_id": thread, "is_falling_back": True}
    if reply:
        rel["m.in_reply_to"] = {"event_id": reply}
    if rel:
        content["m.relates_to"] = rel
    if mentions:
        content["m.mentions"] = {"user_ids": mentions}
    return {"event_id": eid, "sender": sender, "type": "m.room.message", "content": content,
            "origin_server_ts": 1}


def _factory(cfg):
    return lambda: matrix.client_from_config(cfg)


@respx.mock
def test_settings_menu_by_buttons_first_room_enables(tmp_path):
    store, ws, cfg = _env(tmp_path)
    _mock_matrix()
    text, kb = duty.menu(duty.load(store))
    assert "комната не выбрана" in text
    assert kb["inline_keyboard"][0][0]["callback_data"] == "duty:rooms"

    text, kb = duty.handle_callback(store, _factory(cfg), "duty:rooms")
    labels = [row[0]["text"] for row in kb["inline_keyboard"]]
    assert labels == ["Болталка", "Поддержка продаж", "🔄 обновить список", "← назад"]   # по имени

    text, kb = duty.handle_callback(store, _factory(cfg), "duty:room:1")
    state = duty.load(store)
    assert state.room_id == ROOM_A and state.active and state.since == "s0" and state.me == ME
    assert "Дежурство: 🟢 включено · Поддержка продаж" in text
    assert kb["inline_keyboard"][0][0]["callback_data"] == "duty:off"
    assert duty.dm_rooms(store) == {DM}

    text, kb = duty.handle_callback(store, _factory(cfg), "duty:off")
    assert "Дежурство: 🔕 выключено" in text and not duty.load(store).active
    assert duty.load(store).room_id == ROOM_A and duty.load(store).since == ""   # комната осталась
    assert kb["inline_keyboard"][0][0]["callback_data"] == "duty:on"
    assert "дежурка" not in text

    text, kb = duty.handle_callback(store, lambda: pytest.fail("список из кеша"), "duty:rooms")
    assert "За какой комнатой" in text and kb["inline_keyboard"][-2][0]["callback_data"] == "duty:refresh"
    store.close()


@respx.mock
def test_toggle_command(tmp_path):
    store, ws, cfg = _env(tmp_path)
    _mock_matrix()
    text, kb = duty.toggle(store, _factory(cfg))          # комнаты нет → выбор комнаты
    assert "За какой комнатой" in text and kb["inline_keyboard"]
    duty.set_room(store, matrix.client_from_config(cfg), matrix.Room(ROOM_A, "Поддержка продаж"))
    text, kb = duty.toggle(store, _factory(cfg))
    assert text.startswith("🟢 Дежурство включено") and duty.load(store).active and not kb
    text, kb = duty.toggle(store, _factory(cfg))
    assert text.startswith("🔕 Дежурство выключено") and not duty.load(store).active
    # без сети включается всё равно: позицию «сейчас» выставит первая проверка демона
    text, _ = duty.toggle(store, lambda: None)
    assert text.startswith("🟢 Дежурство включено") and duty.load(store).since == ""
    store.close()


@respx.mock
def test_personal_is_independent_of_duty(tmp_path):
    store, ws, cfg = _env(tmp_path)
    _mock_matrix({
        DM: ([_ev("$d1", "@olga:mx.test", "привет, глянь PR"), _ev("$d2", ME, "щас")], 2),
        "!dm2:mx.test": ([_ev("$d3", "@petr:mx.test", "уже прочитал")], 0),
        OTHER: ([_ev("$o1", "@ivan:mx.test", "Я Сам, релиз сегодня?"), _ev("$o2", "@ivan:mx.test", "без меня")], 1),
        ROOM_A: ([_ev("$a1", "@ivan:mx.test", "в дежурство")], 1),
    }, direct={"@olga:mx.test": [DM], "@petr:mx.test": ["!dm2:mx.test"]})
    text, kb = duty.handle_callback(store, _factory(cfg), "duty:personal")
    state = duty.load(store)
    assert state.personal and not state.active and state.watching and state.since == "s0"
    assert "Личное (1:1 и упоминания): 🟢 включено" in text
    client = matrix.client_from_config(cfg)
    state, items, end = duty.check(store, client)
    client.close()
    assert end == "s1"
    assert [(i.who, i.direct, i.room) for i in items] == [
        ("olga", True, ""), ("Иван Петров", False, "Релизы")]       # прочитанное, своё и дежурство — мимо
    text = duty.format_items(state, items)
    assert "💬 <b>личные</b>" in text and "📣 <b>упоминание · Релизы</b>" in text and "дежурство ·" not in text
    text, _ = duty.handle_callback(store, lambda: None, "duty:personal")
    assert "🔕 выключено" in text and not duty.load(store).watching and duty.load(store).since == ""
    store.close()


def test_stale_button_and_cancel_need_no_network(tmp_path):
    store, ws, cfg = _env(tmp_path)
    text, _ = duty.handle_callback(store, lambda: None, "duty:menu")
    assert "комната не выбрана" in text
    text, _ = duty.handle_callback(store, lambda: None, "duty:rooms")
    assert "Matrix не настроен" in text
    store.close()


@respx.mock
def test_check_classifies_thread_reply_mention_and_skips_own(tmp_path):
    store, ws, cfg = _env(tmp_path)
    events = [
        _ev("$1", "@ivan:mx.test", "Коллеги, у клиента пропадают сообщения после перезагрузки " * 5),
        _ev("$2", ME, "моё — пропускаем"),
        _ev("$3", "@ivan:mx.test", "посмотрел логи", thread="$root"),
        _ev("$4", "@olga:mx.test", "да, это оно", reply="$mine"),
        _ev("$5", "@olga:mx.test", "Я Сам, глянь пожалуйста"),
        _ev("$6", "@olga:mx.test", "просто так", mentions=[ME]),
        {"event_id": "$7", "sender": "@olga:mx.test", "type": "m.reaction", "content": {}},
    ]
    extra = {"$root": _ev("$root", ME, "Не грузится виджет на сайте клиента, разбираюсь"),
             "$mine": _ev("$mine", ME, "проверьте кеш")}
    _mock_matrix({ROOM_A: (events, 0)})
    _mock_events(extra)
    client = matrix.client_from_config(cfg)
    duty.set_room(store, client, matrix.Room(ROOM_A, "Поддержка продаж"))
    duty.enable(store, client)
    state, items, end = duty.check(store, client)
    assert end == "s1" and duty.load(store).since == "s1"
    assert [i.who for i in items] == ["Иван Петров", "Иван Петров", "olga", "olga", "olga"]
    assert len(items[0].body) <= duty.BODY_MAX and items[0].body.endswith("…")
    assert items[0].thread == "" and items[0].to_me == ""
    assert items[1].thread.startswith("Не грузится виджет") and items[1].to_me == "мой тред"
    assert items[2].to_me == "ответ мне"
    assert items[3].to_me == "упоминание" and items[4].to_me == "упоминание"
    text = duty.format_items(state, items)
    assert text.startswith("🟢 <b>дежурство · Поддержка продаж</b>") and "🧵 в треде «Не грузится" in text
    assert duty.check(store, client)[1] == []                      # позиция сдвинута — повтора нет
    client.close()
    store.close()


@respx.mock
def test_poll_keeps_position_when_telegram_fails(tmp_path):
    store, ws, cfg = _env(tmp_path)
    _mock_matrix({ROOM_A: ([_ev("$1", "@ivan:mx.test", "есть кто?")], 1)})
    client = matrix.client_from_config(cfg)
    duty.set_room(store, client, matrix.Room(ROOM_A, "Поддержка продаж"))
    duty.enable(store, client)
    client.close()

    class Broken:
        def send(self, text, **kw):
            raise notify.NotifyError("лежит")

    with pytest.raises(notify.NotifyError):
        duty.poll(store, _factory(cfg), Broken())
    assert duty.load(store).since == "s0"

    sent = []

    class Ok:
        def send(self, text, **kw):
            sent.append(text)

    assert duty.poll(store, _factory(cfg), Ok()) == 1
    assert "есть кто?" in sent[0] and duty.load(store).since == "s1"
    duty.disable(store)
    assert duty.poll(store, lambda: pytest.fail("сеть при выключенном"), Ok()) == 0
    store.close()


@respx.mock
def test_bot_duty_command_and_button_edit(tmp_path, monkeypatch):
    store, ws, cfg = _env(tmp_path)
    _mock_matrix()
    respx.get(f"{TG}/bottok/getUpdates").mock(return_value=httpx.Response(200, json={"ok": True, "result": [
        {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/matrix", "message_id": 10}},
        {"update_id": 2, "callback_query": {"id": "cb1", "data": "duty:rooms",
                                             "message": {"chat": {"id": 42}, "message_id": 11}}},
        {"update_id": 3, "callback_query": {"id": "cb2", "data": "duty:room:1",
                                             "message": {"chat": {"id": 42}, "message_id": 11}}},
        {"update_id": 4, "callback_query": {"id": "cb3", "data": "duty:off",
                                             "message": {"chat": {"id": 7}, "message_id": 11}}},
        {"update_id": 5, "message": {"chat": {"id": 42}, "text": "/duty", "message_id": 12}},
    ]}))
    sent = respx.post(f"{TG}/bottok/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 99}}))
    edited = respx.post(f"{TG}/bottok/editMessageText").mock(return_value=httpx.Response(200, json={"ok": True}))
    answered = respx.post(f"{TG}/bottok/answerCallbackQuery").mock(return_value=httpx.Response(200, json={"ok": True}))
    sender = notify.TelegramNotifier("tok", "42")
    try:
        handled = bot.process_updates(store, ws, cfg, sender)
    finally:
        sender.close()
    assert [h["kind"] for h in handled] == ["command", "button", "button", "command"]
    first = json.loads(sent.calls[0].request.content)
    assert "комната не выбрана" in first["text"] and first["reply_markup"]["inline_keyboard"]
    edits = [json.loads(c.request.content) for c in edited.calls]
    assert any(e["text"] == "⏳ делаю…" for e in edits)              # долгое — сразу видно, что жив
    edits = [e for e in edits if e["text"] != "⏳ делаю…"]
    assert edits[0]["message_id"] == 11 and "За какой комнатой" in edits[0]["text"]
    assert "Дежурство: 🟢 включено" in edits[1]["text"]
    assert answered.call_count == 2                       # чужой чат — мимо
    last = json.loads(sent.calls[1].request.content)
    assert last["text"].startswith("🔕 Дежурство выключено")
    assert store.get_workspace_meta(notify.OFFSET_META) == "5"
    store.close()


@respx.mock
def test_daemon_poll_bots_sends_duty_news(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    store = Store(tmp_path / "s.db")
    work = store.get_workspace_by_slug("work")
    store.set_workspace_settings(work.id, {"telegram.chat_id": "42", "matrix.homeserver": HS})
    store.set_workspace_secret(work.id, "matrix.token", "mtok")
    _mock_matrix({ROOM_A: ([_ev("$1", "@ivan:mx.test", "помогите")], 1)})
    store.use_workspace(work.id)
    cfg = workspaces.config_for_workspace(store, work)
    client = matrix.client_from_config(cfg)
    duty.set_room(store, client, matrix.Room(ROOM_A, "Поддержка продаж"))
    duty.enable(store, client)
    client.close()
    sent = respx.post(f"{TG}/bottok/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}))
    handled = daemon.poll_duty(store)
    assert handled == [{"workspace": "work", "kind": "duty", "count": 1}]
    assert "помогите" in json.loads(sent.calls[0].request.content)["text"]
    store.close()


def test_access_falls_back_to_matrix_mcp_config(tmp_path, monkeypatch):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"homeserver": "https://riot.x/", "user_id": "@u:x", "access_token": "t"}))
    monkeypatch.setenv("JWU_MATRIX_MCP_CONFIG", str(path))
    cfg = Config()
    assert matrix.access_from_config(cfg) == ("https://riot.x", "@u:x", "t", "matrix-mcp")
    path.write_text("{битый")
    assert matrix.access_from_config(cfg)[3] == ""
    assert matrix.client_from_config(cfg) is None


@respx.mock
def test_room_pages_and_name_cache(tmp_path, monkeypatch):
    store, ws, cfg = _env(tmp_path)
    api = f"{HS}/_matrix/client/v3"
    ids = [f"!r{i:02d}:mx.test" for i in range(30)] + ["!dm:mx.test"]
    respx.get(f"{api}/joined_rooms").mock(return_value=httpx.Response(200, json={"joined_rooms": ids}))
    names = respx.get(url__regex=rf"{api}/rooms/[^/]+/state/m\.room\.name").mock(
        side_effect=lambda req: httpx.Response(404, json={}) if "dm" in str(req.url)
        else httpx.Response(200, json={"name": "Комната " + str(req.url).split("%21r")[1][:2]}))
    respx.get(url__regex=rf"{api}/rooms/[^/]+/state/m\.room\.canonical_alias").mock(
        return_value=httpx.Response(404, json={}))
    client = matrix.client_from_config(cfg)
    text, kb = duty.rooms_keyboard(store, client)
    assert "Комнат: 30" in text                                        # личка без имени — не в кнопках
    rows = kb["inline_keyboard"]
    assert len(rows) == duty.PAGE_SIZE + 3 and rows[0][0]["text"] == "Комната 00"
    assert [b["text"] for b in rows[-3]] == ["1/3", "▶"]
    calls = names.call_count
    duty.list_rooms(store, client)
    assert names.call_count == calls                                   # имена — из кеша, сети нет
    duty.list_rooms(store, client, refresh=True)
    assert names.call_count == calls                                   # обновление — только новые
    client.close()
    text, kb = duty.handle_callback(store, lambda: pytest.fail("листание без сети"), "duty:page:2")
    rows = kb["inline_keyboard"]
    assert rows[0][0]["text"] == "Комната 24" and rows[0][0]["callback_data"] == "duty:room:24"
    assert [b["text"] for b in rows[-3]] == ["◀", "3/3"]
    store.close()


def test_pick_room():
    rooms = [matrix.Room("!1:x", "Болталка"), matrix.Room("!2:x", "Поддержка продаж"),
             matrix.Room("!3:x", "Поддержка партнёров")]
    assert duty.pick_room(rooms, "2").id == "!2:x"
    assert duty.pick_room(rooms, "продаж").id == "!2:x"
    assert duty.pick_room(rooms, "поддержка") is None          # неоднозначно
    assert duty.pick_room(rooms, "!3:x").id == "!3:x"
    assert duty.pick_room(rooms, "9") is None


def test_client_never_posts():
    assert not [m for m in dir(matrix.MatrixClient) if m.startswith(("send", "post", "put", "reply"))]


def test_edit_is_not_a_new_message():
    """Правку (m.replace) не пересказываем второй раз: об исходном сообщении уже сказали."""
    edit = {"event_id": "$e", "sender": "@olga:mx.test", "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "* исправленный текст",
                        "m.new_content": {"msgtype": "m.text", "body": "исправленный текст"},
                        "m.relates_to": {"rel_type": "m.replace", "event_id": "$orig"},
                        "m.mentions": {"user_ids": [ME]}}}
    ev = matrix.parse_event(edit)
    assert ev.replaces == "$orig" and not ev.is_message
    assert matrix.parse_event(_ev("$1", "@olga:mx.test", "текст")).is_message
