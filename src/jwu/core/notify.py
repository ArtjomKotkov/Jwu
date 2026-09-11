"""Уведомления после синка: что из дельт стоит человека, и отправка в Telegram.

Дельты копятся в памяти и ждут, пока их посмотрят в дашборде или через `jwu changes`.
Для работы с телефона этого мало: конфликт, красный билд или «needs work» должны
догонять сами. Отсюда два слоя:

- ``select_notable`` — фильтр: из всех дельт синка остаются только те, что требуют
  действия (см. ``NOTABLE_KINDS``); новые упоминания добавляются отдельной строкой,
  потому что они не дельты (см. ``Service.collect_mentions``).
- ``TelegramNotifier`` — доставка через Bot API. Никакой очереди и ретраев: сообщение
  либо ушло сразу после синка, либо потерялось — следующий синк принесёт следующие
  события. Пропущенное уведомление не страшно, задвоенное — раздражает.

Отправка вызывается из ``Service.sync`` (то есть после ЛЮБОГО сетевого синка: руками,
из дашборда, из демона, из day-analyze) и никогда не роняет сам синк.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Optional

import httpx

from .models import Delta, Mention

if TYPE_CHECKING:
    from .config import Config

TELEGRAM_API = "https://api.telegram.org"
# Telegram режет сообщение на 4096 символов; оставляем запас на разметку.
MAX_MESSAGE = 3800

# Дельты, о которых стоит будить человека. Остальное (апрувы чужих PR, новые задачи в
# выборке, исчезновение из списка) подождёт дашборда.
NOTABLE_KINDS: dict[str, str] = {
    "new_conflict": "⚠️ конфликт",
    "build_failed": "❌ сборка упала",
    "reviewer_needs_work": "✍️ needs work",
    "new_pr_task": "☑️ новая задача в PR",
    "new_pr_comment": "💬 комментарий в PR",
    "status_change": "🔀 статус",
    "returned_from_testing": "↩️ вернули с тестов",
    "qa_comment": "🧪 комментарий QA",
}
# Что слать по умолчанию, если пользователь не настроил свой набор.
DEFAULT_KINDS: tuple[str, ...] = (
    "new_conflict", "build_failed", "reviewer_needs_work", "new_pr_task",
    "returned_from_testing", "qa_comment",
)


class NotifyError(RuntimeError):
    pass


@dataclass
class Notification:
    """Собранное уведомление: заголовок контура + строки событий."""

    workspace: str
    deltas: list[Delta] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.deltas or self.mentions)


def select_notable(deltas: Iterable[Delta], *, kinds: Iterable[str] = DEFAULT_KINDS) -> list[Delta]:
    """Оставить только дельты выбранных видов, без дублей по (ключ, вид)."""
    wanted = set(kinds)
    seen: set[tuple[str, str]] = set()
    out: list[Delta] = []
    for d in deltas:
        if d.kind not in wanted or (d.key, d.kind) in seen:
            continue
        seen.add((d.key, d.kind))
        out.append(d)
    return out


def _short(text: str, limit: int = 120) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_message(note: Notification) -> str:
    """HTML-текст для Telegram: одна строка на событие, жирный ключ, кратко."""
    lines = [f"<b>jwu · {html.escape(note.workspace)}</b>"]
    for d in note.deltas:
        label = NOTABLE_KINDS.get(d.kind, d.kind)
        detail = f" — {html.escape(_short(d.detail, 80))}" if d.detail else ""
        title = f" <i>{html.escape(_short(d.summary, 90))}</i>" if d.summary else ""
        lines.append(f"{label}: <b>{html.escape(d.key)}</b>{detail}{title}")
    for m in note.mentions:
        who = html.escape(m.author or "кто-то")
        lines.append(
            f"📣 упоминание: <b>{html.escape(m.task_key)}</b> от {who}"
            f" — {html.escape(_short(m.text, 160))}"
        )
    return "\n".join(lines)


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Порезать длинный текст по строкам, чтобы не упереться в лимит Telegram."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


class TelegramNotifier:
    """Отправка в чат через Bot API. ``client`` подменяется в тестах."""

    def __init__(self, token: str, chat_id: str, *, client: Optional[httpx.Client] = None,
                 timeout: float = 10.0, api: str = TELEGRAM_API) -> None:
        if not token or not chat_id:
            raise NotifyError("Telegram не настроен: нужны токен бота и chat_id")
        self.token = token
        self.chat_id = chat_id
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self._api = api.rstrip("/")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def send(self, text: str) -> list[int]:
        """Отправить текст (при необходимости — несколькими сообщениями). Вернуть их id."""
        ids: list[int] = []
        for chunk in split_message(text):
            try:
                resp = self._client.post(
                    f"{self._api}/bot{self.token}/sendMessage",
                    json={
                        "chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
            except httpx.HTTPError as exc:
                raise NotifyError(f"Telegram недоступен: {exc}") from exc
            if resp.status_code >= 400:
                # Токен в URL — в ошибку его не тащим.
                raise NotifyError(f"Telegram ответил {resp.status_code}: {resp.text[:200]}")
            body = resp.json() if resp.content else {}
            ids.append(int((body.get("result") or {}).get("message_id", 0) or 0))
        return ids

    def get_me(self) -> dict:
        """Проверка токена: кто мы (`/getMe`)."""
        try:
            resp = self._client.get(f"{self._api}/bot{self.token}/getMe")
        except httpx.HTTPError as exc:
            raise NotifyError(f"Telegram недоступен: {exc}") from exc
        if resp.status_code >= 400:
            raise NotifyError(f"Telegram ответил {resp.status_code}: токен бота неверный?")
        return (resp.json() or {}).get("result") or {}


def notifier_from_config(cfg: "Config") -> Optional[TelegramNotifier]:
    """Собрать отправщик из конфига воркспейса; None — уведомления не настроены."""
    from .config import telegram_token

    chat_id = (cfg.telegram.chat_id or "").strip()
    if not chat_id:
        return None
    token = telegram_token(cfg)
    if not token:
        return None
    return TelegramNotifier(token, chat_id)


def build_notification(workspace: str, deltas: Iterable[Delta], mentions: Iterable[Mention] = (),
                       *, kinds: Iterable[str] = DEFAULT_KINDS) -> Notification:
    return Notification(
        workspace=workspace,
        deltas=select_notable(deltas, kinds=kinds),
        mentions=list(mentions),
    )


def send_after_sync(notifier: TelegramNotifier, note: Notification) -> bool:
    """Отправить, если есть что. True — ушло."""
    if not note:
        return False
    notifier.send(format_message(note))
    return True
