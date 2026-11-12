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


def chat_mention(source, room_id: str, room_name: str, message, who: str):
    """Упоминание из чата (``jwu.core.chats.ChatMessage``) в общем виде — для ``add_mentions``."""
    from .core.chats import mention

    return mention(source, room_id, room_name, message, who)


def add_mentions(store: "Store", mentions: list) -> list:
    """Записать упоминания в общий список jwu (/mentions, дашборд). Повторы пропускаются."""
    return store.add_mentions(mentions)


def bitbucket_request(cfg: "Config", method: str, path: str, *, json: Optional[dict] = None,
                      timeout: float = 20.0) -> tuple[int, object]:
    """Запрос к REST Bitbucket контура с токеном jwu (токен аддону не виден).

    ``path`` — от корня хоста (``/rest/<плагин>/…``). → (HTTP-код, JSON или текст ответа).
    Нет Bitbucket в конфиге — ``ValueError``.
    """
    import httpx

    from .core.config import bitbucket_token

    base = (cfg.bitbucket.base_url or "").rstrip("/")
    if not base:
        raise ValueError("в контуре нет Bitbucket")
    headers = {"Authorization": f"Bearer {bitbucket_token(cfg)}", "Accept": "application/json"}
    with httpx.Client(timeout=timeout, headers=headers) as client:
        resp = client.request(method.upper(), base + "/" + path.lstrip("/"), json=json)
    try:
        body: object = resp.json()
    except ValueError:
        body = resp.text[:500]
    return resp.status_code, body


def session_name(store: "Store") -> str:
    """Имя текущей сессии агента (как его видят доска и строка статуса) — для MCP-инструментов."""
    from .mcp_server import _session_name

    return _session_name(store)
