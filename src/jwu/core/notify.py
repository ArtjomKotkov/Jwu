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
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Iterable, Optional

import httpx

from .models import Delta, Mention

if TYPE_CHECKING:
    from .config import Config

TELEGRAM_API = "https://api.telegram.org"
OFFSET_META = "telegram:offset"   # последний обработанный update_id (meta воркспейса)
# Ключ сущности в тексте: задача PROJ-1 либо PR PROJ/repo#42.
KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+|[\w.-]+/[\w.-]+#\d+)\b")
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


@dataclass
class Links:
    """Куда вести ссылки в уведомлении: задачи — в трекер, PR — в хостинг."""

    jira: str = ""        # https://jira.example.com → /browse/KEY
    bitbucket: str = ""   # https://git.example.com → /projects/P/repos/r/pull-requests/N
    github: str = ""      # https://github.com → /owner/repo/pull/N


PR_KEY_RE = re.compile(r"^(?P<project>[^/#]+)/(?P<repo>[^#]+)#(?P<id>\d+)$")


def key_url(key: str, links: Optional[Links]) -> str:
    """URL для ключа задачи или PR; пусто — если хостов нет или ключ не разобрать."""
    if links is None:
        return ""
    m = PR_KEY_RE.match(key)
    if m:
        if links.bitbucket:
            return (f"{links.bitbucket}/projects/{m.group('project')}/repos/{m.group('repo')}"
                    f"/pull-requests/{m.group('id')}")
        if links.github:
            return f"{links.github}/{m.group('project')}/{m.group('repo')}/pull/{m.group('id')}"
        return ""
    if re.match(r"^[A-Z][A-Z0-9]+-\d+$", key) and links.jira:
        return f"{links.jira}/browse/{key}"
    return ""


def _key_html(key: str, links: Optional[Links]) -> str:
    url = key_url(key, links)
    label = html.escape(key)
    return f'<a href="{html.escape(url, quote=True)}">{label}</a>' if url else f"<b>{label}</b>"


def _clean_detail(kind: str, detail: str) -> str:
    """Деталь без повторения того, что уже сказано заголовком группы."""
    text = " ".join((detail or "").split())
    if kind == "reviewer_needs_work":
        text = text.replace(": needs work", "")
    if kind == "new_conflict" and text.startswith("появился"):
        text = ""
    if kind == "build_failed" and text == "сборка упала":
        text = ""
    return text


# Порядок групп в сообщении: сначала блокеры мержа, потом остальное.
_GROUP_ORDER = ["new_conflict", "build_failed", "reviewer_needs_work", "new_pr_task",
                "returned_from_testing", "qa_comment", "new_pr_comment", "status_change"]


def format_message(note: Notification, *, links: Optional[Links] = None) -> str:
    """HTML для Telegram: заголовок контура, группы по виду события, ключи — ссылками.

    Одна строка на событие раньше не читалась: тип, ключ, деталь и заголовок задачи
    слипались. Теперь событие — это две строки: ключ (ссылкой) с деталью и, ниже,
    заголовок курсивом; события одного вида собраны под общим заголовком.
    """
    stamp = datetime.now().strftime("%d.%m %H:%M")
    lines = [f"🔔 <b>jwu · {html.escape(note.workspace)}</b>  <i>{stamp}</i>"]
    by_kind: dict[str, list[Delta]] = {}
    for d in note.deltas:
        by_kind.setdefault(d.kind, []).append(d)
    order = [k for k in _GROUP_ORDER if k in by_kind] + [k for k in by_kind if k not in _GROUP_ORDER]
    for kind in order:
        items = by_kind[kind]
        lines.append("")
        lines.append(f"{NOTABLE_KINDS.get(kind, kind)}")
        for d in items:
            detail = _clean_detail(kind, d.detail)
            head = f"• {_key_html(d.key, links)}"
            if detail:
                head += f" — {html.escape(_short(detail, 90))}"
            lines.append(head)
            if d.summary:
                lines.append(f"   <i>{html.escape(_short(d.summary, 100))}</i>")
    if note.mentions:
        lines.append("")
        lines.append("📣 упоминания")
        for m in note.mentions:
            who = html.escape(m.author or "кто-то")
            lines.append(f"• {_key_html(m.task_key, links)} — {who}")
            lines.append(f"   <i>{html.escape(_short(m.text, 180))}</i>")
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

    def get_updates(self, offset: Optional[int] = None, *, limit: int = 100) -> list[dict]:
        """Входящие сообщения боту (long polling не используем — демон и так периодический)."""
        params: dict = {"limit": limit, "timeout": 0, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = self._client.get(f"{self._api}/bot{self.token}/getUpdates", params=params)
        except httpx.HTTPError as exc:
            raise NotifyError(f"Telegram недоступен: {exc}") from exc
        if resp.status_code >= 400:
            raise NotifyError(f"Telegram ответил {resp.status_code}: {resp.text[:200]}")
        return list((resp.json() or {}).get("result") or [])

    def get_me(self) -> dict:
        """Проверка токена: кто мы (`/getMe`)."""
        try:
            resp = self._client.get(f"{self._api}/bot{self.token}/getMe")
        except httpx.HTTPError as exc:
            raise NotifyError(f"Telegram недоступен: {exc}") from exc
        if resp.status_code >= 400:
            raise NotifyError(f"Telegram ответил {resp.status_code}: токен бота неверный?")
        return (resp.json() or {}).get("result") or {}


def links_from_config(cfg: "Config") -> Links:
    """Хосты для ссылок в уведомлении — из конфига контура."""
    return Links(jira=(cfg.jira.base_url or "").rstrip("/"),
                 bitbucket=(cfg.bitbucket.base_url or "").rstrip("/"),
                 github=(cfg.github.web_url or "").rstrip("/"))


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


def send_after_sync(notifier: TelegramNotifier, note: Notification,
                    *, links: Optional[Links] = None) -> bool:
    """Отправить, если есть что. True — ушло."""
    if not note:
        return False
    notifier.send(format_message(note, links=links))
    return True


def parse_incoming(update: dict, *, chat_id: str) -> Optional[tuple[str, str]]:
    """Сообщение боту → (ключ, текст заметки) либо None.

    Принимаются только сообщения из настроенного чата (чужой чат — молча мимо). Ответ на
    уведомление (reply) относится к ПЕРВОМУ ключу в тексте уведомления; сообщение
    «PROJ-1 текст» — к этому ключу. Всё остальное — не заметка.
    """
    msg = update.get("message") or {}
    if str((msg.get("chat") or {}).get("id", "")) != str(chat_id):
        return None
    text = " ".join((msg.get("text") or "").split())
    if not text:
        return None
    replied = (msg.get("reply_to_message") or {}).get("text") or ""
    if replied:
        found = KEY_RE.search(replied)
        if found:
            return found.group(1), text
    lead = KEY_RE.match(text)
    if lead and len(text) > len(lead.group(1)):
        return lead.group(1), text[len(lead.group(1)):].strip(" :—-")
    return None
