"""Экраны TUI, связанные с воркспейсами: выбор/создание, ввод строки, карточка фичи.

Живут отдельно от ``dashboard.py`` (он и так большой) и НИЧЕГО оттуда не импортируют —
зависимость строго односторонняя. Как и весь TUI, экраны не знают про сеть и БД: всё,
что им нужно, приходит переданными callable.
"""

from __future__ import annotations

from typing import Callable, Optional

from rich.console import Group
from rich.markup import escape
from rich.rule import Rule
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Static

from ..core.dates import fmt_dt as _fmt_dt
from ..core.models import LOCAL_FEATURE_BADGES, LocalFeature, Workspace

WORKSPACE_COLUMNS = ["Workspace", "Название", "Jira", "Bitbucket", "Папок", "Работ"]


class TextPromptScreen(ModalScreen):
    """Модалка с одним полем ввода. Enter — подтвердить, Escape — отмена."""

    CSS = """
    TextPromptScreen { align: center middle; }
    #prompt-box { width: 80; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    #prompt-title { height: auto; margin-bottom: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, title: str, *, value: str = "",
                 placeholder: str = "", on_submit: Callable[[str], None]) -> None:
        super().__init__()
        self._title = title
        self._value = value
        self._placeholder = placeholder
        self._on_submit = on_submit

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Static(Text.from_markup(f"[b]{escape(self._title)}[/b]"), id="prompt-title")
            yield Input(value=self._value, placeholder=self._placeholder, id="prompt-input")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss()
        if value:
            self._on_submit(value)

    def action_cancel(self) -> None:
        self.dismiss()


class WorkspacePickerScreen(ModalScreen):
    """Выбор воркспейса: Enter — открыть, n — создать, a — привязать текущую папку.

    В стартовом режиме (``at_startup``) Escape закрывает приложение: без воркспейса
    дашборду просто нечего показывать.
    """

    CSS = """
    WorkspacePickerScreen { align: center middle; }
    #ws-box { width: 90; height: auto; max-height: 80%; border: round $accent;
              padding: 1 2; background: $surface; }
    #ws-hint { height: auto; margin-top: 1; color: $text-muted; }
    #ws-table { height: auto; max-height: 20; }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Отмена"),
        Binding("n", "create", "Создать"),
        Binding("a", "bind_cwd", "Привязать папку"),
        Binding("enter", "choose", "Открыть", priority=True),
    ]

    def __init__(
        self,
        *,
        workspaces_fn: Callable[[], list[Workspace]],
        choose_fn: Callable[[int], None],
        create_fn: Optional[Callable[[str], None]] = None,
        bind_cwd_fn: Optional[Callable[[int], None]] = None,
        cwd: str = "",
        at_startup: bool = False,
    ) -> None:
        super().__init__()
        self._workspaces_fn = workspaces_fn
        self._choose_fn = choose_fn
        self._create_fn = create_fn
        self._bind_cwd_fn = bind_cwd_fn
        self._cwd = cwd
        self._at_startup = at_startup
        self._rows: list[Workspace] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="ws-box"):
            yield Static(Text.from_markup("[b]Воркспейсы[/b]"), id="ws-title")
            yield DataTable(id="ws-table")
            yield Static(id="ws-hint")

    def on_mount(self) -> None:
        table = self.query_one("#ws-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        for col in WORKSPACE_COLUMNS:
            table.add_column(col, key=col)
        self._reload()
        table.focus()

    def _reload(self) -> None:
        table = self.query_one("#ws-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        self._rows = list(self._workspaces_fn())
        for ws in self._rows:
            table.add_row(
                Text(ws.slug, style="cyan"),
                Text(ws.name or "—"),
                Text("да" if ws.jira_enabled else "нет",
                     style="green" if ws.jira_enabled else "dim"),
                Text("да" if ws.bitbucket_enabled else "нет",
                     style="green" if ws.bitbucket_enabled else "dim"),
                Text(str(len(ws.paths))),
                Text(str(ws.jobs_count)),
            )
        if self._rows:
            table.move_cursor(row=min(max(cursor, 0), len(self._rows) - 1))
        hint = "[dim]Enter — открыть · n — создать · a — привязать текущую папку"
        hint += " · Escape — выход[/dim]" if self._at_startup else " · Escape — отмена[/dim]"
        if self._cwd:
            hint += f"\n[dim]текущая папка: {escape(self._cwd)}[/dim]"
        self.query_one("#ws-hint", Static).update(Text.from_markup(hint))

    def _selected(self) -> Optional[Workspace]:
        idx = self.query_one("#ws-table", DataTable).cursor_row
        if idx is None or idx < 0 or idx >= len(self._rows):
            return None
        return self._rows[idx]

    def action_choose(self) -> None:
        ws = self._selected()
        if ws is None:
            return
        self.dismiss()
        self._choose_fn(ws.id)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_choose()

    def action_create(self) -> None:
        if self._create_fn is None:
            return

        def do(slug: str) -> None:
            self._create_fn(slug)  # type: ignore[misc]
            self._reload()

        self.app.push_screen(TextPromptScreen(
            "Новый воркспейс: короткое имя (латиница, напр. home-jwu)",
            placeholder="home-jwu", on_submit=do,
        ))

    def action_bind_cwd(self) -> None:
        ws = self._selected()
        if ws is None or self._bind_cwd_fn is None:
            return
        self._bind_cwd_fn(ws.id)
        self._reload()

    def action_cancel(self) -> None:
        self.dismiss()
        if self._at_startup:
            self.app.exit()


class FeatureDetailScreen(Screen):
    """Карточка локальной фичи: описание, статус, связанные работы."""

    CSS = "VerticalScroll { padding: 1 2; }"
    BINDINGS = [
        Binding("escape,backspace,q", "app.pop_screen", "← Назад"),
        Binding("s", "cycle_status", "Статус"),
        Binding("e", "edit_title", "Заголовок"),
    ]

    def __init__(
        self,
        feature_id: int,
        *,
        get_fn: Optional[Callable[[int], Optional[LocalFeature]]],
        jobs_fn: Optional[Callable[[int], list]] = None,
        status_fn: Optional[Callable[[int, str], None]] = None,
        edit_fn: Optional[Callable[[int, str], None]] = None,
        refresh_interval: float = 0.0,
    ) -> None:
        super().__init__()
        self.feature_id = feature_id
        self._get_fn = get_fn
        self._jobs_fn = jobs_fn
        self._status_fn = status_fn
        self._edit_fn = edit_fn
        self._refresh_interval = refresh_interval

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(Static(id="feature-body"))
        yield Footer()

    def on_mount(self) -> None:
        self._reload()
        if self._refresh_interval:
            self.set_interval(self._refresh_interval, self._reload)

    def _reload(self) -> None:
        feature = self._get_fn(self.feature_id) if self._get_fn else None
        body = self.query_one("#feature-body", Static)
        if feature is None:
            body.update("[dim]фича не найдена[/dim]")
            return
        self.sub_title = feature.key
        label, color = LOCAL_FEATURE_BADGES.get(feature.status, (feature.status, "white"))
        parts = [
            Text.from_markup(
                f"[b cyan]{escape(feature.key)}[/b cyan] [{color}]{escape(label)}[/{color}]  "
                f"{escape(feature.title)}"
            ),
            Rule(style="cyan"),
        ]
        meta = f"[dim]обновлена {escape(_fmt_dt(feature.updated_at))}[/dim]"
        if feature.priority:
            meta += f"   [dim]·  приоритет:[/dim] {escape(feature.priority)}"
        parts.append(Text.from_markup(meta))
        if feature.description:
            parts.append(Text(""))
            parts.append(Text(feature.description))
        jobs = self._jobs_fn(self.feature_id) if self._jobs_fn else []
        if jobs:
            parts.append(Text(""))
            parts.append(Text.from_markup("[b]Работы[/b]"))
            for job in jobs:
                parts.append(Text.from_markup(
                    f"  [cyan]#{job.id}[/cyan] [{escape(job.status)}] "
                    f"{escape(job.title or '—')} [dim]({len(job.records)} записей)[/dim]"
                ))
        body.update(Group(*parts))

    def action_cycle_status(self) -> None:
        """Следующий статус по кругу — быстрее, чем открывать отдельное меню."""
        feature = self._get_fn(self.feature_id) if self._get_fn else None
        if feature is None or self._status_fn is None:
            return
        order = list(LOCAL_FEATURE_BADGES)
        idx = order.index(feature.status) if feature.status in order else -1
        self._status_fn(self.feature_id, order[(idx + 1) % len(order)])
        self._reload()

    def action_edit_title(self) -> None:
        feature = self._get_fn(self.feature_id) if self._get_fn else None
        if feature is None or self._edit_fn is None:
            return

        def do(value: str) -> None:
            self._edit_fn(self.feature_id, value)  # type: ignore[misc]
            self._reload()

        self.app.push_screen(TextPromptScreen(
            "Название фичи", value=feature.title, on_submit=do))
