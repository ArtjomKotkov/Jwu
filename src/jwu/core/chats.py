"""Чаты как источник: общий вид комнат и сообщений и реестр адаптеров.

Ядро jwu не знает, какой это мессенджер. Адаптер (Matrix, Telegram, WhatsApp…) приходит из
аддона хуком ``chat_sources() -> {имя: фабрика(cfg) -> источник | None}`` и отдаёт данные в
виде ниже; потребители (дежурство, упоминания) работают только с этим видом.

Источник только читает: писать в чаты jwu не умеет — отвечает человек.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional, Protocol

if TYPE_CHECKING:
    from .config import Config


class ChatError(RuntimeError):
    """Источник не настроен или не отвечает."""


@dataclass
class ChatRoom:
    id: str
    name: str = ""

    @property
    def title(self) -> str:
        return self.name or self.id


@dataclass
class ChatMessage:
    id: str
    sender: str
    body: str
    ts: int = 0                  # мс с эпохи
    thread_root: str = ""        # id корня треда, если сообщение в треде
    reply_to: str = ""           # id сообщения, на которое это ответ
    mentions: list[str] = field(default_factory=list)   # кого упомянули явно (id людей)
    sealed: bool = False         # текст недоступен (шифрование) — видно только кто и когда
    edit_of: str = ""            # правка: id исправленного сообщения

    @property
    def is_new(self) -> bool:
        """Новое сообщение. Правка — не новое: об исходном уже сказали."""
        return not self.edit_of and (self.sealed or bool(self.body))


@dataclass
class RoomNews:
    messages: list[ChatMessage] = field(default_factory=list)
    unread: int = 0              # непрочитано мной на момент проверки (0 — уже прочитал)
    limited: bool = False        # сообщений было больше, чем отдали


@dataclass
class ChatNews:
    cursor: str                                   # позиция, с которой читать в следующий раз
    rooms: dict[str, RoomNews] = field(default_factory=dict)
    direct_rooms: Optional[set[str]] = None       # личные диалоги, если источник их сообщил


class ChatSource(Protocol):
    name: str      # «matrix»
    title: str     # «Matrix»

    def whoami(self) -> str: ...
    def display_name(self, user_id: str) -> str: ...
    def room_ids(self) -> list[str]: ...
    def room_name(self, room_id: str) -> str: ...
    def room_names(self, room_ids: list[str]) -> dict[str, str]: ...
    def news(self, cursor: str = "") -> ChatNews:
        """Без ``cursor`` — точка отсчёта «сейчас» (без сообщений); с ним — что нового."""
    def message(self, room_id: str, message_id: str) -> Optional[ChatMessage]: ...
    def link(self, room_id: str, message_id: str) -> str:
        """Ссылка на сообщение для человека (пусто — источник не умеет)."""
    def close(self) -> None: ...
    def __enter__(self) -> "ChatSource": ...
    def __exit__(self, *exc: object) -> None: ...


def link(source, room_id: str, message_id: str) -> str:
    """Ссылка на сообщение, если адаптер её умеет."""
    fn = getattr(source, "link", None)
    try:
        return str(fn(room_id, message_id) or "") if callable(fn) else ""
    except Exception:  # noqa: BLE001
        return ""


def mention(source, room_id: str, room_name: str, msg: ChatMessage, who: str):
    """Упоминание из чата в общем виде ``Mention`` (его хранит и показывает ядро)."""
    from datetime import datetime, timezone

    from .models import Mention

    created = datetime.fromtimestamp(msg.ts / 1000, tz=timezone.utc).isoformat() if msg.ts else ""
    return Mention(task_key=room_id, comment_id=msg.id, author=who, text=msg.body, created=created,
                   source=getattr(source, "name", "chat"), place=room_name or room_id,
                   url=link(source, room_id, msg.id))


def sort_rooms(rooms: list[ChatRoom]) -> list[ChatRoom]:
    """По имени; комнаты без имени (обычно личные) — в конце."""
    return sorted(rooms, key=lambda r: (not r.name, r.title.lower()))


Factory = Callable[["Config"], Optional[ChatSource]]


def factories() -> dict[str, Factory]:
    """Адаптеры чатов: встроенные + из аддонов (``chat_sources``)."""
    from . import addons

    out: dict[str, Factory] = dict(_builtin())
    for addon in addons.active():
        for name, factory in (addon.call("chat_sources", default={}) or {}).items():
            out[str(name)] = factory       # аддон перекрывает встроенный
    return out


def _builtin() -> dict[str, Factory]:
    """Встроенные адаптеры (переезжают в аддоны — тогда их здесь не будет)."""
    from .matrix import client_from_config

    return {"matrix": client_from_config}


def factory(name: str, cfg: "Config") -> Callable[[], Optional[ChatSource]]:
    """Ленивый конструктор источника ``name`` по конфигу: сеть нужна не каждой кнопке.

    Адаптера нет (аддон не стоит или выключен) — фабрика вернёт None, как «не настроен».
    """
    make = factories().get(name)
    return (lambda: make(cfg)) if make else (lambda: None)
