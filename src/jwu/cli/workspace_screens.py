"""Экраны TUI, связанные с воркспейсами: выбор/создание, ввод строки, карточки фичи и правила.

Живут отдельно от ``dashboard.py`` (он и так большой) и НИЧЕГО оттуда не импортируют —
зависимость строго односторонняя. Как и весь TUI, экраны не знают про сеть и БД: всё,
что им нужно, приходит переданными callable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from rich.console import Group
from rich.markup import escape
from rich.rule import Rule
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    DataTable, Footer, Header, Input, Select, Static, TextArea, Tree,
)

from ..core import gitinfo
from ..core.dates import fmt_dt as _fmt_dt
from ..core.models import (
    LOCAL_FEATURE_BADGES, WORKSPACE_RULE_BADGES, WORKSPACE_RULE_KINDS,
    LocalFeature, Workspace, WorkspacePath, WorkspaceRule,
)

WORKSPACE_COLUMNS = ["Workspace", "Название", "Провайдер", "PR", "Папок", "Работ"]


class TextPromptScreen(ModalScreen[Optional[str]]):
    """Модалка с одним полем ввода. Enter — подтвердить, Escape — отмена.

    Результат отдаётся ЧЕРЕЗ ``dismiss(value)``, а не вызовом колбэка на месте:
    иначе обработчик успевал перерисовать нижний экран, пока модалка ещё не снята,
    и его правки не доезжали до отрисовки (таблица обновлялась только после перезапуска).
    Вызывающий передаёт колбэк вторым аргументом ``push_screen`` — он сработает
    уже после того, как экран действительно закрыт.
    """

    CSS = """
    TextPromptScreen { align: center middle; }
    #prompt-box { width: 80; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    #prompt-title { height: auto; margin-bottom: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, title: str, *, value: str = "", placeholder: str = "") -> None:
        super().__init__()
        self._title = title
        self._value = value
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Static(Text.from_markup(f"[b]{escape(self._title)}[/b]"), id="prompt-title")
            yield Input(value=self._value, placeholder=self._placeholder, id="prompt-input")

    def on_mount(self) -> None:
        field = self.query_one("#prompt-input", Input)
        field.focus()
        field.cursor_position = len(field.value)  # чтобы можно было дописывать к пути

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


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
                Text(ws.provider_label,
                     style="cyan" if ws.provider != "local" else "dim"),
                Text("да" if ws.prs_enabled else "нет",
                     style="green" if ws.prs_enabled else "dim"),
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

        def do(slug: Optional[str]) -> None:
            if not slug:
                return
            self._create_fn(slug)  # type: ignore[misc]
            self._reload()

        self.app.push_screen(
            TextPromptScreen("Новый воркспейс: короткое имя (латиница, напр. home-jwu)",
                             placeholder="home-jwu"),
            do,
        )

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


@dataclass
class TreeEntry:
    """Что стоит за узлом дерева: путь, каталог ли это и папка ли воркспейса."""

    path: str
    is_dir: bool
    root: Optional[WorkspacePath] = None  # задано только у корневых узлов


class WorkspaceTree(Tree[TreeEntry]):
    """Структура воркспейса деревом: корни — папки контура, внутри — их содержимое.

    Содержимое подгружается ЛЕНИВО, при раскрытии узла: дерево не обходит диск заранее,
    поэтому большой каталог не тормозит вкладку. Каталоги-репозитории помечаются
    «имя/ветка» (см. core.gitinfo — читается файлами, без запуска git).

    Textual из коробки вешает на раскрытие только ``space``, а ←/→ отдаёт под переход
    к родителю (shift+←). Привычное по редакторам поведение приходится задать самим.
    """

    BINDINGS = [
        *Tree.BINDINGS,
        Binding("right,l", "expand_node", "Раскрыть", show=False),
        Binding("left,h", "collapse_node", "Свернуть", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__("workspace", **kwargs)
        self.show_root = False
        self.guide_depth = 2

    # --- наполнение ------------------------------------------------------ #

    def set_roots(self, paths: list[WorkspacePath]) -> None:
        """Пересобрать корни (папки воркспейса), сохранив раскрытые узлы."""
        expanded = {data.path for data, node in self._walk_entries() if node.is_expanded}
        self.root.remove_children()
        for wp in paths:
            node = self.root.add(
                wp.path, data=TreeEntry(path=wp.path, is_dir=True, root=wp),
                allow_expand=True,
            )
            if wp.path in expanded:
                node.expand()
        self.root.expand()
        # Без курсора (cursor_line = -1) стрелки некуда применить — дерево выглядит
        # «мёртвым». Ставим его на первую папку сразу после наполнения.
        if paths and self.cursor_line < 0:
            self.cursor_line = 0

    def _walk_entries(self):
        stack = list(self.root.children)
        while stack:
            node = stack.pop()
            data = node.data
            if data is not None:
                yield data, node
            stack.extend(node.children)

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        node = event.node
        data = node.data
        if data is None or not data.is_dir or node.children:
            return
        for child in self._children_of(data.path):
            node.add_leaf(child.path.rsplit("/", 1)[-1], data=child) if not child.is_dir \
                else node.add(child.path.rsplit("/", 1)[-1], data=child, allow_expand=True)

    @staticmethod
    def _children_of(path: str) -> list[TreeEntry]:
        """Содержимое каталога: сначала папки, затем файлы; служебное скрыто."""
        try:
            entries = sorted(
                Path(path).iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except OSError:
            return []
        out: list[TreeEntry] = []
        for item in entries:
            if item.name.startswith(".") or item.name in gitinfo.SKIP_DIRS:
                continue
            out.append(TreeEntry(path=str(item), is_dir=item.is_dir()))
        return out

    def render_label(self, node, base_style, style):
        label = super().render_label(node, base_style, style)
        data = node.data
        if data is None or not data.is_dir:
            return label
        info = gitinfo.git_info(data.path)
        tags = data.root.tags if data.root else []
        if info is None and not tags:
            return label
        label = label.copy()
        if info is not None:
            label.append("  ")
            label.append(f" {info.label} ", style="black on cyan")
        for tag in tags:
            label.append("  ")
            label.append(f"#{tag}", style="magenta")
        return label

    # --- клавиши --------------------------------------------------------- #

    def action_expand_node(self) -> None:
        node = self.cursor_node
        if node is None:
            return
        if node.allow_expand and not node.is_expanded:
            node.expand()
        else:
            self.action_cursor_down()

    def action_collapse_node(self) -> None:
        node = self.cursor_node
        if node is None:
            return
        if node.is_expanded:
            node.collapse()
        elif node.parent is not None and node.parent is not self.root:
            self.cursor_line = node.parent.line


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

        def do(value: Optional[str]) -> None:
            if not value:
                return
            self._edit_fn(self.feature_id, value)  # type: ignore[misc]
            self._reload()

        self.app.push_screen(
            TextPromptScreen("Название фичи", value=feature.title), do)


class RuleEditScreen(ModalScreen[Optional[dict]]):
    """Создание/правка правила воркспейса: тип, тег, суть и подробности.

    ``TextPromptScreen`` рядом однострочный — инструкцию «как поднять стенд» в него не
    ввести, поэтому подробности живут в ``TextArea``. Enter там переводит строку, так
    что сохранение повешено на Ctrl+S. Результат, как и у соседа, отдаётся через
    ``dismiss(value)``: колбэк на месте не успевал бы дорисовать нижний экран.
    """

    CSS = """
    RuleEditScreen { align: center middle; }
    #rule-box { width: 92; height: auto; max-height: 90%; border: round $accent;
                padding: 1 2; background: $surface; }
    #rule-title { height: auto; margin-bottom: 1; }
    #rule-text { height: 12; margin-top: 1; }
    #rule-hint { height: auto; margin-top: 1; color: $text-muted; }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Отмена"),
        Binding("ctrl+s", "save", "Сохранить", priority=True),
    ]

    def __init__(self, rule: Optional[WorkspaceRule] = None,
                 *, known_tags: Optional[list[str]] = None) -> None:
        super().__init__()
        self._rule = rule
        self._known_tags = known_tags or []

    def compose(self) -> ComposeResult:
        rule = self._rule
        with Vertical(id="rule-box"):
            head = "Правка правила" if rule else "Новое правило воркспейса"
            yield Static(Text.from_markup(f"[b]{head}[/b]"), id="rule-title")
            yield Select(
                [(WORKSPACE_RULE_BADGES[k][0], k) for k in WORKSPACE_RULE_KINDS],
                value=rule.kind if rule else "info",
                allow_blank=False, id="rule-kind",
            )
            tags = f" (известные: {', '.join(self._known_tags)})" if self._known_tags else ""
            yield Input(value=rule.tag if rule else "", id="rule-tag",
                        placeholder=f"тег папки — пусто, если правило общее{tags}")
            yield Input(value=rule.title if rule else "", id="rule-name",
                        placeholder="суть одной строкой")
            yield TextArea(rule.text if rule else "", id="rule-text")
            yield Static(
                Text.from_markup("[dim]Ctrl+S — сохранить · Escape — отмена[/dim]"),
                id="rule-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#rule-name", Input).focus()

    def action_save(self) -> None:
        title = self.query_one("#rule-name", Input).value.strip()
        if not title:
            self.notify("Нужна суть правила — одной строкой", severity="warning")
            return
        self.dismiss({
            "kind": self.query_one("#rule-kind", Select).value,
            "tag": self.query_one("#rule-tag", Input).value.strip(),
            "title": title,
            "text": self.query_one("#rule-text", TextArea).text.strip(),
        })

    def action_cancel(self) -> None:
        self.dismiss(None)


class RuleDetailScreen(Screen):
    """Карточка правила: тип, область действия и подробности целиком."""

    CSS = "VerticalScroll { padding: 1 2; }"
    BINDINGS = [
        Binding("escape,backspace,q", "app.pop_screen", "← Назад"),
        Binding("e", "edit", "Править"),
    ]

    def __init__(
        self,
        rule_id: int,
        *,
        get_fn: Optional[Callable[[int], Optional[WorkspaceRule]]],
        edit_fn: Optional[Callable[[int, dict], None]] = None,
        tags_fn: Optional[Callable[[], list[str]]] = None,
        refresh_interval: float = 0.0,
    ) -> None:
        super().__init__()
        self.rule_id = rule_id
        self._get_fn = get_fn
        self._edit_fn = edit_fn
        self._tags_fn = tags_fn
        self._refresh_interval = refresh_interval

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(Static(id="rule-body"))
        yield Footer()

    def on_mount(self) -> None:
        self._reload()
        if self._refresh_interval:
            self.set_interval(self._refresh_interval, self._reload)

    def _reload(self) -> None:
        rule = self._get_fn(self.rule_id) if self._get_fn else None
        body = self.query_one("#rule-body", Static)
        if rule is None:
            body.update("[dim]правило не найдено[/dim]")
            return
        self.sub_title = rule.title
        label, color = WORKSPACE_RULE_BADGES.get(rule.kind, (rule.kind, "white"))
        scope = f"[cyan]{escape(rule.tag)}[/cyan]" if rule.tag else "[dim]весь воркспейс[/dim]"
        parts = [
            Text.from_markup(f"[b {color}]{escape(label)}[/b {color}]  {escape(rule.title)}"),
            Rule(style="cyan"),
            Text.from_markup(f"[dim]область:[/dim] {scope}   "
                             f"[dim]обновлено:[/dim] {escape(_fmt_dt(rule.updated_at))}"),
        ]
        if rule.text:
            parts += [Text(""), Text(rule.text)]
        body.update(Group(*parts))

    def action_edit(self) -> None:
        rule = self._get_fn(self.rule_id) if self._get_fn else None
        if rule is None or self._edit_fn is None:
            return

        def do(values: Optional[dict]) -> None:
            if not values:
                return
            self._edit_fn(self.rule_id, values)  # type: ignore[misc]
            self._reload()

        known = self._tags_fn() if self._tags_fn else []
        self.app.push_screen(RuleEditScreen(rule, known_tags=known), do)


class LegendScreen(ModalScreen):
    """Легенда клавиш текущего окна: сначала свои, ниже общие.

    Набор клавиш зависит от того, где ты стоишь: на «Работах» `D` удаляет работу, на
    «Правилах» — правило, а в карточке задачи половины этих клавиш нет вовсе. Поэтому
    легенда собирается вызывающим под текущий экран, а не хардкодится здесь.
    """

    CSS = """
    LegendScreen { align: center middle; }
    #legend-box { width: 76; height: auto; max-height: 85%; border: round $accent;
                  padding: 1 2; background: $surface; }
    #legend-body { height: auto; }
    #legend-hint { height: auto; margin-top: 1; color: $text-muted; }
    """
    BINDINGS = [Binding("escape,question_mark,q", "close", "Закрыть")]

    def __init__(self, page: list[tuple[str, str]], common: list[tuple[str, str]],
                 *, title: str = "") -> None:
        super().__init__()
        self._page = page
        self._common = common
        self._where = title

    def compose(self) -> ComposeResult:
        with Vertical(id="legend-box"):
            yield Static(Text.from_markup("[b]Клавиши[/b]"), id="legend-title")
            yield VerticalScroll(Static(self._body(), id="legend-body"))
            yield Static(
                Text.from_markup("[dim]Escape или ? — закрыть[/dim]"), id="legend-hint")

    def _body(self):
        parts = []
        here = f"На этой странице — {self._where}" if self._where else "На этой странице"
        for header, rows, empty in (
            (here, self._page, "своих клавиш нет"),
            ("Общие", self._common, "нет"),
        ):
            parts.append(Rule(header, align="left", style="cyan"))
            if not rows:
                parts.append(Text.from_markup(f"[dim]{empty}[/dim]"))
                continue
            width = max(len(key) for key, _ in rows)
            for key, description in rows:
                parts.append(Text.from_markup(
                    f"  [b cyan]{escape(key.ljust(width))}[/b cyan]  {escape(description)}"
                ))
        return Group(*parts)

    def action_close(self) -> None:
        self.dismiss()
