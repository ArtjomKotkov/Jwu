"""Инструкции: полные пошаговые тексты, на которые коротко ссылаются правила.

Правило — короткое «что и почему» (``testenv``: «стенд работы поднимается по инструкции
backend-stand, порты со смещением по номеру работы»). Инструкция — длинное «как»:
команды по порядку, порты, грабли. Так правила остаются читаемыми и приезжают в каждый
старт работы целиком, а инструкцию агент открывает, только когда она нужна.

Инструкция бывает **своя у воркспейса** (``guide.<имя>`` в настройках контура) и **общая
для всех воркспейсов** (``guide.<имя>`` в общей meta БД) — например, единая установка
Playwright. Своя перекрывает общую с тем же именем. Пишутся только с «да» пользователя.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .store import Store

PREFIX = "guide."
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,59}$")


class GuideError(ValueError):
    pass


@dataclass
class Guide:
    name: str
    title: str
    text: str = ""
    tag: str = ""
    scope: str = "workspace"   # workspace | global
    updated_at: str = ""

    def brief(self) -> dict:
        return {"name": self.name, "title": self.title, "tag": self.tag, "scope": self.scope}

    def to_dict(self) -> dict:
        return asdict(self)


def _parse(name: str, raw: str, scope: str) -> Optional[Guide]:
    try:
        data = json.loads(raw or "")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return Guide(name=name, title=str(data.get("title") or name), text=str(data.get("text") or ""),
                 tag=str(data.get("tag") or ""), scope=scope, updated_at=str(data.get("updated_at") or ""))


def list_guides(store: "Store", workspace_id: int) -> list[Guide]:
    """Свои инструкции воркспейса и общие (своя перекрывает общую с тем же именем)."""
    out: dict[str, Guide] = {}
    for key, raw in store.meta_items(PREFIX).items():
        g = _parse(key[len(PREFIX):], raw, "global")
        if g:
            out[g.name] = g
    for key, raw in store.workspace_settings(workspace_id).items():
        if key.startswith(PREFIX):
            g = _parse(key[len(PREFIX):], raw, "workspace")
            if g:
                out[g.name] = g
    return sorted(out.values(), key=lambda g: (g.scope != "workspace", g.tag, g.name))


def get_guide(store: "Store", workspace_id: int, name: str) -> Guide:
    for g in list_guides(store, workspace_id):
        if g.name == name:
            return g
    raise GuideError(f"Инструкции «{name}» нет. Список — jwu guide list")


def save_guide(store: "Store", workspace_id: int, guide: Guide) -> Guide:
    if not _NAME_RE.match(guide.name):
        raise GuideError("Имя инструкции: латиница в нижнем регистре, цифры, . _ -")
    if not guide.title.strip():
        raise GuideError("У инструкции нужен заголовок")
    guide.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    raw = json.dumps({"title": guide.title, "text": guide.text, "tag": guide.tag,
                      "updated_at": guide.updated_at}, ensure_ascii=False)
    if guide.scope == "global":
        store.set_meta(PREFIX + guide.name, raw)
    else:
        store.set_workspace_settings(workspace_id, {PREFIX + guide.name: raw})
    return guide


def remove_guide(store: "Store", workspace_id: int, name: str) -> Guide:
    guide = get_guide(store, workspace_id, name)
    if guide.scope == "global":
        store.delete_meta(PREFIX + name)
    else:
        store.delete_workspace_settings(workspace_id, [PREFIX + name])
    return guide


def index_md(guides: list[Guide]) -> str:
    """Список инструкций для контекста агента: имя, заголовок, тег — без текста."""
    if not guides:
        return ""
    lines = ["### Инструкции (полный текст — `jwu_guide(name)`)", ""]
    for g in guides:
        scope = " · общая" if g.scope == "global" else ""
        tag = f" [#{g.tag}]" if g.tag else ""
        lines.append(f"- `{g.name}`{tag}{scope} — {g.title}")
    return "\n".join(lines)
