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

from .models import Delta, Issue, Mention, PR

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
    {"command": "menu", "description": "Меню кнопками"},
    {"command": "sync", "description": "Синк всех контуров сейчас"},
    {"command": "status", "description": "Последний проход и что накопилось"},
    {"command": "stuck", "description": "Что застряло по порогам"},
    {"command": "tasks", "description": "Сводка моих задач"},
    {"command": "today", "description": "Что сделал сегодня"},
    {"command": "mentions", "description": "Непрочитанные упоминания"},
    {"command": "seen", "description": "Пометить упоминания прочитанными"},
    {"command": "duty", "description": "Дежурство за комнатой: вкл / выкл"},
    {"command": "personal", "description": "Личное (1:1 и упоминания): вкл / выкл"},
    {"command": "matrix", "description": "Matrix: настройка кнопками"},
    {"command": "help", "description": "Как пользоваться"},
]
MENU_BUTTON = "📋 Меню"
MENU_META = "telegram:menu_id"   # id последнего сообщения с меню (meta воркспейса)
# Постоянная клавиатура — одна кнопка: она шлёт в чат «📋 Меню», бот отвечает сообщением
# с inline-меню, дальше всё переходит кнопками внутри него (см. core.bot.main_menu).
KEYBOARD = {
    "keyboard": [[MENU_BUTTON]],
    "resize_keyboard": True, "is_persistent": True,
    "input_field_placeholder": "PROJ-1 текст → заметка; ответ на уведомление → заметка",
}

# Подписи дельт для Telegram. Шлётся то, что в DEFAULT_KINDS: все события синка, кроме
# «готов к мержу» (о нём и так видно по апрувам и зелёной сборке).
NOTABLE_KINDS: dict[str, str] = {
    "new_conflict": "⚠️ конфликт",
    "build_failed": "❌ сборка упала",
    "reviewer_needs_work": "✍️ needs work",
    "new_pr_task": "☑️ новая задача в PR",
    "new_pr_comment": "💬 комментарий в PR",
    "status_change": "🔀 статус",
    "returned_from_testing": "↩️ вернули с тестов",
    "qa_comment": "🧪 комментарий QA",
    "pr_all_approved": "👍 все апрувы",
    "pr_ready_to_merge": "🚀 готов к мержу",
    "new_issue": "🆕 новая задача",
    "new_comment": "💬 комментарий",
    "resolved": "✔️ решена",
    "new_pr": "🔗 новый PR по задаче",
    "new_review_pr": "👀 новый PR на ревью",
    "new_pr_commit": "📝 коммит в PR",
    "reviewer_approved": "✅ апрув",
    "build_fixed": "✅ сборка позеленела",
    "pr_task_resolved": "☑️ задача в PR закрыта",
    "gone": "📤 ушла из моих",
    "pr_gone": "📤 PR пропал",
}
# Что слать по умолчанию, если пользователь не настроил свой набор.
DEFAULT_KINDS: tuple[str, ...] = (
    "new_conflict", "build_failed", "reviewer_needs_work", "new_pr_task",
    "returned_from_testing", "qa_comment", "new_issue", "status_change", "new_comment",
    "resolved", "new_review_pr", "new_pr", "new_pr_comment", "new_pr_commit",
    "reviewer_approved", "pr_all_approved", "build_fixed", "pr_task_resolved", "gone", "pr_gone",
)


class NotifyError(RuntimeError):
    pass


@dataclass
class Role:
    """Моя роль в задаче/PR события: по ней уведомление делится на «МОЁ» и «УЧАСТВУЮ».

    ``assignee`` — на ком задача (по свежим данным); ``actor`` — кто перевёл статус (только
    у событий смены статуса, из истории изменений Jira).
    """

    label: str
    mine: bool
    assignee: str = ""
    assignee_is_me: bool = False
    actor: str = ""


@dataclass
class Notification:
    """Собранное уведомление: заголовок контура + строки событий."""

    workspace: str
    deltas: list[Delta] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)
    # ключ задачи/PR → моя роль; пусто — старый формат без блоков (роли не вычислены)
    roles: dict[str, Role] = field(default_factory=dict)

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
                "returned_from_testing", "qa_comment", "new_issue", "new_review_pr",
                "status_change", "resolved", "new_comment", "pr_ready_to_merge", "pr_all_approved",
                "reviewer_approved", "new_pr_comment", "new_pr_commit", "new_pr", "build_fixed",
                "pr_task_resolved", "gone", "pr_gone"]


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
    "pr_all_approved": "👍 СОБРАЛ ВСЕ АПРУВЫ",
    "pr_ready_to_merge": "🚀 ГОТОВ К МЕРЖУ",
    "new_issue": "🆕 НОВЫЕ ЗАДАЧИ",
    "new_comment": "💬 КОММЕНТАРИИ В ЗАДАЧАХ",
    "resolved": "✔️ РЕШЕНЫ",
    "new_pr": "🔗 НОВЫЕ PR ПО ЗАДАЧАМ",
    "new_review_pr": "👀 НОВЫЕ PR НА РЕВЬЮ",
    "new_pr_commit": "📝 КОММИТЫ В PR",
    "reviewer_approved": "✅ АПРУВЫ",
    "pr_task_resolved": "☑️ ЗАДАЧИ В PR ЗАКРЫТЫ",
    "gone": "📤 УШЛИ ИЗ ВЫБОРКИ",
    "pr_gone": "📤 PR ПРОПАЛИ (СМЕРЖЕН / ОТКЛОНЁН)",
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
    if kind == "reviewer_approved":
        return text.replace(" проапрувил", "")
    if kind == "gone":
        return "" if text.startswith("ушла из выборки") else text
    if kind in ("new_pr_task", "pr_task_resolved", "status_change", "returned_from_testing",
                "pr_all_approved", "pr_ready_to_merge", "new_review_pr", "resolved"):
        return text
    if kind == "new_pr":
        return f"PR {text}" if text else ""
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


ROLE_ASSIGNEE = "👤 исполнитель"
ROLE_MY_PR = "🔧 мой PR"
ROLE_REPORTER = "✍️ автор задачи"
ROLE_REVIEWER = "👀 ревьюер"
ROLE_MENTIONED = "📣 упомянули"
ROLE_WATCHER = "👁 наблюдаю"
STATUS_KINDS = ("status_change", "returned_from_testing")


def explain_gone(info: dict, issue: Optional[Issue], *, me: Iterable[str]) -> str:
    """Почему задача ушла из моих: «In Review → Ready for demo · перевёл Иванов · теперь на Петрове».

    ``info`` — из ``status_actor`` (последний переход статуса и текущий исполнитель),
    ``issue`` — последний снапшот, пока задача была моей. Переход старше снапшота (статус
    тот же) — значит, её не переводили, а переназначили: статус не пишем.
    """
    mine = {m.casefold() for m in me if m}
    parts: list[str] = []
    to = info.get("to") or ""
    was = issue.status if issue is not None else ""
    if to and to != was:
        parts.append(f"{info.get('from') or was or '?'} → {to}")
        if info.get("actor"):
            parts.append(f"перевёл {info['actor']}")
    assignee = info.get("assignee") or ""
    if not assignee:
        parts.append("теперь не назначена")
    elif _is_me(assignee, mine):
        parts.append("по-прежнему на мне")
    else:
        parts.append(f"теперь на {assignee}")
    return " · ".join(parts)


def _is_me(name: str, me: set[str]) -> bool:
    return bool(name) and name.casefold() in me


def resolve_roles(keys: Iterable[str], *, me: Iterable[str], issues: dict[str, Issue],
                  my_prs: Iterable[PR] = (), review_prs: Iterable[PR] = (),
                  mention_keys: Iterable[str] = (),
                  status: Optional[dict[str, dict]] = None) -> dict[str, Role]:
    """Роль по каждому ключу. Порядок проверки — от самого «моего» к самому стороннему.

    Задача: исполнитель → задача моего PR → автор задачи → задача PR на моём ревью →
    упомянули → наблюдаю. PR: мой → на моём ревью. ``status`` — свежие данные по
    задачам со сменой статуса: ``{key: {"actor": …, "assignee": …}}`` (из changelog).
    """
    from .branches import task_key_of

    me_set = {n.casefold() for n in me if n}
    status = status or {}
    mentioned = set(mention_keys)
    my_pr_keys = {f"{p.project}/{p.repository}#{p.id}" for p in my_prs}
    review_pr_keys = {f"{p.project}/{p.repository}#{p.id}" for p in review_prs}
    my_tasks = {task_key_of(p.source_branch) or task_key_of(p.title) for p in my_prs} - {""}
    review_tasks = {task_key_of(p.source_branch) or task_key_of(p.title) for p in review_prs} - {""}
    out: dict[str, Role] = {}
    for key in dict.fromkeys(keys):
        if PR_KEY_RE.match(key):
            if key in my_pr_keys:
                out[key] = Role(ROLE_MY_PR, True)
            else:
                out[key] = Role(ROLE_REVIEWER if key in review_pr_keys else ROLE_WATCHER, False)
            continue
        issue = issues.get(key)
        fresh = status.get(key) or {}
        assignee = fresh.get("assignee", issue.assignee if issue else "") or ""
        reporter = issue.reporter if issue else ""
        a_me = _is_me(assignee, me_set)
        if a_me:
            label, mine = ROLE_ASSIGNEE, True
        elif key in my_tasks:
            label, mine = ROLE_MY_PR, True
        elif _is_me(reporter, me_set):
            label, mine = ROLE_REPORTER, False
        elif key in review_tasks:
            label, mine = ROLE_REVIEWER, False
        elif key in mentioned:
            label, mine = ROLE_MENTIONED, False
        else:
            label, mine = ROLE_WATCHER, False
        out[key] = Role(label, mine, assignee=assignee, assignee_is_me=a_me,
                        actor=fresh.get("actor", "") or "")
    return out


def _role_line(role: Optional[Role], kind: str) -> str:
    """Строка роли под ключом: «👁 наблюдаю · исп. Иванов» / «перевёл Петров · на мне»."""
    if role is None:
        return ""
    parts = [role.label]
    if kind in STATUS_KINDS:
        if role.actor:
            parts.append(f"перевёл {role.actor}")
        parts.append("на мне" if role.assignee_is_me else
                     f"на {role.assignee}" if role.assignee else "не назначена")
    elif role.assignee and not role.assignee_is_me:
        parts.append(f"исп. {role.assignee}")
    return " · ".join(parts)


def _item(key: str, who: str, body: str, role_line: str, *, links: Optional[Links],
          quote: bool = False, body_limit: int = 110) -> list[str]:
    block = item_block(key, who, body, links=links, quote=quote, body_limit=body_limit)
    if role_line:
        block.insert(1, html.escape(_short(role_line, 90)))
    return block


def _sections(deltas: list[Delta], mentions: list[Mention], roles: dict[str, Role],
              links: Optional[Links]) -> list[str]:
    lines: list[str] = []
    by_kind: dict[str, list[Delta]] = {}
    for d in deltas:
        by_kind.setdefault(d.kind, []).append(d)
    order = [k for k in _GROUP_ORDER if k in by_kind] + [k for k in by_kind if k not in _GROUP_ORDER]
    for kind in order:
        title = SECTION_TITLES.get(kind, NOTABLE_KINDS.get(kind, kind).upper())
        lines += section(title, [
            _item(d.key, _who(kind, d.detail), d.summary, _role_line(roles.get(d.key), kind), links=links)
            for d in by_kind[kind]
        ])
    if mentions:
        lines += section("📣 УПОМИНАНИЯ", [
            _item(m.task_key, m.author or "кто-то", strip_mention_tags(m.text),
                  _role_line(roles.get(m.task_key), "mention"), links=links, quote=True, body_limit=160)
            for m in mentions
        ])
    return lines


def _rank(kind: str) -> int:
    return _GROUP_ORDER.index(kind) if kind in _GROUP_ORDER else len(_GROUP_ORDER)


def _role_extra(role: Optional[Role], kinds: set[str]) -> str:
    """Хвост роли для шапки карточки: на ком задача, кто перевёл статус."""
    if role is None:
        return ""
    line = _role_line(role, next((k for k in kinds if k in STATUS_KINDS), ""))
    return line[len(role.label):].lstrip(" ·") if line.startswith(role.label) else ""


def _card(key: str, deltas: list[Delta], mentions: list[Mention], role: Optional[Role],
          links: Optional[Links]) -> list[str]:
    """Карточка задачи или PR: ссылка и роль, заголовок курсивом, ниже — все её события."""
    head = _key_html(key, links)
    if role is not None:
        head += f" · {html.escape(role.label)}"
        extra = _role_extra(role, {d.kind for d in deltas})
        if extra:
            head += f" · {html.escape(_short(extra, 60))}"
    lines = [head]
    summary = next((d.summary for d in deltas if d.summary), "")
    if summary:
        lines.append(f"<i>{html.escape(_short(summary, 110))}</i>")
    for d in sorted(deltas, key=lambda d: _rank(d.kind)):
        label = NOTABLE_KINDS.get(d.kind, d.kind)
        who = _who(d.kind, d.detail)
        lines.append(f"   {html.escape(label)}" + (f" — {html.escape(_short(who, 60))}" if who else ""))
    for m in mentions:
        lines.append(f"   📣 {html.escape(m.author or 'кто-то')}: "
                     f"<i>«{html.escape(_short(strip_mention_tags(m.text), 140))}»</i>")
    return lines


def _cards(deltas: list[Delta], mentions: list[Mention], roles: dict[str, Role],
           links: Optional[Links]) -> list[str]:
    """Карточки по ключу: одна задача/PR — одна карточка, самые тревожные — сверху."""
    by_key: dict[str, list[Delta]] = {}
    for d in deltas:
        by_key.setdefault(d.key, []).append(d)
    by_mention: dict[str, list[Mention]] = {}
    for m in mentions:
        by_mention.setdefault(m.task_key, []).append(m)
    keys = list(dict.fromkeys(list(by_key) + list(by_mention)))
    keys.sort(key=lambda k: min([_rank(d.kind) for d in by_key.get(k, [])] or [len(_GROUP_ORDER)]))
    out: list[str] = []
    for key in keys:
        out.append("")
        out += _card(key, by_key.get(key, []), by_mention.get(key, []), roles.get(key), links)
    return out


def format_message(note: Notification, *, links: Optional[Links] = None) -> str:
    """HTML для Telegram: шапка и карточки — по одной на задачу или PR со всеми её событиями.

    Ключ — ссылкой, рядом моя роль; ниже курсивом заголовок и строки событий («✅ апрув —
    Иванов», «💬 комментарий в PR — +2»). С ролями карточки делятся на «Моё» и «Участвую».
    """
    stamp = datetime.now().strftime("%d.%m %H:%M")
    lines = [f"🔔 <b>{html.escape(note.workspace)}</b> · {stamp}"]
    if note.roles:
        mine = lambda key: bool(note.roles.get(key) and note.roles[key].mine)  # noqa: E731
        for title, pick in (("📌 <b>Моё</b>", True), ("👥 <b>Участвую</b>", False)):
            deltas = [d for d in note.deltas if mine(d.key) is pick]
            mentions = [m for m in note.mentions if mine(m.task_key) is pick]
            if deltas or mentions:
                lines += ["", title] + _cards(deltas, mentions, note.roles, links)
        return "\n".join(lines)
    return "\n".join(lines + _cards(note.deltas, note.mentions, {}, links))


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
             keyboard: bool = False, markup: Optional[dict] = None) -> list[int]:
        """Отправить текст (при необходимости — несколькими сообщениями). Вернуть их id.

        ``reply_to`` — ответить на конкретное сообщение (подтверждение заметки, ответ на
        команду), чтобы в чате было видно, к чему это. ``keyboard`` — приложить постоянную
        клавиатуру с командами (к последнему куску). ``markup`` — своя клавиатура (например,
        inline-кнопки настройки дежурства), тоже к последнему куску; главнее ``keyboard``.
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
            if i == len(chunks) - 1:
                if markup:
                    payload["reply_markup"] = markup
                elif keyboard:
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
        params: dict = {"limit": limit, "timeout": max(0, int(timeout)),
                        "allowed_updates": '["message","callback_query"]'}
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

    def _post(self, method: str, payload: dict) -> dict:
        try:
            resp = self._client.post(f"{self._api}/bot{self.token}/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise NotifyError(f"Telegram недоступен: {exc}") from exc
        if resp.status_code >= 400:
            raise NotifyError(f"Telegram ответил {resp.status_code}: {resp.text[:200]}")
        return resp.json() if resp.content else {}

    def edit(self, message_id: int, text: str, *, markup: Optional[dict] = None) -> None:
        """Заменить текст и кнопки своего сообщения (меню настройки дежурства)."""
        payload: dict = {"chat_id": self.chat_id, "message_id": int(message_id), "text": text,
                         "parse_mode": "HTML", "disable_web_page_preview": True,
                         "reply_markup": markup or {"inline_keyboard": []}}
        self._post("editMessageText", payload)

    def delete(self, message_id: int) -> bool:
        """Удалить сообщение в чате (своё меню или нажатие кнопки меню). Не вышло — не беда."""
        try:
            self._post("deleteMessage", {"chat_id": self.chat_id, "message_id": int(message_id)})
            return True
        except NotifyError:
            return False

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Снять «часики» с нажатой кнопки (без этого Telegram крутит их ~15 с)."""
        payload: dict = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        self._post("answerCallbackQuery", payload)

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
                       *, kinds: Iterable[str] = DEFAULT_KINDS,
                       roles: Optional[dict[str, Role]] = None) -> Notification:
    return Notification(
        workspace=workspace,
        deltas=select_notable(deltas, kinds=kinds),
        mentions=list(mentions),
        roles=dict(roles or {}),
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
