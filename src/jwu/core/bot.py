"""Входящие сообщения Telegram-боту: команды и ответы-заметки.

Бот не только шлёт уведомления — ему можно ответить. Два вида входящих:

- **ответ на уведомление** или сообщение вида ``PROJ-1 текст`` → заметка-контекст по ключу
  (см. ``notify.parse_incoming``), в чат уходит подтверждение «записал»;
- **команда**: ``/sync`` — внеплановый проход демона, ``/status`` — когда был последний
  проход и что накопилось, ``/mentions`` —
  непрочитанные упоминания, ``/duty`` — тумблер дежурства за комнатой Matrix, ``/personal`` —
  тумблер личного (1:1 и упоминания), ``/matrix`` — их настройка кнопками (``core.duty``), ``/help``;
- **нажатие inline-кнопки** (callback_query) настройки дежурства → меню перерисовывается
  на месте.

Всё считается из памяти jwu (без сети и без логина в трекер), поэтому демон может
опрашивать бота часто — раз в минуту — независимо от интервала синка. Сообщения не из
настроенного чата игнорируются; смещение обработанных апдейтов лежит в meta воркспейса.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Callable, Optional

from . import duty, notify
from .dates import fmt_ago

if TYPE_CHECKING:
    from .config import Config
    from .models import Workspace
    from .store import Store

HELP = (
    "<b>Команды</b>\n"
    "/sync — синк всех контуров прямо сейчас\n"
    "/status — последний проход и что накопилось\n"
    "/tasks — сводка моих задач по статусам и PR\n"
    "/today — что сделал сегодня: задачи, их статус и итог\n"
    "/mentions — непрочитанные упоминания\n"
    "/seen — пометить все упоминания прочитанными\n"
    "/duty — дежурство за комнатой Matrix: вкл / выкл\n"
    "/personal — личное: непрочитанное 1:1 и упоминания, вкл / выкл\n"
    "/matrix — настройка Matrix кнопками\n"
    "/help — это сообщение\n\n"
    "Ответ на уведомление → заметка по его ключу. "
    "Сообщение вида <code>PROJ-1 текст</code> → заметка по PROJ-1."
)


def _cmd_status(store: "Store", ws: "Workspace") -> str:
    from .daemon import LAST_PASS_META, LAST_PASS_SUMMARY_META, SingleInstance

    from .daemon import pass_in_progress

    pid = SingleInstance().holder_pid()
    running = pass_in_progress(store)
    last = store.get_meta(LAST_PASS_META)
    summary = (store.get_meta(LAST_PASS_SUMMARY_META) or "").removeprefix("синк: ")
    state = duty.load(store)
    lines = [
        f"ℹ️ <b>{html.escape(ws.slug)}</b>",
        "",
        f"{'🟢 демон работает' if pid else '🔴 демон не запущен'}"
        + (f" · ⏳ синк идёт (начат {fmt_ago(running)})" if running else ""),
        f"🔄 синк: {fmt_ago(last, fallback='ещё не было') if last else 'ещё не было'}"
        + (f" · {html.escape(summary)}" if summary else ""),
    ]
    if state.configured:
        lines.append(f"🔔 дежурство: {'вкл' if state.active else 'выкл'} · {html.escape(state.title)}")
    lines.append(f"💬 личное в Matrix: {'вкл' if state.personal else 'выкл'}")
    return "\n".join(lines)


TASKS_LIMIT = 40

# Блоки «Моих задач» по смыслу статуса: (заголовок, совпадение(статус, QA-статусы, «в работе»)).
_STATUS_BLOCKS = [
    ("🔁 Вернули — доделать", lambda s, q, w: s in q),
    ("🛠 В работе", lambda s, q, w: s in w),
    ("📋 Не начаты", lambda s, q, w: s in ("open", "to do", "selected for development", "backlog")),
    ("👀 На ревью", lambda s, q, w: "review" in s),
    ("🧪 На тестах и дальше", lambda s, q, w: True),
]


def _cmd_tasks(store: "Store", ws: "Workspace", links: Optional[notify.Links]) -> str:
    """Мои задачи по смыслу статуса (что делать — сверху), внутри — по приоритету."""
    from . import todo
    from .daemon import LAST_PASS_META

    qa = todo._statuses(store, ws.id, todo.QA_STATUSES_SETTING, todo.DEFAULT_QA_STATUSES)
    work = set(todo.IN_WORK_STATUSES)
    issues = store.latest_issues("mine")
    prs_mine, prs_review = store.latest_prs("mine"), store.latest_prs("review")
    last = store.get_meta(LAST_PASS_META)
    lines = [f"📋 <b>Мои задачи · {html.escape(ws.slug)}</b> · {len(issues)}",
             f"синк: {fmt_ago(last, fallback='ещё не было') if last else 'ещё не было'}"]
    if not issues:
        lines += ["", "Моих задач нет."]
    blocks: dict[str, list] = {title: [] for title, _ in _STATUS_BLOCKS}
    for issue in issues:
        s = (issue.status or "").casefold()
        for title, match in _STATUS_BLOCKS:
            if match(s, qa, work):
                blocks[title].append(issue)
                break
    shown = 0
    for title, group in blocks.items():
        if not group:
            continue
        group.sort(key=lambda i: (todo._prio(i.priority), i.key))
        lines += ["", f"<b>{title}</b> · {len(group)}"]
        for issue in group:
            if shown >= TASKS_LIMIT:
                break
            icon = todo.PRIORITY_ICON.get(todo._prio(issue.priority), "⚪") if issue.priority else "⚪"
            status = f" · <i>{html.escape(issue.status)}</i>" if title in ("🧪 На тестах и дальше", "📋 Не начаты", "🛠 В работе") else ""
            lines.append(f"{icon} {notify._key_html(issue.key, links)} — {html.escape(notify._short(issue.summary, 60))}{status}")
            shown += 1
    if len(issues) > shown:
        lines += ["", f"… и ещё {len(issues) - shown}"]
    lines += ["", "🔴 blocker · 🟠 critical · 🟡 major · 🟢 minor",
              f"PR: моих {len(prs_mine)}, в ревью у меня {len(prs_review)}"]
    return "\n".join(lines)


TODAY_TASKS = 20
SUMMARY_MAX = 160


def _cmd_today(store: "Store", ws: "Workspace", links: Optional[notify.Links]) -> str:
    """Что делал сегодня: задачи с заголовком, текущим статусом и итогом простыми словами.

    Задачи — из работ jwu с записями за сегодня (в поясе ворклогов контура). «Что делалось»
    — последний ``summary`` за сегодня у каждой работы задачи; их нет — заголовок работы.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from . import timechain

    tz = datetime.now().astimezone().tzinfo or ZoneInfo("UTC")   # пояс не настроен — системный
    name = timechain.get_timezone(store, ws.id) or ""
    if name:
        try:
            tz = timechain.resolve_tz(name)
        except Exception:  # noqa: BLE001
            pass
    today = datetime.now(tz).date()

    def local(ts: str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz)
        except ValueError:
            return None

    issues = {i.key: i for i in store.latest_issues()}
    tasks: dict[str, dict] = {}
    for job in store.list_jobs():
        recs = [(local(r.ts), r) for r in job.records]
        recs = [(dt, r) for dt, r in recs if dt and dt.date() == today]
        if not recs:
            continue
        key = job.task_key or job.feature_key or f"#{job.id}"
        task = tasks.setdefault(key, {"first": recs[0][0], "summaries": [], "titles": [], "job": job})
        task["first"] = min(task["first"], min(dt for dt, _ in recs))
        # итог работы — последний: закрыли повторно — прежний итог устарел, не дублируем
        summaries = [r.text for _, r in recs if r.kind == "summary" and r.text.strip()]
        task["summaries"] += summaries[-1:]
        task["titles"].append(job.title)
    head = f"🕒 <b>Сегодня</b> · {today.strftime('%d.%m')}"
    if not tasks:
        return head + "\n\nВ журнале работ за сегодня пусто."
    lines = [head]
    for key, task in sorted(tasks.items(), key=lambda kv: kv[1]["first"])[:TODAY_TASKS]:
        job = task["job"]
        issue = issues.get(key)
        if issue is not None:
            title, status = issue.summary, issue.status
        elif job.feature_id:
            feature = store.get_feature(job.feature_id)
            title = feature.title if feature else job.title
            status = feature.status if feature else ""
        else:
            title, status = job.title, ""
        lines.append("")
        lines.append(f"{notify._key_html(key, links) if not key.startswith('#') else ''}"
                     + (f" · {html.escape(status)}" if status else "")
                     if not key.startswith("#") else (f"<b>{html.escape(status)}</b>" if status else "—"))
        lines.append(f"<i>{html.escape(notify._short(title, 110))}</i>")
        done = task["summaries"][-2:] or [t for t in dict.fromkeys(task["titles"]) if t and t != title][:1]
        for text in done:
            lines.append(f"   ✍️ {html.escape(notify._short(text.splitlines()[0], SUMMARY_MAX))}")
    if len(tasks) > TODAY_TASKS:
        lines += ["", f"… и ещё {len(tasks) - TODAY_TASKS}"]
    return "\n".join(lines)


def _cmd_mentions(store: "Store", links: Optional[notify.Links]) -> str:
    items = store.unseen_mentions()
    if not items:
        return "📣 Непрочитанных упоминаний нет."
    lines = notify.section(f"📣 НЕПРОЧИТАННЫЕ УПОМИНАНИЯ ({len(items)})", [
        notify.item_block(m.task_key, m.author or "кто-то", notify.strip_mention_tags(m.text),
                          links=links, quote=True, body_limit=160)
        for m in items[:10]
    ])
    if len(items) > 10:
        lines += ["", f"… и ещё {len(items) - 10}, /seen — пометить все прочитанными"]
    return "\n".join(lines).lstrip("\n")


SYNC_REQUEST_META = "daemon:sync_requested"   # {slug: когда попросили /sync} — общий meta БД


def request_sync(store: "Store", slug: str) -> None:
    """Запомнить, что из чата контура попросили /sync: итог отправит демон после прохода.

    В БД, а не в памяти цикла: /sync забирают два пути (опрос бота между проходами и хук
    после синка), и итог должен прийти, каким бы путём команда ни пришла.
    """
    import json
    from datetime import datetime, timezone

    try:
        pending = json.loads(store.get_meta(SYNC_REQUEST_META) or "{}")
    except ValueError:
        pending = {}
    pending.setdefault(slug, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    store.set_meta(SYNC_REQUEST_META, json.dumps(pending))


def handle_command(text: str, *, store: "Store", ws: "Workspace", login: str,
                   links: Optional[notify.Links], on_sync: Optional[Callable[[], bool]]) -> str:
    """Ответ на команду (HTML). Неизвестная команда → подсказка."""
    cmd = text.split()[0].lower().split("@")[0]
    if cmd == "/sync":
        if on_sync is None:
            return "Демон не запущен — синк запустить некому. Руками: <code>jwu sync</code>."
        request_sync(store, ws.slug)
        from .daemon import pass_in_progress

        running = pass_in_progress(store)
        if running:
            on_sync()
            return f"⏳ Синк уже идёт (начат {fmt_ago(running)}) — итог пришлю."
        return "🔄 Синк запущен — по окончании пришлю итог." if on_sync() else "🔄 Синк уже идёт — итог пришлю."
    if cmd == "/status":
        return _cmd_status(store, ws)
    if cmd == "/mentions":
        return _cmd_mentions(store, links)
    if cmd == "/tasks":
        return _cmd_tasks(store, ws, links)
    if cmd == "/today":
        return _cmd_today(store, ws, links)
    if cmd == "/seen":
        n = len(store.unseen_mentions())
        store.mark_mentions_seen(None)
        return f"✅ Помечено прочитанными: {n}." if n else "✅ Непрочитанных и не было."
    if cmd in ("/help", "/start"):
        return HELP
    return f"Не знаю команду {html.escape(cmd)}.\n\n{HELP}"


MENU_PREFIX = "menu:"
# (подпись, действие) — inline-меню; действие → команда того же имени
MENU_ROWS = [
    [("🗂 Что делать", "board")],
    [("🔄 Синк сейчас", "sync"), ("ℹ️ Статус", "status")],
    [("📋 Мои задачи", "tasks"), ("🕒 Что сделал сегодня", "today")],
    [("📣 Упоминания", "mentions"), ("✅ Прочитал все", "seen")],
    [("📡 Matrix: дежурство и личное", "matrix")],
    [("⏰ Утренняя сводка", "brief")],
]


def _menu_btn(text: str, action: str) -> dict:
    return {"text": text, "callback_data": f"{MENU_PREFIX}{action}"}


def _addon_buttons() -> list[tuple[str, str]]:
    from . import addons

    out: list[tuple[str, str]] = []
    for addon in addons.load():
        for label, action in addon.call("menu_buttons", default=[]) or []:
            out.append((label, f"addon:{addon.name}:{action}"))
    return out


def main_menu() -> tuple[str, dict]:
    rows = [[_menu_btn(label, action) for label, action in row] for row in MENU_ROWS]
    rows += [[_menu_btn(label, action)] for label, action in _addon_buttons()]
    return ("📋 <b>Меню jwu</b>", {"inline_keyboard": rows})


BACK_TO_MENU = [_menu_btn("← меню", "main")]


def handle_menu(action: str, *, store: "Store", ws: "Workspace", cfg: "Config", login: str,
                on_sync: Optional[Callable[[], bool]]) -> tuple[str, dict]:
    """Кнопка меню → (текст, клавиатура) для того же сообщения. Под ответом — «← меню»."""
    if action == "main":
        return main_menu()
    if action.startswith("addon:"):
        from . import addons

        _, name, sub = action.split(":", 2)
        for addon in addons.load():
            if addon.name == name:
                res = addon.call("menu_action", {"store": store, "workspace": ws, "cfg": cfg}, sub)
                if res:
                    text, markup = res
                    return text, {"inline_keyboard": list((markup or {}).get("inline_keyboard") or []) + [BACK_TO_MENU]}
        return "Аддон не ответил.", {"inline_keyboard": [BACK_TO_MENU]}
    if action == "board":
        from . import brief, todo

        text = brief.render(todo.build(store, ws.id, login, session="telegram"), ws.slug,
                            notify.links_from_config(cfg))
        text = text.replace("☀️", "🗂", 1)
        return text, {"inline_keyboard": [BACK_TO_MENU]}
    if action == "brief":
        from . import brief

        text, markup = brief.menu(store, ws.id)
        return text, {"inline_keyboard": list(markup.get("inline_keyboard") or []) + [BACK_TO_MENU]}
    if action == "matrix":
        text, markup = duty.menu(duty.load(store))
        return text, {"inline_keyboard": list(markup.get("inline_keyboard") or []) + [BACK_TO_MENU]}
    reply = handle_command(f"/{action}", store=store, ws=ws, login=login,
                           links=notify.links_from_config(cfg), on_sync=on_sync)
    if len(reply) > notify.MAX_MESSAGE:
        reply = reply[:notify.MAX_MESSAGE].rsplit("\n", 1)[0] + "\n…"
    return reply, {"inline_keyboard": [BACK_TO_MENU]}


DUTY_COMMANDS = ("/duty", "/personal", "/matrix", "/duty_settings")


def _matrix_factory(cfg: "Config"):
    """Ленивый конструктор клиента Matrix: сеть нужна не каждой кнопке."""
    from . import matrix

    return lambda: matrix.client_from_config(cfg)


def handle_duty_command(cmd: str, *, store: "Store", cfg: "Config") -> tuple[str, dict]:
    """/duty, /personal → тумблеры; /matrix → меню настройки кнопками."""
    factory = _matrix_factory(cfg)
    if cmd in ("/matrix", "/duty_settings"):
        return duty.menu(duty.load(store))
    if cmd == "/personal":
        return duty.toggle_personal(store, factory)
    return duty.toggle(store, factory)


def handle_callback(upd: dict, *, store: "Store", cfg: "Config",
                    sender: notify.TelegramNotifier, ws: Optional["Workspace"] = None,
                    login: str = "", on_sync: Optional[Callable[[], bool]] = None) -> Optional[dict]:
    """Нажатие inline-кнопки из нашего чата → перерисовать меню на месте."""
    cq = upd.get("callback_query") or {}
    msg = cq.get("message") or {}
    if str((msg.get("chat") or {}).get("id", "")) != str(sender.chat_id):
        return None
    data = str(cq.get("data") or "")
    try:
        sender.answer_callback(str(cq.get("id", "")), "⏳ принято")
    except notify.NotifyError:
        pass  # часики сами пропадут
    mid = msg.get("message_id")
    # «⏳ делаю…» — на любой кнопке, кроме мгновенных (вернуться в меню): аддоны и сеть
    # бывают небыстрыми, а молчание выглядит как «бот умер».
    instant = {MENU_PREFIX + "main", duty.CALLBACK_PREFIX + duty.CB_MENU}
    if mid and data not in instant:
        try:   # долгое — сразу видно, что бот жив и работает
            sender.edit(int(mid), "⏳ делаю…")
        except notify.NotifyError:
            pass
    try:
        return _handle_callback_action(data, mid, store=store, cfg=cfg, sender=sender, ws=ws,
                                       login=login, on_sync=on_sync)
    except Exception as exc:  # noqa: BLE001 — ответ должен быть всегда, даже ошибкой
        text = f"⚠️ Не получилось: {html.escape(str(exc) or type(exc).__name__)}"
        try:
            if mid:
                sender.edit(int(mid), text, markup={"inline_keyboard": [BACK_TO_MENU]})
            else:
                sender.send(text)
        except notify.NotifyError:
            pass
        return {"kind": "error", "text": data, "reply": text}


SLOW_ACTIONS = {"sync", "tasks", "today", "mentions", "status", "board"}


def _handle_callback_action(data: str, mid, *, store, cfg, sender, ws, login, on_sync) -> dict:
    if data.startswith(MENU_PREFIX) and ws is not None:
        text, markup = handle_menu(data[len(MENU_PREFIX):], store=store, ws=ws, cfg=cfg,
                                   login=login, on_sync=on_sync)
    elif data.startswith("brief:") and ws is not None:
        from . import brief

        text, markup = brief.handle_callback(store, ws.id, data)
        markup = {"inline_keyboard": list(markup.get("inline_keyboard") or []) + [BACK_TO_MENU]}
    elif data.startswith("git:"):
        from . import gitgrant

        text, markup = gitgrant.handle_callback(store, data)
    elif data.startswith(duty.CALLBACK_PREFIX):
        text, markup = duty.handle_callback(store, _matrix_factory(cfg), data)
        if data in (duty.CALLBACK_PREFIX + duty.CB_MENU, duty.CALLBACK_PREFIX + duty.CB_ON,
                    duty.CALLBACK_PREFIX + duty.CB_OFF, duty.CALLBACK_PREFIX + duty.CB_PERSONAL) \
                or data.startswith(duty.CALLBACK_PREFIX + duty.CB_ROOM):
            markup = {"inline_keyboard": list((markup or {}).get("inline_keyboard") or []) + [BACK_TO_MENU]}
    else:
        text, markup = "Эта кнопка устарела — открой меню заново.", {"inline_keyboard": [BACK_TO_MENU]}
    if mid:
        sender.edit(int(mid), text, markup=markup)
    else:
        sender.send(text, markup=markup or None)
    return {"kind": "button", "text": data, "reply": text}


def _addon_text(store: "Store", ws: "Workspace", cfg: "Config", text: str) -> Optional[str]:
    """Присланный боту текст — сначала аддонам (например, ссылка, которую аддон умеет запомнить)."""
    from . import addons

    for addon in addons.load():
        reply = addon.call("bot_text", {"store": store, "workspace": ws, "cfg": cfg}, text)
        if reply:
            return reply
    return None


def show_menu(store: "Store", sender: notify.TelegramNotifier, *, pressed: Optional[int],
              text: str = "") -> dict:
    """Одно меню на чат: старое и нажатие «📋 Меню» удаляются, новое — внизу, без цитаты."""
    for mid in (pressed, store.get_workspace_meta(notify.MENU_META)):
        if mid:
            sender.delete(int(mid))
    body, markup = main_menu()
    ids = sender.send(body, markup=markup)
    if ids and ids[-1]:
        store.set_workspace_meta(notify.MENU_META, str(ids[-1]))
    return {"kind": "command", "text": text, "reply": body}


def handle_update(upd: dict, *, store: "Store", ws: "Workspace", cfg: "Config",
                  sender: notify.TelegramNotifier, login: str = "",
                  on_sync: Optional[Callable[[], bool]] = None) -> Optional[dict]:
    """Одно входящее: нажатие кнопки, команда или заметка → ответ в чат. None — не наше.

    Ответ есть всегда: упало что-то внутри — в чат уходит «⚠️ Не получилось: …», чтобы
    по молчанию бота было понятно одно — демон не работает.
    """
    try:
        return _handle_update(upd, store=store, ws=ws, cfg=cfg, sender=sender, login=login,
                              on_sync=on_sync)
    except Exception as exc:  # noqa: BLE001
        text = f"⚠️ Не получилось: {html.escape(str(exc) or type(exc).__name__)}"
        try:
            sender.send(text)
        except notify.NotifyError:
            pass
        return {"kind": "error", "text": str((upd.get("message") or {}).get("text") or ""), "reply": text}


def _handle_update(upd: dict, *, store: "Store", ws: "Workspace", cfg: "Config",
                   sender: notify.TelegramNotifier, login: str = "",
                   on_sync: Optional[Callable[[], bool]] = None) -> Optional[dict]:
    if upd.get("callback_query"):
        return handle_callback(upd, store=store, cfg=cfg, sender=sender, ws=ws,
                               login=login, on_sync=on_sync)
    msg = upd.get("message") or {}
    if str((msg.get("chat") or {}).get("id", "")) != str(sender.chat_id):
        return None
    links = notify.links_from_config(cfg)
    text = " ".join((msg.get("text") or "").split())
    reply_to = msg.get("message_id")
    markup: dict = {}
    cmd = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""
    if text == notify.MENU_BUTTON or cmd in ("/menu", "/start"):
        return show_menu(store, sender, pressed=reply_to, text=text)
    if cmd in DUTY_COMMANDS:
        reply, markup = handle_duty_command(cmd, store=store, cfg=cfg)
        item = {"kind": "command", "text": text, "reply": reply}
    elif text.startswith("/"):
        reply = handle_command(text, store=store, ws=ws, login=login, links=links, on_sync=on_sync)
        item = {"kind": "command", "text": text, "reply": reply}
    else:
        addon_reply = _addon_text(store, ws, cfg, text)
        if addon_reply:
            sender.send(addon_reply, reply_to=reply_to)
            return {"kind": "addon", "text": text, "reply": addon_reply}
        parsed = notify.parse_incoming(upd, chat_id=sender.chat_id)
        if parsed is None:
            reply = ("Не понял, к чему это. Ответь на уведомление или начни с ключа: "
                     "<code>PROJ-1 текст</code>. Меню — кнопка «📋 Меню»")
            item = {"kind": "ignored", "text": text}
        else:
            key, note_text = parsed
            note = store.add_note(key, note_text, author="telegram", kind="context")
            reply = f"📝 записал в {notify._key_html(key, links)}: <i>{html.escape(notify._short(note_text, 120))}</i>"
            item = {"kind": "note", "key": key, "note_id": note.id, "text": note_text}
    # Ответом (с цитатой) — только к заметкам: видно, к чему «записал». Команды — просто сообщением.
    if item["kind"] not in ("note", "ignored"):
        reply_to = None
    sender.send(reply, reply_to=reply_to, markup=markup or None,
                keyboard=text.lower().startswith("/help"))
    return item


def process_updates(
    store: "Store", ws: "Workspace", cfg: "Config", sender: notify.TelegramNotifier, *,
    login: str = "", on_sync: Optional[Callable[[], bool]] = None, long_poll: int = 0,
    dispatch: Optional[Callable[[dict], None]] = None,
) -> list[dict]:
    """Забрать новые сообщения боту и обработать.

    ``long_poll`` — сколько секунд Telegram может держать запрос в ожидании сообщения.
    ``dispatch`` — отдать каждое входящее исполнителю (демон: пул потоков, чтобы слушатель
    кнопок не ждал синка, Matrix и прочих долгих ответов); тогда смещение сдвигается сразу,
    а в списке — ``{"kind": "dispatched"}``. Без него — обработать здесь же, по порядку.
    Смещение сдвигается и для пропущенных сообщений, чтобы чужие не крутились вечно.
    """
    store.use_workspace(ws.id)
    offset = int(store.get_workspace_meta(notify.OFFSET_META) or 0)
    updates = sender.get_updates(offset + 1 if offset else None, timeout=long_poll)
    last = max([offset] + [int(u.get("update_id", 0) or 0) for u in updates])
    if dispatch is not None:
        if last != offset:
            store.set_workspace_meta(notify.OFFSET_META, str(last))
        for upd in updates:
            dispatch(upd)
        return [{"kind": "dispatched", "text": ""} for _ in updates]
    handled: list[dict] = []
    for upd in updates:
        item = handle_update(upd, store=store, ws=ws, cfg=cfg, sender=sender, login=login, on_sync=on_sync)
        if item:
            handled.append(item)
    if last != offset:
        store.set_workspace_meta(notify.OFFSET_META, str(last))
    return handled
