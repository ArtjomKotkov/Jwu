"""Живой TUI-дашборд на Textual.

App получает собранный из памяти ``DashboardData``. Refresh — посекционный (только активная
вкладка), детали PR тянутся лениво при заходе. Сам TUI не знает о токенах/сети (всё через
переданные callable) — это держит его тестируемым через Pilot.
"""

from __future__ import annotations

import re
import webbrowser
import zlib
from pathlib import Path
from datetime import datetime, timezone
from time import monotonic
from typing import Callable, Optional

from rich import box
from rich.console import Group, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    ContentSwitcher, DataTable, Header, Static, Tab, Tabs,
)

from ..core import gitinfo
from ..core.jira import JiraError
from .copy_modal import (
    copy_items_for_issue,
    copy_items_for_job,
    copy_items_for_pr,
    notify_copied,
    open_copy_modal,
)
from ..core.models import (
    JOB_RECORD_BADGES,
    LOCAL_FEATURE_BADGES,
    WORKSPACE_RULE_BADGES,
    Issue,
    Job,
    LocalFeature,
    Mention,
    PR,
    WorkspaceRule,
    PRComment,
    Workspace,
    WorkspacePath,
    classify_attachment,
)
from .workspace_screens import (
    FeatureDetailScreen,
    LegendScreen,
    RuleDetailScreen,
    RuleEditScreen,
    TextPromptScreen,
    WorkspacePickerScreen,
    WorkspaceTree,
)
from ..core.service import DashboardData, PRDetail
from ..core.ui_prefs import UIPrefs, load_ui_prefs, save_ui_prefs


def _classify_sync_error(exc: BaseException) -> str:
    """Короткий текст ошибки синка для пользователя.

    Ошибку доступа (логин/токен/гейт — 401/403 или JiraError при логине) показываем
    как «Не удалось авторизоваться», а не сырым дампом ответа Jira/Bitbucket.
    Остальное (сеть и пр.) — как есть.
    """
    code = getattr(exc, "status_code", None)
    if code in (401, 403) or (isinstance(exc, JiraError) and code is None
                              and "Логин" in str(exc)):
        return "Не удалось авторизоваться"
    return str(exc) or "Не удалось синхронизироваться"

DELTA_ICON = {
    "new_issue": "🆕",
    "status_change": "🔁",
    "new_comment": "💬",
    "new_pr": "🔀",
    "new_pr_comment": "💬",
    "new_pr_commit": "⬆",
    "reviewer_approved": "✅",
    "new_conflict": "⚠",
    "resolved": "✅",
    "gone": "🚪",
    "pr_gone": "🏁",
}

# Чем подписать изменение, если у дельты нет своего detail (обычно он есть и точнее).
DELTA_LABEL = {
    "new_issue": "новая задача",
    "status_change": "смена статуса",
    "new_comment": "новые комментарии",
    "new_pr": "новый PR",
    "new_pr_comment": "новые комментарии",
    "new_pr_commit": "новый коммит",
    "reviewer_approved": "апрув",
    "new_conflict": "merge-конфликт",
    "resolved": "решена",
    "gone": "ушла из выборки",
    "pr_gone": "смержен / отклонён",
}

# Дельты, у которых detail — голое значение («10564», «Fixed»), а не фраза: им нужна
# подпись, иначе строка в панели читается как ребус.
DELTA_PREFIXED = {"new_pr", "resolved"}


def delta_chip(kind: str, detail: str) -> str:
    """Что именно случилось — одной строкой для панели изменений."""
    label = DELTA_LABEL.get(kind, kind)
    if not detail:
        return label
    return f"{label}: {detail}" if kind in DELTA_PREFIXED else detail

# Иконка по виду вложения (Attachment.kind) — для правой колонки карточки задачи.
ATTACH_ICON = {"image": "🖼", "log": "📄", "doc": "📕", "archive": "🗜", "video": "🎬", "other": "📎"}

# pane id -> (table id, kind, section-токен)
# Набор ПОЛНЫЙ всегда: вкладки не создаются/не удаляются, а прячутся (см.
# _apply_tab_visibility) — так compose/on_mount остаются простыми и предсказуемыми.
TABS = {
    "tab-workspaces": ("t-workspaces", "workspace", "workspaces"),
    "tab-structure": ("t-structure", "tree", "structure"),
    "tab-rules": ("t-rules", "rule", "rules"),
    "tab-mine": ("t-mine", "issue", "mine"),
    "tab-mentions": ("t-mentions", "mention", "mentions"),
    "tab-features": ("t-features", "feature", "features"),
    "tab-prs-mine": ("t-prs-mine", "pr", "prs_mine"),
    "tab-prs-review": ("t-prs-review", "pr", "prs_review"),
    "tab-jobs": ("t-jobs", "job", "jobs"),
}

_TAB_TITLE = {
    "tab-workspaces": "Workspace",
    "tab-structure": "Структура",
    "tab-rules": "Правила",
    "tab-mine": "Мои задачи",
    "tab-mentions": "Упоминания",
    "tab-features": "Фичи",
    "tab-prs-mine": "PR: мои",
    "tab-prs-review": "PR: на ревью",
    "tab-jobs": "Работы",
}

# Две полосы вкладок: наверху — выбор контура (единственное, что не зависит от того,
# какой воркспейс активен), внизу — всё его содержимое. Разделение именно рядами, а не
# разделителем внутри одной полосы: так видно, что переключение вверху меняет контекст
# для всего, что внизу. «Структура» и «Правила» идут первыми в нижнем ряду — это ответ
# на вопросы «где код» и «как тут принято», с них начинается работа в незнакомом контуре.
GLOBAL_TABS = ("tab-workspaces",)
PROJECT_TABS = ("tab-structure", "tab-rules", "tab-mine", "tab-mentions", "tab-features",
                "tab-prs-mine", "tab-prs-review", "tab-jobs")
TAB_ROWS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tabs-global", GLOBAL_TABS),
    ("tabs-project", PROJECT_TABS),
)
_TAB_ORDER = GLOBAL_TABS + PROJECT_TABS
_ROW_OF_PANE = {pane: row for row, panes in TAB_ROWS for pane in panes}

# Вкладки, которые видны только при подключённой интеграции воркспейса.
JIRA_TABS = ("tab-mine", "tab-mentions")
BITBUCKET_TABS = ("tab-prs-mine", "tab-prs-review")


# Textual зовёт клавиши по именам («question_mark»), человек — символами.
_KEY_LABEL = {
    "question_mark": "?", "slash": "/", "backslash": "\\", "escape": "esc",
    "left_square_bracket": "[", "right_square_bracket": "]",
    "full_stop": ".", "comma": ",", "minus": "-", "plus": "+", "underscore": "_",
    "space": "пробел", "enter": "enter", "backspace": "backspace",
}


def _key_label(keys: str) -> str:
    """«question_mark» → «?», «escape,backspace,q» → «esc / backspace / q»."""
    return " / ".join(_KEY_LABEL.get(k.strip(), k.strip()) for k in keys.split(",") if k.strip())


# Клавиши, которых нет в BINDINGS (их обрабатывают сами виджеты), но без которых
# легенда врёт: enter в таблице — основное действие вкладки.
_TAB_HINTS = {
    "workspaces": [("enter", "Переключиться на этот воркспейс")],
    "structure": [("→ / ←", "Раскрыть / свернуть узел дерева (или мышью)")],
    "rules": [("enter", "Открыть правило целиком")],
    "mine": [("enter", "Открыть карточку задачи")],
    "mentions": [("enter", "Открыть задачу и пометить упоминание прочитанным")],
    "features": [("enter", "Открыть фичу")],
    "prs_mine": [("enter", "Открыть PR")],
    "prs_review": [("enter", "Открыть PR")],
    "jobs": [("enter", "Открыть работу")],
}

# Как назвать детальный экран в шапке легенды (иначе туда уедет имя класса).
_SCREEN_TITLE = {
    "IssueDetailScreen": "карточка задачи",
    "PRDetailScreen": "карточка PR",
    "JobDetailScreen": "карточка работы",
    "FeatureDetailScreen": "карточка фичи",
    "RuleDetailScreen": "карточка правила",
}


def _binding_parts(binding) -> tuple[str, str, str]:
    """(клавиша, действие, подпись) из Binding либо из кортежа — в BINDINGS бывает и то и то."""
    if isinstance(binding, tuple):
        key, action, description = (list(binding) + ["", "", ""])[:3]
        return str(key), str(action), str(description)
    return (str(binding.key), str(binding.action),
            str(getattr(binding, "description", "") or ""))


def _bar_id(pane_id: str) -> str:
    """id кнопки вкладки в полосе по id её панели («tab-mine» → «bar-mine»)."""
    return "bar-" + pane_id[len("tab-"):]


def _pane_id(bar_id: str) -> str:
    return "tab-" + bar_id[len("bar-"):]


class DashboardTable(DataTable):
    BINDINGS = [
        *DataTable.BINDINGS,
        Binding("h", "cursor_left", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("l", "cursor_right", show=False),
    ]


class TabBar(Tabs):
    """Полоса вкладок, которая сообщает о ВЫБОРЕ пользователя, а не о смене подсветки.

    Штатный ``Tabs.TabActivated`` для деки не годится сразу с двух сторон: он летит и
    при авто-активации первой вкладки на монтировании (это не выбор), и НЕ летит, когда
    кликают по уже подсвеченной вкладке — а для второй полосы это как раз полноценный
    выбор («вернуться сюда»). Поэтому ловим ровно пользовательские жесты.
    """

    class Picked(Message):
        def __init__(self, bar: "TabBar", tab_id: str) -> None:
            super().__init__()
            self.bar = bar
            self.tab_id = tab_id

    async def _on_tab_clicked(self, event: Tab.Clicked) -> None:
        tab_id = event.tab.id or ""
        await super()._on_tab_clicked(event)
        self.post_message(self.Picked(self, tab_id))

    def _on_underline_clicked(self, event) -> None:
        super()._on_underline_clicked(event)
        self.post_message(self.Picked(self, self.active))

    def action_next_tab(self) -> None:
        super().action_next_tab()
        self.post_message(self.Picked(self, self.active))

    def action_previous_tab(self) -> None:
        super().action_previous_tab()
        self.post_message(self.Picked(self, self.active))


class TabDeck(Vertical):
    """Две полосы вкладок над общим переключателем панелей.

    ``TabbedContent`` даёт ровно одну полосу, а нам нужны два ряда: общие вкладки
    сверху, проектные под ними. Поэтому полосы (``Tabs``) и содержимое
    (``ContentSwitcher``) собираются вручную, а этот виджет держит их согласованными
    и отдаёт наружу простой API: ``active``, ``get_tab``, ``set_pane_visible``.

    Полоса, которой активная панель НЕ принадлежит, гасится (``-row-idle``): её
    подсвеченная вкладка — это «куда вернёшься», а не «где ты сейчас».
    """

    DEFAULT_CSS = """
    TabDeck { height: 1fr; }
    TabDeck > TabBar { height: 2; }
    TabDeck > TabBar.-row-idle { opacity: 55%; }
    TabDeck > ContentSwitcher { height: 1fr; }
    """

    class Activated(Message):
        """Сменилась активная панель (клик по вкладке или программно)."""

        def __init__(self, deck: "TabDeck", pane: str) -> None:
            super().__init__()
            self.deck = deck
            self.pane = pane

        @property
        def control(self) -> "TabDeck":
            return self.deck

    def __init__(self, *children, id: str | None = None) -> None:
        super().__init__(*children, id=id)
        self._active = ""

    # --- состояние ------------------------------------------------------- #

    @property
    def active(self) -> str:
        return self._active

    @active.setter
    def active(self, pane_id: str) -> None:
        if pane_id and pane_id != self._active:
            self._activate(pane_id)

    def _activate(self, pane_id: str) -> None:
        self._active = pane_id
        self._sync_bars()
        self.post_message(self.Activated(self, pane_id))

    def _sync_bars(self) -> None:
        """Подтянуть полосы и переключатель под текущую активную панель."""
        try:
            self.query_one("#panes", ContentSwitcher).current = self._active
        except Exception:  # noqa: BLE001 — панель могла не смонтироваться (тестовая сборка)
            return
        for row_id, panes in TAB_ROWS:
            try:
                tabs = self.query_one(f"#{row_id}", TabBar)
            except Exception:  # noqa: BLE001
                continue
            owns = self._active in panes
            tabs.set_class(not owns, "-row-idle")
            if owns:
                tabs.active = _bar_id(self._active)

    def on_tab_bar_picked(self, event: TabBar.Picked) -> None:
        """Пользователь выбрал вкладку в любой из полос → показать её панель."""
        event.stop()
        pane = _pane_id(event.tab_id)
        if pane in TABS and pane != self._active:
            self._activate(pane)

    # --- вкладки --------------------------------------------------------- #

    def get_tab(self, pane_id: str) -> Tab:
        return self.query_one(f"#{_bar_id(pane_id)}", Tab)

    def set_pane_visible(self, pane_id: str, visible: bool) -> None:
        """Показать/спрятать вкладку. Класс ставим сами, а не через Tabs.hide():
        тот при скрытии активной вкладки сам выбирает следующую и шлёт событие —
        то есть уводит панель из-под рук, пока мы ещё пересобираем видимость."""
        try:
            tab = self.get_tab(pane_id)
        except Exception:  # noqa: BLE001
            return
        tab.set_class(not visible, "-hidden")
        row = self._row(pane_id)
        if row is not None:
            # подчёркивание активной вкладки живёт в координатах строки — после
            # смены её состава его надо пересчитать (так же делает сам Tabs.hide)
            highlight = getattr(row, "_highlight_active", None)
            if highlight is not None:
                row.call_after_refresh(highlight)

    def _row(self, pane_id: str) -> Optional[TabBar]:
        try:
            return self.query_one(f"#{_ROW_OF_PANE[pane_id]}", TabBar)
        except Exception:  # noqa: BLE001
            return None

ISSUE_COLUMNS = ["Key", "Статус", "Приоритет", "Summary"]
MENTION_COLUMNS = ["Когда", "Задача", "От кого", "Упоминание"]
# «Мои PR» — с контекстом задачи (кому уже не моя, кому ушла), на ревью — без него.
PR_MINE_COLUMNS = ["PR", "Конфликт", "Задача", "Назначен", "Статус", "Title", "Ревью"]
PR_REVIEW_COLUMNS = ["PR", "Конфликт", "Title", "Ревью"]
JOB_COLUMNS = ["ID", "Обновлено", "Статус", "Якорь", "PR", "Title"]
WORKSPACE_COLUMNS = ["", "Workspace", "Название", "Jira", "Bitbucket", "Папок", "Работ"]
PATH_COLUMNS = ["Папка", "Git", "Метка", "Состояние", "Добавлена"]
FEATURE_COLUMNS = ["Ключ", "Обновлена", "Статус", "Приоритет", "Название"]
RULE_COLUMNS = ["Тип", "Тег", "Правило", "Обновлено"]


from ..core.dates import (  # noqa: E402
    day_key as _day_key,
    day_name as _day_name,
    fmt_ago as _fmt_ago,
    fmt_day as _fmt_day,
    fmt_dt as _fmt_dt,
)


def _day_label(when: str) -> str:
    """Заголовок дня для разделителя: «Сегодня — 30.07.2026»."""
    return f"{_day_name(when).capitalize()} — {_fmt_day(when)}"


def _day_color(when: str) -> str:
    """Цвет названия дня: свежее — заметнее (сегодня зелёным, вчера жёлтым)."""
    return {"сегодня": "green", "вчера": "yellow"}.get(_day_name(when), "cyan")


def _human_size(n: int) -> str:
    size = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{int(size)} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(n)} Б"


def _fmt_dur(secs: float) -> str:
    secs = max(0, int(secs))
    m, s = divmod(secs, 60)
    return f"{m}м {s:02d}с" if m else f"{s}с"


def _conflict_cell(pr: PR) -> str:
    return "—" if pr.conflicted is None else ("⚠ да" if pr.conflicted else "нет")


# Jira wiki блоки кода: {code:lang}...{code}, {noformat}...{noformat}, а также
# ```fenced``` (так оформляют код в комментах) — все идут отдельной панелью.
_JIRA_BLOCK_RE = re.compile(
    r"\{code(?::([^}]*))?\}\n?(.*?)\{code\}"
    r"|\{noformat\}\n?(.*?)\{noformat\}"
    r"|```[ \t]*([\w.+#-]*)[ \t]*\n?(.*?)```",
    re.DOTALL,
)


# Инлайн-разметка Jira внутри прозы: упоминания [~login], вложения [^файл],
# встроенные картинки !img.png!, ссылки [текст|url] / [url] и голые http(s)-ссылки.
# Порядок важен (упоминание/вложение раньше ссылки — у них тоже скобка `[`).
_INLINE_RE = re.compile(
    r"\[~(?P<user>[^\]\r\n]+)\]"
    r"|\[\^(?P<file>[^\]\r\n]+)\]"
    r"|!(?P<img>[^!\r\n|\s][^!\r\n|]*?\.[A-Za-z0-9]+)(?:\|[^!\r\n]*)?!"
    r"|\[(?:(?P<ltext>[^\]|\r\n]*)\|)?(?P<lurl>https?://[^\]\r\n]+)\]"
    r"|(?P<bare>https?://[^\s\]]+)"
)


# Цветовое выделение Jira: {color:#hex|name}...{color}.
_COLOR_RE = re.compile(r"\{color:([^}\r\n]*)\}(.*?)\{color\}", re.DOTALL)

# Эмфаза Jira: жирный *…* и {*}…{*}, курсив _…_, моноширинный {{…}}.
# Жёсткие границы у *…*/_…_ — разделитель примыкает к не-пробелу и не к слову/самому
# себе, чтобы '*'/'_' в обычном тексте и коде не давали ложного оформления.
_EMPH_RE = re.compile(
    r"\{\*\}(?P<bbody>.*?)\{\*\}"                          # {*}…{*} (пустой → ничего)
    r"|(?<![\w*])\*(?!\s)(?P<bold>.+?)(?<!\s)\*(?![\w*])"  # *bold*
    r"|(?<![\w_])_(?!\s)(?P<ital>.+?)(?<!\s)_(?![\w_])"    # _italic_
    r"|\{\{(?P<mono>.*?)\}\}",                             # {{mono}}
    re.DOTALL,
)

# Имя цвета rich: #hex (3–8) или простое слово. Иначе цвет игнорируем.
_COLOR_VAL_RE = re.compile(r"#[0-9A-Fa-f]{3,8}|[A-Za-z][A-Za-z0-9_]*")


def _join(a: str, b: str) -> str:
    """Склейка rich-стилей пробелом (последний цвет в строке у rich побеждает)."""
    return f"{a} {b}".strip() if a and b else (a or b)


def _color_style(val: str) -> str:
    """Валидный rich-цвет из значения {color:…} либо '' (тогда цвет не применяем)."""
    m = _COLOR_VAL_RE.fullmatch((val or "").strip())
    return m.group(0) if m else ""


def _attachment_chip(filename: str, attach_map: Optional[dict[str, int]]) -> Text:
    """Чип вложения «🖼 имя» как в правом блоке; кликабелен, если файл есть в attach_map."""
    icon = ATTACH_ICON.get(classify_attachment(filename), "📎")
    idx = attach_map.get(filename) if attach_map else None
    if idx is not None:
        return Text.from_markup(
            f"{icon} [@click=screen.open_attachment({idx})][cyan u]{escape(filename)}[/cyan u][/]")
    return Text.from_markup(f"{icon} [cyan]{escape(filename)}[/cyan]")


def _emit_leaf(
    chunk: str, style: str, attach_map: Optional[dict[str, int]], clickable: bool, t: Text
) -> None:
    """Дописать в t прозу с лист-разметкой: обычный текст без интерпретации,
    а упоминания/вложения/ссылки — отдельными оформленными спанами. base-style style
    сохраняется на обычном тексте (несёт унаследованные цвет/жирный/курсив)."""
    pos = 0
    for m in _INLINE_RE.finditer(chunk):
        if m.start() > pos:
            t.append(chunk[pos:m.start()], style=style)
        if m.group("user"):  # упоминание [~login] → @login, жирным цветом пользователя
            user = m.group("user")
            t.append(f"@{user}", style=f"bold {author_color(user)}")
        elif m.group("file") or m.group("img"):
            fname = m.group("file") or m.group("img")
            t.append_text(_attachment_chip(fname, attach_map if clickable else None))
        else:  # ссылка
            url = m.group("lurl") or m.group("bare")
            label = (m.group("ltext") or "").strip() or url
            if clickable:
                t.append_text(Text.from_markup(
                    f"[link={url}][cyan u]{escape(label)}[/cyan u][/link]"))
            else:
                t.append(label, style="cyan underline")
        pos = m.end()
    if pos < len(chunk):
        t.append(chunk[pos:], style=style)


def _emit_emph(
    chunk: str, style: str, attach_map: Optional[dict[str, int]], clickable: bool, t: Text
) -> None:
    """Дописать прозу, разбирая эмфазу (*жирный*, _курсив_, {{mono}}, {*}…{*}).
    Внутри эмфазы рекурсивно разбираем вложенную эмфазу и лист-разметку."""
    pos = 0
    for m in _EMPH_RE.finditer(chunk):
        if m.start() > pos:
            _emit_leaf(chunk[pos:m.start()], style, attach_map, clickable, t)
        if m.group("bbody") is not None:      # {*}…{*}
            inner, delta = m.group("bbody"), "bold"
        elif m.group("bold") is not None:     # *жирный*
            inner, delta = m.group("bold"), "bold"
        elif m.group("ital") is not None:     # _курсив_
            inner, delta = m.group("ital"), "italic"
        else:                                 # {{mono}}
            inner, delta = m.group("mono") or "", "dim"
        if inner:
            _emit_emph(inner, _join(style, delta), attach_map, clickable, t)
        pos = m.end()
    if pos < len(chunk):
        _emit_leaf(chunk[pos:], style, attach_map, clickable, t)


def _inline_segments(
    chunk: str, style: str, attach_map: Optional[dict[str, int]], *, clickable: bool = True
) -> Text:
    """Собрать строку прозы в rich.Text. Каскад: цвет → эмфаза → лист-разметка.

    Обычный текст идёт без интерпретации разметки; {color:…} даёт цвет, *…*/_…_/{{…}}
    — жирный/курсив/моно, а упоминания/вложения/ссылки — отдельными спанами (ссылки
    кликабельны при clickable=True; для ячеек таблицы — clickable=False)."""
    t = Text()
    pos = 0
    for m in _COLOR_RE.finditer(chunk):
        if m.start() > pos:
            _emit_emph(chunk[pos:m.start()], style, attach_map, clickable, t)
        _emit_emph(m.group(2), _join(style, _color_style(m.group(1))),
                   attach_map, clickable, t)
        pos = m.end()
    if pos < len(chunk):
        _emit_emph(chunk[pos:], style, attach_map, clickable, t)
    return t


def render_jira_text(
    text: str, *, highlight: bool = False, attach_map: Optional[dict[str, int]] = None
) -> list[RenderableType]:
    """Разбить Jira-вики текст на куски: прозу (как есть) и код-блоки в панелях.

    Текст вставляется через rich.Text (без интерпретации разметки), чтобы '[', '{' и т.п.
    из контента не ломали рендер. Код-блоки {code}/{noformat} → обособлённая панель.
    Внутри прозы вложения [^файл]/!img! и ссылки [url] оформляются (см. _inline_segments).
    """
    parts: list[RenderableType] = []
    style = "yellow" if highlight else ""

    def add_prose(chunk: str) -> None:
        chunk = chunk.strip("\n")
        if chunk:
            parts.append(_inline_segments(chunk, style, attach_map))

    pos = 0
    for m in _JIRA_BLOCK_RE.finditer(text or ""):
        add_prose((text or "")[pos:m.start()])
        if m.group(2) is not None:  # {code(:lang)?}
            lang = (m.group(1) or "").split("|")[0].strip() or "code"
            code = m.group(2)
        elif m.group(3) is not None:  # {noformat}
            lang = "noformat"
            code = m.group(3)
        else:  # ```fenced```
            lang = (m.group(4) or "").strip() or "code"
            code = m.group(5)
        parts.append(
            Panel(
                Text(code.strip("\n") or " "),
                title=lang,
                title_align="left",
                border_style="grey37",
                padding=(0, 1),
            )
        )
        pos = m.end()
    rest = (text or "")[pos:]
    if rest.strip("\n") or not parts:
        add_prose(rest)
    return parts


# Markdown (Bitbucket PR): ```lang\n код \n``` — fenced-блоки кода.
_MD_FENCE_RE = re.compile(r"```[ \t]*([\w.+#-]*)[ \t]*\n?(.*?)```", re.DOTALL)


def render_md_text(text: str, *, highlight: bool = False) -> list[RenderableType]:
    """Разбить markdown-текст PR на прозу и ```fenced```-блоки кода.

    Тот же приём, что и render_jira_text (текст через rich.Text без интерпретации
    разметки), но блок оформлен ИНАЧЕ — двойная рамка, чтобы отличать от Jira-кода.
    """
    parts: list[RenderableType] = []
    style = "yellow" if highlight else ""

    def add_prose(chunk: str) -> None:
        chunk = chunk.strip("\n")
        if chunk:
            parts.append(Text(chunk, style=style))

    pos = 0
    for m in _MD_FENCE_RE.finditer(text or ""):
        add_prose((text or "")[pos:m.start()])
        lang = (m.group(1) or "").strip() or "code"
        parts.append(
            Panel(
                Text(m.group(2).strip("\n") or " "),
                title=lang,
                title_align="left",
                border_style="grey50",
                box=box.DOUBLE,
                padding=(0, 1),
            )
        )
        pos = m.end()
    rest = (text or "")[pos:]
    if rest.strip("\n") or not parts:
        add_prose(rest)
    return parts


def _indent_renderables(items: list[RenderableType], pad: str) -> list[RenderableType]:
    """Сдвинуть прозу (Text) вправо на pad; блоки (Panel) оставить как есть."""
    if not pad:
        return items
    out: list[RenderableType] = []
    for it in items:
        if isinstance(it, Text):
            lines = it.plain.split("\n")
            t = Text(style=it.style)
            for i, line in enumerate(lines):
                t.append(pad + line)
                if i < len(lines) - 1:
                    t.append("\n")
            out.append(t)
        else:
            out.append(it)
    return out


# палитра для авторов комментов: стабильный цвет на имя (crc32 — детерминирован между сессиями)
_AUTHOR_PALETTE = [
    "cyan", "magenta", "green", "yellow", "blue", "bright_red",
    "bright_cyan", "bright_magenta", "bright_green", "orange3",
    "spring_green2", "deep_pink2", "gold3", "turquoise2",
]


def author_color(name: str) -> str:
    if not name:
        return "white"
    return _AUTHOR_PALETTE[zlib.crc32(name.encode("utf-8")) % len(_AUTHOR_PALETTE)]


def status_color(status: str) -> str:
    s = (status or "").lower()
    if "progress" in s or "разраб" in s:
        return "blue"
    if "review" in s or "ревью" in s:
        return "yellow"
    if "done" in s or "closed" in s or "resolved" in s or "готов" in s or "закрыт" in s:
        return "green"
    if "cancel" in s or "отмен" in s:
        return "grey50"
    if "pause" in s or "hold" in s or "пауз" in s:
        return "grey50"
    if "test" in s or "stand" in s or "qa" in s or "стенд" in s:
        return "magenta"
    return "cyan"


def priority_color(priority: str) -> str:
    s = (priority or "").lower()
    # blocker → красный, critical → розоватый, major/high → жёлтый,
    # minor/low → светло-зелёный, trivial/lowest → серый.
    if "block" in s or "блок" in s:
        return "red"
    if "highest" in s or "critical" in s or "крит" in s:
        return "light_pink3"
    if "major" in s or "high" in s or "выс" in s:
        return "yellow"
    if "lowest" in s or "trivial" in s:
        return "grey50"
    if "minor" in s or "low" in s or "низ" in s:
        return "bright_green"
    if "medium" in s or "normal" in s or "сред" in s:
        return "yellow"
    return "white"


REVIEWER_SLOT_WIDTH = 25


def _fit_slot(value: str, width: int = REVIEWER_SLOT_WIDTH) -> str:
    """Подогнать строку под фиксированную ширину слота: обрезать с «…» либо паддить пробелами."""
    if len(value) > width:
        return value[: max(0, width - 3)] + "..."
    return value.ljust(width)


def reviewers_cell(reviewers, current_user: str = "") -> Text:
    """Колонка ревью: по каждому — [A]/[NW]/[N] + имя, окрашенные по статусу.

    Ревьюверы отсортированы по имени (display_name) без учёта регистра. Каждый слот —
    ровно REVIEWER_SLOT_WIDTH символов: длиннее обрезаем с многоточием, короче — паддим
    пробелами, чтобы следующий ревьювер начинался точно через 25 символов.

    Если ``current_user`` совпадает с `name` ревьювера — слот рендерится **жирным**
    (цвет статуса не меняется), чтобы пользователь сразу видел себя в списке.
    """
    if not reviewers:
        return Text("—", style="dim")
    def _sort_key(rv):
        return (rv.display_name or rv.name or "").casefold()
    ordered = sorted(reviewers, key=_sort_key)
    t = Text()
    me = (current_user or "").casefold()
    for rev in ordered:
        if rev.approved:
            code, color = "A", "green"
        elif (rev.status or "") == "NEEDS_WORK":
            code, color = "NW", "yellow"
        else:
            code, color = "N", "grey50"
        name = rev.display_name or rev.name or "—"
        is_me = me and (rev.name or "").casefold() == me
        style = f"bold {color}" if is_me else color
        t.append(_fit_slot(f"[{code}] {name}"), style=style)
    return t


_PR_URL_RE = re.compile(r"/projects/([^/]+)/repos/([^/]+)/pull-requests/(\d+)")
_TASK_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-[0-9]+)\b")


def parse_pr_url(url: str) -> Optional[tuple[str, str, int]]:
    """Достать (project, repo, id) из ссылки на PR Bitbucket."""
    m = _PR_URL_RE.search(url or "")
    return (m.group(1), m.group(2), int(m.group(3))) if m else None


def pr_task_key(pr: PR) -> str:
    """Извлечь ключ задачи из PR: сначала из source_branch, иначе из title. Пусто — не нашли."""
    for src in (pr.source_branch, pr.title):
        m = _TASK_KEY_RE.search(src or "")
        if m:
            return m.group(1)
    return ""


def normalize_issue_key(value: str) -> str:
    """Нормализовать ввод поиска в ключ задачи: обрезать пробелы, в верхний регистр."""
    return (value or "").strip().upper()


def _msafe(s: str) -> str:
    """Жёстко экранировать текст под markup: каждую `[` → `\\[`.

    `rich.markup.escape` НЕ экранирует «висячую» `[` без закрывающей `]`
    (например, после обрезки `summary[:50]` внутри `[тег]`). Парсер Textual
    в `Static` жадно дочитывает такой тег до `]` со следующей строки и ломается
    («closing tag '[/cyan]' does not match any open tag»). Экранируем все `[`.
    """
    return s.replace("\\", "\\\\").replace("[", "\\[")


def _group_threads(comments: list[PRComment]) -> list[list[PRComment]]:
    """Сгруппировать в треды: коммент верхнего уровня (depth 0) + его ответы (depth>0)."""
    threads: list[list[PRComment]] = []
    for c in comments:
        if c.depth == 0 or not threads:
            threads.append([c])
        else:
            threads[-1].append(c)
    return threads


def _append_comment(body: Text, c: PRComment) -> None:
    """Вписать строку коммента в тело диффа (как пузырёк Bitbucket)."""
    pad = "  " + "    " * c.depth
    marker = "↳ " if c.depth else "💬 "
    body.append(f"{pad}{marker}", style="bright_cyan")
    body.append(f"{c.author}", style=f"bold {author_color(c.author)}")
    if c.created:
        body.append(f"  {_fmt_dt(c.created)}", style="dim")
    body.append(": ", style=f"bold {author_color(c.author)}")
    body.append(f"{(c.text or '').strip()}\n")


def _inline_thread_panel(thread: list[PRComment]) -> Panel:
    """Дифф с комментом(ами), вставленными ПОСЛЕ прокомментированной строки (как в Bitbucket)."""
    head = thread[0]
    ctx = head.context
    idx = head.anchor_idx if 0 <= head.anchor_idx < len(ctx) else len(ctx) - 1
    body = Text()
    if not ctx:
        for c in thread:
            _append_comment(body, c)
    for i, ln in enumerate(ctx):
        style = "green" if ln.startswith("+") else "red" if ln.startswith("-") else "grey50"
        body.append(ln + "\n", style=style)
        if i == idx:
            for c in thread:
                _append_comment(body, c)
    return Panel(body or Text(" "), title=f"{head.file}:{head.line}", title_align="left",
                 border_style="grey37", padding=(0, 1))


def _general_thread(thread: list[PRComment]) -> list[RenderableType]:
    """Общий коммент PR (без привязки к строке) — автор + текст; ответы сдвигаем вправо по глубине."""
    parts: list[RenderableType] = []
    for c in thread:
        pad = "    " * c.depth          # сдвиг ответа вправо по уровню вложенности
        author = f"[b {author_color(c.author)}]{escape(c.author)}[/]"
        when = f"  [dim]{_fmt_dt(c.created)}[/dim]" if c.created else ""
        if c.depth:
            # ответ: вертикаль и угол (box-drawing) образуют единую линию-продолжение к автору
            parts.append(Text.from_markup(f"{pad}[dim]│[/dim]\n{pad}[dim]╰▶[/dim] {author}{when}"))
            text_pad = pad + "   "       # текст — под «╰▶ »
        else:
            parts.append(Text.from_markup(f"{author}{when}"))
            text_pad = ""
        # проза + ```fenced```-блоки кода; прозу сдвигаем под автора, блоки — как есть
        parts += _indent_renderables(render_md_text(c.text or ""), text_pad)
    return parts


class ConfirmScreen(ModalScreen):
    """Маленький модал подтверждения: y/enter — да, n/esc — нет."""

    CSS = """
    ConfirmScreen { align: center middle; }
    #box { width: auto; max-width: 60; padding: 1 3; border: round $warning; background: $panel; }
    """
    BINDINGS = [
        Binding("y,enter", "yes", "Да"),
        Binding("n,escape", "no", "Нет"),
    ]

    def __init__(self, question: str, on_yes: Callable[[], None]) -> None:
        super().__init__()
        self._question = question
        self._on_yes = on_yes

    def compose(self) -> ComposeResult:
        yield Static(
            f"{self._question}\n\n[dim]y / enter — да    ·    n / esc — нет[/dim]", id="box"
        )

    def action_yes(self) -> None:
        self.dismiss()
        self._on_yes()

    def action_no(self) -> None:
        self.dismiss()


# --------------------------------------------------------------------------- #
# Детальные экраны
# --------------------------------------------------------------------------- #


class IssueDetailScreen(Screen):
    CSS = """
    #title { padding: 1 2 0 2; height: auto; }
    #cols { height: 1fr; }
    #left { width: 2fr; padding: 0 2 1 2; }
    #right { width: 1fr; padding: 0 2 1 2; border-left: solid $accent; }
    .sec { margin-top: 1; }
    """
    BINDINGS = [
        Binding("escape,backspace", "app.pop_screen", "← Назад"),
        Binding("o", "open", "Открыть (браузер / папку)"),
        Binding("p", "open_first_pr", "Открыть PR"),
        Binding("y", "copy_issue_key", "Копировать ключ"),
        Binding("Y", "copy_menu", "Копировать…"),
    ]

    def __init__(
        self,
        issue: Issue,
        *,
        jira_base: str,
        user: str,
        pr_detail_fn: Optional[Callable[[str, str, int], PRDetail]] = None,
        jobs: Optional[list] = None,
        job_get_fn: Optional[Callable[[int], Optional[Job]]] = None,
        issue_get_fn: Optional[Callable[[str], Issue]] = None,
        refresh_interval: float = 0.0,
        loading: bool = False,
    ) -> None:
        super().__init__()
        self.issue = issue
        self.jira_base = jira_base.rstrip("/")
        self.user = user
        self._pr_detail_fn = pr_detail_fn
        self.jobs = jobs or []
        self._job_get_fn = job_get_fn
        self._issue_get_fn = issue_get_fn
        self._refresh_interval = refresh_interval
        # loading → задача открыта по ключу (поиск), данные ещё тянутся из сети
        self._loading = loading

    def compose(self) -> ComposeResult:
        yield Header()
        # Шапка — на всю ширину, по левому краю, крупно (насколько позволяет терминал)
        yield Static(self._title_renderable(), id="title")
        with Horizontal(id="cols"):
            with VerticalScroll(id="left"):
                yield Static(Rule("Описание", align="left", style="cyan"))
                yield Static(self._descr_renderable(), id="descr")
                yield Static(self._comments_head(), classes="sec", id="comments-head")
                yield Static(self._comments_renderable(), id="comments")
            with VerticalScroll(id="right"):
                yield Static(Rule("Задача", align="left", style="cyan"))
                yield Static(self._info_markup(), id="info")
                yield Static(Rule("Связи", align="left", style="cyan"), classes="sec")
                yield Static(self._links_markup(), id="links")
                yield Static(Rule("Ветки", align="left", style="cyan"), classes="sec")
                yield Static(self._branches_markup(), id="branches")
                yield Static(Rule("PR", align="left", style="cyan"), classes="sec")
                yield Static(self._prs_markup(), id="prs")
                yield Static(Rule("Вложения", align="left", style="cyan"), classes="sec")
                yield Static(self._attachments_markup(), id="attachments")
                yield Static(Rule("Работы", align="left", style="cyan"), classes="sec")
                yield Static(self._jobs_markup(), id="jobs")

    def on_mount(self) -> None:
        if self._issue_get_fn is not None:
            # поиск (loading) — данных ещё нет, тянем с заглушкой «загрузка…».
            # открыли из таблицы — кэш из памяти неполный (часто без комментов/вложений),
            # поэтому разово дотягиваем свежую карточку из сети поверх показанного.
            if self._loading:
                self._initial_load()
            else:
                self._refresh()
        # периодическое авто-дотягивание открытой задачи из сети (раз в self._refresh_interval сек)
        if self._issue_get_fn is not None and self._refresh_interval:
            self.set_interval(self._refresh_interval, self._refresh)

    @work(thread=True, exclusive=True, group="issue-load")
    def _initial_load(self) -> None:
        """Первичная загрузка задачи по ключу (поиск): экран уже на экране, тут наполняем."""
        try:
            issue = self._issue_get_fn(self.issue.key)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 — нет задачи / доступа / сеть
            self.app.call_from_thread(self._load_failed, str(exc))
            return
        self.app.call_from_thread(self._apply_loaded, issue)

    def _load_failed(self, msg: str) -> None:
        """Задачу не достать — сообщить и закрыть карточку (вернуться к списку)."""
        self.notify(f"Не открыть {self.issue.key}: {msg}", severity="error")
        self.app.pop_screen()

    def _apply_loaded(self, issue: Issue) -> None:
        self._loading = False
        self._apply_issue(issue)

    @work(thread=True, exclusive=True)
    def _refresh(self) -> None:
        try:
            issue = self._issue_get_fn(self.issue.key)  # type: ignore[misc]
        except Exception:  # noqa: BLE001 — сеть/доступ недоступны, оставляем как было
            return
        self.app.call_from_thread(self._apply_issue, issue)

    def _apply_issue(self, issue: Issue) -> None:
        """Обновить динамические секции экрана свежими данными задачи."""
        self.issue = issue
        self.query_one("#title", Static).update(self._title_renderable())
        self.query_one("#descr", Static).update(self._descr_renderable())
        self.query_one("#comments-head", Static).update(self._comments_head())
        self.query_one("#comments", Static).update(self._comments_renderable())
        self.query_one("#info", Static).update(self._info_markup())
        self.query_one("#links", Static).update(self._links_markup())
        self.query_one("#branches", Static).update(self._branches_markup())
        self.query_one("#prs", Static).update(self._prs_markup())
        self.query_one("#attachments", Static).update(self._attachments_markup())

    # --- рендер динамических секций (compose + рефреш) ------------------- #

    def _title_renderable(self) -> RenderableType:
        it = self.issue
        summary = (f"[b]{escape(it.summary)}[/b]" if it.summary
                   else "[dim]⏳ загрузка…[/dim]" if self._loading else "")
        return Group(
            Text.from_markup(f"[b cyan]{escape(it.key)}[/b cyan]   {summary}"),
            Rule(style="cyan"),
        )

    def _attach_index_map(self) -> dict[str, int]:
        """Имя файла → индекс вложения (для кликабельных чипов в тексте описания/комментов)."""
        return {a.filename: i for i, a in enumerate(self.issue.attachments)}

    def _descr_renderable(self) -> RenderableType:
        it = self.issue
        if it.description:
            return Group(*render_jira_text(it.description, attach_map=self._attach_index_map()))
        return Text.from_markup("[dim]⏳ загрузка задачи…[/dim]" if self._loading else "[dim]—[/dim]")

    def _comments_head(self) -> RenderableType:
        return Rule(f"Комментарии ({len(self.issue.comments)})", align="left", style="cyan")

    def _comments_renderable(self) -> RenderableType:
        marker = f"[~{self.user}]" if self.user else None
        return (Group(*self._comment_parts(marker))
                if self.issue.comments else Text.from_markup("[dim]нет[/dim]"))

    # --- правая колонка ------------------------------------------------- #

    def _info_markup(self) -> str:
        it = self.issue
        sc, pc = status_color(it.status), priority_color(it.priority)
        lines = [
            f"[dim]Статус:[/dim] [{sc}]{escape(it.status or '—')}[/{sc}]",
            f"[dim]Приоритет:[/dim] [{pc}]{escape(it.priority or '—')}[/{pc}]",
            f"[dim]Назначена:[/dim] {escape(it.assignee or '—')}",
            f"[dim]Автор:[/dim] {escape(it.reporter or '—')}",
            f"[dim]Обновлено:[/dim] {escape(_fmt_dt(it.updated))}",
            f"[dim]Создана:[/dim] {escape(_fmt_dt(it.created))}",
        ]
        if it.resolution:
            lines.append(f"[dim]Резолюция:[/dim] {escape(it.resolution)}")
        return "\n".join(lines)

    def _links_markup(self) -> str:
        if not self.issue.links:
            return "[dim]нет[/dim]"
        lines = []
        for ln in self.issue.links:
            key = (f"[@click=app.open_issue('{escape(ln.key)}')][cyan u]{escape(ln.key)}[/cyan u][/]"
                   if self._issue_get_fn is not None else f"[cyan]{escape(ln.key)}[/cyan]")
            lines.append(f"{key} [dim]{escape(ln.type)}[/dim] "
                         f"{_msafe(ln.summary[:40])} [dim]({escape(ln.status)})[/dim]")
        return "\n".join(lines)

    def _branches_markup(self) -> str:
        if not self.issue.branches:
            return "[dim]нет[/dim]"
        lines = []
        for br in self.issue.branches:
            repo = f" [dim]{escape(br.repository)}[/dim]" if br.repository else ""
            lines.append(f"[magenta]{escape(br.name)}[/magenta]{repo}")
        return "\n".join(lines)

    _PR_STATUS_COLOR = {"OPEN": "green", "MERGED": "magenta", "DECLINED": "red"}
    _PR_STATUS_ORDER = {"OPEN": 0, "MERGED": 1, "DECLINED": 2}

    def _prs_markup(self) -> str:
        prs = self.issue.pull_requests
        if not prs:
            return "[dim]нет[/dim]"
        # Открытые сверху, затем смерженные/отклонённые.
        prs = sorted(prs, key=lambda p: self._PR_STATUS_ORDER.get((p.status or "").upper(), 3))
        lines = []
        for pr in prs:
            st = (pr.status or "").upper()
            sc = self._PR_STATUS_COLOR.get(st, "grey50")
            badge = f"[{sc}]{escape(st or '—')}[/{sc}]"
            label = f"PR {pr.id} · {escape(pr.name)}"
            parsed = parse_pr_url(pr.url)
            if parsed and self._pr_detail_fn is not None:
                p, r, i = parsed
                link = f"[@click=screen.open_pr('{p}','{r}',{i})][cyan u]{label}[/cyan u][/]"
            else:
                link = f"[cyan]{label}[/cyan]"
            lines.append(f"{link} {badge}")
        return "\n".join(lines)

    def _attachments_markup(self) -> str:
        atts = self.issue.attachments
        if not atts:
            return "[dim]нет[/dim]"
        lines = []
        for i, a in enumerate(atts):
            icon = ATTACH_ICON.get(a.kind, "📎")
            name = _msafe(a.filename[:36])
            label = (f"[@click=screen.open_attachment({i})][cyan u]{name}[/cyan u][/]"
                     if a.url else f"[cyan]{name}[/cyan]")
            lines.append(f"{icon} {label} [dim]{_human_size(a.size)}[/dim]")
        return "\n".join(lines)

    def _jobs_markup(self) -> str:
        if not self.jobs:
            return "[dim]нет[/dim]"
        lines = []
        for j in self.jobs:
            sc = status_color(j.status)
            prs = ", ".join(f"#{p.pr_id}" for p in j.prs) or "—"
            label = f"#{j.id} {escape(j.title or '—')}"
            head = (f"[@click=screen.open_job({j.id})][cyan u]{label}[/cyan u][/]"
                    if self._job_get_fn is not None else f"[cyan]{label}[/cyan]")
            lines.append(f"{head}  [{sc}]{escape(j.status)}[/{sc}] [dim]PR: {prs}[/dim]")
        return "\n".join(lines)

    # --- левая колонка -------------------------------------------------- #

    def _comment_parts(self, marker: Optional[str]) -> list[RenderableType]:
        parts: list[RenderableType] = []
        attach_map = self._attach_index_map()
        for i, c in enumerate(self.issue.comments):
            if i:
                parts.append(Rule(style="grey30"))
            mine = bool(marker and marker in (c.body or ""))
            color = author_color(c.author)
            tag = " [yellow]● упоминание[/yellow]" if mine else ""
            parts.append(Text.from_markup(
                f"[b {color}]{escape(c.author)}[/b {color}] "
                f"[dim]{escape(_fmt_dt(c.created))}[/dim]{tag}"
            ))
            parts += render_jira_text(c.body or "", highlight=mine, attach_map=attach_map)
        return parts

    # --- действия ------------------------------------------------------- #

    def action_open(self) -> None:
        if self.jira_base:
            webbrowser.open(f"{self.jira_base}/browse/{self.issue.key}")

    def action_copy_issue_key(self) -> None:
        notify_copied(self, self.issue.key)

    def action_copy_menu(self) -> None:
        open_copy_modal(self, copy_items_for_issue(
            self.issue, self.jira_base, user=self.user))

    def action_open_pr(self, project: str, repo: str, pr_id: int) -> None:
        """Клик по PR / клавиша p → экран PR (с возвратом по Esc)."""
        url = next((p.url for p in self.issue.pull_requests
                    if parse_pr_url(p.url) == (project, repo, pr_id)), "")
        pr = PR(id=pr_id, project=project, repository=repo, url=url)
        self.app.push_screen(PRDetailScreen(
            pr, detail_fn=self._pr_detail_fn, refresh_interval=self._refresh_interval))

    def action_open_job(self, job_id: int) -> None:
        self.app.push_screen(JobDetailScreen(job_id, get_fn=self._job_get_fn))

    def action_open_attachment(self, idx: int) -> None:
        """Клик по вложению → открыть его в браузере (скачивание идёт через jwu attachments)."""
        atts = self.issue.attachments
        if 0 <= idx < len(atts) and atts[idx].url:
            webbrowser.open(atts[idx].url)

    def action_open_first_pr(self) -> None:
        for pr in self.issue.pull_requests:
            parsed = parse_pr_url(pr.url)
            if parsed and self._pr_detail_fn is not None:
                self.action_open_pr(*parsed)
                return


class PRDetailScreen(Screen):
    CSS = "VerticalScroll { padding: 1 2; } Rule { margin: 1 0 0 0; }"
    BINDINGS = [
        Binding("escape,backspace", "app.pop_screen", "← Назад"),
        Binding("o", "open", "Открыть (браузер / папку)"),
        Binding("Y", "copy_menu", "Копировать…"),
    ]

    def __init__(
        self,
        pr: PR,
        *,
        detail_fn: Optional[Callable[[str, str, int], PRDetail]] = None,
        refresh_interval: float = 0.0,
    ) -> None:
        super().__init__()
        self.pr = pr
        self._detail_fn = detail_fn
        self._refresh_interval = refresh_interval

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(
            Static(self._head(), id="pr-head"),
            Static("[dim]загрузка комментов…[/dim]", id="pr-body"),
        )

    def on_mount(self) -> None:
        if self._detail_fn is None:
            self.query_one("#pr-body", Static).update("[dim]детали недоступны[/dim]")
            return
        self._load()
        # авто-перезагрузка открытого PR из сети (раз в self._refresh_interval сек)
        if self._refresh_interval:
            self.set_interval(self._refresh_interval, self._load)

    def _head(self) -> RenderableType:
        pr = self.pr
        parts: list[RenderableType] = [
            Text.from_markup(
                f"[b cyan]PR {pr.id}[/b cyan] [b]{escape(pr.state)}[/b] {escape(pr.title)}\n"
                f"[magenta]{escape(pr.source_branch)}[/magenta] → "
                f"[magenta]{escape(pr.target_branch)}[/magenta]  "
                f"[dim]{escape(pr.project)}/{escape(pr.repository)}[/dim]"
            )
        ]
        if pr.conflicted is not None:
            parts.append(Text.from_markup(
                f"конфликт: {'[red]⚠ да[/red]' if pr.conflicted else '[green]нет[/green]'}"
            ))
        if pr.reviewers:
            rev = ["[b]Ревьюеры[/b]"]
            for r in pr.reviewers:
                if r.approved:
                    mark = "[green]✓ approved[/green]"
                elif r.status == "NEEDS_WORK":
                    mark = "[red]✗ needs work[/red]"
                else:
                    mark = "[dim]· не смотрел[/dim]"
                rev.append(f"  {mark}  {escape(r.display_name or r.name)}")
            parts.append(Text.from_markup("\n".join(rev)))
        return Group(*parts)

    @work(thread=True, exclusive=True)
    def _load(self) -> None:
        try:
            detail = self._detail_fn(self.pr.project, self.pr.repository, self.pr.id)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(
                self.query_one("#pr-body", Static).update,
                Text.from_markup(f"[red]ошибка загрузки:[/red] {escape(str(exc))}"),
            )
            return
        self.app.call_from_thread(self._render_detail, detail)

    def _render_detail(self, detail: PRDetail) -> None:
        self.pr = detail.pr  # полные данные (title, reviewers, conflict)
        self.query_one("#pr-head", Static).update(self._head())

        parts: list[RenderableType] = []
        if (self.pr.description or "").strip():
            parts.append(Rule("Описание", align="left", style="cyan"))
            parts += render_md_text(self.pr.description)
            parts.append(Text("\n"))
        if detail.commits:
            parts.append(Rule(f"Коммиты ({len(detail.commits)})", align="left", style="cyan"))
            commit_lines = []
            for c in detail.commits[:20]:
                msg = c.get("message", "").splitlines()[0] if c.get("message") else ""
                commit_lines.append(f"[yellow]{escape(c.get('id', ''))}[/yellow] {_msafe(msg[:70])}")
            parts.append(Text.from_markup("\n".join(commit_lines)))

        parts.append(Text("\n"))  # отступ перед секцией
        parts.append(Rule(f"Комментарии ({len(detail.comments)})", align="left", style="cyan"))
        if not detail.comments:
            parts.append(Text.from_markup("[dim]комментариев нет[/dim]"))
        for n, thread in enumerate(_group_threads(detail.comments)):
            if n:
                parts.append(Text(""))
                parts.append(Rule(style="grey30"))
            head = thread[0]
            if head.file and head.context:
                parts.append(_inline_thread_panel(thread))  # коммент ВНУТРИ диффа
            else:
                parts += _general_thread(thread)
        self.query_one("#pr-body", Static).update(Group(*parts))

    def action_open(self) -> None:
        if self.pr.url:
            webbrowser.open(self.pr.url)

    def action_copy_menu(self) -> None:
        open_copy_modal(self, copy_items_for_pr(self.pr))


class JobDetailScreen(Screen):
    CSS = "VerticalScroll { padding: 1 2; } .sec { margin-top: 1; }"
    BINDINGS = [
        Binding("escape,backspace,q", "app.pop_screen", "← Назад"),
        Binding("x", "close_job", "Закрыть (неактуальна)"),
        Binding("d", "finish_job", "✓ Завершить (done)"),
        Binding("D", "delete_job", "✕ Удалить"),
        Binding("y", "copy_issue_key", "Копировать ключ"),
        Binding("Y", "copy_menu", "Копировать…"),
    ]

    def __init__(
        self,
        job_id: int,
        *,
        get_fn: Optional[Callable[[int], Optional[Job]]],
        jira_base: str = "",
        refresh_interval: float = 0.0,
    ) -> None:
        super().__init__()
        self.job_id = job_id
        self._get_fn = get_fn
        self.jira_base = jira_base.rstrip("/")
        self._refresh_interval = refresh_interval
        self._title = ""
        self._job: Optional[Job] = None

    def action_close_job(self) -> None:
        fn = getattr(self.app, "_job_status_fn", None)
        if fn is not None:
            fn(self.job_id, "cancelled")
            getattr(self.app, "_run_memory_refresh", lambda: None)()
        self.app.pop_screen()

    def action_finish_job(self) -> None:
        def do() -> None:
            fn = getattr(self.app, "_job_status_fn", None)
            if fn is not None:
                fn(self.job_id, "done")
                getattr(self.app, "_run_memory_refresh", lambda: None)()
            self.app.pop_screen()

        self.app.push_screen(ConfirmScreen(
            f"Завершить работу #{self.job_id} «{self._title}» (done)?", do))

    def action_delete_job(self) -> None:
        def do() -> None:
            fn = getattr(self.app, "_job_delete_fn", None)
            if fn is not None:
                fn(self.job_id)
                getattr(self.app, "_run_memory_refresh", lambda: None)()
            self.app.pop_screen()
        self.app.push_screen(ConfirmScreen(
            f"Удалить работу #{self.job_id} «{self._title}» безвозвратно?", do))

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(Static(id="job-body"))

    def on_mount(self) -> None:
        self._reload()
        # авто-обновление открытой работы из памяти (записи jwu job add появляются сразу)
        if self._refresh_interval:
            self.set_interval(self._refresh_interval, self._reload)

    def action_copy_issue_key(self) -> None:
        if self._job is None or not self._job.task_key:
            self.notify("Ключ задачи недоступен", severity="warning")
            return
        notify_copied(self, self._job.task_key)

    def action_copy_menu(self) -> None:
        if self._job is None:
            self.notify("Работа не загружена", severity="warning")
            return
        open_copy_modal(self, copy_items_for_job(self._job, self.jira_base))

    def _reload(self) -> None:
        job = self._get_fn(self.job_id) if self._get_fn else None
        body = self.query_one("#job-body", Static)
        if job is None:
            self._job = None
            body.update("[dim]работа не найдена[/dim]")
            return
        self._job = job
        self._title = job.title or ""
        self.sub_title = f"#{job.id} {job.title}".strip()
        sc = status_color(job.status)
        parts: list[RenderableType] = [
            Text.from_markup(
                f"[b cyan]Работа #{job.id}[/b cyan]  [b {sc}]{escape(job.status)}[/b {sc}]  "
                f"{escape(job.title or '—')}\n"
                f"[dim]задача:[/dim] [cyan]{escape(job.task_key)}[/cyan]   "
                f"[dim]обновлена:[/dim] {escape(_fmt_dt(job.updated_at))}"
            )
        ]
        if job.prs:
            prs = "  ".join(
                escape(f"{p.project}/{p.repo}#{p.pr_id}" if p.project else f"#{p.pr_id}")
                for p in job.prs
            )
            parts.append(Text(""))
            parts.append(Rule("PR", align="left", style="cyan"))
            parts.append(Text.from_markup(prs))
        parts.append(Text(""))
        parts.append(Rule(f"Записи ({len(job.records)})", align="left", style="cyan"))
        if not job.records:
            parts.append(Text.from_markup("[dim]записей нет[/dim]"))
        for r in job.records:
            badge = JOB_RECORD_BADGES.get((r.kind or "").lower())
            if badge:
                label, color = badge
                st = f" [{color}]{escape(r.status)}[/{color}]" if r.status else ""
                parts.append(Text.from_markup(
                    f"[dim]{escape(_fmt_dt(r.ts))}[/dim] [b {color}]{escape(label)}[/b {color}]{st}"
                ))
                parts.append(Text.from_markup(f"[{color}]{escape((r.text or '').strip())}[/{color}]"))
            else:
                st = f" [{escape(r.status)}]" if r.status else ""
                parts.append(Text.from_markup(
                    f"[dim]{escape(_fmt_dt(r.ts))} · {escape(r.kind)}{st}[/dim]"
                ))
                parts.append(Text((r.text or "").strip()))
        body.update(Group(*parts))


# --------------------------------------------------------------------------- #
# Главный экран
# --------------------------------------------------------------------------- #


class Splitter(Static):
    """Тонкая вертикальная ручка между колонками: тащи мышью — меняет ширину правой."""

    MIN_LEFT = 30   # минимум для области вкладок
    MIN_RIGHT = 24  # минимум для колонки «Изменения»

    def __init__(self) -> None:
        super().__init__("", id="splitter")
        self._dragging = False

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._dragging = True
        self.capture_mouse()
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self._dragging = False
        self.release_mouse()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        total = self.app.size.width
        # правая колонка тянется от позиции мыши до правого края
        new_w = total - event.screen_x
        new_w = max(self.MIN_RIGHT, min(total - self.MIN_LEFT, new_w))
        self.app.query_one("#changes-col").styles.width = new_w
        event.stop()


class JwuDashboard(App):
    CSS = """
    #body { height: 1fr; }
    #tabs { width: 1fr; height: 1fr; }
    #splitter { width: 1; height: 1fr; background: $panel; }
    #splitter:hover { background: $accent; }
    #changes-col { width: 42; height: 1fr; border: round $accent; }
    #changes-pane { width: 100%; height: 1fr; padding: 0 1; }
    #changes { height: auto; }
    #status { height: auto; padding: 0 1; color: $text-muted; border-top: solid $accent; }
    DataTable { height: 1fr; }
    """

    LOCAL_SECTIONS = ("jobs",)  # читаются из памяти, без сети

    BINDINGS = [
        Binding("q", "quit", "Выход"),
        Binding("question_mark", "show_legend", "Клавиши"),
        Binding("slash", "search_issue", "Поиск задачи"),
        Binding("b", "toggle_changes", "Панель изменений"),
        Binding("R", "refresh_all", "Обновить всё"),
        Binding("o", "open", "Открыть (браузер / папку)"),
        Binding("c", "clear_section", "Очистить"),
        Binding("C", "ack_changes", "Очистить всё"),
        Binding("x", "close_job", "Закрыть работу"),
        Binding("d", "finish_job", "✓ Завершить работу"),
        Binding("D", "delete_job", "✕ Удалить работу"),
        Binding("D", "remove_path", "✕ Отвязать папку"),
        Binding("D", "delete_workspace", "✕ Удалить воркспейс"),
        Binding("W", "pick_workspace", "Воркспейс"),
        Binding("N", "new_workspace", "＋ Воркспейс"),
        Binding("N", "new_feature", "＋ Фича"),
        Binding("N", "new_rule", "＋ Правило"),
        Binding("e", "edit_rule", "Править правило"),
        Binding("D", "delete_rule", "✕ Удалить правило"),
        Binding("a", "add_path", "Добавить папку"),
        Binding("[", "tab_prev", "← вкладка"),
        Binding("]", "tab_next", "→ вкладка"),
        Binding("y", "copy_issue_key", "Копировать ключ"),
        Binding("Y", "copy_menu", "Копировать…"),
    ]

    def __init__(
        self,
        data: DashboardData,
        *,
        memory_fn: Optional[Callable[[], DashboardData]] = None,
        full_sync_fn: Optional[Callable[[], DashboardData]] = None,
        pr_detail_fn: Optional[Callable[[str, str, int], PRDetail]] = None,
        issue_get_fn: Optional[Callable[[str], Issue]] = None,
        job_get_fn: Optional[Callable[[int], Optional[Job]]] = None,
        job_delete_fn: Optional[Callable[[int], None]] = None,
        job_status_fn: Optional[Callable[[int, str], None]] = None,
        ack_changes_fn: Optional[Callable[[], DashboardData]] = None,
        clear_changes_fn: Optional[Callable[[list[tuple[str, str]]], DashboardData]] = None,
        mentions_seen_fn: Optional[Callable[[Optional[list[int]]], DashboardData]] = None,
        # воркспейсы и локальные фичи — тоже только через callable (TUI не знает про БД)
        workspaces_fn: Optional[Callable[[], list]] = None,
        workspace_switch_fn: Optional[Callable[[int], DashboardData]] = None,
        workspace_create_fn: Optional[Callable[[str], None]] = None,
        workspace_delete_fn: Optional[Callable[[int], DashboardData]] = None,
        path_add_fn: Optional[Callable[[int, str], DashboardData]] = None,
        path_remove_fn: Optional[Callable[[int, str], DashboardData]] = None,
        feature_get_fn: Optional[Callable[[int], object]] = None,
        feature_jobs_fn: Optional[Callable[[int], list]] = None,
        feature_create_fn: Optional[Callable[[str], None]] = None,
        feature_status_fn: Optional[Callable[[int, str], None]] = None,
        feature_edit_fn: Optional[Callable[[int, str], None]] = None,
        rule_get_fn: Optional[Callable[[int], object]] = None,
        rule_create_fn: Optional[Callable[[dict], None]] = None,
        rule_edit_fn: Optional[Callable[[int, dict], None]] = None,
        rule_delete_fn: Optional[Callable[[int], None]] = None,
        tags_fn: Optional[Callable[[], list]] = None,
        cwd: str = "",
        start_with_picker: bool = False,
        jira_base: str = "",
        env_label: str = "",
        auto_update: bool = False,
        fast_interval: float = 5.0,
        slow_interval: float = 900.0,
        detail_interval: float = 60.0,
        index_interval: float = 120.0,
    ) -> None:
        super().__init__()
        self.data = data
        # последние известные данные о пользователе — переживают рефреши из памяти,
        # где user/display_name/email приходят пустыми (снимок без сети)
        self._user = data.user or ""
        self._display_name = data.display_name or ""
        self._email = data.email or ""
        self._memory_fn = memory_fn
        self._full_sync_fn = full_sync_fn
        self._pr_detail_fn = pr_detail_fn
        self._issue_get_fn = issue_get_fn
        self._job_get_fn = job_get_fn
        self._job_delete_fn = job_delete_fn
        self._job_status_fn = job_status_fn
        self._ack_changes_fn = ack_changes_fn
        self._clear_changes_fn = clear_changes_fn
        self._mentions_seen_fn = mentions_seen_fn
        self._workspaces_fn = workspaces_fn
        self._workspace_switch_fn = workspace_switch_fn
        self._workspace_create_fn = workspace_create_fn
        self._workspace_delete_fn = workspace_delete_fn
        self._path_add_fn = path_add_fn
        self._path_remove_fn = path_remove_fn
        self._feature_get_fn = feature_get_fn
        self._feature_jobs_fn = feature_jobs_fn
        self._feature_create_fn = feature_create_fn
        self._feature_status_fn = feature_status_fn
        self._feature_edit_fn = feature_edit_fn
        self._rule_get_fn = rule_get_fn
        self._rule_create_fn = rule_create_fn
        self._rule_edit_fn = rule_edit_fn
        self._rule_delete_fn = rule_delete_fn
        self._tags_fn = tags_fn
        self.cwd = cwd
        self._start_with_picker = start_with_picker
        self.jira_base = jira_base.rstrip("/")
        self.env_label = env_label
        self.auto_update = auto_update
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.detail_interval = detail_interval
        # период переиндексации папок воркспейса (git-маркеры), сек
        self.index_interval = index_interval
        # путь папки → найденные в ней репозитории; None = ещё не индексировали
        self._git_index: dict[str, list] = {}
        self._tree_roots: list[str] = []   # корни дерева структуры (папки воркспейса)
        self._loading_workspace = False    # идёт смена контура (показываем маркер)
        self._rows: dict[str, list] = {}        # table id -> объекты (Issue|PR) по строкам
        self._last_row: dict[str, int | None] = {}  # где был курсор — чтобы знать сторону движения
        self._sort: dict[str, tuple[int, bool]] = {}  # table id -> (колонка, reverse)
        self._changed_issue_keys: set[str] = set()
        self._changed_pr_ids: set[int] = set()
        self._deltas_by_section: dict[str, list] = {}
        # состояние авто-обновления (для живой статус-строки) — независимо по каждому виду:
        # "memory" (быстрый рефреш из локальной БД) и "network" (полный синк по сети).
        self._sync_running: dict[str, float] = {}  # kind → monotonic-стартa, если идёт
        self._last_fast_mono = monotonic()
        self._last_slow_mono = monotonic()
        # последняя сетевая синхронизация: упала ли, когда (ISO) и текст ошибки
        self._sync_failed = False
        self._fail_at: Optional[str] = None
        self._sync_error = ""
        # подряд упавшие авто-синки; после 2-х (попытка + 1 ретрай) авто-синк паузим,
        # чтобы не плодить ошибки. Сбрасывается успешным или ручным (R) синком.
        self._fail_count = 0
        self._auto_paused = False
        self._RETRY_LIMIT = 2

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with TabDeck(id="tabs"):
                for row_id, panes in TAB_ROWS:
                    yield TabBar(
                        *(Tab(_TAB_TITLE[p], id=_bar_id(p)) for p in panes),
                        id=row_id,
                    )
                with ContentSwitcher(id="panes"):
                    # управление контурами: список всех, переключение, создание, удаление
                    with Vertical(id="tab-workspaces"):
                        yield Static(id="ws-head")
                        yield DashboardTable(id="t-workspaces")
                    # содержимое активного контура: папки и их вложенная структура
                    with Vertical(id="tab-structure"):
                        yield Static(id="structure-head")
                        yield WorkspaceTree(id="t-structure")
                    # что агент обязан знать о проекте: запреты, инструкции, общая инфа
                    with Vertical(id="tab-rules"):
                        yield Static(id="rules-head")
                        yield DashboardTable(id="t-rules")
                    with Vertical(id="tab-mine"):
                        yield DashboardTable(id="t-mine")
                    with Vertical(id="tab-mentions"):
                        yield DashboardTable(id="t-mentions")
                    with Vertical(id="tab-features"):
                        yield DashboardTable(id="t-features")
                    with Vertical(id="tab-prs-mine"):
                        yield DashboardTable(id="t-prs-mine")
                    with Vertical(id="tab-prs-review"):
                        yield DashboardTable(id="t-prs-review")
                    with Vertical(id="tab-jobs"):
                        yield DashboardTable(id="t-jobs")
            yield Splitter()
            with Vertical(id="changes-col"):
                yield VerticalScroll(Static(id="changes"), id="changes-pane")
        # Статус живёт внизу, а НЕ в боковой панели: панель по умолчанию скрыта, и вместе
        # с ней пропала бы вся обратная связь по синку. Легенда клавиш — по «?».
        yield Static(id="status")

    def on_mount(self) -> None:
        self.title = "jwu"
        self.sub_title = self._user
        # применяем сохранённую тему (если есть и доступна в текущей сборке Textual)
        saved = load_ui_prefs().theme
        if saved and saved in self.available_themes:
            self.theme = saved
        for table_id, kind, _ in TABS.values():
            if kind == "tree":
                continue
            table = self.query_one(f"#{table_id}", DataTable)
            table.cursor_type = "row"
            table.zebra_stripes = True
            if kind == "pr":
                cols = PR_MINE_COLUMNS if table_id == "t-prs-mine" else PR_REVIEW_COLUMNS
            else:
                if kind == "tree":
                    continue  # дерево наполняется само, колонок у него нет
                cols = {"issue": ISSUE_COLUMNS, "mention": MENTION_COLUMNS,
                        "job": JOB_COLUMNS,
                        "workspace": WORKSPACE_COLUMNS,
                        "feature": FEATURE_COLUMNS,
                        "rule": RULE_COLUMNS}[kind]
            for col in cols:
                table.add_column(col, key=col)
        self._apply_tab_visibility()
        self._show_changes(False)   # панель изменений открывается по «b», когда нужна
        self._tabs.active = self._initial_tab()
        self._render()
        self._focus_active_table()
        if self._start_with_picker:
            # воркспейс не определился — показываем выбор поверх пустого дашборда
            self.call_after_refresh(self.action_pick_workspace)
        self.set_interval(1.0, self._update_status)  # живой таймер/счётчик в статус-строке
        # Индексация папок идёт всегда (а не только при -a): она локальная и дешёвая,
        # зато ветка в таблице не устаревает, пока ты переключаешься между репозиториями.
        self._run_reindex()
        if self.index_interval > 0:
            self.set_interval(self.index_interval, self._run_reindex)
        if self.auto_update:
            if self._memory_fn is not None:
                self.set_interval(self.fast_interval, self._auto_fast_refresh)
            if self._full_sync_fn is not None:
                # Сетевой синк — one-shot таймером: отсчёт начнётся в _after_refresh
                # ОТ ОКОНЧАНИЯ предыдущего синка, чтобы длинные синки не «съедали»
                # следующий интервал.
                self._schedule_next_slow_sync()

    def watch_theme(self, old: Optional[str], new: Optional[str]) -> None:
        """Текстуалевский watcher реактивного поля `theme` — сохраняем выбор пользователя."""
        if new and new != old:
            save_ui_prefs(UIPrefs(theme=new))

    @property
    def _tabs(self) -> TabDeck:
        return self.query_one("#tabs", TabDeck)

    def on_tab_deck_activated(self, event: TabDeck.Activated) -> None:
        """При смене вкладки — фокус на её таблицу, обновить статус и панель изменений."""
        spec = TABS.get(event.pane)
        if spec:
            try:
                self.query_one(f"#{spec[0]}", DataTable).focus()
            except Exception:  # noqa: BLE001
                pass
        self._render_changes()
        self._update_status()
        # Набор доступных действий зависит от вкладки (см. check_action) — пересчитываем,
        # чтобы легенда по «?» показывала клавиши той вкладки, где ты сейчас.
        self.screen.refresh_bindings()

    # --- рендеринг ------------------------------------------------------ #

    def _data_for(self, section: str) -> list:
        return {
            "workspaces": self.data.workspaces,
            "structure": self.data.paths,
            "rules": self.data.rules,
            "mine": self.data.mine,
            "mentions": self.data.mentions,
            "features": self.data.features,
            "prs_mine": self.data.prs_mine,
            "prs_review": self.data.prs_review,
            "jobs": self.data.jobs,
        }[section]

    # --- видимость вкладок ----------------------------------------------- #

    def _visible_tabs(self) -> tuple[str, ...]:
        """Вкладки, актуальные для текущего воркспейса, в фиксированном порядке.

        Вкладки отключённой интеграции прячем, а не показываем пустыми: в личном
        воркспейсе «PR: на ревью» — это шум, а не подсказка. Локальные фичи, наоборот,
        нужны именно там, где Jira нет (иначе они дублировали бы задачи).
        """
        hidden: set[str] = set()
        if not self.data.jira_enabled:
            hidden |= set(JIRA_TABS)
        else:
            hidden.add("tab-features")
        if not self.data.bitbucket_enabled:
            hidden |= set(BITBUCKET_TABS)
        return tuple(pane for pane in _TAB_ORDER if pane not in hidden)

    def _initial_tab(self) -> str:
        """Вкладка при старте: сразу к работе, а не к структуре воркспейса.

        «Workspace» стоит первой в порядке (её удобно листать влево), но открывать
        дашборд каждый раз на списке папок незачем — там справочная информация.
        """
        visible = self._visible_tabs()
        for candidate in ("tab-mine", "tab-features", "tab-jobs"):
            if candidate in visible:
                return candidate
        return visible[0] if visible else "tab-workspace"

    def _apply_tab_visibility(self) -> None:
        tabs = self._tabs
        visible = self._visible_tabs()
        for pane_id in _TAB_ORDER:
            tabs.set_pane_visible(pane_id, pane_id in visible)
        if visible and tabs.active not in visible:
            tabs.active = self._initial_tab()

    def _focus_active_table(self) -> None:
        spec = TABS.get(self._tabs.active)
        if not spec:
            return
        try:
            self.query_one(f"#{spec[0]}", DataTable).focus()
        except Exception:  # noqa: BLE001
            pass

    def _compute_deltas_by_section(self) -> None:
        """Разложить дельты по вкладкам (по принадлежности key/id к спискам вкладки)."""
        deltas = self.data.deltas
        self._changed_issue_keys = {
            d.key for d in deltas if "/" not in d.key and "#" not in d.key
        }
        self._changed_pr_ids = set()
        mine_keys = {i.key for i in self.data.mine}
        prm_ids = {p.id for p in self.data.prs_mine}
        prr_ids = {p.id for p in self.data.prs_review}
        by: dict[str, list] = {s: [] for s in
                               ("workspaces", "structure", "rules", "mine", "mentions",
                                "features", "prs_mine", "prs_review", "jobs")}
        for d in deltas:
            if d.section:  # дельта исчезновения (gone/pr_gone) — сущности в списке уже нет
                if d.section in by:
                    by[d.section].append(d)
                continue
            m = re.search(r"#(\d+)$", d.key)
            if m:
                pid = int(m.group(1))
                self._changed_pr_ids.add(pid)
                if pid in prm_ids:
                    by["prs_mine"].append(d)
                if pid in prr_ids:
                    by["prs_review"].append(d)
            else:
                if d.key in mine_keys:
                    by["mine"].append(d)
        self._deltas_by_section = by

    def _render(self) -> None:
        self._compute_deltas_by_section()
        self._render_changes()
        self._render_workspace_head()
        for pane_id, (table_id, kind, section) in TABS.items():
            items = self._sorted(table_id, kind, self._data_for(section))
            if kind == "issue":
                self._fill_issues(table_id, items)
            elif kind == "mention":
                self._fill_mentions(table_id, items)
            elif kind == "pr":
                self._fill_prs(table_id, items)
            elif kind == "job":
                self._fill_jobs(table_id, items)
            elif kind == "workspace":
                self._fill_workspaces(table_id, items)
            elif kind == "tree":
                self._fill_tree(table_id, items)
            elif kind == "rule":
                self._fill_rules(table_id, items)
            else:
                self._fill_features(table_id, items)
            # У упоминаний нет дельт: «новое» для них — непрочитанные записи.
            badge = (sum(1 for m in self.data.mentions if not m.seen) if section == "mentions"
                     else len(self._deltas_by_section.get(section, [])))
            label = f"{_TAB_TITLE[pane_id]} ({len(items)})"
            if badge:
                label += f" ●{badge}"
            self._tabs.get_tab(pane_id).label = label
        self._update_status()

    def _next_memory_hint(self, *, label: str = "след.") -> str:
        if not self.auto_update:
            return ""
        rem = self._last_fast_mono + self.fast_interval - monotonic()
        return f"   ·   {label} через {_fmt_dur(rem)}"

    def _next_network_hint(self, *, label: str = "след.") -> str:
        if not self.auto_update:
            return ""
        rem = self._last_slow_mono + self.slow_interval - monotonic()
        return f"   ·   {label} через {_fmt_dur(rem)}"

    def _memory_line(self) -> str:
        """Строка состояния быстрого рефреша из памяти.

        Сам факт «идёт синк памяти» не показываем — рефреш слишком частый и
        мозолил бы глаза; таймер просто стартует с нуля после очередного тика.
        """
        return f"[dim]🗂  из памяти{self._next_memory_hint()}[/dim]"

    def _network_line(self) -> str:
        """Строка состояния сетевого синка (всегда показывается)."""
        started = self._sync_running.get("network")
        if started is not None:
            return f"[yellow]⟳ синхронизация с сетью… {_fmt_dur(monotonic() - started)}[/yellow]"
        if self._sync_failed:
            ago = _fmt_ago(self._fail_at)
            err = f": {self._sync_error}" if self._sync_error else ""
            if self._auto_paused:
                # ретраи исчерпаны — следующего авто-синка не будет (только R вручную)
                return (f"[red]🔄 последняя синхронизация: {ago} — не удалось{err}; "
                        f"авто-синхронизация остановлена (R — повторить)[/red]")
            nxt = self._next_network_hint(label="след. попытка")
            return (f"[red]🔄 последняя синхронизация: {ago} — не удалось{err}"
                    f"[/red][dim]{nxt}[/dim]")
        last_network = max(
            (v for k, v in self.data.last_sync.items()
             if k not in self.LOCAL_SECTIONS and v),
            default=None,
        )
        ago = _fmt_ago(last_network)
        return f"[dim]🔄 последний синк: {ago}{self._next_network_hint()}[/dim]"

    def _user_block(self) -> str:
        """Кто смотрит дашборд: имя/логин, почта и окружение (1–2 строки)."""
        if self._display_name and self._user:
            who = f"{self._display_name} ({self._user})"
        else:
            who = self._display_name or self._user or "—"
        head = f"👤 {escape(who)}"
        if self._email:
            head += f"   ·   ✉ {escape(self._email)}"
        lines = [f"[dim]{head}[/dim]"]
        if self.env_label:
            lines.append(f"[dim]🌐 {escape(self.env_label)}[/dim]")
        return "\n".join(lines)

    def _update_status(self) -> None:
        status = self.query_one("#status", Static)
        if self._loading_workspace:
            status.update("[yellow]⟳ переключаю воркспейс, читаю данные…[/yellow]")
            return
        # Внизу на всю ширину — значит в строку: состояние сети и памяти независимы и
        # показываются оба, а «?» тут потому, что легенды на экране больше нет.
        line = f"{self._network_line()}   ·   {self._memory_line()}"
        status.update(f"{line}\n{self._user_block()}   ·   [dim]? — клавиши[/dim]")

    # Сколько сущностей и сколько изменений внутри одной сущности показывать в панели.
    MAX_CHANGE_GROUPS = 8
    MAX_CHANGES_PER_GROUP = 5

    def _render_changes(self) -> None:
        """Панель «Изменения» активной вкладки — сгруппированная по задачам/PR.

        Плоский список дельт на узкой колонке читался как лента: ключ задачи повторялся
        в каждой строке, а сами изменения тонули. Здесь одна сущность — один блок:
        заголовок «ключ: название» и под ним компактный список того, что в ней случилось.
        """
        panel = self.query_one("#changes", Static)
        try:
            active = self._tabs.active
        except Exception:  # noqa: BLE001
            active = ""
        section = TABS.get(active, (None, None, None))[2]
        title = _TAB_TITLE.get(active, "")
        if section == "mentions":
            groups, total = self._mention_groups()
            action = "прочитано"
        else:
            groups, total = self._delta_groups(section)
            action = "очистить"
        if not groups:
            panel.update(f"[b]Изменения · {title}[/b]\n[dim]Изменений нет.[/dim]")
            return
        close = f"[@click=app.clear_section][b yellow]\\[✕ {action}][/b yellow][/]"
        lines = [f"[b]Изменения · {title} ({total})[/b]   {close}"]
        for key, summary, items in groups[:self.MAX_CHANGE_GROUPS]:
            head = f"[cyan u]{escape(key)}[/cyan u][dim]:[/dim] {_msafe(summary[:60])}"
            lines.append(f"[@click=app.open_delta('{escape(key)}')]{head}[/]")
            for icon, text in items[:self.MAX_CHANGES_PER_GROUP]:
                lines.append(f"   {icon} [dim]{_msafe(text)}[/dim]")
            if len(items) > self.MAX_CHANGES_PER_GROUP:
                lines.append(f"   [dim]…ещё {len(items) - self.MAX_CHANGES_PER_GROUP}[/dim]")
        if len(groups) > self.MAX_CHANGE_GROUPS:
            lines.append(f"[dim]…ещё {len(groups) - self.MAX_CHANGE_GROUPS} шт.[/dim]")
        panel.update("\n".join(lines))

    def _delta_groups(self, section: str) -> tuple[list[tuple[str, str, list]], int]:
        """Дельты вкладки → [(ключ, название, [(иконка, что случилось), …])] + их общее число.

        Порядок групп — по первому появлению сущности в дельтах: свежие события синка
        идут последними, но «первым замеченным» остаётся то, что случилось раньше.
        """
        groups: dict[str, tuple[str, list]] = {}
        deltas = self._deltas_by_section.get(section, [])
        for d in deltas:
            summary, items = groups.setdefault(d.key, (d.summary, []))
            if not summary and d.summary:  # заголовок мог прийти только с частью дельт
                groups[d.key] = (d.summary, items)
            items.append((DELTA_ICON.get(d.kind, "•"), delta_chip(d.kind, d.detail)))
        return [(key, summary, items) for key, (summary, items) in groups.items()], len(deltas)

    def _mention_groups(self) -> tuple[list[tuple[str, str, list]], int]:
        """Непрочитанные упоминания, сгруппированные по задаче.

        У упоминаний нет дельт: их «изменение» — это само появление новой записи,
        поэтому панель показывает именно непрочитанные (см. _fill_mentions).
        """
        groups: dict[str, tuple[str, list]] = {}
        fresh = [m for m in self.data.mentions if not m.seen]
        for m in fresh:
            summary, items = groups.setdefault(m.task_key, (m.summary, []))
            author = f"{m.author}: " if m.author else ""
            items.append(("💬", f"{author}{self._mention_text(m)[:80]}"))
        return [(key, summary, items) for key, (summary, items) in groups.items()], len(fresh)

    def _sorted(self, table_id: str, kind: str, items: list) -> list:
        state = self._sort.get(table_id)
        if state is None:
            return items
        col, reverse = state
        if kind == "issue":
            keyfns = [lambda i: i.key, lambda i: i.status, lambda i: i.priority, lambda i: i.summary]
        elif kind == "mention":  # Когда · Задача · От кого · Упоминание
            keyfns = [
                lambda m: m.created or "",
                lambda m: m.task_key,
                lambda m: (m.author or "").casefold(),
                lambda m: (m.text or "").casefold(),
            ]
        elif kind == "pr":
            # Колонки разные для «PR: мои» (7) и «PR: на ревью» (4) — см. PR_MINE_COLUMNS / PR_REVIEW_COLUMNS.
            conflict_key = lambda p: 1 if p.conflicted else 0  # noqa: E731
            reviewers_approved = lambda p: sum(1 for r in p.reviewers if r.approved)  # noqa: E731
            title_key = lambda p: p.title.lower()  # noqa: E731
            if table_id == "t-prs-mine":
                task_status = self.data.task_status or {}
                task_assignee = self.data.task_assignee or {}
                keyfns = [
                    lambda p: p.id,
                    conflict_key,
                    lambda p: pr_task_key(p),
                    lambda p: task_assignee.get(pr_task_key(p), ""),
                    lambda p: task_status.get(pr_task_key(p), ""),
                    title_key,
                    reviewers_approved,
                ]
            else:  # t-prs-review
                keyfns = [
                    lambda p: p.id,
                    conflict_key,
                    title_key,
                    reviewers_approved,
                ]
        elif kind == "rule":      # Тип · Тег · Правило · Обновлено
            keyfns = [
                lambda r: r.kind,
                lambda r: (r.tag or "").casefold(),
                lambda r: (r.title or "").casefold(),
                lambda r: r.updated_at,
            ]
        elif kind == "job":       # ID · Обновлено · Статус · Задача · PR · Title
            keyfns = [
                lambda j: j.id,
                lambda j: j.updated_at,
                lambda j: j.status,
                lambda j: j.task_key,
                lambda j: min((p.pr_id for p in j.prs), default=0),
                lambda j: (j.title or "").lower(),
            ]
        else:
            return items
        if col >= len(keyfns):
            return items
        return sorted(items, key=keyfns[col], reverse=reverse)

    def _key_cell(self, label: str, changed: bool) -> Text:
        cell = Text()
        if changed:
            cell.append("● ", style="yellow")
        cell.append(label, style="bold yellow" if changed else "cyan")
        return cell

    @staticmethod
    def _restore_cursor(table: DataTable, row: int | None) -> None:
        """Вернуть курсор примерно туда же после перезаполнения (для авто-обновления)."""
        if row is not None and table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1))

    def _fill_issues(self, table_id: str, issues: list[Issue]) -> None:
        table = self.query_one(f"#{table_id}", DataTable)
        cur = table.cursor_row
        table.clear()
        for it in issues:
            changed = it.key in self._changed_issue_keys
            table.add_row(
                self._key_cell(it.key, changed),
                Text(it.status, style=status_color(it.status)),
                Text(it.priority, style=priority_color(it.priority)),
                Text(it.summary, style="yellow" if changed else ""),
            )
        self._rows[table_id] = list(issues)
        self._restore_cursor(table, cur)

    @staticmethod
    def _mention_text(mention: Mention) -> str:
        """Тело упоминания одной строкой (схлопнуть переводы и лишние пробелы).

        Сами маркеры [~login], вложения и ссылки оформляются дальше в _inline_segments.
        """
        return " ".join((mention.text or "").split())

    def _fill_mentions(self, table_id: str, mentions: list[Mention]) -> None:
        """Вкладка «Упоминания»: когда · задача · автор · текст комментария.

        Строка — само упоминание, а не задача: непрочитанные помечены точкой, и
        пометка снимается заходом внутрь, а не обновлением задачи.
        """
        table = self.query_one(f"#{table_id}", DataTable)
        cur = table.cursor_row
        table.clear()
        for m in mentions:
            fresh = not m.seen
            base = "yellow" if fresh else ""
            # вложения/картинки/ссылки в тексте упоминания оформляем (некликабельно)
            cell = _inline_segments(self._mention_text(m), base, None, clickable=False)
            cell.truncate(80, overflow="ellipsis")
            table.add_row(
                Text(_fmt_dt(m.created), style="dim"),
                self._key_cell(m.task_key, fresh),
                Text(m.author or "—", style=author_color(m.author)),
                cell,
            )
        self._rows[table_id] = list(mentions)
        self._restore_cursor(table, cur)

    def _fill_prs(self, table_id: str, prs: list[PR]) -> None:
        table = self.query_one(f"#{table_id}", DataTable)
        cur = table.cursor_row
        table.clear()
        with_task_cols = table_id == "t-prs-mine"  # колонки задачи/назначен/статус только в «Мои PR»
        task_status = self.data.task_status or {}
        task_assignee = self.data.task_assignee or {}
        for pr in prs:
            changed = pr.id in self._changed_pr_ids
            conflict = Text("⚠", style="dark_orange") if pr.conflicted else Text("")
            title_cell = Text(pr.title, style="yellow" if changed else "")
            review_cell = reviewers_cell(pr.reviewers, current_user=self._user)
            if with_task_cols:
                key = pr_task_key(pr)
                if key:
                    task_cell = Text.from_markup(
                        f"[@click=app.open_issue('{escape(key)}')][cyan u]{escape(key)}[/cyan u][/]"
                    )
                else:
                    task_cell = Text("—", style="dim")
                assignee = task_assignee.get(key, "") if key else ""
                assignee_cell = (
                    Text(assignee, style=author_color(assignee)) if assignee
                    else Text("—", style="dim")
                )
                status = task_status.get(key, "") if key else ""
                status_cell = (
                    Text(status, style=status_color(status)) if status
                    else Text("—", style="dim")
                )
                table.add_row(
                    self._key_cell(str(pr.id), changed),
                    conflict,
                    task_cell,
                    assignee_cell,
                    status_cell,
                    title_cell,
                    review_cell,
                )
            else:
                table.add_row(
                    self._key_cell(str(pr.id), changed),
                    conflict,
                    title_cell,
                    review_cell,
                )
        self._rows[table_id] = list(prs)
        self._restore_cursor(table, cur)

    def _fill_jobs(self, table_id: str, jobs: list) -> None:
        """Вкладка «Работы»: строки, разделённые по дням горизонтальной чертой с датой.

        Разделители вставляются только когда список действительно идёт по времени
        (порядок по умолчанию либо сортировка по «Обновлено») — при сортировке по
        статусу или заголовку черта «с этого места другой день» была бы неправдой.
        """
        table = self.query_one(f"#{table_id}", DataTable)
        cur = table.cursor_row
        table.clear()
        cells = [
            [str(j.id), _fmt_dt(j.updated_at), j.status, j.anchor,
             ", ".join(str(p.pr_id) for p in j.prs) or "—", j.title or "—"]
            for j in jobs
        ]
        widths = [
            max([len(JOB_COLUMNS[i])] + [len(row[i]) for row in cells])
            for i in range(len(JOB_COLUMNS))
        ]
        by_day = self._sort.get(table_id, (1, True))[0] == 1
        if by_day and jobs:  # заголовок дня длиннее «30.07.2026 15:10» — иначе черта разойдётся
            widths[1] = max([widths[1]] + [len(_day_label(j.updated_at)) + 3 for j in jobs])
        rows: list = []   # объект строки; None у разделителя — по нему нечего открывать
        day = None
        for job, row in zip(jobs, cells):
            if by_day and _day_key(job.updated_at) != day:
                if day is not None:  # пустая строка отбивает закончившийся день от следующего
                    table.add_row(*(Text("") for _ in widths))
                    rows.append(None)
                day = _day_key(job.updated_at)
                table.add_row(*self._day_divider(job.updated_at, widths))
                rows.append(None)
            table.add_row(
                Text(row[0], style="cyan"),
                Text(row[1], style="dim"),
                Text(row[2], style=status_color(job.status)),
                Text(row[3]),
                Text(row[4]),
                Text(row[5]),
            )
            rows.append(job)
        self._rows[table_id] = rows
        self._restore_cursor(table, cur)
        self._skip_divider(table, table_id, step=1)

    def _skip_divider(self, table: DataTable, table_id: str, *, step: int) -> None:
        """Сдвинуть курсор с разделителя дня на ближайшую настоящую строку.

        Разделитель — не работа: на нём нечего открывать и нечего закрывать, поэтому
        курсор через него проезжает, а не застревает.
        """
        rows = self._rows.get(table_id) or []
        idx = table.cursor_row
        if idx is None or idx >= len(rows) or rows[idx] is not None:
            return
        for direction in (step, -step):
            probe = idx + direction
            while 0 <= probe < len(rows):
                if rows[probe] is not None:
                    table.move_cursor(row=probe)
                    return
                probe += direction

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Курсор встал на разделитель дня → проехать дальше в ту же сторону."""
        table_id = event.data_table.id or ""
        prev = self._last_row.get(table_id)
        idx = event.cursor_row
        self._last_row[table_id] = idx
        step = 1 if prev is None or (idx is not None and idx >= prev) else -1
        self._skip_divider(event.data_table, table_id, step=step)

    @staticmethod
    def _day_divider(when: str, widths: list[int]) -> list[Text]:
        """Строка-разделитель дня: черта во всю таблицу с заголовком «сегодня — 30.07.2026».

        Заголовок живёт в колонке «Обновлено» — там же, где время самих работ, так что
        глазу не нужно перепрыгивать. Сначала словами (в них и ищут «а что сегодня»),
        потом дата.
        """
        head = Text("─ ", style="dim")
        head.append(_day_name(when).capitalize(), style=f"bold {_day_color(when)}")
        head.append(" — ", style="dim")
        head.append(_fmt_day(when), style="bold cyan")
        head.append(" ", style="dim")
        head.append("─" * max(0, widths[1] - head.cell_len), style="dim")

        cells = [Text("─" * w, style="dim") for w in widths]
        cells[1] = head
        return cells

    def _render_workspace_head(self) -> None:
        """Шапки двух вкладок: управление контурами и структура активного."""
        self._update_head("#ws-head", self._workspaces_head_lines())
        self._update_head("#structure-head", self._structure_head_lines())
        self._update_head("#rules-head", self._rules_head_lines())

    def _update_head(self, selector: str, lines: list[str]) -> None:
        try:
            head = self.query_one(selector, Static)
        except Exception:  # noqa: BLE001 — шапки нет в урезанных тестовых сборках
            return
        head.update(Text.from_markup("\n".join(lines)))

    def _workspaces_head_lines(self) -> list[str]:
        ws = self.data.workspace
        active = (f"[dim]активный:[/dim] [b]{escape(ws.name or ws.slug)}[/b] "
                  f"[dim]({escape(ws.slug)})[/dim]" if ws else
                  "[yellow]активный воркспейс не выбран[/yellow]")
        # Клавиши тут не перечисляем: для этого есть легенда по «?», и она знает
        # про текущую вкладку. Шапка — только состояние.
        return [f"Воркспейсов: {len(self.data.workspaces)}   ·   {active}"]

    def _structure_head_lines(self) -> list[str]:
        ws = self.data.workspace
        if ws is None:
            return ["[dim]Воркспейс не выбран (W — выбрать).[/dim]"]
        yes, no = "[green]да[/green]", "[dim]нет[/dim]"
        active_jobs = len([j for j in self.data.jobs if j.status == "active"])
        lines = [
            f"[b]{escape(ws.name or ws.slug)}[/b] [dim]({escape(ws.slug)})[/dim]   "
            f"[dim]Jira:[/dim] {yes if ws.jira_enabled else no}   "
            f"[dim]Bitbucket:[/dim] {yes if ws.bitbucket_enabled else no}   "
            f"[dim]работ:[/dim] {len(self.data.jobs)} (активных {active_jobs})",
        ]
        if not self.data.paths:
            lines.append(
                "[yellow]Папок нет[/yellow][dim] — пока воркспейс не привязан ни к одной "
                "папке, jwu не сможет выбрать его автоматически. Нажми [/dim][b]a[/b][dim].[/dim]"
            )
        return lines

    @work(thread=True, exclusive=True, group="reindex")
    def _run_reindex(self) -> None:
        """Переиндексировать в фоне: чтение диска не должно морозить интерфейс."""
        paths = [p.path for p in self.data.paths]
        try:
            index = gitinfo.index(paths)
        except Exception:  # noqa: BLE001
            return
        self.call_from_thread(self._apply_index, index)

    def _apply_index(self, index: dict) -> None:
        self._git_index = index
        try:
            self.query_one("#t-structure", WorkspaceTree).refresh()
            self._update_head("#structure-head", self._structure_head_lines())
        except Exception:  # noqa: BLE001 — виджета может не быть в тестовой сборке
            pass

    def _git_cell(self, path: str) -> Text:
        """Маркер git для папки: «имя/ветка» либо сколько репозиториев внутри."""
        repos = self._git_index.get(path)
        if repos is None:
            return Text("…", style="dim")   # ещё не проиндексировано
        if not repos:
            return Text("—", style="dim")
        if repos[0].root == path:
            return Text(repos[0].label, style="cyan")
        return Text(f"{len(repos)} репо", style="dim cyan")

    def _fill_workspaces(self, table_id: str, items: list) -> None:
        table = self.query_one(f"#{table_id}", DataTable)
        cur = table.cursor_row
        table.clear()
        active_id = self.data.workspace.id if self.data.workspace else None
        for ws in items:
            is_active = ws.id == active_id
            table.add_row(
                Text("→" if is_active else " ", style="bold green"),
                Text(ws.slug, style="bold cyan" if is_active else "cyan"),
                Text(ws.name or "—"),
                Text("да" if ws.jira_enabled else "нет",
                     style="green" if ws.jira_enabled else "dim"),
                Text("да" if ws.bitbucket_enabled else "нет",
                     style="green" if ws.bitbucket_enabled else "dim"),
                Text(str(len(ws.paths))),
                Text(str(ws.jobs_count)),
            )
        self._rows[table_id] = list(items)
        self._restore_cursor(table, cur)

    def _fill_tree(self, table_id: str, paths: list) -> None:
        """Обновить дерево структуры. Пересобираем корни, только если набор папок изменился —
        иначе фоновая индексация схлопывала бы раскрытые узлы под руками."""
        tree = self.query_one(f"#{table_id}", WorkspaceTree)
        self._rows[table_id] = list(paths)
        roots = [p.path for p in paths]
        if roots != self._tree_roots:
            self._tree_roots = roots
            tree.set_roots(list(paths))
        else:
            tree.refresh()  # перерисовать метки (могла смениться ветка)

    def _fill_features(self, table_id: str, features: list) -> None:
        table = self.query_one(f"#{table_id}", DataTable)
        cur = table.cursor_row
        table.clear()
        for f in features:
            label, color = LOCAL_FEATURE_BADGES.get(f.status, (f.status, "white"))
            table.add_row(
                Text(f.key, style="cyan"),
                Text(_fmt_dt(f.updated_at), style="dim"),
                Text(label, style=color),
                Text(f.priority or "—", style="dim"),
                Text(f.title or "—"),
            )
        self._rows[table_id] = list(features)
        self._restore_cursor(table, cur)

    def _fill_rules(self, table_id: str, rules: list) -> None:
        """Вкладка «Правила»: тип · область · суть · когда правили.

        В строке только суть (``title``); подробности — многострочная инструкция —
        живут в карточке по enter, иначе таблица превратилась бы в простыню.
        """
        table = self.query_one(f"#{table_id}", DataTable)
        cur = table.cursor_row
        table.clear()
        for r in rules:
            label, color = WORKSPACE_RULE_BADGES.get(r.kind, (r.kind, "white"))
            title = Text(r.title or "—")
            if r.text:
                title.append("  …", style="dim")   # есть подробности, смотри карточку
            table.add_row(
                Text(label, style=color),
                Text(r.tag, style="cyan") if r.tag else Text("общее", style="dim"),
                title,
                Text(_fmt_dt(r.updated_at), style="dim"),
            )
        self._rows[table_id] = list(rules)
        self._restore_cursor(table, cur)

    def _rules_head_lines(self) -> list[str]:
        rules = self.data.rules
        general = sum(1 for r in rules if not r.tag)
        bans = sum(1 for r in rules if r.kind == "constraint")
        if not rules:
            # пустое состояние объясняет, зачем вкладка нужна; когда правила есть —
            # объяснение только занимает место
            return [
                "[yellow]Правил нет[/yellow][dim] — это то, что агент обязан знать о "
                "проекте: что нельзя делать, как поднять стенд, где что лежит.[/dim]",
                "[dim]Общие правила приезжают в контекст любой работы. Завести — [/dim]"
                "[b]N[/b][dim].[/dim]",
            ]
        return [
            f"Правил: {len(rules)}   ·   [dim]общих:[/dim] {general}   ·   "
            f"[dim]запретов:[/dim] {bans}"
        ]

    # --- взаимодействие ------------------------------------------------- #

    def action_tab_next(self) -> None:
        tabs = self._tabs
        order = self._visible_tabs() or _TAB_ORDER
        idx = order.index(tabs.active) if tabs.active in order else 0
        tabs.active = order[(idx + 1) % len(order)]

    def action_tab_prev(self) -> None:
        tabs = self._tabs
        order = self._visible_tabs() or _TAB_ORDER
        idx = order.index(tabs.active) if tabs.active in order else 0
        tabs.active = order[(idx - 1) % len(order)]

    def _active(self) -> tuple[str, str, str]:
        return TABS.get(self._tabs.active, ("t-mine", "issue", "mine"))

    def _obj_at(self, table_id: str, idx: int | None):
        rows = self._rows.get(table_id, [])
        if idx is None or idx < 0 or idx >= len(rows):
            return None
        return rows[idx]

    def _selected_obj(self):
        table_id, kind, _ = self._active()
        if kind == "tree":
            node = self.query_one(f"#{table_id}", WorkspaceTree).cursor_node
            data = getattr(node, "data", None)
            # у корневого узла есть привязанная папка воркспейса — её и отдаём
            return getattr(data, "root", None) or data
        return self._obj_at(table_id, self.query_one(f"#{table_id}", DataTable).cursor_row)

    def _open_detail(self, obj) -> None:
        # открыли объект → «прочитали» его изменения: снимаем пометку до показа
        # (важно ДО push_screen — _render() обращается к виджетам базового экрана)
        self._ack_object(obj)
        # авто-рефреш открытой сущности — только при включённом -a.
        # сетевые (задача/PR) тянутся из сети раз в detail_interval, локальные
        # (работа/анализ) перечитываются из памяти раз в fast_interval.
        detail_iv = self.detail_interval if self.auto_update else 0.0
        local_iv = self.fast_interval if self.auto_update else 0.0
        if isinstance(obj, Issue):
            jobs = [j for j in self.data.jobs if j.task_key == obj.key]
            self.push_screen(IssueDetailScreen(
                obj, jira_base=self.jira_base, user=self.data.user,
                pr_detail_fn=self._pr_detail_fn,
                jobs=jobs, job_get_fn=self._job_get_fn,
                issue_get_fn=self._issue_get_fn, refresh_interval=detail_iv,
            ))
        elif isinstance(obj, Mention):
            # карточку задачи грузим только здесь: за самим упоминанием мы не следим
            self._mark_mentions_seen([obj.id])
            if obj.task_key and self._issue_get_fn is not None:
                self._open_issue_loading(obj.task_key)
        elif isinstance(obj, PR):
            self.push_screen(PRDetailScreen(
                obj, detail_fn=self._pr_detail_fn, refresh_interval=detail_iv))
        elif isinstance(obj, Job):
            self.push_screen(JobDetailScreen(
                obj.id, get_fn=self._job_get_fn, jira_base=self.jira_base,
                refresh_interval=local_iv))
        elif isinstance(obj, Workspace):
            self._switch_workspace(obj.id)
        elif isinstance(obj, WorkspaceRule):
            self.push_screen(RuleDetailScreen(
                obj.id, get_fn=self._rule_get_fn, edit_fn=self._apply_rule_edit,
                tags_fn=self._known_tags, refresh_interval=local_iv))
        elif isinstance(obj, LocalFeature):
            self.push_screen(FeatureDetailScreen(
                obj.id, get_fn=self._feature_get_fn, jobs_fn=self._feature_jobs_fn,
                status_fn=self._feature_status_fn, edit_fn=self._feature_edit_fn,
                refresh_interval=local_iv))

    # --- панель изменений, поиск и легенда ------------------------------- #

    def _show_changes(self, visible: bool) -> None:
        """Показать/спрятать боковую панель вместе с её ручкой-разделителем."""
        for selector in ("#changes-col", "#splitter"):
            try:
                self.query_one(selector).display = visible
            except Exception:  # noqa: BLE001 — урезанная тестовая сборка
                pass

    def action_toggle_changes(self) -> None:
        """b — свернуть/развернуть панель «Изменения»."""
        try:
            column = self.query_one("#changes-col")
        except Exception:  # noqa: BLE001
            return
        self._show_changes(not column.display)

    def action_search_issue(self) -> None:
        """/ — модалка поиска: ввёл ключ, сразу открылась карточка (данные подтянутся)."""
        if self._issue_get_fn is None:
            self.notify("Поиск задач недоступен: в этом воркспейсе нет Jira",
                        severity="warning")
            return

        def do(value: Optional[str]) -> None:
            key = normalize_issue_key(value or "")
            if key:
                self._open_issue_loading(key)

        self.push_screen(
            TextPromptScreen("Открыть задачу по ключу", placeholder="PROJ-123"), do)

    def action_show_legend(self) -> None:
        """? — легенда клавиш: сначала клавиши текущего окна, ниже общие."""
        page, common, where = self._legend_sections()
        self.push_screen(LegendScreen(page, common, title=where))

    def _legend_sections(self) -> tuple[list, list, str]:
        """Клавиши текущего окна и общие. Собирается ДО показа модалки — потом
        активным экраном станет она сама, и контекст будет уже не тот."""
        scoped = {a for actions in self._TAB_SCOPED_ACTIONS.values() for a in actions}
        screen = self.screen
        on_top = screen is not self.screen_stack[0]

        def rows(bindings) -> list[tuple[str, str]]:
            out: list[tuple[str, str]] = []
            for binding in bindings:
                key, action, description = _binding_parts(binding)
                if not description:
                    continue
                try:
                    if self.check_action(action, ()) is False:
                        continue      # действие недоступно здесь — и в легенде ему не место
                except Exception:  # noqa: BLE001
                    pass
                label = _key_label(key)
                if (label, description) not in out:
                    out.append((label, description))
            return out

        name = type(screen).__name__
        if on_top:
            # поверх дашборда открыт детальный экран — «страница» это его клавиши,
            # и они ПЕРЕКРЫВАЮТ общие: одну и ту же клавишу в обеих секциях не показываем
            page = rows(getattr(type(screen), "BINDINGS", []))
            taken = {key for key, _ in page}
            common = [row for row in rows(b for b in self.BINDINGS
                                          if _binding_parts(b)[1] not in scoped)
                      if row[0] not in taken]
            return page, common, _SCREEN_TITLE.get(name, name)

        page, common = [], []
        for binding in self.BINDINGS:
            (page if _binding_parts(binding)[1] in scoped else common).append(binding)
        section = self._active()[2]
        return (_TAB_HINTS.get(section, []) + rows(page), rows(common),
                _TAB_TITLE.get(self._tabs.active, ""))

    def _open_issue_loading(self, key: str) -> None:
        """Сразу показать карточку задачи по ключу; данные подтянет сам экран (loading)."""
        detail_iv = self.detail_interval if self.auto_update else 0.0
        jobs = [j for j in self.data.jobs if j.task_key == key]
        self.push_screen(IssueDetailScreen(
            Issue(key=key), jira_base=self.jira_base, user=self.data.user,
            pr_detail_fn=self._pr_detail_fn, jobs=jobs, job_get_fn=self._job_get_fn,
            issue_get_fn=self._issue_get_fn, refresh_interval=detail_iv, loading=True,
        ))

    def action_open_issue(self, key: str) -> None:
        """Клик по связанной задаче (секция «Связи») → открыть её карточку, Esc — назад."""
        if self._issue_get_fn is not None:
            self._open_issue_loading(key)

    def action_open_delta(self, key: str) -> None:
        """Клик по строке в «Изменениях» → открыть карточку задачи/PR, где произошло изменение."""
        m = re.match(r"^([^/]+)/([^/]+)#(\d+)$", key)
        if m:  # PR-дельта: project/repo#id
            project, repo, pr_id = m.group(1), m.group(2), int(m.group(3))
            pr = next(
                (p for p in (*self.data.prs_mine, *self.data.prs_review)
                 if p.id == pr_id and p.project == project and p.repository == repo),
                None,
            )
            if pr is not None:
                self._open_detail(pr)
            elif self._pr_detail_fn is not None:
                self.push_screen(PRDetailScreen(
                    PR(id=pr_id, project=project, repository=repo),
                    detail_fn=self._pr_detail_fn,
                    refresh_interval=self.detail_interval if self.auto_update else 0.0,
                ))
            return
        # иначе — задача по ключу: берём из текущих данных или дотягиваем из сети
        issue = next((i for i in self.data.mine if i.key == key), None)
        if issue is not None:
            self._open_detail(issue)
        elif self._issue_get_fn is not None:
            self._open_issue_loading(key)

    def _object_delta_pairs(self, obj) -> list[tuple[str, str]]:
        """(key, kind) накопленных изменений, относящихся к объекту (для очистки пометки)."""
        if isinstance(obj, Issue):
            return [(d.key, d.kind) for d in self.data.deltas if d.key == obj.key]
        if isinstance(obj, PR):
            pairs = []
            for d in self.data.deltas:
                m = re.search(r"#(\d+)$", d.key)
                if m and int(m.group(1)) == obj.id:
                    pairs.append((d.key, d.kind))
            return pairs
        return []

    def _ack_object(self, obj) -> None:
        """Снять пометку «есть обновление» с открытого объекта (очистить его дельты)."""
        if self._clear_changes_fn is None:
            return
        pairs = self._object_delta_pairs(obj)
        if not pairs:
            return
        try:
            self.data = self._clear_changes_fn(pairs)
        except Exception:  # noqa: BLE001
            return
        self._render()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter по строке → детальный экран (объект берём из таблицы-источника)."""
        table_id = event.data_table.id or ""
        self._open_detail(self._obj_at(table_id, event.cursor_row))

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """Клик по заголовку колонки → сортировка (повторный клик — реверс)."""
        table_id = event.data_table.id or ""
        col = event.column_index
        cur = self._sort.get(table_id)
        reverse = (cur is not None and cur[0] == col and not cur[1])
        self._sort[table_id] = (col, reverse)
        self._apply_sort_indicators(table_id)
        self._render()

    def _apply_sort_indicators(self, table_id: str) -> None:
        """Подставить «↑»/«↓» к заголовку отсортированной колонки в указанной таблице."""
        try:
            table = self.query_one(f"#{table_id}", DataTable)
        except Exception:  # noqa: BLE001
            return
        base = self._base_column_labels(table_id)
        sort_col, sort_reverse = self._sort.get(table_id, (-1, False))
        for idx, col_key in enumerate(table.columns):
            label = base[idx] if idx < len(base) else str(col_key.value or "")
            if idx == sort_col:
                label = f"{label} {'↓' if sort_reverse else '↑'}"
            table.columns[col_key].label = Text(label)
        table.refresh()

    def _base_column_labels(self, table_id: str) -> list[str]:
        """Базовые (без индикатора сортировки) заголовки колонок для таблицы."""
        for tab_id, (tid, kind, _) in TABS.items():  # noqa: B007
            if tid != table_id:
                continue
            if kind == "pr":
                return PR_MINE_COLUMNS if table_id == "t-prs-mine" else PR_REVIEW_COLUMNS
            return {"issue": ISSUE_COLUMNS, "mention": MENTION_COLUMNS,
                    "job": JOB_COLUMNS, "rule": RULE_COLUMNS}.get(kind, [])
        return []

    def action_open(self) -> None:
        obj = self._selected_obj()
        if isinstance(obj, Issue) and self.jira_base:
            webbrowser.open(f"{self.jira_base}/browse/{obj.key}")
        elif isinstance(obj, Mention) and self.jira_base and obj.task_key:
            webbrowser.open(f"{self.jira_base}/browse/{obj.task_key}")
        elif isinstance(obj, PR) and obj.url:
            webbrowser.open(obj.url)
        elif isinstance(obj, WorkspacePath) and Path(obj.path).exists():
            # папку открываем файловым менеджером ОС — без subprocess
            webbrowser.open(f"file://{obj.path}")
        elif getattr(obj, "path", None) and Path(obj.path).exists():
            target = obj.path if Path(obj.path).is_dir() else str(Path(obj.path).parent)
            webbrowser.open(f"file://{target}")

    def action_copy_issue_key(self) -> None:
        obj = self._selected_obj()
        if isinstance(obj, Issue):
            notify_copied(self, obj.key)
        elif isinstance(obj, Mention):
            notify_copied(self, obj.task_key)
        elif isinstance(obj, LocalFeature):
            notify_copied(self, obj.key)
        elif isinstance(obj, Job) and obj.anchor:
            notify_copied(self, obj.anchor)
        elif isinstance(obj, WorkspacePath):
            notify_copied(self, obj.path)

    def action_copy_menu(self) -> None:
        obj = self._selected_obj()
        if isinstance(obj, Issue):
            open_copy_modal(self, copy_items_for_issue(
                obj, self.jira_base, user=self.data.user))
        elif isinstance(obj, Job):
            open_copy_modal(self, copy_items_for_job(obj, self.jira_base))
        elif isinstance(obj, PR):
            open_copy_modal(self, copy_items_for_pr(obj))

    def action_ack_changes(self) -> None:
        """C / «очистить всё» — убрать ВСЕ накопленные изменения (и упоминания тоже)."""
        self._mark_mentions_seen(None)
        if self._ack_changes_fn is None:
            return
        try:
            self.data = self._ack_changes_fn()
        except Exception:  # noqa: BLE001
            return
        self._render()

    def _mark_mentions_seen(self, mention_ids: Optional[list[int]]) -> None:
        """Пометить упоминания прочитанными: перечисленные либо все (None)."""
        if self._mentions_seen_fn is None or not self.data.mentions:
            return
        try:
            self.data = self._mentions_seen_fn(mention_ids)
        except Exception:  # noqa: BLE001
            return
        self._render()

    def action_clear_section(self) -> None:
        """c / «✕ очистить» — убрать изменения только активной вкладки."""
        if self._active()[2] == "mentions":
            self._mark_mentions_seen(None)  # у упоминаний «очистить» = «прочитано»
            return
        if self._clear_changes_fn is None:
            return
        section = self._active()[2]
        pairs = [(d.key, d.kind) for d in self._deltas_by_section.get(section, [])]
        if not pairs:
            return
        try:
            self.data = self._clear_changes_fn(pairs)
        except Exception:  # noqa: BLE001
            return
        self._render()

    # Клавиши, доступные только на своей вкладке: секция -> действия.
    # ВАЖНО: скрывает кнопку только False. None у Textual значит «показать серой»,
    # и такая кнопка ещё и занимает клавишу — из-за этого `D` (удалить работу)
    # перехватывал отвязку папки на вкладке Workspace.
    _TAB_SCOPED_ACTIONS = {
        "jobs": ("delete_job", "close_job", "finish_job"),
        "workspaces": ("new_workspace", "delete_workspace"),
        "structure": ("add_path", "remove_path"),
        "features": ("new_feature",),
        "rules": ("new_rule", "edit_rule", "delete_rule"),
    }

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Показывать кнопку только на той вкладке, где она осмысленна."""
        for section, actions in self._TAB_SCOPED_ACTIONS.items():
            if action in actions:
                try:
                    return True if self._active()[2] == section else False
                except Exception:  # noqa: BLE001
                    return False
        if action == "copy_issue_key":
            try:
                obj = self._selected_obj()
                return True if isinstance(obj, (Issue, Job, Mention)) else None
            except Exception:  # noqa: BLE001
                return None
        if action == "copy_menu":
            try:
                obj = self._selected_obj()
                return True if isinstance(obj, (Issue, Job, PR)) else None
            except Exception:  # noqa: BLE001
                return None
        return True

    # --- воркспейс и локальные фичи -------------------------------------- #

    def action_pick_workspace(self) -> None:
        if self._workspaces_fn is None or self._workspace_switch_fn is None:
            return
        self.push_screen(WorkspacePickerScreen(
            workspaces_fn=self._workspaces_fn,
            choose_fn=self._switch_workspace,
            create_fn=self._workspace_create_fn,
            bind_cwd_fn=self._bind_cwd,
            cwd=self.cwd,
            at_startup=self._start_with_picker and self.data.workspace is None,
        ))

    def _switch_workspace(self, workspace_id: int) -> None:
        """Смена контура: чтение из БД уходит в поток, экран сразу говорит «загружаю».

        На большой базе снимок собирается заметное время, и без явного маркера
        переключение выглядит зависанием.
        """
        if self._workspace_switch_fn is None:
            return
        self._loading_workspace = True
        self._update_status()
        self._load_workspace(workspace_id)

    @work(thread=True, exclusive=True, group="switch-workspace")
    def _load_workspace(self, workspace_id: int) -> None:
        try:
            data = self._workspace_switch_fn(workspace_id)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._workspace_switch_failed, str(exc))
            return
        self.call_from_thread(self._workspace_switched, data)

    def _workspace_switch_failed(self, error: str) -> None:
        self._loading_workspace = False
        self.notify(error, title="Не удалось сменить воркспейс", severity="error")
        self._update_status()

    def _workspace_switched(self, data: DashboardData) -> None:
        self._loading_workspace = False
        self._start_with_picker = False
        self._apply_data(data)
        self._apply_tab_visibility()
        self._focus_active_table()
        self._run_reindex()  # у нового контура свои папки — переиндексировать
        name = data.workspace.slug if data.workspace else "?"
        self.query_one("#status", Static).update(f"воркспейс: {name}")

    def _bind_cwd(self, workspace_id: int) -> None:
        if self._path_add_fn is None or not self.cwd:
            return
        try:
            data = self._path_add_fn(workspace_id, self.cwd)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), title="Не удалось привязать папку", severity="error")
            return
        self._apply_data(data)

    def action_new_workspace(self) -> None:
        """Создать воркспейс прямо со вкладки (не заходя в модалку выбора)."""
        if self._workspace_create_fn is None:
            return

        def do(slug: Optional[str]) -> None:
            if not slug:
                return
            try:
                self._workspace_create_fn(slug)  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001
                self.notify(str(exc), title="Не удалось создать воркспейс", severity="error")
                return
            self._run_memory_refresh()
            self.query_one("#status", Static).update(
                f"воркспейс «{slug}» создан — привяжи к нему папки на вкладке «Структура»")

        self.push_screen(
            TextPromptScreen("Новый воркспейс: короткое имя латиницей",
                             placeholder="home-jwu"),
            do,
        )

    def action_delete_workspace(self) -> None:
        ws = self._selected_obj()
        if not isinstance(ws, Workspace) or self._workspace_delete_fn is None:
            return
        if len(self.data.workspaces) <= 1:
            self.notify("Это единственный воркспейс — удалять нечего.",
                        title="Нельзя удалить", severity="warning")
            return

        def do() -> None:
            try:
                data = self._workspace_delete_fn(ws.id)  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001
                self.notify(str(exc), title="Не удалось удалить", severity="error")
                return
            self._apply_data(data)
            self._apply_tab_visibility()

        self.push_screen(ConfirmScreen(
            f"Удалить воркспейс «{ws.label}» вместе со всеми его работами "
            f"({ws.jobs_count}), заметками и настройками?", do))

    def action_add_path(self) -> None:
        """Добавить папку в текущий воркспейс (только на вкладке Workspace)."""
        ws = self.data.workspace
        if ws is None or self._path_add_fn is None:
            return

        def do(path: Optional[str]) -> None:
            if not path:
                return
            try:
                data = self._path_add_fn(ws.id, path)  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001
                self.notify(str(exc), title="Не удалось добавить папку", severity="error")
                return
            self._apply_data(data)
            self._run_reindex()
            self.query_one("#status", Static).update(f"папка добавлена: {path}")

        self.push_screen(
            TextPromptScreen("Папка воркспейса", value=self.cwd, placeholder="/path/to/repo"),
            do,
        )

    def action_remove_path(self) -> None:
        path_row = self._selected_obj()
        ws = self.data.workspace
        if ws is None or path_row is None or self._path_remove_fn is None:
            return
        target = getattr(path_row, "path", None)
        if not target:
            return

        def do() -> None:
            self._apply_data(self._path_remove_fn(ws.id, target))  # type: ignore[misc]

        self.push_screen(ConfirmScreen(f"Отвязать папку {target} от воркспейса?", do))

    def action_new_feature(self) -> None:
        if self._feature_create_fn is None:
            return

        def do(title: Optional[str]) -> None:
            if not title:
                return
            self._feature_create_fn(title)  # type: ignore[misc]
            self._run_memory_refresh()

        self.push_screen(
            TextPromptScreen("Новая фича", placeholder="что нужно сделать"), do)

    # --- правила воркспейса ---------------------------------------------- #

    def _known_tags(self) -> list[str]:
        """Теги папок контура — подсказка при выборе области действия правила."""
        try:
            return sorted(self._tags_fn() or []) if self._tags_fn else []
        except Exception:  # noqa: BLE001 — подсказка не стоит падения экрана
            return []

    def _apply_rule_edit(self, rule_id: int, values: dict) -> None:
        if self._rule_edit_fn is None:
            return
        self._rule_edit_fn(rule_id, values)
        self._run_memory_refresh()

    def action_new_rule(self) -> None:
        if self._rule_create_fn is None:
            return

        def do(values: Optional[dict]) -> None:
            if not values:
                return
            self._rule_create_fn(values)  # type: ignore[misc]
            self._run_memory_refresh()

        self.push_screen(RuleEditScreen(known_tags=self._known_tags()), do)

    def action_edit_rule(self) -> None:
        rule = self._selected_obj()
        if not isinstance(rule, WorkspaceRule) or self._rule_edit_fn is None:
            return

        def do(values: Optional[dict]) -> None:
            if values:
                self._apply_rule_edit(rule.id, values)

        self.push_screen(RuleEditScreen(rule, known_tags=self._known_tags()), do)

    def action_delete_rule(self) -> None:
        rule = self._selected_obj()
        if not isinstance(rule, WorkspaceRule) or self._rule_delete_fn is None:
            return

        def do() -> None:
            self._rule_delete_fn(rule.id)  # type: ignore[misc]
            self._run_memory_refresh()

        self.push_screen(ConfirmScreen(f"Удалить правило «{rule.title}»?", do))

    def action_close_job(self) -> None:
        job = self._selected_obj()
        if not isinstance(job, Job) or self._job_status_fn is None:
            return
        self._job_status_fn(job.id, "cancelled")
        self._run_memory_refresh()
        self.query_one("#status", Static).update(f"работа #{job.id} закрыта (неактуальна)")

    def action_finish_job(self) -> None:
        job = self._selected_obj()
        if not isinstance(job, Job) or self._job_status_fn is None:
            return

        def do() -> None:
            self._job_status_fn(job.id, "done")  # type: ignore[misc]
            self._run_memory_refresh()
            self.query_one("#status", Static).update(f"работа #{job.id} завершена (done)")

        self.push_screen(ConfirmScreen(
            f"Завершить работу #{job.id} «{job.title or ''}» (done)?", do))

    def action_delete_job(self) -> None:
        job = self._selected_obj()
        if not isinstance(job, Job) or self._job_delete_fn is None:
            return

        def do() -> None:
            self._job_delete_fn(job.id)  # type: ignore[misc]
            self._run_memory_refresh()
            self.query_one("#status", Static).update(f"работа #{job.id} удалена")

        self.push_screen(ConfirmScreen(
            f"Удалить работу #{job.id} «{job.title or ''}» безвозвратно?", do))

    def _begin_sync(self, kind: str) -> None:
        self._sync_running[kind] = monotonic()
        self._update_status()

    def _end_sync(self, kind: str) -> None:
        self._sync_running.pop(kind, None)

    def _auto_fast_refresh(self) -> None:
        self._last_fast_mono = monotonic()
        self._run_memory_refresh()

    def _auto_slow_sync(self) -> None:
        # Отсчёт следующего тика — от ОКОНЧАНИЯ синка (см. _after_refresh),
        # а не от его старта, чтобы долгий синк не съедал интервал.
        self._begin_sync("network")
        self._run_full_sync()

    def action_refresh_all(self) -> None:
        """R — полный синк всех вкладок (единственный ручной способ обновления)."""
        if self._full_sync_fn is None:
            self.query_one("#status", Static).update("полный синк недоступен (нет доступа)")
            return
        # ручной синк снимает паузу и обнуляет счётчик: даём авто-синку новый шанс
        self._fail_count = 0
        self._auto_paused = False
        self._begin_sync("network")
        self._run_full_sync()

    def _schedule_next_slow_sync(self) -> None:
        """One-shot таймер на следующий авто-синк (через self.slow_interval секунд)."""
        if not self.auto_update or self._full_sync_fn is None or self._auto_paused:
            return
        self._last_slow_mono = monotonic()  # точка отсчёта countdown в статус-строке
        self.set_timer(self.slow_interval, self._auto_slow_sync)

    @work(thread=True, exclusive=True, group="sync")
    def _run_full_sync(self) -> None:
        if self._full_sync_fn is None:
            self.call_from_thread(self._end_sync, "network")
            return
        try:
            data = self._full_sync_fn()
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._after_refresh, None, _classify_sync_error(exc))
            return
        self.call_from_thread(self._after_refresh, data, None)

    @work(thread=True, group="mem")
    def _run_memory_refresh(self) -> None:
        """Лёгкое обновление из памяти (локальные вкладки + свежие данные после синка)."""
        if self._memory_fn is None:
            return
        try:
            data = self._memory_fn()
        except Exception:  # noqa: BLE001 — память недоступна крайне редко; молчим
            return
        self.call_from_thread(self._apply_data, data)

    def _apply_data(self, data: DashboardData) -> None:
        self.data = data
        if data.user:  # не затираем известные данные пустыми (рефреш из памяти)
            self._user = data.user
            self.sub_title = self._user
        if data.display_name:
            self._display_name = data.display_name
        if data.email:
            self._email = data.email
        self._render()

    def _after_refresh(self, data: Optional[DashboardData], error: Optional[str]) -> None:
        self._end_sync("network")
        try:
            if error is not None:
                # синк упал: запомнить момент и ошибку (статус-строка покажет «не удалось»
                # до следующего успеха), плюс показать всплывающее уведомление.
                self._sync_failed = True
                self._fail_at = datetime.now(timezone.utc).isoformat()
                self._sync_error = error
                self._fail_count += 1
                # после исчерпания ретраев — пауза, чтобы не плодить ошибки
                self._auto_paused = self._fail_count >= self._RETRY_LIMIT
                if self._auto_paused:
                    self.notify(f"{error}. Авто-синхронизация остановлена — "
                                f"нажмите R, чтобы повторить.",
                                title="Синхронизация не удалась",
                                severity="error", timeout=10)
                else:
                    self.notify(error, title="Синхронизация не удалась",
                                severity="error", timeout=10)
                self._update_status()
                return
            # успех — сбрасываем счётчик и снимаем паузу
            self._sync_failed = False
            self._sync_error = ""
            self._fail_count = 0
            self._auto_paused = False
            if data is not None:
                self._apply_data(data)
            else:
                self._update_status()
        finally:
            # Следующий авто-синк отсчитывается от КОНЦА текущего, а не от старта.
            # На паузе (_auto_paused) планировщик сам ничего не ставит.
            self._schedule_next_slow_sync()

