"""Matrix как источник чатов (``chats.ChatSource``): только чтение.

Кто я, комнаты с именами, инкрементальный ``/sync`` (новые сообщения всех комнат, счётчики
непрочитанного, личные диалоги из ``m.direct``) и одно сообщение по id. Писать в Matrix
клиент не умеет вовсе — у него нет такого метода, и это намеренно: отвечает человек.

Доступ: ``Config.matrix`` (homeserver + секрет ``matrix.token``); если там пусто,
``client_from_config`` берёт homeserver и токен из конфига matrix-mcp — он у дежурного
обычно уже настроен для агентов. Токен в ошибки и выводы не попадает.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.parse import quote

import httpx

from .chats import ChatError, ChatMessage, ChatNews, ChatRoom, RoomNews, sort_rooms

if TYPE_CHECKING:
    from .config import Config

API = "/_matrix/client/v3"


class MatrixError(ChatError):
    """Matrix не настроен или не отвечает."""


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


def parse_event(raw: dict) -> ChatMessage:
    content = raw.get("content") or {}
    rel = content.get("m.relates_to") or {}
    reply_to = ((rel.get("m.in_reply_to") or {}).get("event_id") or "")
    thread_root = rel.get("event_id", "") if rel.get("rel_type") == "m.thread" else ""
    replaces = rel.get("event_id", "") if rel.get("rel_type") == "m.replace" else ""
    body = content.get("body") or ""
    if raw.get("type") == ENCRYPTED:
        body = ENCRYPTED_BODY   # связи (треды, правки) у E2E открыты, текст — нет
    elif raw.get("type") != "m.room.message":
        body = ""               # не сообщение — для ленты его нет
    if content.get("msgtype") in ("m.image", "m.file", "m.video", "m.audio") and body:
        body = f"[{content['msgtype'][2:]}] {body}"
    return ChatMessage(
        id=raw.get("event_id", ""), sender=raw.get("sender", ""), body=body,
        ts=int(raw.get("origin_server_ts") or 0), thread_root=thread_root, reply_to=reply_to,
        mentions=list((content.get("m.mentions") or {}).get("user_ids") or []),
        sealed=raw.get("type") == ENCRYPTED, edit_of=replaces,
    )


class MatrixClient:
    """Адаптер Matrix к ``chats.ChatSource``: HTTP-клиент к одному homeserver.
    ``client`` подменяется в тестах."""

    name = "matrix"
    title = "Matrix"

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

    def display_name(self, user_id: str) -> str:
        try:
            return str(self._get(f"/profile/{quote(user_id)}/displayname").get("displayname") or "")
        except MatrixError:
            return ""

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

    def room_ids(self) -> list[str]:
        return [str(r) for r in (self._get("/joined_rooms").get("joined_rooms") or [])]

    def room_names(self, room_ids: list[str], *, workers: int = 16) -> dict[str, str]:
        """Имена комнат пачкой, параллельно: у дежурного их сотни, подряд — минуты."""
        from concurrent.futures import ThreadPoolExecutor

        if not room_ids:
            return {}
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(room_ids)))) as pool:
            return dict(zip(room_ids, pool.map(self.room_name, room_ids)))

    def rooms(self) -> list[ChatRoom]:
        """Комнаты, где я состою, с именами — по имени (без имени — в конце)."""
        names = self.room_names(self.room_ids())
        return sort_rooms([ChatRoom(id=rid, name=name) for rid, name in names.items()])

    def news(self, cursor: str = "", *, timeline: int = 30) -> ChatNews:
        """Без ``cursor`` — лёгкая точка отсчёта (без ленты) и личные диалоги; с ним — что
        нового (один инкрементальный ``/sync`` на все комнаты)."""
        params = {"timeout": 0, "filter": _sync_filter(timeline if cursor else 0)}
        if cursor:
            params["since"] = cursor
        data = self._get("/sync", params)
        rooms: dict[str, RoomNews] = {}
        for rid, room in ((data.get("rooms") or {}).get("join") or {}).items():
            tl = room.get("timeline") or {}
            rooms[rid] = RoomNews(
                messages=[parse_event(e) for e in (tl.get("events") or [])],
                unread=int((room.get("unread_notifications") or {}).get("notification_count") or 0),
                limited=bool(tl.get("limited")),
            )
        direct = None
        for ev in ((data.get("account_data") or {}).get("events") or []):
            if ev.get("type") == "m.direct":
                direct = {str(r) for v in (ev.get("content") or {}).values() for r in (v or [])}
        return ChatNews(cursor=str(data.get("next_batch") or cursor), rooms=rooms, direct_rooms=direct)

    def link(self, room_id: str, message_id: str) -> str:
        return f"https://matrix.to/#/{quote(room_id, safe='!:')}/{quote(message_id, safe='$')}"

    def message(self, room_id: str, message_id: str) -> Optional[ChatMessage]:
        try:
            return parse_event(self._get(f"/rooms/{quote(room_id)}/event/{quote(message_id)}"))
        except MatrixError:
            return None


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
