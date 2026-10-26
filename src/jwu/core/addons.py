"""Аддоны: подмешиваемый функционал из отдельных (в т.ч. приватных) пакетов.

Аддон — Python-пакет, установленный рядом с jwu (``pipx inject jwu -e <путь>`` и в venv
MCP-сервера), с entry point в группе ``jwu.addons``, указывающим на модуль. Сам jwu об
аддонах ничего не знает: он только находит их и даёт подключиться. Модуль аддона может
определить любые из функций (все необязательны):

- ``register_cli(app)`` — добавить команды в typer-приложение ``jwu``;
- ``register_mcp(mcp)`` — добавить MCP-инструменты (``@mcp.tool()``);
- ``skills_dir() -> Path`` — каталог скиллов аддона (``<имя>/SKILL.md``), их ставит ``jwu install``;
- ``bot_text(ctx, text) -> str | None`` — обработать присланный боту текст (вернуть ответ HTML
  или None — не моё); ``ctx`` — ``{"store", "workspace", "cfg"}``;
- ``menu_buttons() -> [(подпись, действие)]`` и ``menu_action(ctx, действие) -> (текст, клавиатура)
  | None`` — кнопки в меню бота;
- ``after_hints() -> {скилл: подсказка}`` — что предложить после шага jwu (например, после
  ``jwu-track-time``); отдаётся агенту через ``jwu_addons()``;
- ``doctor(store, workspace) -> [(название, статус ok|warn|fail, деталь)]``.

Ошибка в аддоне не роняет jwu: аддон помечается сломанным, причина видна в ``jwu doctor``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import metadata
from types import ModuleType
from typing import Any, Optional

GROUP = "jwu.addons"


@dataclass
class Addon:
    name: str
    module: Optional[ModuleType] = None
    version: str = ""
    error: str = ""

    def has(self, attr: str) -> bool:
        return self.module is not None and callable(getattr(self.module, attr, None))

    def call(self, attr: str, *args: Any, default: Any = None) -> Any:
        if not self.has(attr):
            return default
        try:
            return getattr(self.module, attr)(*args)
        except Exception as exc:  # noqa: BLE001 — аддон не должен ронять jwu
            self.error = f"{attr}: {type(exc).__name__}: {exc}"
            return default


_cache: Optional[list[Addon]] = None


def load(*, refresh: bool = False) -> list[Addon]:
    """Найти и импортировать аддоны (один раз на процесс)."""
    global _cache
    if _cache is not None and not refresh:
        return _cache
    found: list[Addon] = []
    try:
        eps = metadata.entry_points(group=GROUP)
    except TypeError:  # python < 3.10
        eps = metadata.entry_points().get(GROUP, [])  # type: ignore[attr-defined]
    for ep in eps:
        addon = Addon(name=ep.name)
        try:
            addon.module = ep.load()
            dist = getattr(ep, "dist", None)
            addon.version = dist.version if dist is not None else ""
        except Exception as exc:  # noqa: BLE001
            addon.error = f"не загрузился: {type(exc).__name__}: {exc}"
        found.append(addon)
    _cache = found
    return found


def describe() -> list[dict]:
    out = []
    for a in load():
        out.append({"name": a.name, "version": a.version, "error": a.error,
                    "after": a.call("after_hints", default={}) or {}})
    return out
