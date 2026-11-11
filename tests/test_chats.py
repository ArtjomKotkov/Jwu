"""Источники чатов: общий вид и реестр адаптеров (встроенные + аддоны)."""

import types

from jwu.core import addons, chats
from jwu.core.config import Config


class _EP:
    def __init__(self, name, module):
        self.name, self._module = name, module
        self.dist = types.SimpleNamespace(version="1")

    def load(self):
        return self._module


def test_message_is_new_unless_edit():
    assert chats.ChatMessage("$1", "@a", "текст").is_new
    assert chats.ChatMessage("$2", "@a", "", sealed=True).is_new          # текста нет, но сообщение есть
    assert not chats.ChatMessage("$3", "@a", "правка", edit_of="$1").is_new
    assert not chats.ChatMessage("$4", "@a", "").is_new


def test_sort_rooms_named_first():
    rooms = [chats.ChatRoom("!z"), chats.ChatRoom("!b", "бета"), chats.ChatRoom("!a", "Альфа")]
    assert [r.id for r in chats.sort_rooms(rooms)] == ["!a", "!b", "!z"]


def test_registry_from_addons_and_unknown(monkeypatch):
    made = []
    mod = types.ModuleType("fake")
    mod.chat_sources = lambda: {"fakechat": lambda cfg: made.append(cfg) or "SRC",
                                "otherchat": lambda cfg: "OTHER"}
    monkeypatch.setattr(addons, "_entry_points", lambda: [_EP("fake", mod)])
    monkeypatch.setattr(addons, "_cache", None)
    try:
        cfg = Config()
        assert set(chats.factories()) == {"fakechat", "otherchat"}         # встроенных нет
        assert chats.factory("fakechat", cfg)() == "SRC" and made == [cfg]
        assert chats.factory("otherchat", cfg)() == "OTHER"
        assert chats.factory("nope", cfg)() is None                   # адаптера нет — как «не настроен»
    finally:
        addons._cache = None
