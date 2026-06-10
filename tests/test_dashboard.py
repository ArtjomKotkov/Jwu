import asyncio
import sqlite3

import pytest
from textual.widgets import DataTable, TabbedContent

from jwu.cli.copy_modal import (
    CopyModalScreen,
    copy_items_for_issue,
    copy_items_for_job,
    copy_items_for_pr,
)
from jwu.cli.dashboard import JwuDashboard, _fmt_ago
from jwu.core.models import Issue, Job, PR
from jwu.core.service import DashboardData, dashboard_from_memory
from jwu.core.store import Store


def _issue(key, status="Open"):
    return Issue(key=key, summary=f"summary {key}", status=status, priority="High")


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


# --- store: вью-фильтрация и last_sync ------------------------------------ #


def test_latest_issues_filtered_by_view(store):
    run = store.start_sync_run(["mine", "mentions"])
    store.save_issue_snapshot(run, _issue("A-1"), ["mine"])
    store.save_issue_snapshot(run, _issue("A-2"), ["mentions"])
    store.save_issue_snapshot(run, _issue("A-3"), ["mine", "mentions"])
    assert {i.key for i in store.latest_issues("mine")} == {"A-1", "A-3"}
    assert {i.key for i in store.latest_issues("mentions")} == {"A-2", "A-3"}
    assert len(store.latest_issues()) == 3


def test_latest_prs_filtered_by_view(store):
    run = store.start_sync_run(["review"])
    store.save_pr_snapshot(run, PR(id=1, project="P", repository="r"), ["mine"])
    store.save_pr_snapshot(run, PR(id=2, project="P", repository="r"), ["review"])
    assert [p.id for p in store.latest_prs("mine")] == [1]
    assert [p.id for p in store.latest_prs("review")] == [2]
    assert len(store.latest_prs()) == 2


def test_last_sync_at_present_after_run(store):
    assert store.last_sync_at() is None
    store.start_sync_run(["mine"])
    assert store.last_sync_at() is not None


# --- store: миграция старой БД без колонки views -------------------------- #


def test_migration_adds_views_column(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE issue_snapshots (id INTEGER PRIMARY KEY, sync_run_id INT,"
        " key TEXT, signature TEXT, fields TEXT, fetched_at TEXT);"
        "CREATE TABLE pr_snapshots (id INTEGER PRIMARY KEY, sync_run_id INT,"
        " pr_id INT, project TEXT, repo TEXT, conflicted INT, fields TEXT, fetched_at TEXT);"
    )
    con.commit()
    con.close()

    s = Store(db)  # не должно падать — миграция добавит views
    try:
        cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(issue_snapshots)")}
        assert "views" in cols
        # и снапшот с вью пишется/читается
        run = s.start_sync_run(["mine"])
        s.save_issue_snapshot(run, _issue("M-1"), ["mine"])
        assert [i.key for i in s.latest_issues("mine")] == ["M-1"]
    finally:
        s.close()


# --- service: агрегатор дашборда ------------------------------------------ #


def test_dashboard_from_memory_splits(store):
    run = store.start_sync_run(["mine", "mentions"])
    store.save_issue_snapshot(run, _issue("M-1"), ["mine"])
    store.save_issue_snapshot(run, _issue("X-1"), ["mentions"])
    store.save_pr_snapshot(run, PR(id=1, project="P", repository="r"), ["mine"])
    store.save_pr_snapshot(run, PR(id=2, project="P", repository="r"), ["review"])

    d = dashboard_from_memory(store, user="alice")
    assert d.user == "alice"
    assert [i.key for i in d.mine] == ["M-1"]
    assert [i.key for i in d.mentions] == ["X-1"]
    assert [p.id for p in d.prs_mine] == [1]
    assert [p.id for p in d.prs_review] == [2]
    assert "mine" in d.to_json_dict()


def test_dashboard_shows_all_jobs_including_closed(tmp_path):
    s = Store(tmp_path / "s.db")
    j1 = s.create_job("A-1", "активная")
    j2 = s.create_job("A-2", "закрытая")
    s.set_job_status(j2.id, "cancelled")
    d = dashboard_from_memory(s, user="u")
    s.close()
    statuses = {j.id: j.status for j in d.jobs}
    assert statuses[j1.id] == "active"
    assert statuses[j2.id] == "cancelled"  # закрытая остаётся в списке


def test_fmt_ago():
    # пусто → фоллбэк, невалидный ввод тоже → фоллбэк (наружу ISO/мусор не пускаем)
    assert "синк" in _fmt_ago(None)
    assert "синк" in _fmt_ago("not-a-date")


def test_render_jira_code_block():
    from rich.panel import Panel
    from rich.text import Text

    from jwu.cli.dashboard import render_jira_text

    parts = render_jira_text("до\n{code:java}\nint x = 1;\n{code}\nпосле")
    assert [type(p).__name__ for p in parts] == ["Text", "Panel", "Text"]
    panel = [p for p in parts if isinstance(p, Panel)][0]
    assert panel.title == "java"
    assert "int x = 1;" in panel.renderable.plain

    # noformat и обычный текст
    parts2 = render_jira_text("{noformat}raw{noformat}")
    assert isinstance(parts2[0], Panel) and parts2[0].title == "noformat"
    assert all(isinstance(p, Text) for p in render_jira_text("просто текст"))


def test_render_md_code_block():
    from rich.panel import Panel
    from rich.text import Text

    from jwu.cli.dashboard import render_md_text

    parts = render_md_text("до\n```python\nx = 1\n```\nпосле")
    assert [type(p).__name__ for p in parts] == ["Text", "Panel", "Text"]
    panel = [p for p in parts if isinstance(p, Panel)][0]
    assert panel.title == "python"
    assert "x = 1" in panel.renderable.plain

    # без языка → "code"; обычный текст без блоков
    assert render_md_text("```\nraw\n```")[0].title == "code"
    assert all(isinstance(p, Text) for p in render_md_text("просто текст"))


# --- TUI: смоук через Pilot ----------------------------------------------- #


def _dash_data():
    from jwu.core.models import Comment

    rich_issue = _issue("A-1")
    rich_issue.description = "до\n{code:java}\nint x=1;\n{code}\nпосле"
    rich_issue.comments = [
        Comment(id="1", author="Bob", body="обычный"),
        Comment(id="2", author="Carol", body="эй [~alice] глянь"),
    ]
    return DashboardData(
        user="alice",
        last_sync={"mine": None},
        mine=[rich_issue, _issue("A-2")],
        prs_review=[PR(id=5, project="P", repository="r", title="t", conflicted=True, url="u")],
    )


def test_render_jira_text_inline_attachments_and_links():
    from rich.text import Text as RText

    from jwu.cli.dashboard import render_jira_text

    body = "см [^app.log] и !shot.png! ссылка [тут|http://e.com] голая http://bare.io"
    parts = render_jira_text(body, attach_map={"app.log": 0})
    plain = "".join(p.plain for p in parts if isinstance(p, RText))
    assert "📄 app.log" in plain          # чип вложения как в правом блоке
    assert "🖼 shot.png" in plain         # встроенная !картинка!
    assert "тут" in plain and "http://e.com" not in plain  # ссылка показана лейблом
    assert "http://bare.io" in plain      # голый URL остаётся (лейбл = url)
    assert "[^app.log]" not in plain      # сырой маркер вложения убран


def test_render_jira_image_with_spaces_in_name():
    from rich.text import Text as RText

    from jwu.cli.dashboard import render_jira_text

    # Jira-картинка с пробелами в имени и параметрами размера (как в WMDJANGOCHAT-25)
    body = "вот:\n!Снимок экрана 2026-06-03 в 19.38.24.png|width=966,height=378!"
    parts = render_jira_text(body)
    plain = "".join(p.plain for p in parts if isinstance(p, RText))
    assert "🖼 Снимок экрана 2026-06-03 в 19.38.24.png" in plain
    assert "!" not in plain and "width=" not in plain   # сырой маркер/параметры убраны


def _spans_with(text, needle):
    """Стили всех спанов rich.Text, содержащие подстроку needle."""
    return [str(sp.style) for sp in text.spans if needle in str(sp.style)]


def test_render_jira_color_and_bold():
    from rich.text import Text as RText

    from jwu.cli.dashboard import render_jira_text

    # цвет + жирный (как в WMDJANGOCHAT-25), плюс «пустой» {*}{*}
    parts = render_jira_text("{color:#de350b}*Сервер*{color}{*}{*}")
    text = next(p for p in parts if isinstance(p, RText))
    assert "Сервер" in text.plain
    # сырые маркеры убраны
    assert "{color" not in text.plain and "*" not in text.plain and "{*}" not in text.plain
    # есть спан со стилем, несущим и цвет, и жирный
    assert any("#de350b" in s and "bold" in s for s in _spans_with(text, "#de350b"))


def test_render_jira_italic_mono_and_nested_link():
    from rich.text import Text as RText

    from jwu.cli.dashboard import render_jira_text

    parts = render_jira_text("_курсив_ и {{mono}} текст")
    text = next(p for p in parts if isinstance(p, RText))
    assert "курсив" in text.plain and "mono" in text.plain
    assert "_" not in text.plain and "{{" not in text.plain
    assert any("italic" in str(sp.style) for sp in text.spans)

    # вложенность цвет → жирный → ссылка: ссылка показана лейблом, маркеры убраны
    parts2 = render_jira_text("{color:#de350b}*[https://e.com]*{color}")
    text2 = next(p for p in parts2 if isinstance(p, RText))
    assert "https://e.com" in text2.plain
    assert "{color" not in text2.plain and "*" not in text2.plain


def test_render_jira_fenced_block_and_no_false_bold():
    from rich.panel import Panel
    from rich.text import Text as RText

    from jwu.cli.dashboard import render_jira_text

    # ```fenced``` в Jira-прозе → отдельная панель; '*' внутри не даёт жирного
    parts = render_jira_text("до\n```\nrequest, *args, **kwargs\n```\nпосле")
    assert [type(p).__name__ for p in parts] == ["Text", "Panel", "Text"]
    panel = next(p for p in parts if isinstance(p, Panel))
    assert "*args, **kwargs" in panel.renderable.plain

    # одиночные '*' в обычной прозе не оформляются как жирный
    plain = render_jira_text("умножение 2 * 2 = 4")
    text = next(p for p in plain if isinstance(p, RText))
    assert "2 * 2 = 4" in text.plain
    assert not any("bold" in str(sp.style) for sp in text.spans)


def test_deltas_by_section_and_tab_badge():
    from jwu.core.models import Delta

    data = _dash_data()  # mine=[A-1, A-2], prs_review=[#5]
    data.deltas = [
        Delta(key="A-1", kind="new_comment", summary="s"),
        Delta(key="P/r#5", kind="new_conflict", summary="t"),
    ]
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test():
            assert [d.key for d in app._deltas_by_section["mine"]] == ["A-1"]
            assert [d.key for d in app._deltas_by_section["prs_review"]] == ["P/r#5"]
            assert app._deltas_by_section["mentions"] == []
            tabs = app.query_one("#tabs", TabbedContent)
            assert "●1" in str(tabs.get_tab("tab-mine").label)
            assert "●1" in str(tabs.get_tab("tab-prs-review").label)
            assert "●" not in str(tabs.get_tab("tab-mentions").label)

    asyncio.run(run())


def test_gone_delta_routed_to_section_even_when_absent_from_list():
    """gone/pr_gone попадают в свою вкладку по d.section, хотя сущности в списке уже нет."""
    from jwu.core.models import Delta

    data = _dash_data()  # mine=[A-1, A-2], prs_review=[#5]
    data.deltas = [
        Delta(key="GONE-9", kind="gone", summary="ушла", section="mine"),
        Delta(key="P/r#77", kind="pr_gone", summary="смержен", section="prs_review"),
    ]
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test():
            assert [d.key for d in app._deltas_by_section["mine"]] == ["GONE-9"]
            assert [d.key for d in app._deltas_by_section["prs_review"]] == ["P/r#77"]

    asyncio.run(run())


def test_changes_panel_survives_bracket_in_truncated_summary():
    """Регресс: `summary[:50]`, обрезанный внутри `[тег]`, ронял рендер панели."""
    from textual.widgets import Static

    from jwu.core.models import Delta

    data = _dash_data()  # активная вкладка — «Мои задачи» (A-1, A-2)
    # обрезка на 50-м символе попадает внутрь `[acme]` → висячая `[`
    long_with_bracket = "Нужен фикс расхождения данных в статистике v2 [acme]"
    data.deltas = [
        Delta(key="A-1", kind="new_comment", summary=long_with_bracket),
        Delta(key="A-2", kind="status_change", summary="Проблема [1win]", detail="A → B"),
    ]
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test():
            panel = str(app.query_one("#changes", Static).render())  # не должно бросать MarkupError
            assert "A-1" in panel

    asyncio.run(run())


def test_scoped_changes_panel_shows_only_active_section():
    from textual.widgets import Static

    from jwu.core.models import Comment, Delta

    data = _dash_data()  # активная вкладка по умолчанию — «Мои задачи» (A-1, A-2)
    mention_issue = _issue("X-1")
    mention_issue.comments = [Comment(id="9", author="Z", body="[~alice]")]
    data.mentions = [mention_issue]
    # дельта относится только к упоминаниям, не к активной вкладке «Мои задачи»
    data.deltas = [Delta(key="X-1", kind="new_comment", summary="s")]
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test():
            panel = str(app.query_one("#changes", Static).render())
            assert "Мои задачи" in panel and "нет" in panel.lower()  # активной нет дельт
            assert app._deltas_by_section["mentions"][0].key == "X-1"  # но они есть у упоминаний

    asyncio.run(run())


def test_refresh_all_is_only_manual_sync():
    """R запускает полный синк; обновления одной вкладки (action_refresh/r) больше нет."""
    data = _dash_data()
    calls = []
    app = JwuDashboard(
        data,
        memory_fn=lambda: data,
        full_sync_fn=lambda: data,
        jira_base="https://jira.test",
    )

    async def run() -> None:
        async with app.run_test():
            app._run_full_sync = lambda: calls.append("full")
            app.action_refresh_all()
            assert calls == ["full"]
            # частичного обновления вкладки больше нет — ни метода, ни биндинга r
            assert not hasattr(app, "action_refresh")
            assert "r" not in {b.key for b in app.BINDINGS}
            assert "R" in {b.key for b in app.BINDINGS}

    asyncio.run(run())


def test_failed_sync_marks_status_and_notifies():
    """Упавший синк: уведомление + метка [неудачно] и след. попытка в строке; успех её снимает."""
    data = _dash_data()
    app = JwuDashboard(
        data, memory_fn=lambda: data, full_sync_fn=lambda: data,
        jira_base="https://jira.test", auto_update=True, slow_interval=600,
    )

    async def run() -> None:
        async with app.run_test():
            notes = []
            app.notify = lambda *a, **k: notes.append((a, k))  # type: ignore[method-assign]
            app.query_one("#tabs", TabbedContent).active = "tab-mine"
            app._after_refresh(None, "Jira недоступна")
            assert app._sync_failed is True
            assert notes and notes[0][1].get("severity") == "error"
            line = app._network_line()
            assert "[неудачно]" in line
            assert "след. попытка через" in line
            # успешный синк снимает метку
            app._after_refresh(data, None)
            assert app._sync_failed is False
            assert "[неудачно]" not in app._network_line()

    asyncio.run(run())


def test_auto_update_starts_timers():
    data = _dash_data()
    app = JwuDashboard(
        data, memory_fn=lambda: data, full_sync_fn=lambda: data,
        jira_base="https://jira.test", auto_update=True, fast_interval=99, slow_interval=999,
    )
    intervals = []
    timers = []
    app.set_interval = lambda *a, **k: intervals.append(a)  # type: ignore[method-assign]
    app.set_timer = lambda *a, **k: timers.append(a)  # type: ignore[method-assign]

    async def run() -> None:
        async with app.run_test():
            # set_interval — статус-тикер (1с) + быстрый рефреш памяти
            assert len(intervals) >= 2
            # set_timer — one-shot слот следующего сетевого синка
            assert any(a[0] == 999 for a in timers)

    asyncio.run(run())


def test_auto_update_off_only_status_ticker():
    data = _dash_data()
    app = JwuDashboard(data, memory_fn=lambda: data, full_sync_fn=lambda: data,
                       jira_base="https://jira.test")  # auto_update=False
    intervals = []
    app.set_interval = lambda *a, **k: intervals.append(a)  # type: ignore[method-assign]

    async def run() -> None:
        async with app.run_test():
            assert len(intervals) == 1  # только статус-тикер, без авто-синков

    asyncio.run(run())


def test_status_shows_syncing_then_countdown():
    from textual.widgets import Static

    data = _dash_data()
    app = JwuDashboard(data, memory_fn=lambda: data, full_sync_fn=lambda: data,
                       jira_base="https://jira.test", auto_update=True,
                       fast_interval=7, slow_interval=600)

    async def run() -> None:
        async with app.run_test():
            # сетевой синк — подменяет только свою (сетевую) строку
            app._begin_sync("network")
            rendered = str(app.query_one("#status", Static).render())
            assert "синхронизация с сетью" in rendered
            assert "🗂  из памяти" in rendered  # строка памяти остаётся
            app._end_sync("network")
            app._update_status()
            rendered = str(app.query_one("#status", Static).render())
            assert "след. через" in rendered
            # синк памяти НЕ показывается — он слишком частый
            assert "обновление из памяти" not in rendered

    asyncio.run(run())


def test_status_shows_user_identity_and_env():
    from textual.widgets import Static

    data = _dash_data()
    data.display_name = "Иван Котков"
    data.email = "alice@example.com"
    app = JwuDashboard(data, jira_base="https://jira.test",
                       env_label="PROJ @ jira.test")

    async def run() -> None:
        async with app.run_test():
            app._update_status()
            txt = str(app.query_one("#status", Static).render())
            assert "Иван Котков" in txt and "alice" in txt
            assert "alice@example.com" in txt
            assert "PROJ @ jira.test" in txt

    asyncio.run(run())


def test_user_identity_survives_memory_refresh():
    """Рефреш из памяти приходит с пустыми user/display_name/email — не затираем."""
    from textual.widgets import Static

    data = _dash_data()
    data.display_name = "Иван Котков"
    data.email = "alice@example.com"
    app = JwuDashboard(data, env_label="PROJ @ jira.test")

    async def run() -> None:
        async with app.run_test():
            blank = DashboardData(last_sync={"mine": None}, mine=list(data.mine))
            app._apply_data(blank)  # снимок из памяти, без сети
            app._update_status()
            txt = str(app.query_one("#status", Static).render())
            assert "Иван Котков" in txt and "alice@example.com" in txt

    asyncio.run(run())


def test_tui_job_close_and_delete():
    from jwu.cli.dashboard import ConfirmScreen
    from jwu.core.models import Job

    data = _dash_data()
    data.jobs = [Job(id=7, task_key="A-1", status="active", title="dev")]
    calls: dict = {}
    app = JwuDashboard(
        data, memory_fn=lambda: data,
        job_delete_fn=lambda i: calls.__setitem__("del", i),
        job_status_fn=lambda i, s: calls.__setitem__("status", (i, s)),
        jira_base="https://jira.test",
    )

    async def run() -> None:
        async with app.run_test() as pilot:
            app.query_one("#tabs", TabbedContent).active = "tab-jobs"
            await pilot.pause()
            app.query_one("#t-jobs", DataTable).focus()
            await pilot.pause()
            app.action_close_job()                       # x — закрыть
            assert calls["status"] == (7, "cancelled")
            app.action_delete_job()                      # d — удалить (с подтверждением)
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await pilot.pause()
            assert calls["del"] == 7

    asyncio.run(run())


def test_check_action_scopes_job_buttons():
    data = _dash_data()
    data.jobs = []
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            assert app.check_action("delete_job", ()) is None      # на «Мои задачи» скрыто
            assert app.check_action("refresh", ()) is True
            app.query_one("#tabs", TabbedContent).active = "tab-jobs"
            await pilot.pause()
            app.query_one("#t-jobs", DataTable).focus()
            await pilot.pause()
            assert app.check_action("delete_job", ()) is True       # на «Работы» доступно
            assert app.check_action("close_job", ()) is True

    asyncio.run(run())


def test_opening_object_clears_its_change_mark():
    from jwu.core.models import Delta

    data = _dash_data()  # mine=[A-1, A-2]
    data.deltas = [
        Delta(key="A-1", kind="new_comment", summary="s"),
        Delta(key="A-2", kind="status_change", summary="s2"),
    ]
    cleared = {}

    def clear(pairs):
        cleared["pairs"] = pairs
        fresh = _dash_data()
        fresh.deltas = [Delta(key="A-2", kind="status_change", summary="s2")]  # A-1 «прочитан»
        return fresh

    app = JwuDashboard(data, clear_changes_fn=clear,
                       jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test():
            assert "A-1" in app._changed_issue_keys  # помечен как обновлённый
            app._ack_object(data.mine[0])            # заходим в A-1 → снять пометку
            assert cleared["pairs"] == [("A-1", "new_comment")]  # очищены только дельты A-1
            assert "A-1" not in app._changed_issue_keys          # пометка пропала
            assert "A-2" in app._changed_issue_keys              # у соседа осталась

    asyncio.run(run())


def test_pressing_enter_clears_change_mark_end_to_end():
    """Интеграция: Enter по обновлённой строке открывает деталь и снимает пометку."""
    from jwu.core.models import Delta

    data = _dash_data()  # активная вкладка — «Мои задачи», курсор на A-1
    data.deltas = [Delta(key="A-1", kind="new_comment", summary="s")]

    def clear(pairs):
        fresh = _dash_data()
        fresh.deltas = []
        return fresh

    app = JwuDashboard(data, clear_changes_fn=clear,
                       pr_detail_fn=_pr_detail_stub, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            assert "A-1" in app._changed_issue_keys
            await pilot.press("enter")   # открыть деталь A-1 (ack происходит до push)
            await pilot.pause()
            assert "A-1" not in app._changed_issue_keys  # пометка снята, без падений

    asyncio.run(run())


def test_status_lives_inside_changes_column():
    from textual.widgets import Static

    data = _dash_data()
    data.deltas = []  # изменений нет
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test():
            # статус-строка теперь ВНУТРИ правой колонки (нижняя секция), а не отдельным баром
            col = app.query_one("#changes-col")
            assert list(col.query("#status"))            # #status вложен в колонку
            # панель без дельт — без кнопок «скрыть»/«очистить», колонка не прячется
            panel = str(app.query_one("#changes", Static).render())
            assert "нет" in panel.lower() and "скрыть" not in panel
            assert not hasattr(app, "action_toggle_changes")

    asyncio.run(run())


def test_splitter_drag_resizes_changes_column():
    from jwu.cli.dashboard import Splitter

    class _Ev:
        def __init__(self, x): self.screen_x = x
        def stop(self): pass

    app = JwuDashboard(_dash_data(), jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test(size=(80, 24)):
            sp = app.query_one(Splitter)
            sp._dragging = True
            sp.on_mouse_move(_Ev(50))     # мышь на колонке 50 → правая колонка = 80-50 = 30
            assert int(app.query_one("#changes-col").styles.width.value) == 30
            sp.on_mouse_move(_Ev(70))     # 80-70=10 < MIN_RIGHT(24) → зажимается до 24
            assert int(app.query_one("#changes-col").styles.width.value) == 24

    asyncio.run(run())


def test_sort_analysis_and_jobs():
    from jwu.core.models import Analysis, Job

    app = JwuDashboard(_dash_data(), jira_base="https://jira.test")

    analyses = [
        Analysis(id=1, created_at="2026-05-20T10:00", title="b"),
        Analysis(id=2, created_at="2026-05-22T10:00", title="a"),
    ]
    app._sort["t-analysis"] = (1, False)  # по «Дата/время», возр.
    assert [a.id for a in app._sorted("t-analysis", "analysis", analyses)] == [1, 2]
    app._sort["t-analysis"] = (1, True)   # убыв. — свежие сверху
    assert [a.id for a in app._sorted("t-analysis", "analysis", analyses)] == [2, 1]

    jobs = [
        Job(id=1, task_key="A-1", updated_at="2026-05-20T10:00", status="active"),
        Job(id=2, task_key="A-2", updated_at="2026-05-22T10:00", status="done"),
    ]
    app._sort["t-jobs"] = (1, True)       # «Обновлено» убыв.
    assert [j.id for j in app._sorted("t-jobs", "job", jobs)] == [2, 1]
    app._sort["t-jobs"] = (2, False)      # по «Статус»
    assert [j.status for j in app._sorted("t-jobs", "job", jobs)] == ["active", "done"]


def test_tui_ack_changes_clears_panel():
    from textual.widgets import Static

    from jwu.core.models import Delta

    data = _dash_data()
    data.deltas = [Delta(key="A-1", kind="new_comment", summary="s")]
    cleared = {}

    def ack():
        cleared["x"] = True
        fresh = _dash_data()
        fresh.deltas = []
        return fresh

    app = JwuDashboard(data, ack_changes_fn=ack,
                       jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test():
            panel = app.query_one("#changes", Static)
            assert "A-1" in str(panel.render())   # активная вкладка (mine) показывает A-1
            app.action_ack_changes()              # ✕ закрыть
            assert cleared["x"]
            assert "нет" in str(panel.render()).lower()

    asyncio.run(run())


def test_tui_smoke_renders_and_quits():
    app = JwuDashboard(_dash_data(), jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            assert app.query_one("#t-mine", DataTable).row_count == 2
            assert app.query_one("#t-prs-review", DataTable).row_count == 1
            # refresh без доступа не роняет приложение
            await pilot.press("R")
            await pilot.press("q")

    asyncio.run(run())


def test_author_color_stable_and_in_palette():
    from jwu.cli.dashboard import _AUTHOR_PALETTE, author_color

    assert author_color("Bob") == author_color("Bob")  # детерминирован
    assert author_color("Bob") in _AUTHOR_PALETTE
    assert author_color("") == "white"


def test_status_priority_colors():
    from jwu.cli.dashboard import priority_color, status_color

    assert status_color("In Progress") == "blue"
    assert status_color("In Review") == "yellow"
    assert status_color("Done") == "green"
    # blocker → красный, critical → розоватый, major/high → жёлтый,
    # minor/low → светло-зелёный, trivial/lowest → серый.
    assert priority_color("Blocker") == "red"
    assert priority_color("Critical") == "light_pink3"
    assert priority_color("Major") == "yellow"
    assert priority_color("High") == "yellow"
    assert priority_color("Medium") == "yellow"
    assert priority_color("Minor") == "bright_green"
    assert priority_color("Low") == "bright_green"
    assert priority_color("Trivial") == "grey50"


def test_group_threads():
    from jwu.cli.dashboard import _group_threads
    from jwu.core.models import PRComment

    cs = [PRComment(id="1", author="A", depth=0),
          PRComment(id="2", author="B", depth=1),
          PRComment(id="3", author="C", depth=0)]
    assert [len(t) for t in _group_threads(cs)] == [2, 1]


def test_general_thread_indents_replies_by_depth():
    from jwu.cli.dashboard import _general_thread
    from jwu.core.models import PRComment

    thread = [
        PRComment(id="1", author="Bob", text="вопрос", depth=0),
        PRComment(id="2", author="Artem", text="ответ", depth=1),
        PRComment(id="3", author="Bob", text="ещё", depth=2),
    ]
    parts = _general_thread(thread)
    # parts идут парами: [автор, текст] на каждый коммент
    author_lines = [parts[i].plain for i in range(0, len(parts), 2)]
    text_lines = [parts[i].plain for i in range(1, len(parts), 2)]
    # верхний уровень — без отступа, ответы — со сдвигом вправо по глубине
    assert not author_lines[0].startswith(" ") and not text_lines[0].startswith(" ")
    assert author_lines[1].startswith("    ") and "╰▶" in author_lines[1] and "│\n" in author_lines[1]
    assert author_lines[2].startswith("        ")  # глубина 2 — сдвиг больше
    assert len(text_lines[2]) - len(text_lines[2].lstrip()) > \
           len(text_lines[1]) - len(text_lines[1].lstrip())  # текст глубже — отступ больше


def test_general_thread_renders_code_block_in_comment():
    from rich.panel import Panel

    from jwu.cli.dashboard import _general_thread
    from jwu.core.models import PRComment

    thread = [PRComment(id="1", author="Bob", text="смотри:\n```py\nf(x)\n```\nвот", depth=0)]
    parts = _general_thread(thread)
    panel = [p for p in parts if isinstance(p, Panel)]
    assert panel and "f(x)" in panel[0].renderable.plain  # код-блок отрисован панелью


def test_inline_thread_panel_inserts_comment_after_anchored_line():
    from jwu.cli.dashboard import _inline_thread_panel
    from jwu.core.models import PRComment

    c = PRComment(id="1", author="Bob", text="вопрос", file="a.py", line=11,
                  context=[" a", "+b"], anchor_idx=1)
    lines = _inline_thread_panel([c]).renderable.plain.splitlines()
    assert lines[0].strip() == "a"
    assert "+b" in lines[1]
    # коммент вставлен ПОСЛЕ прокомментированной строки (+b), а не над блоком
    assert "Bob" in lines[2] and "вопрос" in lines[2]


def test_reviewers_cell_slots_and_sorting():
    from jwu.cli.dashboard import REVIEWER_SLOT_WIDTH, reviewers_cell
    from jwu.core.models import Reviewer

    cell = reviewers_cell([
        # умышленно не по алфавиту — должен отсортироваться
        Reviewer(name="ckotkov", display_name="Сергей Петров"),                  # N (без статуса)
        Reviewer(name="akotkov", display_name="Artjom Kotkov", approved=True),    # A
        Reviewer(name="obogim", display_name="Oleg Bogimirky", status="NEEDS_WORK"),  # NW
    ])
    plain = cell.plain
    # ровно три слота по 25 символов = 75
    assert len(plain) == 3 * REVIEWER_SLOT_WIDTH
    # порядок: Artjom (A) → Oleg (NW) → Сергей (N), отсортированы по имени без регистра
    assert plain[:REVIEWER_SLOT_WIDTH] == "[A] Artjom Kotkov".ljust(REVIEWER_SLOT_WIDTH)
    assert plain[REVIEWER_SLOT_WIDTH:2 * REVIEWER_SLOT_WIDTH] == "[NW] Oleg Bogimirky".ljust(REVIEWER_SLOT_WIDTH)
    assert plain[2 * REVIEWER_SLOT_WIDTH:] == "[N] Сергей Петров".ljust(REVIEWER_SLOT_WIDTH)
    # цвета по статусу прокинуты на весь слот (бейдж + имя)
    spans = {s.start: s.style for s in cell.spans}
    assert spans[0] == "green"
    assert spans[REVIEWER_SLOT_WIDTH] == "yellow"
    assert spans[2 * REVIEWER_SLOT_WIDTH] == "grey50"


def test_reviewers_cell_truncation():
    from jwu.cli.dashboard import REVIEWER_SLOT_WIDTH, reviewers_cell
    from jwu.core.models import Reviewer

    cell = reviewers_cell([
        Reviewer(name="x", display_name="A" * 100, approved=True),
    ])
    assert len(cell.plain) == REVIEWER_SLOT_WIDTH
    assert cell.plain.endswith("...")


def test_reviewers_cell_empty():
    from jwu.cli.dashboard import reviewers_cell

    assert reviewers_cell([]).plain == "—"


def test_tui_clear_section_clears_only_active_tab():
    from textual.widgets import Static

    from jwu.core.models import Delta

    data = _dash_data()  # активная вкладка — «Мои задачи» (A-1, A-2); ещё есть PR #5
    data.deltas = [
        Delta(key="A-1", kind="new_comment", summary="s"),
        Delta(key="P/r#5", kind="new_conflict", summary="t"),
    ]
    cleared = {}

    def clear(pairs):
        cleared["pairs"] = pairs
        fresh = _dash_data()
        fresh.deltas = [Delta(key="P/r#5", kind="new_conflict", summary="t")]
        return fresh

    app = JwuDashboard(data, clear_changes_fn=clear,
                       jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            panel = app.query_one("#changes", Static)
            assert "A-1" in str(panel.render())
            await pilot.press("c")                       # очистить активную секцию
            assert cleared["pairs"] == [("A-1", "new_comment")]  # только дельта вкладки
            assert "нет" in str(panel.render()).lower()

    asyncio.run(run())


def test_changed_rows_marked():
    from jwu.core.models import Delta

    data = _dash_data()
    data.deltas = [Delta(key="A-1", kind="new_comment", summary="s")]
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test():
            assert "A-1" in app._changed_issue_keys

    asyncio.run(run())


def test_parse_pr_url():
    from jwu.cli.dashboard import parse_pr_url

    assert parse_pr_url(
        "https://git.example.com/projects/PROJ/repos/repo/pull-requests/10564"
    ) == ("PROJ", "repo", 10564)
    assert parse_pr_url("https://example.com/x") is None


def _pr_detail_stub(project, repo, pr_id):
    from jwu.core.models import PRComment
    from jwu.core.service import PRDetail

    pr = PR(id=pr_id, project=project, repository=repo, title="t", state="OPEN")
    comment = PRComment(id="1", author="Bob", text="смотри тут", file="a.py", line=10,
                        context=[" ctx", "+added"])
    return PRDetail(pr=pr, comments=[comment], commits=[{"id": "abc", "message": "fix"}])


def test_tui_pr_tab_enter_opens_pr_detail():
    app = JwuDashboard(_dash_data(),
                       pr_detail_fn=_pr_detail_stub, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            app.query_one("#tabs", TabbedContent).active = "tab-prs-review"
            await pilot.pause()
            app.query_one("#t-prs-review", DataTable).focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            from jwu.cli.dashboard import PRDetailScreen
            assert isinstance(app.screen, PRDetailScreen)
            await pilot.press("escape")
            await pilot.press("q")

    asyncio.run(run())


def test_tui_issue_to_pr_navigation_via_p():
    from jwu.core.models import DevPullRequest

    issue = _issue("PROJ-1")
    issue.pull_requests = [DevPullRequest(
        id="#10564", status="OPEN", name="fix",
        url="https://git.example.com/projects/PROJ/repos/repo/pull-requests/10564",
    )]
    data = DashboardData(user="alice", last_sync={"mine": None}, mine=[issue])
    app = JwuDashboard(data,
                       pr_detail_fn=_pr_detail_stub, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            await pilot.press("enter")          # задача
            await pilot.pause()
            await pilot.press("p")              # перейти в её PR
            await pilot.pause()
            from jwu.cli.dashboard import PRDetailScreen
            assert isinstance(app.screen, PRDetailScreen)

    asyncio.run(run())


def test_render_day_context_md():
    from jwu.cli.main import _render_day_context_md
    from jwu.core.models import Delta, Reviewer
    from jwu.core.service import DayContext

    from jwu.core.models import Comment

    issue = _issue("WM-1")
    issue.comments = []
    issue.assignee = "Alice"
    pr = PR(id=7, project="P", repository="r", title="fix", conflicted=True,
            reviewers=[Reviewer(name="rev", status="NEEDS_WORK")], comment_count=2)
    # упоминание, где последний коммент НЕ мой → должно пометиться «ждёт ответа»
    mention = _issue("WM-2")
    mention.assignee = "Боб"
    mention.comments = [Comment(id="1", author="Боб", body="эй [~alice] глянь")]
    ctx = DayContext(
        user="alice", me_display="Alice", synced_at="2026-05-21T10:00",
        deltas=[Delta(key="WM-1", kind="new_comment", summary="s", detail="+1")],
        mine=[issue], prs_mine=[pr], prs_review=[],
        mentions=[(mention, ["эй [~alice] глянь\nвторая строка"])],
        pr_comments={7: []},
    )
    md = _render_day_context_md(ctx)
    assert "## Изменения с прошлого синка (1)" in md
    assert "## Мои задачи (1)" in md
    assert "КОНФЛИКТ" in md and "NEEDS_WORK" in md
    assert "## Упоминания (1)" in md
    # новые обогащения контекста
    assert "состояние: конфликт" in md         # готовность PR (конфликт приоритетнее)
    assert "обновлён:" in md                    # возраст PR
    assert "assignee: Alice" in md              # assignee задачи
    assert "· ждёт ответа" in md                # последний коммент не мой
    # перенос строки в упоминании схлопнут в пробел
    assert "вторая строка" in md and "глянь\nвторая" not in md


def test_tui_analysis_tab_opens_screen():
    from jwu.core.models import Analysis

    data = _dash_data()
    data.analyses = [Analysis(id=1, created_at="2026-05-21T10:00", title="День 1")]
    full = Analysis(id=1, created_at="2026-05-21T10:00", title="День 1", content="# План\n- пункт")
    app = JwuDashboard(data,
                       analysis_get_fn=lambda i: full, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            assert app.query_one("#t-analysis", DataTable).row_count == 1
            app.query_one("#tabs", TabbedContent).active = "tab-analysis"
            await pilot.pause()
            app.query_one("#t-analysis", DataTable).focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            from jwu.cli.dashboard import AnalysisScreen
            assert isinstance(app.screen, AnalysisScreen)

    asyncio.run(run())


def test_issue_detail_two_column_layout():
    from jwu.cli.dashboard import IssueDetailScreen
    from jwu.core.models import DevBranch, DevPullRequest, IssueLink, Job, JobPRLink

    data = _dash_data()
    it = data.mine[0]  # A-1
    it.reporter = "Босс"
    it.links = [IssueLink(key="X-2", type="blocks", status="Open", summary="зависимость")]
    it.pull_requests = [
        DevPullRequest(
            id="#10", status="OPEN", name="fix",
            url="https://git.example.com/projects/PROJ/repos/r/pull-requests/10"),
        DevPullRequest(
            id="#7", status="MERGED", name="old fix",
            url="https://git.example.com/projects/PROJ/repos/r/pull-requests/7"),
    ]
    it.branches = [DevBranch(name="feature/A-1", repository="r")]
    data.jobs = [Job(id=1, task_key="A-1", status="active", title="dev",
                     prs=[JobPRLink(pr_id=10)])]
    app = JwuDashboard(data, pr_detail_fn=lambda *a: None,
                       job_get_fn=lambda i: None, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            await pilot.press("enter")
            await pilot.pause()
            scr = app.screen
            assert isinstance(scr, IssueDetailScreen)
            scr.query_one("#title")
            scr.query_one("#left")
            scr.query_one("#right")
            assert "Босс" in scr._info_markup() and "Назначена" in scr._info_markup()
            assert "X-2" in scr._links_markup()
            prs = scr._prs_markup()
            assert "PR #10" in prs and "open_pr" in prs
            assert "PR #7" in prs and "MERGED" in prs  # смерженные тоже видны
            assert "feature/A-1" in scr._branches_markup()
            assert "#1" in scr._jobs_markup()

    asyncio.run(run())


def test_tui_enter_opens_issue_detail():
    app = JwuDashboard(_dash_data(), jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            await pilot.press("enter")  # на первой строке «Мои задачи»
            await pilot.pause()
            from jwu.cli.dashboard import IssueDetailScreen
            assert isinstance(app.screen, IssueDetailScreen)
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("q")

    asyncio.run(run())


def test_dashboard_jobs_tab_renders():
    from jwu.cli.dashboard import JwuDashboard
    from jwu.core.models import Job
    from jwu.core.service import DashboardData

    data = DashboardData(user="alice", jobs=[Job(id=1, task_key="X-1", title="job1", status="active")])
    app = JwuDashboard(data, job_get_fn=lambda i: Job(id=i, task_key="X-1", title="job1"))

    async def run() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#t-jobs")
            assert table.row_count == 1

    asyncio.run(run())


def test_tui_jobs_tab_enter_opens_job_detail():
    from jwu.cli.dashboard import JobDetailScreen
    from jwu.core.models import Job, JobRecord

    record = JobRecord(id=1, job_id=1, kind="note", text="первая запись", ts="2026-05-21T10:00:00")
    job_full = Job(id=1, task_key="X-1", title="job1", status="active",
                   updated_at="2026-05-21T10:00:00", records=[record])
    data = DashboardData(
        user="alice",
        jobs=[Job(id=1, task_key="X-1", title="job1", status="active",
                  updated_at="2026-05-21T10:00:00", records=[record])],
    )
    app = JwuDashboard(data, job_get_fn=lambda i: job_full)

    async def run() -> None:
        async with app.run_test() as pilot:
            app.query_one("#tabs", TabbedContent).active = "tab-jobs"
            await pilot.pause()
            app.query_one("#t-jobs", DataTable).focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, JobDetailScreen)
            await pilot.press("escape")

    asyncio.run(run())


def test_dashboard_includes_active_jobs(tmp_path):
    store = Store(tmp_path / "state.db")
    j = store.create_job("PROJ-399", "dev-сервер")
    store.create_job("X-1", "done one")
    store.set_job_status(store.create_job("X-2", "paused").id, "paused")
    data = dashboard_from_memory(store)
    active_ids = {job.id for job in data.jobs}
    assert j.id in active_ids
    payload = data.to_json_dict()
    assert "jobs" in payload and any(x["task_key"] == "PROJ-399" for x in payload["jobs"])
    store.close()


def test_issue_detail_live_refresh_applies_fresh_data():
    """Авто-дотягивание открытой задачи перерисовывает секции свежими данными."""
    from textual.widgets import Static

    from jwu.cli.dashboard import IssueDetailScreen
    from jwu.core.models import Comment

    issue = _issue("PROJ-1", status="Open")
    issue.comments = [Comment(id="1", author="Bob", body="старый")]
    data = DashboardData(user="alice", last_sync={"mine": None}, mine=[issue])

    updated = _issue("PROJ-1", status="In Progress")
    updated.comments = [Comment(id="1", author="Bob", body="старый"),
                        Comment(id="2", author="Carol", body="новый коммент")]

    app = JwuDashboard(data, issue_get_fn=lambda key: updated,
                       jira_base="https://jira.test", auto_update=True, detail_interval=60)

    async def run() -> None:
        async with app.run_test() as pilot:
            await pilot.press("enter")  # открыть задачу
            await pilot.pause()
            assert isinstance(app.screen, IssueDetailScreen)
            app.screen._apply_issue(updated)  # имитируем тик авто-рефреша
            await pilot.pause()
            assert app.screen.issue.status == "In Progress"
            assert len(app.screen.issue.comments) == 2
            assert "In Progress" in str(app.screen.query_one("#info", Static).render())
            await pilot.press("escape")
            await pilot.press("q")

    asyncio.run(run())


def test_detail_refresh_interval_only_with_auto_update():
    """refresh_interval уходит в детальный экран только при включённом -a."""
    from jwu.cli.dashboard import IssueDetailScreen

    def _open_issue(auto: bool):
        issue = _issue("PROJ-1")
        data = DashboardData(user="alice", last_sync={"mine": None}, mine=[issue])
        app = JwuDashboard(data, issue_get_fn=lambda k: issue, jira_base="https://jira.test",
                           auto_update=auto, detail_interval=42)
        captured = {}

        async def run() -> None:
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, IssueDetailScreen)
                captured["iv"] = app.screen._refresh_interval
                await pilot.press("escape")
                await pilot.press("q")

        asyncio.run(run())
        return captured["iv"]

    assert _open_issue(auto=True) == 42
    assert _open_issue(auto=False) == 0.0


def test_job_detail_live_refresh_refetches_from_memory():
    """Открытая работа при -a имеет таймер и перечитывает данные из памяти на _reload."""
    from jwu.cli.dashboard import JobDetailScreen
    from jwu.core.models import Job

    job = Job(id=7, task_key="X-1", title="dev", status="active", records=[])
    calls = {"n": 0}

    def get(i):
        calls["n"] += 1
        return job

    data = DashboardData(user="alice", jobs=[job])
    app = JwuDashboard(data, job_get_fn=get, jira_base="https://jira.test",
                       auto_update=True, fast_interval=5)

    async def run() -> None:
        async with app.run_test() as pilot:
            app.query_one("#tabs", TabbedContent).active = "tab-jobs"
            await pilot.pause()
            app.query_one("#t-jobs", DataTable).focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, JobDetailScreen)
            assert app.screen._refresh_interval == 5  # таймер заведётся
            before = calls["n"]
            assert before >= 1  # отрисовка при открытии
            app.screen._reload()  # имитируем тик авто-рефреша
            assert calls["n"] == before + 1  # перечитали из памяти заново
            await pilot.press("escape")
            await pilot.press("q")

    asyncio.run(run())


def test_local_detail_refresh_off_without_auto_update():
    """Без -a у локального детального экрана нет авто-рефреша (interval=0)."""
    from jwu.cli.dashboard import JobDetailScreen
    from jwu.core.models import Job

    job = Job(id=7, task_key="X-1", title="dev", status="active", records=[])
    data = DashboardData(user="alice", jobs=[job])
    app = JwuDashboard(data, job_get_fn=lambda i: job, jira_base="https://jira.test")  # auto off

    async def run() -> None:
        async with app.run_test() as pilot:
            app.query_one("#tabs", TabbedContent).active = "tab-jobs"
            await pilot.pause()
            app.query_one("#t-jobs", DataTable).focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, JobDetailScreen)
            assert app.screen._refresh_interval == 0.0
            await pilot.press("escape")
            await pilot.press("q")

    asyncio.run(run())


# --- поиск по задаче ------------------------------------------------------- #


def test_normalize_issue_key():
    """Ключ обрезается по краям и приводится к верхнему регистру."""
    from jwu.cli.dashboard import normalize_issue_key

    assert normalize_issue_key("  wmdjangochat-25  ") == "WMDJANGOCHAT-25"
    assert normalize_issue_key("PROJ-1") == "PROJ-1"
    assert normalize_issue_key("") == ""
    assert normalize_issue_key("   ") == ""


def test_linked_issue_click_opens_card_and_back():
    """Клик по связанной задаче (секция «Связи») открывает её карточку, Esc — назад."""
    from jwu.cli.dashboard import IssueDetailScreen
    from jwu.core.models import Issue, IssueLink

    main = Issue(key="A-1", summary="main")
    main.links = [IssueLink(type="blocks", direction="outward", key="B-2",
                            summary="linked", status="Open")]
    linked = Issue(key="B-2", summary="linked full")

    data = DashboardData(user="alice", last_sync={"mine": None}, mine=[main])
    app = JwuDashboard(data, issue_get_fn=lambda k: {"A-1": main, "B-2": linked}[k],
                       jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            await pilot.press("enter")  # открыть A-1
            await app.workers.wait_for_complete()
            await pilot.pause()
            await app.screen.run_action("app.open_issue('B-2')")  # клик по связи
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, IssueDetailScreen)
            assert app.screen.issue.key == "B-2"
            await pilot.press("escape")  # назад к A-1
            await pilot.pause()
            assert app.screen.issue.key == "A-1"
            await pilot.press("escape")
            await pilot.press("q")

    asyncio.run(run())


def test_search_opens_issue_detail_for_normalized_key():
    """Enter в поле поиска тянет задачу по нормализованному ключу и открывает карточку."""
    from textual.widgets import Input

    from jwu.cli.dashboard import IssueDetailScreen

    calls = []

    def get(key):
        calls.append(key)
        return _issue(key)

    data = DashboardData(user="alice", last_sync={"mine": None}, mine=[_issue("A-1")])
    app = JwuDashboard(data, issue_get_fn=get, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            inp = app.query_one("#search", Input)
            inp.focus()
            await pilot.pause()
            inp.value = "  wmdjangochat-25 "
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert calls == ["WMDJANGOCHAT-25"]
            assert isinstance(app.screen, IssueDetailScreen)
            assert app.screen.issue.key == "WMDJANGOCHAT-25"
            assert inp.value == ""  # поле очищено после сабмита
            await pilot.press("escape")
            await pilot.press("q")

    asyncio.run(run())


def test_search_opens_card_immediately_then_loads():
    """Enter сразу открывает карточку (loading), данные подтягиваются уже на экране."""
    import threading

    from textual.widgets import Input

    from jwu.cli.dashboard import IssueDetailScreen

    gate = threading.Event()

    def get(key):
        gate.wait(2)  # держим воркер, пока тест проверяет состояние загрузки
        return _issue(key)

    data = DashboardData(user="alice", last_sync={"mine": None}, mine=[_issue("A-1")])
    app = JwuDashboard(data, issue_get_fn=get, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            inp = app.query_one("#search", Input)
            inp.focus()
            await pilot.pause()
            inp.value = "B-2"
            await pilot.press("enter")
            await pilot.pause()
            # карточка уже на экране и грузится — ДО ответа сети
            assert isinstance(app.screen, IssueDetailScreen)
            assert app.screen.issue.key == "B-2"
            assert app.screen._loading is True
            gate.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen._loading is False
            assert app.screen.issue.summary == "summary B-2"  # данные наполнились
            await pilot.press("escape")
            await pilot.press("q")

    asyncio.run(run())


def test_search_empty_input_does_not_fetch():
    """Пустой/пробельный ввод не дёргает issue_get_fn."""
    from textual.widgets import Input

    calls = []

    def get(key):
        calls.append(key)
        return _issue(key)

    data = DashboardData(user="alice", last_sync={"mine": None}, mine=[_issue("A-1")])
    app = JwuDashboard(data, issue_get_fn=get, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            inp = app.query_one("#search", Input)
            inp.focus()
            await pilot.pause()
            inp.value = "   "
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert calls == []
            await pilot.press("q")

    asyncio.run(run())


def test_search_missing_issue_notifies_and_survives():
    """Исключение из issue_get_fn (нет задачи/доступа) не открывает карточку и не роняет app."""
    from textual.widgets import Input

    from jwu.cli.dashboard import IssueDetailScreen

    def get(key):
        raise RuntimeError("404")

    data = DashboardData(user="alice", last_sync={"mine": None}, mine=[_issue("A-1")])
    app = JwuDashboard(data, issue_get_fn=get, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            inp = app.query_one("#search", Input)
            inp.focus()
            await pilot.pause()
            inp.value = "NOPE-1"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not isinstance(app.screen, IssueDetailScreen)
            await pilot.press("q")

    asyncio.run(run())


def test_hjkl_moves_table_cursor():
    """hjkl на таблице двигают курсор; в поле поиска h/j/k/l — обычный ввод."""
    from textual.widgets import Input

    data = _dash_data()  # mine=[A-1, A-2]
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            table = app.query_one("#t-mine", DataTable)
            assert table.cursor_row == 0
            await pilot.press("j")
            assert table.cursor_row == 1
            await pilot.press("k")
            assert table.cursor_row == 0

            inp = app.query_one("#search", Input)
            inp.focus()
            await pilot.pause()
            await pilot.press("j")
            assert inp.value == "j"
            assert table.cursor_row == 0
            await pilot.press("q")

    asyncio.run(run())


def test_bracket_keys_switch_tabs():
    """[ и ] переключают вкладки по кругу."""
    from jwu.cli.dashboard import _TAB_ORDER

    data = _dash_data()
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            tabs = app.query_one("#tabs", TabbedContent)
            assert tabs.active == "tab-mine"
            await pilot.press("]")
            assert tabs.active == "tab-mentions"
            await pilot.press("[")
            assert tabs.active == "tab-mine"
            for _ in range(len(_TAB_ORDER)):
                await pilot.press("]")
            assert tabs.active == "tab-mine"
            await pilot.press("q")

    asyncio.run(run())


def test_copy_items_for_issue():
    from jwu.core.models import Comment

    issue = _issue("ABC-1")
    issue.summary = "Fix bug"
    issue.comments = [Comment(id="1", author="Bob", body="эй [~alice] глянь сюда")]
    items = copy_items_for_issue(issue, "https://jira.test", user="alice")
    by_key = {i.hotkey: i for i in items}
    assert by_key["i"].value == "ABC-1"
    assert by_key["u"].value == "https://jira.test/browse/ABC-1"
    assert by_key["m"].value == "[ABC-1](https://jira.test/browse/ABC-1)"
    assert by_key["w"].value == "[ABC-1|https://jira.test/browse/ABC-1]"
    assert by_key["t"].value == "Fix bug"
    assert by_key["s"].value == "ABC-1: Fix bug"
    assert by_key["e"].value == "эй [~alice] глянь сюда"
    assert "e" not in {i.hotkey for i in copy_items_for_issue(issue, "https://jira.test")}


def test_copy_items_for_job_and_pr():
    job = Job(id=7, task_key="X-1", title="dev work")
    jitems = {i.hotkey: i for i in copy_items_for_job(job, "https://jira.test")}
    assert jitems["i"].value == "X-1"
    assert jitems["n"].value == "7"
    assert jitems["t"].value == "dev work"

    pr = PR(
        id=5, project="P", repository="r", title="pr title",
        url="https://bb/pr/5", source_branch="feat", target_branch="main",
        latest_commit="abc123def",
    )
    pitems = {i.hotkey: i for i in copy_items_for_pr(pr)}
    assert pitems["p"].value == "5"
    assert pitems["r"].value == "P/r"
    assert pitems["m"].value == "[P/r#5](https://bb/pr/5)"
    assert pitems["w"].value == "[P/r#5|https://bb/pr/5]"
    assert pitems["b"].value == "feat → main"
    assert pitems["f"].value == "feat"
    assert pitems["c"].value == "abc123def"
    assert "w" in {i.hotkey for i in copy_items_for_job(job, "https://jira.test")}


def test_y_copies_issue_key_from_list(monkeypatch):
    copied: list[str] = []
    monkeypatch.setattr("jwu.cli.copy_modal.copy_to_clipboard", copied.append)

    data = _dash_data()
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            notes = []
            app.notify = lambda *a, **k: notes.append((a, k))  # type: ignore[method-assign]
            await pilot.press("y")
            assert copied == ["A-1"]
            assert notes and notes[0][0][0] == "Скопировано: A-1"
            await pilot.press("q")

    asyncio.run(run())


def test_y_ignored_on_pr_tab(monkeypatch):
    copied: list[str] = []
    monkeypatch.setattr("jwu.cli.copy_modal.copy_to_clipboard", copied.append)

    data = _dash_data()
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            for _ in range(3):  # mine → mentions → prs-mine → prs-review
                await pilot.press("]")
            assert app.query_one("#tabs", TabbedContent).active == "tab-prs-review"
            await pilot.press("y")
            assert copied == []
            await pilot.press("q")

    asyncio.run(run())


def test_Y_copies_issue_key_from_list_via_modal(monkeypatch):
    copied: list[str] = []
    monkeypatch.setattr("jwu.cli.copy_modal.copy_to_clipboard", copied.append)

    data = _dash_data()
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            notes = []
            app.notify = lambda *a, **k: notes.append((a, k))  # type: ignore[method-assign]
            await pilot.press("Y")
            await pilot.pause()
            assert isinstance(app.screen, CopyModalScreen)
            await pilot.press("i")
            assert copied == ["A-1"]
            assert notes and notes[0][0][0] == "Скопировано: ключ Jira"
            await pilot.press("q")

    asyncio.run(run())


def test_Y_copies_issue_key_from_detail(monkeypatch):
    copied: list[str] = []
    monkeypatch.setattr("jwu.cli.copy_modal.copy_to_clipboard", copied.append)

    app = JwuDashboard(_dash_data(), jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("Y")
            await pilot.pause()
            await pilot.press("i")
            assert copied == ["A-1"]
            await pilot.press("escape")

    asyncio.run(run())


def test_Y_copy_modal_on_pr_tab(monkeypatch):
    copied: list[str] = []
    monkeypatch.setattr("jwu.cli.copy_modal.copy_to_clipboard", copied.append)

    data = _dash_data()
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            for _ in range(3):  # mine → mentions → prs-mine → prs-review
                await pilot.press("]")
            assert app.query_one("#tabs", TabbedContent).active == "tab-prs-review"
            await pilot.press("Y")
            await pilot.pause()
            assert isinstance(app.screen, CopyModalScreen)
            await pilot.press("p")
            assert copied == ["5"]
            await pilot.press("q")

    asyncio.run(run())


def test_copy_modal_quit_with_q(monkeypatch):
    copied: list[str] = []
    monkeypatch.setattr("jwu.cli.copy_modal.copy_to_clipboard", copied.append)

    app = JwuDashboard(_dash_data(), jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            await pilot.press("Y")
            await pilot.pause()
            assert isinstance(app.screen, CopyModalScreen)
            await pilot.press("q")
            await pilot.pause()
            assert app.is_running is False

    asyncio.run(run())


def test_copy_modal_navigation_and_enter(monkeypatch):
    copied: list[str] = []
    monkeypatch.setattr("jwu.cli.copy_modal.copy_to_clipboard", copied.append)

    app = JwuDashboard(_dash_data(), jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            await pilot.press("Y")
            await pilot.pause()
            await pilot.press("j")
            await pilot.press("enter")
            assert copied == ["https://jira.test/browse/A-1"]
            await pilot.press("q")

    asyncio.run(run())
