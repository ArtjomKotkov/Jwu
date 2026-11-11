"""Встроенный аддон «telegram»: фасад чата jwu через Telegram Bot API.

Ядро (бот, демон, уведомления) шлёт и читает чат только через отправщик фасада
(``jwu.core.notify.notifier_from_config``); здесь — как этот отправщик собрать из конфига
воркспейса и проверить в ``jwu doctor``. Выключить: ``jwu addons disable telegram``.
"""

from __future__ import annotations


def facades() -> dict:
    from .transport import sender_from_config

    return {"telegram": sender_from_config}


def doctor(store, workspace) -> list[tuple]:
    """Конфиг и живой доступ к ботам контура."""
    from jwu.core.config import telegram_backup_token, telegram_token
    from jwu.core.workspaces import config_for_workspace

    from .transport import TelegramNotifier

    cfg = config_for_workspace(store, workspace)
    if not cfg.telegram.chat_id:
        return [("Telegram", "skip", "уведомления не настроены — jwu configure --telegram-chat <id> --telegram-token …")]
    token = telegram_token(cfg)
    if not token:
        return [("Telegram", "warn", "chat_id задан, токена бота нет — jwu configure --telegram-token … / TELEGRAM_BOT_TOKEN")]
    out = [("Telegram", "ok", f"чат {cfg.telegram.chat_id}")]
    for title, tok, role in (("Доступ telegram", token, "main"),
                             ("Резервный бот telegram", telegram_backup_token(cfg), "backup")):
        if not tok:
            continue
        sender = TelegramNotifier(tok, cfg.telegram.chat_id, role=role)
        try:
            me = sender.get_me()
            out.append((title, "ok", f"бот @{me.get('username', '?')}"))
        except Exception as exc:  # noqa: BLE001
            out.append((title, "fail", f"{exc} — токен от @BotFather, chat_id — id чата с ботом"))
        finally:
            sender.close()
    return out
