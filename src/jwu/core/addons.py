"""Аддоны: подмешиваемый функционал из отдельных (в т.ч. приватных) пакетов.

Аддон — Python-пакет, установленный рядом с jwu (``pipx inject jwu -e <путь>`` и в venv
MCP-сервера), с entry point в группе ``jwu.addons``, указывающим на модуль. Сам jwu об
аддонах ничего не знает: он только находит их и даёт подключиться. Модуль аддона может
определить любые из функций (все необязательны):

- ``REQUIRES = ["имя", …]`` — без каких аддонов этот не работает. Нужного нет, он выключен
  или сломан — этот не подключается вовсе (причина видна в ``jwu addons list`` и ``jwu doctor``);
- ``register_cli(app)`` — добавить команды в typer-приложение ``jwu``;
- ``register_mcp(mcp)`` — добавить MCP-инструменты (``@mcp.tool()``);
- ``skills_dir() -> Path`` — каталог скиллов аддона (``<имя>/SKILL.md``), их ставит ``jwu install``;
- ``agents_dir() -> Path`` — каталог субагентов аддона (``<имя>.md``), их тоже ставит ``jwu install``;
- ``bot_text(ctx, text) -> str | None`` — обработать присланный боту текст (вернуть ответ HTML
  или None — не моё); ``ctx`` — ``{"store", "workspace", "cfg"}``;
- ``bot_commands() -> [(команда, описание)]`` и ``bot_command(ctx, команда, текст) -> str |
  (текст, клавиатура) | None`` — свои команды бота (``/duty``…), попадают и в меню команд Telegram;
- ``callback_prefixes() -> [префикс]`` и ``bot_callback(ctx, data) -> (текст, клавиатура) | None``
  — свои inline-кнопки (``callback_data`` с этим префиксом приходят аддону);
- ``menu_buttons() -> [(подпись, действие)]`` и ``menu_action(ctx, действие) -> (текст, клавиатура)
  | None`` — кнопки в меню бота;
- ``facades() -> {имя: фабрика(cfg) -> отправщик | None}`` — фасад чата бота (встроенный —
  Telegram): куда jwu пишет уведомления и откуда читает команды (см. ``notify.notifier_from_config``);
- ``chat_sources() -> {имя: фабрика(cfg) -> источник | None}`` — адаптер чатов (Matrix,
  мессенджеры): источник отдаёт комнаты и сообщения в общем виде ``jwu.core.chats``;
- ``daemon_tasks() -> [(имя, раз_в_секунд, fn(store))]`` — периодическая работа в демоне
  (общий цикл, шаг ~25 с); ``store`` — общий, контуры с ботом — ``jwu.addon_api.bot_senders``;
- ``after_hints() -> {скилл: подсказка}`` — что предложить после шага jwu (например, после
  ``jwu-track-time``); отдаётся агенту через ``jwu_addons()``;
- ``doctor(store, workspace) -> [(название, статус ok|warn|fail, деталь)]``.

Установленный аддон можно выключить, не удаляя: ``jwu addons disable <имя>`` — тогда он не
импортируется вовсе. Список выключенных — в ``addons.json`` рядом с конфигом jwu.

Ошибка в аддоне не роняет jwu: аддон помечается сломанным, причина видна в ``jwu doctor``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

GROUP = "jwu.addons"

ACTIVE, DISABLED, BROKEN, BLOCKED = "active", "disabled", "broken", "blocked"


@dataclass
class Addon:
    name: str
    module: Optional[ModuleType] = None
    version: str = ""
    error: str = ""
    state: str = ACTIVE
    requires: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.state == ACTIVE and self.module is not None

    def has(self, attr: str) -> bool:
        return self.active and callable(getattr(self.module, attr, None))

    def call(self, attr: str, *args: Any, default: Any = None) -> Any:
        if not self.has(attr):
            return default
        try:
            return getattr(self.module, attr)(*args)
        except Exception as exc:  # noqa: BLE001 — аддон не должен ронять jwu
            self.error = f"{attr}: {type(exc).__name__}: {exc}"
            return default


# --------------------------------------------------------------------------- #
# Вкл/выкл — файл рядом с конфигом: читается до БД и до импорта аддонов
# --------------------------------------------------------------------------- #


def state_path() -> Path:
    from .config import config_path

    return config_path().parent / "addons.json"


def disabled() -> set[str]:
    try:
        return set(json.loads(state_path().read_text(encoding="utf-8")).get("disabled") or [])
    except (OSError, ValueError, AttributeError):
        return set()


def set_enabled(name: str, on: bool) -> None:
    off = disabled()
    (off.discard if on else off.add)(name)
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"disabled": sorted(off)}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


# --------------------------------------------------------------------------- #
# Поиск и загрузка
# --------------------------------------------------------------------------- #

_cache: Optional[list[Addon]] = None


def _entry_points():
    try:
        return list(metadata.entry_points(group=GROUP))
    except TypeError:  # python < 3.10
        return list(metadata.entry_points().get(GROUP, []))  # type: ignore[attr-defined]


def _resolve(found: list[Addon]) -> None:
    """Зависимости: аддон без нужного (нет, выключен, сломан) не подключается — и по цепочке."""
    by_name = {a.name: a for a in found}
    changed = True
    while changed:
        changed = False
        for addon in found:
            if addon.state != ACTIVE:
                continue
            for req in addon.requires:
                dep = by_name.get(req)
                if dep is None or dep.state != ACTIVE:
                    why = "не установлен" if dep is None else {DISABLED: "выключен", BROKEN: "сломан",
                                                               BLOCKED: "не подключён"}[dep.state]
                    addon.state, addon.error = BLOCKED, f"нужен аддон {req} — он {why}"
                    changed = True
                    break


def load(*, refresh: bool = False) -> list[Addon]:
    """Найти и импортировать аддоны (один раз на процесс). Все — и выключенные, и сломанные."""
    global _cache
    if _cache is not None and not refresh:
        return _cache
    off = disabled()
    found: list[Addon] = []
    for ep in _entry_points():
        dist = getattr(ep, "dist", None)
        addon = Addon(name=ep.name, version=dist.version if dist is not None else "")
        if ep.name in off:
            addon.state = DISABLED
            found.append(addon)
            continue
        try:
            addon.module = ep.load()
            addon.requires = [str(r) for r in (getattr(addon.module, "REQUIRES", None) or [])]
        except Exception as exc:  # noqa: BLE001
            addon.state, addon.error = BROKEN, f"не загрузился: {type(exc).__name__}: {exc}"
        found.append(addon)
    _resolve(found)
    _cache = found
    return found


def active() -> list[Addon]:
    """Подключённые аддоны — только их хуки jwu и зовёт."""
    return [a for a in load() if a.active]


def get(name: str) -> Optional[Addon]:
    return next((a for a in load() if a.name == name), None)


def dependents(name: str) -> list[str]:
    """Кто из включённых аддонов требует этот (выключишь его — отвалятся и они)."""
    return sorted(a.name for a in load() if a.state in (ACTIVE, BLOCKED) and name in a.requires)


def describe() -> list[dict]:
    return [{"name": a.name, "version": a.version, "state": a.state, "error": a.error,
             "requires": a.requires, "after": a.call("after_hints", default={}) or {}}
            for a in load()]
