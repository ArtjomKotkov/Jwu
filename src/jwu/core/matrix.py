"""Мини-клиент Matrix для режима дежурства бота: только чтение.

Умеет ровно то, что нужно демону для дежурства: кто я, список комнат с именами,
инкрементальный ``/sync`` (новые события всех комнат, счётчики непрочитанного, карта
личных диалогов ``m.direct``) и одно событие по id (для «ответ на моё?»). Писать в Matrix клиент не умеет вовсе — у него нет такого метода, и это
намеренно: дежурство только читает, отвечает человек.

Доступ: ``Config.matrix`` (homeserver + секрет ``matrix.token``); если там пусто,
``client_from_config`` берёт homeserver и токен из конфига matrix-mcp — он у дежурного
обычно уже настроен для агентов. Токен в ошибки и выводы не попадает.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from .config import Config

API = "/_matrix/client/v3"


class MatrixError(RuntimeError):
    """Matrix не настроен или не отвечает."""


@dataclass
class Room:
    id: str
    name: str

    @property
    def title(self) -> str:
        return self.name or self.id


@dataclass
class RoomUpdate:
    events: list["Event"] = field(default_factory=list)
    unread: int = 0          # unread_notifications.notification_count — 0, если я уже прочитал
    limited: bool = False    # событий было больше, чем отдали


@dataclass
class SyncResult:
    next_batch: str
    rooms: dict[str, RoomUpdate] = field(default_factory=dict)
    direct: Optional[dict[str, list[str]]] = None   # m.direct, если пришёл (первый раз или изменился)

    @property
    def direct_rooms(self) -> Optional[set[str]]:
        if self.direct is None:
            return None
        return {rid for rooms in self.direct.values() for rid in (rooms or [])}


ENCRYPTED = "m.room.encrypted"
ENCRYPTED_BODY = "🔒 зашифровано — текст в Element"


def _sync_filter(timeline: int) -> str:
    """Фильтр /sync: только лента сообщений и m.direct. Список комнат в фильтр не кладём —
    у дежурного их сотни, и URL не влезает (414); отбор — на нашей стороне."""
    return json.dumps({
        "room": {"timeline": {"limit": timeline, "types": ["m.room.message", "m.room.encrypted"]},
                 "state": {"not_types": ["*"]}, "ephemeral": {"not_types": ["*"]},
                 "account_data": {"not_types": ["*"]}},
        "presence": {"not_types": ["*"]},
        "account_data": {"types": ["m.direct"]},
    }, separators=(",", ":"))


@dataclass
class Event:
    """Событие ленты комнаты в том виде, в каком его смотрит дежурство."""

    id: str
    sender: str
    body: str
    ts: int = 0
    type: str = "m.room.message"
    msgtype: str = "m.text"
    thread_root: str = ""      # id корня треда, если сообщение в треде
    reply_to: str = ""         # id сообщения, на которое это ответ
    mentions: list[str] = field(default_factory=list)   # mxid из m.mentions
    display_name: str = ""     # имя отправителя, если известно
    replaces: str = ""         # правка: id исправленного сообщения (m.replace)

    @property
    def encrypted(self) -> bool:
        """Зашифровано (E2E, обычно личка в Element): текста jwu не видит, только кто и когда."""
        return self.type == ENCRYPTED

    @property
    def is_message(self) -> bool:
        """Новое сообщение. Правка — не новое: об исходном уже сказали, второй раз не шлём."""
        if self.replaces:
            return False
        return self.encrypted or (self.type == "m.room.message" and bool(self.body))


def sort_rooms(rooms: list[Room]) -> list[Room]:
    return sorted(rooms, key=lambda r: (not r.name, r.title.lower()))


def parse_event(raw: dict) -> Event:
    content = raw.get("content") or {}
    rel = content.get("m.relates_to") or {}
    reply_to = ((rel.get("m.in_reply_to") or {}).get("event_id") or "")
    thread_root = rel.get("event_id", "") if rel.get("rel_type") == "m.thread" else ""
    replaces = rel.get("event_id", "") if rel.get("rel_type") == "m.replace" else ""
    body = content.get("body") or ""
    if raw.get("type") == ENCRYPTED:
        body = ENCRYPTED_BODY   # связи (треды, правки) у E2E открыты, текст — нет
    if content.get("msgtype") in ("m.image", "m.file", "m.video", "m.audio") and body:
        body = f"[{content['msgtype'][2:]}] {body}"
    mentions = list((content.get("m.mentions") or {}).get("user_ids") or [])
    return Event(
        id=raw.get("event_id", ""), sender=raw.get("sender", ""), body=body,
        ts=int(raw.get("origin_server_ts") or 0), type=raw.get("type", ""),
        msgtype=content.get("msgtype", ""), thread_root=thread_root, reply_to=reply_to,
        mentions=mentions, replaces=replaces,
    )


class MatrixClient:
    """HTTP-клиент к одному homeserver. ``client`` подменяется в тестах."""

    def __init__(self, homeserver: str, token: str, *, client: Optional[httpx.Client] = None,
                 timeout: float = 15.0) -> None:
        if not homeserver or not token:
            raise MatrixError("Matrix не настроен: нужны homeserver и токен")
        self.homeserver = homeserver.rstrip("/")
        self._token = token
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "MatrixClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        try:
            resp = self._client.get(f"{self.homeserver}{API}{path}", params=params,
                                    headers={"Authorization": f"Bearer {self._token}"})
        except httpx.HTTPError as exc:
            raise MatrixError(f"Matrix недоступен: {exc}") from exc
        if resp.status_code == 401:
            raise MatrixError("Matrix ответил 401: токен не подходит")
        if resp.status_code >= 400:
            raise MatrixError(f"Matrix ответил {resp.status_code}: {resp.text[:200]}")
        return resp.json() if resp.content else {}

    def whoami(self) -> str:
        return str(self._get("/account/whoami").get("user_id") or "")

    def room_name(self, room_id: str) -> str:
        """Имя комнаты; без имени — канонический алиас; без него — пусто (покажем id)."""
        try:
            name = self._get(f"/rooms/{quote(room_id)}/state/m.room.name").get("name")
            if name:
                return str(name)
        except MatrixError:
            pass
        try:
            return str(self._get(f"/rooms/{quote(room_id)}/state/m.room.canonical_alias").get("alias") or "")
        except MatrixError:
            return ""

    def joined_room_ids(self) -> list[str]:
        return [str(r) for r in (self._get("/joined_rooms").get("joined_rooms") or [])]

    def room_names(self, room_ids: list[str], *, workers: int = 16) -> dict[str, str]:
        """Имена комнат пачкой, параллельно: у дежурного их сотни, подряд — минуты."""
        from concurrent.futures import ThreadPoolExecutor

        if not room_ids:
            return {}
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(room_ids)))) as pool:
            return dict(zip(room_ids, pool.map(self.room_name, room_ids)))

    def joined_rooms(self) -> list[Room]:
        """Комнаты, где я состою, с именами — отсортированы по имени (без имени — в конце)."""
        names = self.room_names(self.joined_room_ids())
        return sort_rooms([Room(id=rid, name=name) for rid, name in names.items()])

    def latest_token(self, room_id: str) -> str:
        """Позиция «сейчас» в ленте комнаты: с неё дальше читаются только новые события."""
        data = self._get(f"/rooms/{quote(room_id)}/messages", {"dir": "b", "limit": 1})
        return str(data.get("start") or "")

    def new_events(self, room_id: str, since: str, *, limit: int = 100) -> tuple[list[Event], str]:
        """События после позиции ``since`` и новая позиция. Пусто — позиция прежняя."""
        data = self._get(f"/rooms/{quote(room_id)}/messages",
                         {"dir": "f", "from": since, "limit": limit})
        events = [parse_event(e) for e in (data.get("chunk") or [])]
        return events, str(data.get("end") or since)

    def sync(self, since: str = "", *, timeline: int = 30) -> SyncResult:
        """Без ``since`` — лёгкая точка отсчёта (без ленты) и карта личных; с ним — что нового."""
        params = {"timeout": 0, "filter": _sync_filter(timeline if since else 0)}
        if since:
            params["since"] = since
        data = self._get("/sync", params)
        rooms: dict[str, RoomUpdate] = {}
        for rid, room in ((data.get("rooms") or {}).get("join") or {}).items():
            tl = room.get("timeline") or {}
            rooms[rid] = RoomUpdate(
                events=[parse_event(e) for e in (tl.get("events") or [])],
                unread=int((room.get("unread_notifications") or {}).get("notification_count") or 0),
                limited=bool(tl.get("limited")),
            )
        direct = None
        for ev in ((data.get("account_data") or {}).get("events") or []):
            if ev.get("type") == "m.direct":
                direct = {str(k): [str(r) for r in (v or [])] for k, v in (ev.get("content") or {}).items()}
        return SyncResult(next_batch=str(data.get("next_batch") or since), rooms=rooms, direct=direct)

    def direct_map(self, user_id: str) -> dict[str, list[str]]:
        try:
            content = self._get(f"/user/{quote(user_id)}/account_data/m.direct")
        except MatrixError:
            return {}
        return {str(k): [str(r) for r in (v or [])] for k, v in content.items() if isinstance(v, list)}

    def event(self, room_id: str, event_id: str) -> Optional[Event]:
        try:
            return parse_event(self._get(f"/rooms/{quote(room_id)}/event/{quote(event_id)}"))
        except MatrixError:
            return None

    def display_name(self, user_id: str) -> str:
        try:
            return str(self._get(f"/profile/{quote(user_id)}/displayname").get("displayname") or "")
        except MatrixError:
            return ""


# --------------------------------------------------------------------------- #
# Откуда взять доступ
# --------------------------------------------------------------------------- #


def mcp_config_path() -> Path:
    """Конфиг matrix-mcp: macOS — Application Support, иначе XDG_CONFIG_HOME."""
    override = os.environ.get("JWU_MATRIX_MCP_CONFIG")
    if override:
        return Path(override).expanduser()
    mac = Path.home() / "Library" / "Application Support" / "matrix-mcp" / "config.json"
    if mac.exists():
        return mac
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "matrix-mcp" / "config.json"


def read_mcp_config(path: Optional[Path] = None) -> dict:
    """``{"homeserver", "user_id", "access_token"}`` из конфига matrix-mcp или пустой dict."""
    path = path or mcp_config_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: str(data.get(k) or "") for k in ("homeserver", "user_id", "access_token")}


def access_from_config(cfg: "Config") -> tuple[str, str, str, str]:
    """(homeserver, user_id, token, источник) — конфиг jwu, иначе matrix-mcp, иначе пусто."""
    from .config import matrix_token

    token = matrix_token(cfg)
    if cfg.matrix.homeserver and token:
        return cfg.matrix.homeserver, cfg.matrix.user_id, token, "jwu"
    mcp = read_mcp_config()
    if mcp.get("homeserver") and mcp.get("access_token"):
        return (mcp["homeserver"].rstrip("/"), cfg.matrix.user_id or mcp.get("user_id", ""),
                mcp["access_token"], "matrix-mcp")
    return cfg.matrix.homeserver, cfg.matrix.user_id, token, ""


def client_from_config(cfg: "Config") -> Optional[MatrixClient]:
    """Клиент по конфигу воркспейса; None — Matrix не настроен нигде."""
    homeserver, _user, token, source = access_from_config(cfg)
    if not source:
        return None
    return MatrixClient(homeserver, token)
