"""Модалка «Копировать…» и контекстные списки атрибутов для буфера обмена."""

from __future__ import annotations

from dataclasses import dataclass

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Label, ListItem, ListView, Static

from ..core.clipboard import copy_to_clipboard
from ..core.models import Issue, Job, PR


def notify_copied(screen, text: str, *, label: str = "") -> None:
    try:
        copy_to_clipboard(text)
        screen.notify(f"Скопировано: {label or text}")
    except Exception as exc:  # noqa: BLE001 — pyperclip / системный буфер
        screen.notify(f"Не скопировать: {exc}", severity="error")


def _issue_url(jira_base: str, key: str) -> str:
    base = (jira_base or "").rstrip("/")
    return f"{base}/browse/{key}" if base and key else ""


def _md_link(label: str, url: str) -> str:
    return f"[{label}]({url})" if label and url else ""


def _wiki_link(label: str, url: str) -> str:
    return f"[{label}|{url}]" if label and url else ""


def mention_comment_text(issue: Issue, user: str) -> str:
    """Текст последнего коммента задачи, где упомянут user ([~login])."""
    if not user:
        return ""
    marker = f"[~{user}]"
    hits = [c for c in issue.comments if marker in (c.body or "")]
    if not hits:
        return ""
    return " ".join((hits[-1].body or "").split())


@dataclass(frozen=True)
class CopyItem:
    hotkey: str
    label: str
    value: str


def _copy_items_nonempty(items: list[CopyItem]) -> list[CopyItem]:
    return [item for item in items if item.value]


def copy_items_for_issue(
    issue: Issue,
    jira_base: str,
    *,
    user: str = "",
) -> list[CopyItem]:
    url = _issue_url(jira_base, issue.key)
    key_summary = (
        f"{issue.key}: {issue.summary}"
        if issue.key and issue.summary else ""
    )
    mention = mention_comment_text(issue, user)
    return _copy_items_nonempty([
        CopyItem("i", "ключ Jira", issue.key),
        CopyItem("u", "URL", url),
        CopyItem("m", "ссылка Markdown", _md_link(issue.key, url)),
        CopyItem("w", "ссылка Jira wiki", _wiki_link(issue.key, url)),
        CopyItem("t", "заголовок", issue.summary or ""),
        CopyItem("s", "KEY: summary", key_summary),
        CopyItem("e", "текст упоминания", mention),
    ])


def copy_items_for_job(job: Job, jira_base: str) -> list[CopyItem]:
    url = _issue_url(jira_base, job.task_key)
    return _copy_items_nonempty([
        CopyItem("i", "ключ задачи", job.task_key),
        CopyItem("u", "URL задачи", url),
        CopyItem("m", "ссылка Markdown задачи", _md_link(job.task_key, url)),
        CopyItem("w", "ссылка Jira wiki задачи", _wiki_link(job.task_key, url)),
        CopyItem("t", "заголовок работы", job.title or ""),
        CopyItem("n", "номер работы", str(job.id) if job.id else ""),
    ])


def copy_items_for_pr(pr: PR) -> list[CopyItem]:
    repo = f"{pr.project}/{pr.repository}" if pr.project and pr.repository else ""
    pr_label = f"{repo}#{pr.id}" if repo else f"#{pr.id}"
    branches = (
        f"{pr.source_branch} → {pr.target_branch}"
        if pr.source_branch and pr.target_branch else ""
    )
    return _copy_items_nonempty([
        CopyItem("p", "номер PR", str(pr.id) if pr.id else ""),
        CopyItem("r", "репозиторий", repo),
        CopyItem("u", "URL", pr.url),
        CopyItem("m", "ссылка Markdown", _md_link(pr_label, pr.url)),
        CopyItem("w", "ссылка Jira wiki", _wiki_link(pr_label, pr.url)),
        CopyItem("t", "заголовок", pr.title or ""),
        CopyItem("b", "ветки", branches),
        CopyItem("f", "source branch", pr.source_branch or ""),
        CopyItem("c", "последний commit", pr.latest_commit or ""),
    ])


def open_copy_modal(screen: Screen, items: list[CopyItem]) -> None:
    if not items:
        screen.notify("Нечего копировать", severity="warning")
        return
    screen.app.push_screen(CopyModalScreen(items))


class CopyModalScreen(ModalScreen[None]):
    """Модалка «Копировать…»: навигация ↑↓/jk, enter/клик/горячая клавиша пункта."""

    _LIST_HEIGHT_CAP = 12

    CSS = """
    CopyModalScreen { align: center middle; }
    #copy-box {
        width: auto; min-width: 44; max-width: 72;
        height: auto;
        padding: 1 2; border: round $accent; background: $panel;
    }
    #copy-title { height: auto; margin-bottom: 1; text-style: bold; }
    #copy-list { border: none; background: transparent; }
    #copy-hint { height: auto; margin-top: 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Закрыть", show=False),
        Binding("q", "quit_app", "Выход", show=False),
        Binding("up,k", "move_up", show=False),
        Binding("down,j", "move_down", show=False),
        Binding("enter", "copy_selected", "Копировать", show=False),
    ]

    def __init__(self, items: list[CopyItem]) -> None:
        super().__init__()
        self._items = items

    def compose(self) -> ComposeResult:
        with Vertical(id="copy-box"):
            yield Static("Копировать…", id="copy-title")
            with ListView(id="copy-list"):
                for item in self._items:
                    yield ListItem(
                        Label(f" {item.hotkey} — {item.label} "),
                        id=f"copy-{item.hotkey}",
                    )
            yield Static(
                "[dim]↑↓ / j k — выбор · enter / клик — копировать · esc — закрыть · q — выход[/dim]",
                id="copy-hint",
            )

    def on_mount(self) -> None:
        lv = self._list()
        # ListView по умолчанию тянется на весь экран — подгоняем под число пунктов.
        lv.styles.height = min(len(self._items), self._LIST_HEIGHT_CAP)
        lv.focus()

    def action_quit_app(self) -> None:
        self.app.exit()

    def _list(self) -> ListView:
        return self.query_one("#copy-list", ListView)

    def action_move_up(self) -> None:
        lv = self._list()
        if lv.index is None:
            lv.index = 0
        elif lv.index > 0:
            lv.index -= 1

    def action_move_down(self) -> None:
        lv = self._list()
        if lv.index is None:
            lv.index = 0
        elif lv.index < len(self._items) - 1:
            lv.index += 1

    def action_copy_selected(self) -> None:
        lv = self._list()
        idx = lv.index if lv.index is not None else 0
        if 0 <= idx < len(self._items):
            self._copy(self._items[idx])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if 0 <= idx < len(self._items):
            self._copy(self._items[idx])

    def on_key(self, event: events.Key) -> None:
        for item in self._items:
            if event.key == item.hotkey:
                self._copy(item)
                event.prevent_default()
                event.stop()
                return

    def _copy(self, item: CopyItem) -> None:
        notify_copied(self, item.value, label=item.label)
        self.dismiss()
