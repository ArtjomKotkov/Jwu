"""UI-настройки дашборда (тема и т.п.).

Хранятся в единой БД проекта (``meta``-таблица в ``Store``), чтобы не плодить
отдельных файлов настроек. Ключи в ``meta`` префиксуются ``ui.``. Поломанная
запись/недоступная БД не должна ронять дашборд — в этом случае просто считаем,
что настроек нет.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import db_path
from .store import Store


THEME_META_KEY = "ui.theme"


@dataclass
class UIPrefs:
    """Сохраняемые UI-настройки. Поля опциональны — добавляй при необходимости."""
    theme: Optional[str] = None


def load_ui_prefs(store: Optional[Store] = None) -> UIPrefs:
    """Прочитать UI-настройки из ``meta``. На любые ошибки I/O возвращает дефолты."""
    try:
        if store is None:
            with Store(db_path()) as s:
                theme = s.get_meta(THEME_META_KEY)
        else:
            theme = store.get_meta(THEME_META_KEY)
    except Exception:  # noqa: BLE001 — БД может быть недоступна; молчим
        return UIPrefs()
    return UIPrefs(theme=theme or None)


def save_ui_prefs(prefs: UIPrefs, store: Optional[Store] = None) -> None:
    """Сохранить UI-настройки в ``meta``. Ошибки записи проглатываются."""
    try:
        if store is None:
            with Store(db_path()) as s:
                _apply(s, prefs)
        else:
            _apply(store, prefs)
    except Exception:  # noqa: BLE001
        pass


def _apply(store: Store, prefs: UIPrefs) -> None:
    if prefs.theme:
        store.set_meta(THEME_META_KEY, prefs.theme)
