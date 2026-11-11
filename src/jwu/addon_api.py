"""Публичный API jwu для аддонов: то, на что аддон может опираться, не лазая во внутренности.

Всё остальное в ``jwu.*`` — внутреннее и меняется без предупреждения.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterator, Optional

if TYPE_CHECKING:
    from .core.config import Config
    from .core.store import Store
    from .core.workspaces import Workspace


def cli_store() -> "Store":
    """Store воркспейса текущей папки — для команд CLI аддона."""
    from .cli.main import _store

    return _store()


def mcp_store(workspace: Optional[str] = None) -> "Store":
    """Store воркспейса для MCP-инструмента аддона (по папке сессии или ``workspace``)."""
    from .mcp_server import _store_only

    return _store_only(workspace)


def bot_senders(store: "Store") -> Iterator[tuple["Workspace", "Config", object, str]]:
    """(контур, конфиг, отправщик, логин) для каждого контура с ботом. Отправщик закрывает вызывающий."""
    from .core.daemon import _bot_senders

    return _bot_senders(store)


def deliver(store: "Store", sender, text: str) -> str:
    """Отправить в чат контура или, в тихие часы, отложить. → «sent» | «held»."""
    from .core import notifyprefs

    return notifyprefs.deliver(store, sender, text)


def log(message: str) -> None:
    """Строка в лог демона."""
    from .core.daemon import log as _log

    _log(message)
