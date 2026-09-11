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

# Команды бота: меню на «/» (setMyCommands) и постоянная клавиатура под полем ввода.
BOT_COMMANDS = [
    {"command": "sync", "description": "Синк всех контуров сейчас"},
    {"command": "status", "description": "Последний проход и что накопилось"},
    {"command": "stuck", "description": "Что застряло по порогам"},
    {"command": "mentions", "description": "Непрочитанные упоминания"},
    {"command": "seen", "description": "Пометить упоминания прочитанными"},
    {"command": "help", "description": "Как пользоваться"},
]
KEYBOARD = {
    "keyboard": [["/sync", "/status"], ["/stuck", "/mentions", "/seen"]],
    "resize_keyboard": True, "is_persistent": True,
    "input_field_placeholder": "PROJ-1 текст → заметка; ответ на уведомление → заметка",
}

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


SECTION_TITLES: dict[str, str] = {
    "new_conflict": "⚠️ КОНФЛИКТ",
    "build_failed": "❌ СБОРКА УПАЛА",
    "reviewer_needs_work": "✍️ NEEDS WORK",
    "new_pr_task": "☑️ НОВАЯ ЗАДАЧА В PR",
    "new_pr_comment": "💬 КОММЕНТАРИЙ В PR",
    "status_change": "🔀 СТАТУС",
    "returned_from_testing": "↩️ ВЕРНУЛИ С ТЕСТОВ",
    "qa_comment": "🧪 КОММЕНТАРИЙ QA",
    "build_fixed": "✅ СБОРКА ПОЗЕЛЕНЕЛА",
}
MENTION_TAG_RE = re.compile(r"\[~[^\]]+\]\s*[-—:,]?\s*")


def strip_mention_tags(text: str) -> str:
    """Убрать ``[~логин]`` из текста упоминания — в чате это шум."""
    return " ".join(MENTION_TAG_RE.sub("", text or "").split())


def _who(kind: str, detail: str) -> str:
    """Короткое «кто / сколько» для строки ключа: автор needs work, «+2» у комментов."""
    text = " ".join((detail or "").split())
    if kind == "reviewer_needs_work":
        return text.replace(": needs work", "")
    if kind in ("new_pr_comment", "new_comment", "qa_comment"):
        m = re.match(r"\+(\d+) комм\.(?: от (.+))?", text)
        if m:
            return (f"{m.group(2)} · +{m.group(1)}" if m.group(2) else f"+{m.group(1)}")
        return text
    if kind in ("new_pr_task", "pr_task_resolved", "status_change", "returned_from_testing"):
        return text
    return ""


def item_block(key: str, who: str, body: str, *, links: Optional[Links], quote: bool = False,
               body_limit: int = 110) -> list[str]:
    """Элемент секции: строка «ключ · кто» и ниже курсивом заголовок либо цитата."""
    head = _key_html(key, links)
    if who:
        head += f" · {html.escape(_short(who, 60))}"
    lines = [head]
    if body:
        text = html.escape(_short(body, body_limit))
        lines.append(f"<i>«{text}»</i>" if quote else f"<i>{text}</i>")
    return lines


def section(title: str, items: list[list[str]]) -> list[str]:
    """Секция: пустая строка, жирный заголовок, пустая строка, элементы через пустую."""
    out = ["", f"<b>{title}</b>"]
    for block in items:
        out.append("")
        out += block
    return out


def format_message(note: Notification, *, links: Optional[Links] = None) -> str:
    """HTML для Telegram: шапка, секции по виду события, элементы через пустую строку.

    Ключ — ссылкой на PR или задачу, рядом кто/сколько; ниже курсивом заголовок задачи
    (у упоминаний — цитата без тега [~логин]). Никаких буллетов и отступов: в чате они
    только шумят.
    """
    stamp = datetime.now().strftime("%d.%m %H:%M")
    lines = [f"🔔 <b>jwu · {html.escape(note.workspace)}</b>", stamp]
    by_kind: dict[str, list[Delta]] = {}
    for d in note.deltas:
        by_kind.setdefault(d.kind, []).append(d)
    order = [k for k in _GROUP_ORDER if k in by_kind] + [k for k in by_kind if k not in _GROUP_ORDER]
    for kind in order:
        title = SECTION_TITLES.get(kind, NOTABLE_KINDS.get(kind, kind).upper())
        lines += section(title, [
            item_block(d.key, _who(kind, d.detail), d.summary, links=links) for d in by_kind[kind]
        ])
    if note.mentions:
        lines += section("📣 УПОМИНАНИЯ", [
            item_block(m.task_key, m.author or "кто-то", strip_mention_tags(m.text),
                       links=links, quote=True, body_limit=160)
            for m in note.mentions
        ])
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

    def send(self, text: str, *, reply_to: Optional[int] = None,
             keyboard: bool = False) -> list[int]:
        """Отправить текст (при необходимости — несколькими сообщениями). Вернуть их id.

        ``reply_to`` — ответить на конкретное сообщение (подтверждение заметки, ответ на
        команду), чтобы в чате было видно, к чему это. ``keyboard`` — приложить постоянную
        клавиатуру с командами (к последнему куску).
        """
        ids: list[int] = []
        chunks = split_message(text)
        for i, chunk in enumerate(chunks):
            payload: dict = {
                "chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_to is not None:
                payload["reply_to_message_id"] = int(reply_to)
                payload["allow_sending_without_reply"] = True
            if keyboard and i == len(chunks) - 1:
                payload["reply_markup"] = KEYBOARD
            try:
                resp = self._client.post(f"{self._api}/bot{self.token}/sendMessage", json=payload)
            except httpx.HTTPError as exc:
                raise NotifyError(f"Telegram недоступен: {exc}") from exc
            if resp.status_code >= 400:
                # Токен в URL — в ошибку его не тащим.
                raise NotifyError(f"Telegram ответил {resp.status_code}: {resp.text[:200]}")
            body = resp.json() if resp.content else {}
            ids.append(int((body.get("result") or {}).get("message_id", 0) or 0))
        return ids

    def set_commands(self) -> bool:
        """Меню команд бота (подсказки на «/»). Ошибка не критична."""
        try:
            resp = self._client.post(f"{self._api}/bot{self.token}/setMyCommands",
                                     json={"commands": BOT_COMMANDS})
        except httpx.HTTPError:
            return False
        return resp.status_code < 400

    def get_updates(self, offset: Optional[int] = None, *, limit: int = 100,
                    timeout: int = 0) -> list[dict]:
        """Входящие сообщения боту. ``timeout`` > 0 — long polling: Telegram держит запрос
        до появления сообщения (или до истечения секунд), так что реакция мгновенная,
        а пустых опросов нет."""
        params: dict = {"limit": limit, "timeout": max(0, int(timeout)), "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = self._client.get(f"{self._api}/bot{self.token}/getUpdates", params=params,
                                    timeout=httpx.Timeout(float(max(10, timeout + 10))))
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
