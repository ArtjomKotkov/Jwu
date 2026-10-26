"""Matrix в Telegram-боте: дежурство за комнатой и личное — две независимые слежки.

- **Дежурство** (``/duty`` — тумблер) — одна комната целиком: все новые чужие сообщения,
  от кого, в треде ли (и каком), ответ мне / мой тред / упоминание, обрезанный текст.
- **Личное** (``/personal`` — тумблер) — работает и без дежурства: непрочитанное из
  диалогов один на один (``m.direct``) и упоминания меня в остальных комнатах. «Непрочитанное»
  — счётчик Matrix на момент проверки: прочитал в Element — не дёргаем.

Настройка — ``/matrix``, вся кнопками: оба тумблера, выбор комнаты дежурства по страницам.
Демон между проходами (вместе с опросом бота, раз в ~``poll_interval`` секунд) делает один
инкрементальный ``/sync`` на обе слежки и шлёт одно сообщение.

Свои сообщения пропускаются. Matrix только читается — отвечает человек. Позиция ``/sync``
сдвигается только после отправки: лёг Telegram — сообщения придут позже, а не пропадут.

Состояние — в meta воркспейса (``duty:*``): комната, включено ли, личные, позиция, свой
mxid, личные комнаты, кеш имён людей и комнат. Список комнат перечитывается редко: из кеша,
заново — только кнопкой «обновить» или ``jwu duty rooms --refresh``.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .matrix import Event, MatrixClient, MatrixError, Room, SyncResult, sort_rooms

if TYPE_CHECKING:
    from .store import Store

ROOM_META = "duty:room"
ROOM_NAME_META = "duty:room_name"
ENABLED_META = "duty:enabled"
PERSONAL_META = "duty:personal"       # "1" — личное (1:1 и упоминания) включено
SINCE_META = "duty:since"             # next_batch инкрементального /sync
ME_META = "duty:me"
ME_NAME_META = "duty:me_name"
DM_ROOMS_META = "duty:dm_rooms"       # [room_id] личных диалогов (из m.direct)
CHOICES_META = "duty:choices"
NAMES_META = "duty:names"
ROOMS_CACHE_META = "duty:rooms_cache"   # {room_id: имя} — список комнат перечитывается редко

PAGE_SIZE = 12          # комнат на странице выбора; стрелки — листать
BUTTON_MAX = 40
BODY_MAX = 160
THREAD_MAX = 40
DM_PER_ROOM = 3         # из одного лично диалога — последние N сообщений
CALLBACK_PREFIX = "duty:"
# callback_data: duty:on / off / personal / rooms / refresh / page:<n> / room:<i> / menu
CB_ON, CB_OFF, CB_PERSONAL, CB_ROOMS, CB_REFRESH, CB_MENU = "on", "off", "personal", "rooms", "refresh", "menu"
CB_ROOM, CB_PAGE = "room:", "page:"


@dataclass
class State:
    room_id: str = ""
    room_name: str = ""
    enabled: bool = False
    personal: bool = False
    since: str = ""
    me: str = ""
    me_name: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.room_id)

    @property
    def active(self) -> bool:
        return self.enabled and self.configured

    @property
    def watching(self) -> bool:
        """Есть ли за чем следить: дежурство или личное."""
        return self.active or self.personal

    @property
    def title(self) -> str:
        return self.room_name or self.room_id


def load(store: "Store") -> State:
    g = store.get_workspace_meta
    return State(room_id=g(ROOM_META) or "", room_name=g(ROOM_NAME_META) or "",
                 enabled=(g(ENABLED_META) or "") == "1", personal=(g(PERSONAL_META) or "") == "1",
                 since=g(SINCE_META) or "", me=g(ME_META) or "", me_name=g(ME_NAME_META) or "")


def _json_meta(store: "Store", key: str, default):
    try:
        value = json.loads(store.get_workspace_meta(key) or "null")
    except ValueError:
        return default
    return default if value is None else value


def dm_rooms(store: "Store") -> set[str]:
    return set(_json_meta(store, DM_ROOMS_META, []))


def _save_direct(store: "Store", result: SyncResult) -> None:
    rooms = result.direct_rooms
    if rooms is not None:
        store.set_workspace_meta(DM_ROOMS_META, json.dumps(sorted(rooms)))


def _remember_me(store: "Store", client: MatrixClient) -> None:
    me = client.whoami()
    store.set_workspace_meta(ME_META, me)
    store.set_workspace_meta(ME_NAME_META, client.display_name(me) if me else "")


def _reset_position(store: "Store", client: MatrixClient) -> None:
    """Точка отсчёта «сейчас»: старое не пересказываем. Заодно — карта личных."""
    result = client.sync()
    _save_direct(store, result)
    store.set_workspace_meta(SINCE_META, result.next_batch)


def set_room(store: "Store", client: MatrixClient, room: Room) -> State:
    """Настройка: запомнить комнату дежурства и себя."""
    _remember_me(store, client)
    store.set_workspace_meta(ROOM_META, room.id)
    store.set_workspace_meta(ROOM_NAME_META, room.name)
    store.set_workspace_meta(CHOICES_META, "")
    return load(store)


def _switch(store: "Store", key: str, on: bool, client: Optional[MatrixClient]) -> State:
    """Включить/выключить одну из слежек. Позиция /sync общая: при старте с нуля — «сейчас»
    (старое не пересказываем), при выключении последней — сброс. Без клиента позицию
    выставит первая проверка демона."""
    was = load(store).watching
    store.set_workspace_meta(key, "1" if on else "")
    now = load(store)
    if not now.watching:
        store.set_workspace_meta(SINCE_META, "")
    elif not was:
        store.set_workspace_meta(SINCE_META, "")
        if client is not None:
            if not now.me:
                _remember_me(store, client)
            _reset_position(store, client)
    return load(store)


def enable(store: "Store", client: Optional[MatrixClient] = None) -> State:
    return _switch(store, ENABLED_META, True, client)


def disable(store: "Store") -> State:
    return _switch(store, ENABLED_META, False, None)


def set_personal(store: "Store", on: bool, client: Optional[MatrixClient] = None) -> State:
    return _switch(store, PERSONAL_META, on, client)


# --------------------------------------------------------------------------- #
# Список комнат: редко и из кеша
# --------------------------------------------------------------------------- #


def cached_rooms(store: "Store") -> list[Room]:
    cache = _json_meta(store, ROOMS_CACHE_META, {})
    return sort_rooms([Room(id=rid, name=name) for rid, name in cache.items()])


def list_rooms(store: "Store", client: Optional[MatrixClient], *, refresh: bool = False) -> list[Room]:
    """Мои комнаты с именами. Есть кеш и не просили обновить — сеть не трогаем вовсе.

    Обновление спрашивает имена только у новых комнат (вышел из комнаты — забываем).
    """
    cache: dict[str, str] = _json_meta(store, ROOMS_CACHE_META, {})
    if cache and not refresh or client is None:
        return cached_rooms(store)
    ids = client.joined_room_ids()
    missing = [rid for rid in ids if rid not in cache]
    if missing:
        cache.update(client.room_names(missing))
    cache = {rid: cache.get(rid, "") for rid in ids}
    store.set_workspace_meta(ROOMS_CACHE_META, json.dumps(cache, ensure_ascii=False))
    return cached_rooms(store)


def pick_room(rooms: list[Room], query: str) -> Optional[Room]:
    """Комната по номеру (с 1), id или части названия; неоднозначно — None."""
    q = query.strip()
    if q.isdigit() and 1 <= int(q) <= len(rooms):
        return rooms[int(q) - 1]
    for r in rooms:
        if r.id == q or r.name.lower() == q.lower():
            return r
    hits = [r for r in rooms if q.lower() in r.title.lower()]
    return hits[0] if len(hits) == 1 else None


def _choices(store: "Store") -> list[Room]:
    raw = _json_meta(store, CHOICES_META, [])
    return [Room(id=str(r[0]), name=str(r[1])) for r in raw if isinstance(r, list) and len(r) == 2]


# --------------------------------------------------------------------------- #
# Настройка кнопками
# --------------------------------------------------------------------------- #


def _cb(action: str) -> str:
    return f"{CALLBACK_PREFIX}{action}"


def _btn(text: str, action: str) -> dict:
    return {"text": text, "callback_data": _cb(action)}


def _button_text(room: Room) -> str:
    title = room.title
    return title if len(title) <= BUTTON_MAX else title[:BUTTON_MAX - 1] + "…"


def menu(state: State) -> tuple[str, dict]:
    """Меню настройки Matrix: обе слежки и комната дежурства."""
    if state.configured:
        duty_line = f"{'🟢 включено' if state.active else '🔕 выключено'} · {html.escape(state.title)}"
    else:
        duty_line = "комната не выбрана"
    text = ("📡 <b>Matrix</b>\n"
            f"Дежурство: {duty_line}\n"
            f"Личное (1:1 и упоминания): {'🟢 включено' if state.personal else '🔕 выключено'}")
    if not state.configured:
        duty_btn = _btn("🏠 Дежурство: выбрать комнату", CB_ROOMS)
    else:
        duty_btn = _btn("🔕 Дежурство: выключить", CB_OFF) if state.active else _btn("🟢 Дежурство: включить", CB_ON)
    personal_btn = _btn("🔕 Личное: выключить" if state.personal else "🟢 Личное: включить", CB_PERSONAL)
    rows = [[duty_btn], [personal_btn]]
    if state.configured:
        rows.append([_btn("🏠 Сменить комнату дежурства", CB_ROOMS)])
    return text, {"inline_keyboard": rows}


def rooms_keyboard(store: "Store", client: Optional[MatrixClient], *, page: int = 0,
                   refresh: bool = False) -> tuple[str, dict]:
    """Комнаты с названием кнопками, по страницам. ``client=None`` — листаем запомненное.

    Личные диалоги (комнаты без названия) в кнопки не попадают — их сотни, а дежурят в
    общих; их непрочитанное и так приходит, если личные включены.
    """
    if client is not None or refresh:
        rooms = [r for r in list_rooms(store, client, refresh=refresh) if r.name]
        store.set_workspace_meta(CHOICES_META, json.dumps([[r.id, r.name] for r in rooms], ensure_ascii=False))
    else:
        rooms = _choices(store)
    tail = [[_btn("🔄 обновить список", CB_REFRESH)], [_btn("← назад", CB_MENU)]]
    if not rooms:
        return "Комнат с названием не нашёл. Обновить список?", {"inline_keyboard": tail}
    pages = (len(rooms) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    first = page * PAGE_SIZE
    rows = [[_btn(_button_text(r), f"{CB_ROOM}{first + i}")] for i, r in enumerate(rooms[first:first + PAGE_SIZE])]
    nav = []
    if page > 0:
        nav.append(_btn("◀", f"{CB_PAGE}{page - 1}"))
    if pages > 1:
        nav.append(_btn(f"{page + 1}/{pages}", f"{CB_PAGE}{page}"))
    if page < pages - 1:
        nav.append(_btn("▶", f"{CB_PAGE}{page + 1}"))
    if nav:
        rows.append(nav)
    return f"🏠 За какой комнатой следить? Комнат: {len(rooms)}", {"inline_keyboard": rows + tail}


NOT_CONFIGURED = ("Matrix не настроен: <code>jwu configure --matrix-host … --matrix-token …</code> "
                  "(или конфиг matrix-mcp на этой машине).")


def _with_client(client_factory, fn) -> tuple[str, dict]:
    client = client_factory()
    if client is None:
        return NOT_CONFIGURED, {}
    try:
        with client:
            return fn(client)
    except MatrixError as exc:
        return f"Matrix не отвечает: {html.escape(str(exc))}", {}


def handle_callback(store: "Store", client_factory, data: str) -> tuple[str, dict]:
    """Нажатие кнопки → (текст, клавиатура), которыми заменить сообщение с меню.

    ``client_factory`` — ленивый конструктор MatrixClient: сеть только там, где без неё никак.
    """
    action = data[len(CALLBACK_PREFIX):] if data.startswith(CALLBACK_PREFIX) else data
    if action == CB_MENU:
        return menu(load(store))
    if action == CB_OFF:
        return menu(disable(store))
    if action == CB_PERSONAL:
        on = not load(store).personal
        if not on:
            return menu(set_personal(store, False))
        return _with_client(client_factory, lambda c: menu(set_personal(store, True, c)))
    if action.startswith(CB_PAGE):
        try:
            return rooms_keyboard(store, None, page=int(action[len(CB_PAGE):]))
        except ValueError:
            return menu(load(store))
    if action == CB_ROOMS and cached_rooms(store):
        return rooms_keyboard(store, None, refresh=True)    # из кеша: сети нет
    if action in (CB_ROOMS, CB_REFRESH):
        return _with_client(client_factory, lambda c: rooms_keyboard(store, c, refresh=True))
    if action == CB_ON:
        if not load(store).configured:
            return handle_callback(store, client_factory, _cb(CB_ROOMS))
        return _with_client(client_factory, lambda c: menu(enable(store, c)))
    if action.startswith(CB_ROOM):
        try:
            room = _choices(store)[int(action[len(CB_ROOM):])]
        except (ValueError, IndexError):
            return "Список устарел — открой заново.", {"inline_keyboard": [[_btn("🏠 Выбрать комнату", CB_ROOMS)]]}

        def pick(client: MatrixClient) -> tuple[str, dict]:
            first = not load(store).configured
            state = set_room(store, client, room)
            if first:   # первая настройка — сразу и включаем: ради этого её и открыли
                state = enable(store, client)
            return menu(state)

        return _with_client(client_factory, pick)
    return "Не знаю такую кнопку.", {}


def toggle(store: "Store", client_factory) -> tuple[str, dict]:
    """``/duty``: тумблер. Комната не настроена — открыть её выбор."""
    state = load(store)
    if not state.configured:
        return handle_callback(store, client_factory, _cb(CB_ROOMS))
    if state.active:
        disable(store)
        return f"🔕 Дежурство выключено ({html.escape(state.title)}). /duty — включить.", {}
    client = client_factory()
    try:
        if client is None:
            state = enable(store)
        else:
            with client:
                state = enable(store, client)
    except MatrixError:
        state = enable(store)   # позицию выставит демон
    return (f"🟢 Дежурство включено: {html.escape(state.title)}. Новые сообщения пришлю сюда. "
            "/duty — выключить, /matrix — настройка."), {}


def toggle_personal(store: "Store", client_factory) -> tuple[str, dict]:
    """``/personal``: тумблер личного (1:1 и упоминания), независимо от дежурства."""
    if load(store).personal:
        set_personal(store, False)
        return "🔕 Личное выключено. /personal — включить.", {}
    client = client_factory()
    if client is None:
        return NOT_CONFIGURED, {}
    try:
        with client:
            set_personal(store, True, client)
    except MatrixError:
        set_personal(store, True)
    return ("🟢 Личное включено: непрочитанное из диалогов 1:1 и упоминания в чатах пришлю сюда. "
            "/personal — выключить."), {}


# --------------------------------------------------------------------------- #
# Проверка новых сообщений
# --------------------------------------------------------------------------- #


def _short(text: str, limit: int) -> str:
    # markdown из Matrix (**жирный**, `код`, > цитата) в коротком пересказе только мешает
    text = re.sub(r"[*_`~]{1,3}|^>\s?", "", text or "", flags=re.M)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _localpart(mxid: str) -> str:
    return mxid[1:].split(":", 1)[0] if mxid.startswith("@") else mxid


class _Names:
    """Кеш отображаемых имён в meta воркспейса — чтобы не дёргать профиль на каждое сообщение."""

    def __init__(self, store: "Store", client: MatrixClient) -> None:
        self._store, self._client = store, client
        self._cache: dict[str, str] = _json_meta(store, NAMES_META, {})
        self._dirty = False

    def get(self, mxid: str) -> str:
        if mxid not in self._cache:
            self._cache[mxid] = self._client.display_name(mxid) or _localpart(mxid)
            self._dirty = True
        return self._cache[mxid]

    def save(self) -> None:
        if self._dirty:
            self._store.set_workspace_meta(NAMES_META, json.dumps(self._cache, ensure_ascii=False))


@dataclass
class Item:
    who: str
    body: str
    thread: str = ""       # заголовок треда (кусок корневого сообщения)
    to_me: str = ""        # «ответ мне» / «упоминание» / «мой тред»
    direct: bool = False   # из личного диалога
    room: str = ""         # упоминание в другой комнате — её имя

    def html(self) -> str:
        head = f"👤 <b>{html.escape(self.who)}</b>"
        if self.thread:
            head += f"  🧵 в треде «{html.escape(self.thread)}»"
        if self.to_me:
            head += f"  ↩️ <b>{html.escape(self.to_me)}</b>"
        return f"{head}\n   <i>{html.escape(self.body)}</i>"


def _mentions_me(ev: Event, state: State) -> bool:
    if state.me and state.me in ev.mentions:
        return True
    low = ev.body.lower()
    needles = [n for n in (state.me, state.me_name, _localpart(state.me)) if n and len(n) > 2]
    return any(n.lower() in low for n in needles)


def classify(events: list[Event], state: State, client: MatrixClient, names: _Names) -> list[Item]:
    """Новые чужие сообщения комнаты дежурства → пункты. События по id тянутся один раз."""
    seen: dict[str, Optional[Event]] = {}

    def fetch(event_id: str) -> Optional[Event]:
        if event_id not in seen:
            seen[event_id] = client.event(state.room_id, event_id)
        return seen[event_id]

    items: list[Item] = []
    for ev in events:
        if not ev.is_message or ev.sender == state.me:
            continue
        to_me = ""
        thread = ""
        if ev.thread_root:
            root = fetch(ev.thread_root)
            if root is not None:
                thread = _short(root.body, THREAD_MAX)
                if root.sender == state.me:
                    to_me = "мой тред"
        if ev.reply_to and ev.reply_to != ev.thread_root:
            parent = fetch(ev.reply_to)
            if parent is not None and parent.sender == state.me:
                to_me = "ответ мне"
        if not to_me and _mentions_me(ev, state):
            to_me = "упоминание"
        items.append(Item(who=names.get(ev.sender), body=_short(ev.body, BODY_MAX),
                          thread=thread, to_me=to_me))
    return items


class _RoomNames:
    """Имя комнаты для упоминания: из кеша списка комнат, нет там — спросить один раз."""

    def __init__(self, store: "Store", client: MatrixClient) -> None:
        self._store, self._client = store, client
        self._cache: dict[str, str] = _json_meta(store, ROOMS_CACHE_META, {})
        self._dirty = False

    def get(self, room_id: str) -> str:
        if not self._cache.get(room_id):
            self._cache[room_id] = self._client.room_name(room_id)
            self._dirty = True
        return self._cache[room_id] or room_id

    def save(self) -> None:
        if self._dirty:
            self._store.set_workspace_meta(ROOMS_CACHE_META, json.dumps(self._cache, ensure_ascii=False))


def personal_items(result: SyncResult, state: State, dms: set[str], names: _Names,
                   room_names: "_RoomNames") -> list[Item]:
    """Личное: непрочитанное из 1:1 (по DM_PER_ROOM последних с диалога) и упоминания меня
    в остальных комнатах, пока они не прочитаны. Комната дежурства — не здесь: её и так
    пересказываем целиком."""
    items: list[Item] = []
    for rid, upd in result.rooms.items():
        if rid == state.room_id and state.active or upd.unread <= 0:
            continue
        theirs = [e for e in upd.events if e.is_message and e.sender != state.me]
        if rid in dms:
            for ev in theirs[-DM_PER_ROOM:]:
                items.append(Item(who=names.get(ev.sender), body=_short(ev.body, BODY_MAX), direct=True))
            continue
        for ev in theirs:
            if _mentions_me(ev, state):
                items.append(Item(who=names.get(ev.sender), body=_short(ev.body, BODY_MAX),
                                  room=room_names.get(rid), to_me="упоминание"))
    return items


def format_items(state: State, items: list[Item]) -> str:
    room = [i for i in items if not i.direct and not i.room]
    direct = [i for i in items if i.direct]
    mentions = [i for i in items if i.room]
    blocks: list[list[str]] = []
    if room:
        blocks.append([f"🟢 <b>дежурство · {html.escape(state.title)}</b>"] + [it.html() for it in room])
    if direct:
        blocks.append(["💬 <b>личные</b>"] + [it.html() for it in direct])
    by_room: dict[str, list[Item]] = {}
    for it in mentions:
        by_room.setdefault(it.room, []).append(it)
    for name, group in by_room.items():
        blocks.append([f"📣 <b>упоминание · {html.escape(name)}</b>"]
                      + [Item(who=i.who, body=i.body).html() for i in group])
    return "\n\n".join("\n".join(b) for b in blocks)


def check(store: "Store", client: MatrixClient, *, commit: bool = True) -> tuple[State, list[Item], str]:
    """Новое с прошлой проверки: (состояние, пункты, новая позиция). Ничего не включено — без сети.

    ``commit=False`` — позицию не сдвигать (демон сдвинет сам, когда уведомление ушло).
    """
    state = load(store)
    if not state.watching:
        return state, [], state.since
    if not state.me:
        _remember_me(store, client)
        state = load(store)
    if not state.since:
        _reset_position(store, client)
        return load(store), [], load(store).since
    result = client.sync(state.since)
    _save_direct(store, result)
    names = _Names(store, client)
    upd = result.rooms.get(state.room_id) if state.active else None
    items = classify(upd.events, state, client, names) if upd else []
    if state.personal:
        rooms = _RoomNames(store, client)
        items += personal_items(result, state, dm_rooms(store), names, rooms)
        rooms.save()
    names.save()
    if commit:
        advance(store, state, result.next_batch)
    return state, items, result.next_batch


def advance(store: "Store", state: State, end: str) -> None:
    # Дежурство могли выключить или переключить, пока шёл запрос, — тогда не трогаем.
    now = load(store)
    if end and end != state.since and now.watching and now.since == state.since:
        store.set_workspace_meta(SINCE_META, end)


def poll(store: "Store", client_factory, sender) -> int:
    """Шаг демона: проверить и, если есть новое, отправить в Telegram. Вернуть число пунктов."""
    if not load(store).watching:
        return 0
    client = client_factory()
    if client is None:
        return 0
    with client:
        state, items, end = check(store, client, commit=False)
    if items:
        sender.send(format_items(state, items))
    advance(store, state, end)
    return len(items)
