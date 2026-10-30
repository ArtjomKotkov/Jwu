"""Допуск на git-операцию уровня ``grant`` кнопкой в Telegram-боте.

Агент упёрся в ``grant`` → :func:`notify` присылает в чат бота «что, куда, сколько коммитов»
с кнопками. Нажать их может только пользователь в своём чате — агенту до них не дотянуться,
поэтому «допуск» здесь настоящий, а не «модель сказала, что пользователь разрешил».
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from . import gitpolicy as gp

if TYPE_CHECKING:
    from .store import Store

CALLBACK_PREFIX = "git:"


def _text(req: dict) -> str:
    what = gp.OP_TITLES.get(req["op"], req["op"])
    lines = [f"🔐 <b>Нужен допуск: {html.escape(what)}</b>",
             f"{html.escape(req['remote'] or '?')} → <code>{html.escape(req['branch'])}</code>"]
    if req.get("detail"):
        lines.append(html.escape(req["detail"][:400]))
    if req.get("job_id"):
        lines.append(f"работа #{req['job_id']}")
    lines.append(f"запрос #{req['id']} · «да» в чате с агентом тут не считается")
    return "\n".join(lines)


def markup(req: dict, ttl: int) -> dict:
    mins = max(1, ttl // 60)
    return {"inline_keyboard": [[
        {"text": f"✅ Разрешить ({mins} мин)", "callback_data": f"{CALLBACK_PREFIX}ok:{req['id']}"},
        {"text": "⛔ Отказать", "callback_data": f"{CALLBACK_PREFIX}no:{req['id']}"},
    ]]}


def notify(store: "Store", req: dict) -> bool:
    """Отправить запрос в бот воркспейса. Бот не настроен / grant_via без telegram — False."""
    from . import notify as notify_mod
    from .workspaces import config_for_workspace

    policy = gp.load(store)
    if "telegram" not in policy.grant_via:
        return False
    ws = store.get_workspace(store.workspace_id)
    sender = notify_mod.notifier_from_config(config_for_workspace(store, ws)) if ws else None
    if sender is None:
        return False
    sender.send(_text(req), markup=markup(req, policy.grant_ttl))
    return True


def handle_callback(store: "Store", data: str) -> tuple[str, dict]:
    """Кнопка «Разрешить / Отказать» → ответ на запрос; сообщение перерисовывается итогом."""
    try:
        action, raw_id = data[len(CALLBACK_PREFIX):].split(":", 1)
        grant_id = int(raw_id)
    except ValueError:
        return "Кнопка допуска устарела.", {}
    try:
        row = gp.answer_grant(store, grant_id, allow=action == "ok", via="telegram")
    except gp.PolicyError as exc:
        return f"⚠️ {html.escape(str(exc))}", {}
    what = f"{gp.OP_TITLES.get(row['op'], row['op'])} → <code>{html.escape(row['branch'])}</code>"
    if row["status"] == "granted":
        return (f"✅ Разрешено: {what} ({html.escape(row['remote'])}), до {row['expires_at'][11:16]} UTC, "
                f"один раз. Агент может повторить операцию."), {}
    return f"⛔ Отказано: {what} ({html.escape(row['remote'])}).", {}
