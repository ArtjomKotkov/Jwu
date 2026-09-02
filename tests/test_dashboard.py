import asyncio
import sqlite3

import pytest
from textual.widgets import DataTable

from jwu.cli.copy_modal import (
    CopyModalScreen,
    copy_items_for_issue,
    copy_items_for_job,
    copy_items_for_pr,
)
from jwu.cli.dashboard import JwuDashboard, _fmt_ago
from jwu.cli.workspace_screens import WorkspaceTree
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
    from jwu.core.models import Mention

    run = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run, _issue("M-1"), ["mine"])
    store.save_pr_snapshot(run, PR(id=1, project="P", repository="r"), ["mine"])
    store.save_pr_snapshot(run, PR(id=2, project="P", repository="r"), ["review"])
    # упоминания приходят не из снапшотов, а из своей таблицы
    store.add_mentions([Mention(task_key="X-1", comment_id="9", author="Bob",
                                text="эй [~alice]", created="2026-05-21T10:00")])

    d = dashboard_from_memory(store, user="alice")
    assert d.user == "alice"
    assert [i.key for i in d.mine] == ["M-1"]
    assert [m.task_key for m in d.mentions] == ["X-1"]
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

    # Jira-картинка с пробелами в имени и параметрами размера (как в PROJ-25)
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

    # цвет + жирный (как в PROJ-25), плюс «пустой» {*}{*}
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
            tabs = app._tabs
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
        Delta(key="A-2", kind="status_change", summary="Проблема [acme]", detail="A → B"),
    ]
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test():
            panel = str(app.query_one("#changes", Static).render())  # не должно бросать MarkupError
            assert "A-1" in panel

    asyncio.run(run())


def test_scoped_changes_panel_shows_only_active_section():
    from textual.widgets import Static

    from jwu.core.models import Delta

    data = _dash_data()  # активная вкладка по умолчанию — «Мои задачи» (A-1, A-2)
    # дельта относится к PR на ревью, не к активной вкладке «Мои задачи»
    data.deltas = [Delta(key="P/r#5", kind="new_conflict", summary="t")]
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test():
            panel = str(app.query_one("#changes", Static).render())
            assert "Мои задачи" in panel and "нет" in panel.lower()  # активной нет дельт
            assert app._deltas_by_section["prs_review"][0].key == "P/r#5"

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
    """Упавший синк: уведомление + метка «не удалось» и след. попытка в строке; успех её снимает."""
    data = _dash_data()
    app = JwuDashboard(
        data, memory_fn=lambda: data, full_sync_fn=lambda: data,
        jira_base="https://jira.test", auto_update=True, slow_interval=600,
    )

    async def run() -> None:
        async with app.run_test():
            notes = []
            app.notify = lambda *a, **k: notes.append((a, k))  # type: ignore[method-assign]
            app._tabs.active = "tab-mine"
            app._after_refresh(None, "Jira недоступна")
            assert app._sync_failed is True
            assert app._auto_paused is False  # первая ошибка — ещё ретраим
            assert notes and notes[0][1].get("severity") == "error"
            line = app._network_line()
            assert "не удалось" in line
            assert "след. попытка через" in line
            # успешный синк снимает метку и сбрасывает счётчик
            app._after_refresh(data, None)
            assert app._sync_failed is False
            assert app._fail_count == 0
            assert "не удалось" not in app._network_line()

    asyncio.run(run())


def test_second_failure_pauses_auto_sync():
    """Вторая подряд ошибка паузит авто-синк: планировщик не ставит следующий тик."""
    data = _dash_data()
    app = JwuDashboard(
        data, memory_fn=lambda: data, full_sync_fn=lambda: data,
        jira_base="https://jira.test", auto_update=True, slow_interval=600,
    )

    async def run() -> None:
        async with app.run_test():
            app.notify = lambda *a, **k: None  # type: ignore[method-assign]
            timers: list = []
            app.set_timer = lambda *a, **k: timers.append(a)  # type: ignore[method-assign]
            app._after_refresh(None, "Jira недоступна")   # попытка 1 → ретрай
            assert app._auto_paused is False
            assert timers, "после первой ошибки должен планироваться ретрай"
            timers.clear()
            app._after_refresh(None, "Jira недоступна")   # попытка 2 → пауза
            assert app._auto_paused is True
            assert not timers, "на паузе следующий авто-синк не планируется"
            line = app._network_line()
            assert "авто-синхронизация остановлена" in line
            # ручной R снимает паузу и обнуляет счётчик
            app.action_refresh_all()
            assert app._auto_paused is False
            assert app._fail_count == 0

    asyncio.run(run())


def test_classify_sync_error_auth():
    """Ошибка доступа (401/403 или логин в Jira) → «Не удалось авторизоваться»."""
    from jwu.cli.dashboard import _classify_sync_error
    from jwu.core.jira import JiraError
    from jwu.core.bitbucket import BitbucketError

    assert _classify_sync_error(JiraError("Логин в Jira не удался: 403: ...", 403)) \
        == "Не удалось авторизоваться"
    assert _classify_sync_error(JiraError("Логин в Jira не удался: что-то")) \
        == "Не удалось авторизоваться"
    assert _classify_sync_error(BitbucketError("401: токен невалиден", 401)) \
        == "Не удалось авторизоваться"
    # сетевые/прочие ошибки — как есть
    assert _classify_sync_error(JiraError("Сеть/Jira недоступна: timeout")) \
        == "Сеть/Jira недоступна: timeout"


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
            # статус-тикер + переиндексация папок; сетевых авто-синков нет
            assert len(intervals) == 2

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
            app._tabs.active = "tab-jobs"
            await pilot.pause()
            app.query_one("#t-jobs", DataTable).focus()
            await pilot.pause()
            app.action_close_job()                       # x — закрыть
            assert calls["status"] == (7, "cancelled")
            app.action_finish_job()                      # отказ в диалоге — статус не меняется
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert calls["status"] == (7, "cancelled")
            app.action_finish_job()                      # d — завершить (с подтверждением)
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await pilot.pause()
            assert calls["status"] == (7, "done")
            app.action_delete_job()                      # D — удалить (с подтверждением)
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
            # False (а не None) — только оно реально прячет кнопку и освобождает клавишу
            assert app.check_action("delete_job", ()) is False      # на «Мои задачи» скрыто
            assert app.check_action("finish_job", ()) is False
            assert app.check_action("refresh", ()) is True
            app._tabs.active = "tab-jobs"
            await pilot.pause()
            app.query_one("#t-jobs", DataTable).focus()
            await pilot.pause()
            assert app.check_action("delete_job", ()) is True       # на «Работы» доступно
            assert app.check_action("close_job", ()) is True
            assert app.check_action("finish_job", ()) is True

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


def test_status_lives_outside_the_hidden_panel():
    """Статус — снизу, а не в панели: панель скрыта, и вместе с ней он бы пропал."""
    from textual.widgets import Static

    data = _dash_data()
    data.deltas = []  # изменений нет
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test():
            col = app.query_one("#changes-col")
            assert not list(col.query("#status"))        # не внутри панели
            assert col.display is False                  # панель по умолчанию скрыта
            status = str(app.query_one("#status", Static).render())
            assert "последний синк" in status and "из памяти" in status
            assert "? — клавиши" in status               # легенды на экране больше нет
            panel = str(app.query_one("#changes", Static).render())
            assert "нет" in panel.lower()

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


def test_sort_jobs():
    from jwu.core.models import Job

    app = JwuDashboard(_dash_data(), jira_base="https://jira.test")

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
        Reviewer(name="cuser", display_name="Сергей Петров"),                  # N (без статуса)
        Reviewer(name="auser", display_name="Alice Adams", approved=True),    # A
        Reviewer(name="ouser", display_name="Oscar Brown", status="NEEDS_WORK"),  # NW
    ])
    plain = cell.plain
    # ровно три слота по 25 символов = 75
    assert len(plain) == 3 * REVIEWER_SLOT_WIDTH
    # порядок: Alice (A) → Oscar (NW) → Сергей (N), отсортированы по имени без регистра
    assert plain[:REVIEWER_SLOT_WIDTH] == "[A] Alice Adams".ljust(REVIEWER_SLOT_WIDTH)
    assert plain[REVIEWER_SLOT_WIDTH:2 * REVIEWER_SLOT_WIDTH] == "[NW] Oscar Brown".ljust(REVIEWER_SLOT_WIDTH)
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
            app._tabs.active = "tab-prs-review"
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
    from jwu.core.models import Delta, Mention, Reviewer
    from jwu.core.service import DayContext

    issue = _issue("WM-1")
    issue.comments = []
    issue.assignee = "Alice"
    pr = PR(id=7, project="P", repository="r", title="fix", conflicted=True,
            reviewers=[Reviewer(name="rev", status="NEEDS_WORK")], comment_count=2)
    # упоминание — самостоятельная запись, а не задача
    mention = Mention(id=1, task_key="WM-2", comment_id="1", author="Боб",
                      summary="что-то важное", created="2026-05-21T09:00",
                      text="эй [~alice] глянь\nвторая строка")
    ctx = DayContext(
        user="alice", me_display="Alice", synced_at="2026-05-21T10:00",
        deltas=[Delta(key="WM-1", kind="new_comment", summary="s", detail="+1")],
        mine=[issue], prs_mine=[pr], prs_review=[],
        mentions=[mention],
        pr_comments={7: []},
    )
    md = _render_day_context_md(ctx)
    assert "## Изменения с прошлого синка (1)" in md
    assert "## Мои задачи (1)" in md
    assert "КОНФЛИКТ" in md and "NEEDS_WORK" in md
    assert "## Упоминания (1, новых 1)" in md
    assert "WM-2" in md and "от Боб" in md and "· новое" in md
    # новые обогащения контекста
    assert "состояние: конфликт" in md         # готовность PR (конфликт приоритетнее)
    assert "обновлён:" in md                    # возраст PR
    assert "assignee: Alice" in md              # assignee задачи
    # перенос строки в упоминании схлопнут в пробел
    assert "вторая строка" in md and "глянь\nвторая" not in md


def test_analysis_tab_and_screen_are_gone():
    """Вкладка «Анализ» и её экран убраны из дашборда целиком."""
    import jwu.cli.dashboard as dash

    from jwu.core.service import DashboardData

    assert "tab-analysis" not in dash.TABS
    assert not hasattr(dash, "AnalysisScreen")
    assert not hasattr(DashboardData, "analyses")


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
            assert table.row_count == 2          # работа + заголовок её дня

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
            app._tabs.active = "tab-jobs"
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
            app._tabs.active = "tab-jobs"
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
            app._tabs.active = "tab-jobs"
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

    assert normalize_issue_key("  proj-25  ") == "PROJ-25"
    assert normalize_issue_key("PROJ-1") == "PROJ-1"
    assert normalize_issue_key("") == ""
    assert normalize_issue_key("   ") == ""
    # ключ GitHub регистрозависим (имя репозитория) — его в верхний регистр не поднимаем
    assert normalize_issue_key(" dndeck#42 ") == "dndeck#42"


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
            await pilot.press("/")               # поиск живёт в модалке
            await pilot.pause()
            inp = app.screen.query_one("#prompt-input", Input)
            inp.value = "  proj-25 "
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert calls == ["PROJ-25"]
            assert isinstance(app.screen, IssueDetailScreen)
            assert app.screen.issue.key == "PROJ-25"
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
            await pilot.press("/")               # поиск живёт в модалке
            await pilot.pause()
            inp = app.screen.query_one("#prompt-input", Input)
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
            await pilot.press("/")               # поиск живёт в модалке
            await pilot.pause()
            inp = app.screen.query_one("#prompt-input", Input)
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
            await pilot.press("/")               # поиск живёт в модалке
            await pilot.pause()
            inp = app.screen.query_one("#prompt-input", Input)
            inp.value = "NOPE-1"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not isinstance(app.screen, IssueDetailScreen)
            await pilot.press("q")

    asyncio.run(run())


def test_hjkl_moves_table_cursor():
    """hjkl на таблице двигают курсор; в поле ввода h/j/k/l — обычный текст."""
    from textual.widgets import Input

    data = _dash_data()  # mine=[A-1, A-2]
    app = JwuDashboard(data, issue_get_fn=lambda k: _issue(k),
                       jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            table = app.query_one("#t-mine", DataTable)
            assert table.cursor_row == 0
            await pilot.press("j")
            assert table.cursor_row == 1
            await pilot.press("k")
            assert table.cursor_row == 0

            await pilot.press("/")               # поиск живёт в модалке
            await pilot.pause()
            inp = app.screen.query_one("#prompt-input", Input)
            await pilot.press("j")
            assert inp.value == "j"
            assert table.cursor_row == 0
            await pilot.press("escape")
            await pilot.press("q")

    asyncio.run(run())


def test_bracket_keys_switch_tabs():
    """[ и ] переключают вкладки по кругу — только по видимым."""
    data = _dash_data()
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            tabs = app._tabs
            assert tabs.active == "tab-mine"
            await pilot.press("]")
            assert tabs.active == "tab-mentions"
            await pilot.press("[")
            assert tabs.active == "tab-mine"
            # полный круг по ВИДИМЫМ вкладкам возвращает на исходную
            for _ in range(len(app._visible_tabs())):
                await pilot.press("]")
            assert tabs.active == "tab-mine"
            # «Фичи» при подключённой Jira скрыты (их роль — заменять задачи, а не дублировать)
            assert "tab-features" not in app._visible_tabs()
            assert "tab-workspaces" in app._visible_tabs()
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
            assert app._tabs.active == "tab-prs-review"
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
            assert notes and notes[0][0][0] == "Скопировано: ключ задачи"
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
            assert app._tabs.active == "tab-prs-review"
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


# --------------------------------------------------------------------------- #
# Воркспейсы и локальные фичи в TUI
# --------------------------------------------------------------------------- #


def _home_data(store, ws):
    """Снимок личного воркспейса (без Jira/Bitbucket) прямо из памяти."""
    store.use_workspace(ws.id)
    return dashboard_from_memory(store)


def test_tabs_hidden_for_workspace_without_integrations(tmp_path):
    from jwu.core import workspaces

    store = Store(tmp_path / "state.db")
    ws = workspaces.create(store, "home", name="Личное")
    store.use_workspace(ws.id)
    store.create_feature("Тёмная тема")
    data = _home_data(store, ws)
    store.close()

    app = JwuDashboard(data)

    async def run() -> None:
        async with app.run_test() as pilot:
            visible = app._visible_tabs()
            assert "tab-mine" not in visible and "tab-prs-review" not in visible
            assert "tab-features" in visible and "tab-workspaces" in visible
            assert "tab-structure" in visible
            # стартуем на «Фичах»: задач Jira тут нет
            tabs = app._tabs
            assert tabs.active == "tab-features"
            # [ и ] не заходят на скрытые вкладки
            for _ in range(len(visible) * 2):
                await pilot.press("]")
                assert tabs.active in visible
            await pilot.press("q")

    asyncio.run(run())


def test_features_tab_renders_local_features(tmp_path):
    from jwu.core import workspaces

    store = Store(tmp_path / "state.db")
    ws = workspaces.create(store, "home")
    store.use_workspace(ws.id)
    store.create_feature("Тёмная тема", priority="high")
    data = _home_data(store, ws)
    store.close()

    app = JwuDashboard(data)

    async def run() -> None:
        async with app.run_test() as pilot:
            table = app.query_one("#t-features", DataTable)
            assert table.row_count == 1
            row = table.get_row_at(0)
            assert str(row[0]) == "HOME-1"
            assert "открыта" in str(row[2])
            await pilot.press("q")

    asyncio.run(run())


def test_workspace_tab_shows_paths_and_head(tmp_path):
    from jwu.core import workspaces

    store = Store(tmp_path / "state.db")
    ws = workspaces.create(store, "home", name="Личное")
    folder = tmp_path / "repo"
    folder.mkdir()
    workspaces.add_path(store, ws, folder)
    ws = store.get_workspace(ws.id)
    data = _home_data(store, ws)
    store.close()

    app = JwuDashboard(data)

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-structure"
            await pilot.pause()
            assert app._tabs.active == "tab-structure"
            tree = app.query_one("#t-structure", WorkspaceTree)
            roots = [str(n.label) for n in tree.root.children]
            assert roots == [str(folder.resolve())]
            head = app.query_one("#structure-head").render()
            assert "Личное" in str(head)
            await pilot.press("q")

    asyncio.run(run())


def test_add_path_from_workspace_tab(tmp_path):
    """Клавиша a на вкладке Workspace добавляет папку через переданный callable."""
    from jwu.core import workspaces

    store = Store(tmp_path / "state.db")
    ws = workspaces.create(store, "home")
    data = _home_data(store, ws)
    added = []

    def add_path(workspace_id, path):
        added.append((workspace_id, path))
        return data

    app = JwuDashboard(data, path_add_fn=add_path, cwd=str(tmp_path))

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-structure"
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            await pilot.press("enter")  # поле предзаполнено текущей папкой
            await pilot.pause()
            await pilot.press("q")

    asyncio.run(run())
    store.close()
    assert added == [(ws.id, str(tmp_path))]


def test_workspace_picker_switches_workspace(tmp_path):
    from jwu.core import workspaces

    store = Store(tmp_path / "state.db")
    home = workspaces.create(store, "home", name="Личное")
    work = store.get_workspace_by_slug("work")
    store.use_workspace(work.id)
    work_data = dashboard_from_memory(store)
    home_data = _home_data(store, home)
    store.close()

    switched = []

    def switch(workspace_id):
        switched.append(workspace_id)
        return home_data

    app = JwuDashboard(
        work_data,
        workspaces_fn=lambda: [work, home],
        workspace_switch_fn=switch,
    )

    async def run() -> None:
        async with app.run_test() as pilot:
            await pilot.press("W")
            await pilot.pause()
            # пикер — модальный экран, поэтому ищем в нём, а не в дефолтном
            table = app.screen.query_one("#ws-table", DataTable)
            assert table.row_count == 2
            table.move_cursor(row=1)  # home
            await pilot.press("enter")
            await pilot.pause()
            # после переключения вкладки Jira скрылись — это данные личного воркспейса
            assert "tab-mine" not in app._visible_tabs()
            await pilot.press("q")

    asyncio.run(run())
    assert switched == [home.id]


def test_picker_opens_at_startup_when_workspace_unknown():
    """Без воркспейса дашборд стартует с экрана выбора, а не падает."""
    app = JwuDashboard(
        DashboardData(),
        workspaces_fn=lambda: [],
        workspace_switch_fn=lambda wid: DashboardData(),
        start_with_picker=True,
    )

    async def run() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.query_one("#ws-table") is not None  # пикер поверх дашборда
            await pilot.press("escape")  # в стартовом режиме escape закрывает приложение

    asyncio.run(run())


def test_jobs_tab_shows_feature_anchor(tmp_path):
    from jwu.core import workspaces

    store = Store(tmp_path / "state.db")
    ws = workspaces.create(store, "home")
    store.use_workspace(ws.id)
    feature = store.create_feature("Тёмная тема")
    store.create_job("", "по фиче", feature_id=feature.id)
    store.create_job("", "без якоря")
    data = _home_data(store, ws)
    store.close()

    app = JwuDashboard(data)

    async def run() -> None:
        async with app.run_test() as pilot:
            await pilot.press("]")  # Фичи → Работы
            await pilot.pause()
            assert app._tabs.active == "tab-jobs"
            table = app.query_one("#t-jobs", DataTable)
            anchors = {str(table.get_row_at(i)[3]) for i in range(table.row_count)}
            assert feature.key in anchors
            assert any(a.startswith("#") for a in anchors)  # работа без якоря
            await pilot.press("q")

    asyncio.run(run())


def test_picker_shows_job_counts(tmp_path, monkeypatch):
    """Счётчик работ в пикере: у Workspace должно быть поле для него (pydantic строгий)."""
    from jwu.cli import main as cli
    from jwu.core import workspaces

    db = tmp_path / "state.db"
    store = Store(db)
    home = workspaces.create(store, "home")
    store.use_workspace(home.id)
    store.create_job("", "домашняя")
    store.close()
    monkeypatch.setattr(cli, "_open_store", lambda: Store(db))

    items = cli._tui_workspaces()
    counts = {ws.slug: ws.jobs_count for ws in items}
    assert counts == {"work": 0, "home": 1}

    app = JwuDashboard(DashboardData(), workspaces_fn=cli._tui_workspaces,
                       workspace_switch_fn=lambda wid: DashboardData())

    async def run() -> None:
        async with app.run_test() as pilot:
            await pilot.press("W")
            await pilot.pause()
            table = app.screen.query_one("#ws-table", DataTable)
            assert table.row_count == 2
            await pilot.press("escape")

    asyncio.run(run())


def test_workspace_tab_owns_D_key_and_hides_job_buttons(tmp_path):
    """На вкладке Workspace `D` отвязывает папку, а не удаляет работу (клавиша общая)."""
    from jwu.core import workspaces

    store = Store(tmp_path / "state.db")
    ws = workspaces.create(store, "home")
    folder = tmp_path / "repo"
    folder.mkdir()
    workspaces.add_path(store, ws, folder)
    ws = store.get_workspace(ws.id)
    store.use_workspace(ws.id)
    store.create_job("", "домашняя")
    data = dashboard_from_memory(store)
    store.close()

    removed = []
    app = JwuDashboard(
        data,
        path_remove_fn=lambda wid, path: (removed.append((wid, path)), data)[1],
        job_delete_fn=lambda jid: removed.append(("job", jid)),
    )

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-structure"
            await pilot.pause()
            app.query_one("#t-structure", WorkspaceTree).focus()
            await pilot.pause()
            assert app.check_action("delete_job", ()) is False   # прячется, не «серая»
            assert app.check_action("remove_path", ()) is True
            await pilot.press("D")
            await pilot.pause()
            await pilot.press("enter")  # подтверждение ConfirmScreen
            await pilot.pause()
            await pilot.press("q")

    asyncio.run(run())
    assert removed == [(ws.id, str(folder.resolve()))]


def test_tabs_split_into_global_and_project_rows():
    """Две полосы: наверху выбор контура, внизу — всё его содержимое."""
    from textual.widgets import Tabs

    from jwu.cli.dashboard import _bar_id

    app = JwuDashboard(_dash_data(), jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            deck = app._tabs
            top = deck.query_one("#tabs-global", Tabs)
            bottom = deck.query_one("#tabs-project", Tabs)
            assert [t.id for t in top.query("Tab")] == ["bar-workspaces"]
            # «Структура» и «Правила» открывают нижний ряд: с них начинают в новом контуре
            assert [t.id for t in bottom.query("Tab")][:3] == [
                "bar-structure", "bar-rules", "bar-mine"]
            assert "bar-workspaces" not in [t.id for t in bottom.query("Tab")]
            # стартуем на проектной вкладке → верхняя полоса погашена
            assert deck.active == "tab-mine"
            assert top.has_class("-row-idle") and not bottom.has_class("-row-idle")
            # переход на общую вкладку гасит уже нижнюю полосу
            deck.active = "tab-workspaces"
            await pilot.pause()
            assert bottom.has_class("-row-idle") and not top.has_class("-row-idle")
            assert top.active == _bar_id("tab-workspaces")
            await pilot.press("q")

    asyncio.run(run())


def test_clicking_tab_in_other_row_switches_pane():
    """Клик по вкладке в соседней полосе переключает панель — даже если она там подсвечена.

    Регресс: подсветка в неактивной полосе никуда не девается, поэтому клик по ней не
    меняет `Tabs.active` и штатного события активации не даёт. Панель всё равно должна
    переключиться — иначе вкладка выглядит нажатой, но ничего не происходит.
    """
    app = JwuDashboard(_dash_data(), jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            deck = app._tabs
            assert deck.active == "tab-mine"                  # стартуем в проектной полосе
            assert deck.query_one("#tabs-global").active == "bar-workspaces"
            await pilot.click("#bar-workspaces")              # уже подсвечена в своей полосе
            await pilot.pause()
            assert deck.active == "tab-workspaces"
            assert deck.query_one("#panes").current == "tab-workspaces"
            await pilot.click("#bar-structure")               # назад в проектную полосу
            await pilot.pause()
            assert deck.active == "tab-structure"
            await pilot.click("#bar-rules")                   # соседняя вкладка той же полосы
            await pilot.pause()
            assert deck.active == "tab-rules"
            assert deck.query_one("#panes").current == "tab-rules"
            await pilot.press("q")

    asyncio.run(run())


def test_tab_cycling_walks_both_rows():
    """`[` / `]` листают все видимые вкладки подряд — обе полосы одним кольцом."""
    app = JwuDashboard(_dash_data(), jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            assert app._tabs.active == "tab-mine"
            await pilot.press("[")                    # назад — в общую полосу
            await pilot.pause()
            assert app._tabs.active == "tab-rules"
            await pilot.press("[")
            await pilot.pause()
            assert app._tabs.active == "tab-structure"
            await pilot.press("]")
            await pilot.pause()
            assert app._tabs.active == "tab-rules"
            await pilot.press("q")

    asyncio.run(run())


def test_workspace_and_structure_tabs_are_separate(tmp_path):
    """«Workspace» — управление контурами, «Структура» — папки активного."""
    from jwu.core import workspaces

    store = Store(tmp_path / "state.db")
    ws = workspaces.create(store, "home", name="Личное")
    store.use_workspace(ws.id)
    data = dashboard_from_memory(store)
    store.close()

    app = JwuDashboard(data)

    async def run() -> None:
        async with app.run_test() as pilot:
            label = str(app._tabs.get_tab("tab-workspaces").label)
            # счётчик вкладки = число контуров (work + home), а не папок
            assert label == "Workspace (2)"
            assert str(app._tabs.get_tab("tab-structure").label) == "Структура (0)"
            head = str(app.query_one("#structure-head").render())
            assert "Личное" in head and "работ: 0" in head     # шапка — про состояние
            assert "Папок нет" in head          # пустое состояние объясняет, что делать
            # перечня клавиш в шапках больше нет — он живёт в легенде по «?»
            assert "отвязать" not in head
            ws_head = str(app.query_one("#ws-head").render())
            assert "Воркспейсов: 2" in ws_head
            assert "создать" not in ws_head and "переключиться" not in ws_head
            await pilot.press("q")

    asyncio.run(run())


def test_workspaces_tab_lists_and_switches(tmp_path):
    """Вкладка Workspace — управление: список контуров, enter переключает."""
    from jwu.core import workspaces

    store = Store(tmp_path / "state.db")
    home = workspaces.create(store, "home", name="Личное")
    work = store.get_workspace_by_slug("work")
    store.use_workspace(work.id)
    work_data = dashboard_from_memory(store)
    store.use_workspace(home.id)
    home_data = dashboard_from_memory(store)
    store.close()

    switched = []
    app = JwuDashboard(
        work_data,
        workspace_switch_fn=lambda wid: (switched.append(wid), home_data)[1],
    )

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-workspaces"
            await pilot.pause()
            assert app._tabs.active == "tab-workspaces"
            table = app.query_one("#t-workspaces", DataTable)
            assert table.row_count == 2
            slugs = [str(table.get_row_at(i)[1]) for i in range(table.row_count)]
            assert slugs == ["work", "home"]
            assert str(table.get_row_at(0)[0]).strip() == "→"   # маркер активного
            table.move_cursor(row=1)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("q")

    asyncio.run(run())
    assert switched == [home.id]


def test_workspaces_tab_creates_and_deletes(tmp_path):
    """N создаёт контур, D удаляет (с подтверждением) — прямо со вкладки."""
    from jwu.core import workspaces

    store = Store(tmp_path / "state.db")
    home = workspaces.create(store, "home", name="Личное")
    store.use_workspace(home.id)
    data = dashboard_from_memory(store)
    store.close()

    created, deleted = [], []
    app = JwuDashboard(
        data,
        workspace_create_fn=created.append,
        workspace_delete_fn=lambda wid: (deleted.append(wid), data)[1],
    )

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-workspaces"
            await pilot.pause()
            assert app.check_action("new_workspace", ()) is True
            assert app.check_action("add_path", ()) is False   # это на «Структуре»
            await pilot.press("N")
            await pilot.pause()
            for ch in "pet":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause()

            table = app.query_one("#t-workspaces", DataTable)
            table.move_cursor(row=1)          # home, не единственный
            await pilot.press("D")
            await pilot.pause()
            await pilot.press("enter")        # подтверждение
            await pilot.pause()
            await pilot.press("q")

    asyncio.run(run())
    assert created == ["pet"]
    assert deleted == [home.id]


def test_delete_refuses_when_single_workspace(tmp_path):
    store = Store(tmp_path / "state.db")
    data = dashboard_from_memory(store)   # только «work»
    store.close()

    deleted = []
    app = JwuDashboard(data, workspace_delete_fn=deleted.append)

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-workspaces"
            await pilot.pause()
            await pilot.press("D")
            await pilot.pause()
            await pilot.press("q")

    asyncio.run(run())
    assert deleted == []   # единственный контур не удаляем


def _repo(path, *, branch="main"):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    (path / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    return path


def test_structure_tab_shows_git_marker_after_indexing(tmp_path):
    """Рядом с папкой-репозиторием видно имя/ветку; индексация идёт в фоне."""
    from jwu.core import workspaces

    repo = _repo(tmp_path / "backend", branch="release-10.7")
    store = Store(tmp_path / "state.db")
    ws = workspaces.create(store, "dev", paths=[str(repo)])
    store.use_workspace(ws.id)
    data = dashboard_from_memory(store)
    store.close()

    app = JwuDashboard(data)

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-structure"
            await pilot.pause()
            for _ in range(10):   # ждём фонового индексатора
                await pilot.pause()
                if app._git_index:
                    break
            assert app._git_index[str(repo)][0].label == "backend/release-10.7"
            # маркер рисуется прямо в метке узла дерева
            tree = app.query_one("#t-structure", WorkspaceTree)
            node = tree.root.children[0]
            label = tree.render_label(node, tree.get_component_styles("tree--label").rich_style,
                                      tree.get_component_styles("tree--label").rich_style)
            assert "backend/release-10.7" in str(label)
            await pilot.press("q")

    asyncio.run(run())


def test_tree_expands_with_arrow_keys(tmp_path):
    """Дерево видно сразу, подпапки свёрнуты, → раскрывает, ← сворачивает."""
    from jwu.core import workspaces

    root = tmp_path / "proj"
    (root / "src" / "deep").mkdir(parents=True)
    (root / "README.md").write_text("x")
    store = Store(tmp_path / "state.db")
    ws = workspaces.create(store, "dev", paths=[str(root)])
    store.use_workspace(ws.id)
    data = dashboard_from_memory(store)
    store.close()

    app = JwuDashboard(data)

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-structure"
            await pilot.pause()
            tree = app.query_one("#t-structure", WorkspaceTree)
            tree.focus()
            await pilot.pause()
            node = tree.root.children[0]
            assert not node.is_expanded          # по умолчанию свёрнуто

            await pilot.press("right")           # → раскрывает
            await pilot.pause()
            assert node.is_expanded
            names = [str(c.label) for c in node.children]
            assert "src" in names and "README.md" in names

            await pilot.press("left")            # ← сворачивает
            await pilot.pause()
            assert not node.is_expanded
            await pilot.press("q")

    asyncio.run(run())


def test_missing_folder_yields_empty_tree(tmp_path):
    """Пропавшая папка остаётся в списке, но раскрывать нечего — и это не падение."""
    from jwu.core import workspaces

    store = Store(tmp_path / "state.db")
    ws = workspaces.create(store, "dev")
    store.add_workspace_path(ws.id, str(tmp_path / "gone"))
    store.use_workspace(ws.id)
    data = dashboard_from_memory(store)
    store.close()

    app = JwuDashboard(data)

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-structure"
            await pilot.pause()
            tree = app.query_one("#t-structure", WorkspaceTree)
            tree.focus()
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            assert len(tree.root.children[0].children) == 0
            await pilot.press("q")

    asyncio.run(run())


def test_added_path_appears_without_restart(tmp_path, monkeypatch):
    """Регресс: после ввода пути таблица обновлялась только после перезапуска."""
    from jwu.cli import main as cli
    from jwu.core import workspaces
    import jwu.core.config as cfgmod

    db = tmp_path / "state.db"
    store = Store(db)
    ws = workspaces.create(store, "dev")
    store.use_workspace(ws.id)
    data = dashboard_from_memory(store)
    store.close()
    folder = tmp_path / "repo"
    folder.mkdir()
    monkeypatch.setattr(cfgmod, "db_path", lambda cfg=None: db)
    monkeypatch.setattr(cli, "_open_store", lambda: Store(db))

    app = JwuDashboard(data, path_add_fn=cli._tui_add_path, cwd=str(tmp_path))

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-structure"
            await pilot.pause()
            assert len(app.query_one("#t-structure", WorkspaceTree).root.children) == 0
            await pilot.press("a")
            await pilot.pause()
            for ch in "/repo":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            tree = app.query_one("#t-structure", WorkspaceTree)
            assert [str(n.label) for n in tree.root.children] == [str(folder.resolve())]
            await pilot.press("q")

    asyncio.run(run())


def test_tree_rebuild_keeps_expanded_nodes(tmp_path):
    """Регресс: повторная сборка дерева (добавили папку) роняла дашборд.

    Ошибка вылезала только при НЕПУСТОМ дереве, поэтому раскрываем узел заранее.
    """
    from jwu.core import workspaces
    from jwu.core.models import WorkspacePath

    first = tmp_path / "one"
    (first / "src").mkdir(parents=True)
    second = tmp_path / "two"
    second.mkdir()

    store = Store(tmp_path / "state.db")
    ws = workspaces.create(store, "dev", paths=[str(first)])
    store.use_workspace(ws.id)
    data = dashboard_from_memory(store)
    store.close()

    app = JwuDashboard(data)

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-structure"
            await pilot.pause()
            tree = app.query_one("#t-structure", WorkspaceTree)
            tree.focus()
            await pilot.pause()
            await pilot.press("right")      # раскрыли первую папку
            await pilot.pause()
            assert tree.root.children[0].is_expanded

            # добавилась вторая папка → набор корней изменился → пересборка
            app.data.paths = list(app.data.paths) + [
                WorkspacePath(id=2, workspace_id=ws.id, path=str(second))
            ]
            app._render()
            await pilot.pause()

            roots = {str(n.label): n for n in tree.root.children}
            assert set(roots) == {str(first), str(second)}
            # раскрытое состояние пережило пересборку
            assert roots[str(first)].is_expanded
            await pilot.press("q")

    asyncio.run(run())


def test_workspace_switch_shows_loading_marker(tmp_path):
    """Смена контура не должна выглядеть зависанием: сразу показываем маркер."""
    import threading

    from jwu.core import workspaces
    from textual.widgets import Static

    store = Store(tmp_path / "state.db")
    home = workspaces.create(store, "home", name="Личное")
    store.use_workspace(home.id)
    home_data = dashboard_from_memory(store)
    work = store.get_workspace_by_slug("work")
    store.use_workspace(work.id)
    work_data = dashboard_from_memory(store)
    store.close()

    release = threading.Event()
    seen: list[str] = []

    def slow_switch(workspace_id):
        release.wait(timeout=5)      # имитируем медленное чтение большой базы
        return home_data

    app = JwuDashboard(work_data, workspace_switch_fn=slow_switch)

    async def run() -> None:
        async with app.run_test() as pilot:
            app._switch_workspace(home.id)
            await pilot.pause()
            seen.append(str(app.query_one("#status", Static).render()))
            release.set()
            for _ in range(20):      # ждём завершения фонового чтения
                await pilot.pause()
                if not app._loading_workspace:
                    break
            seen.append(str(app.query_one("#status", Static).render()))
            assert app.data.workspace.slug == "home"
            await pilot.press("q")

    asyncio.run(run())
    assert "переключаю воркспейс" in seen[0]        # пока грузится — явный маркер
    assert "переключаю воркспейс" not in seen[1]    # после загрузки маркер снят


def test_workspace_switch_error_clears_marker(tmp_path):
    """Ошибка чтения не должна оставить дашборд с вечным «загружаю»."""
    store = Store(tmp_path / "state.db")
    data = dashboard_from_memory(store)
    store.close()

    def failing(workspace_id):
        raise RuntimeError("БД недоступна")

    app = JwuDashboard(data, workspace_switch_fn=failing)

    async def run() -> None:
        async with app.run_test() as pilot:
            app._switch_workspace(42)
            for _ in range(20):
                await pilot.pause()
                if not app._loading_workspace:
                    break
            assert app._loading_workspace is False
            await pilot.press("q")

    asyncio.run(run())


def _mention(mid=1, key="X-1", comment_id="9", seen=False, text="эй [~alice] глянь"):
    from jwu.core.models import Mention

    return Mention(id=mid, task_key=key, comment_id=comment_id, author="Боб",
                   text=text, created="2026-05-21T10:00", summary="Заголовок задачи",
                   seen=seen)


def test_mentions_tab_lists_mentions_not_issues():
    """Строка вкладки — само упоминание: когда · задача · автор · текст."""
    data = _dash_data()
    data.mentions = [_mention(), _mention(mid=2, comment_id="10", seen=True,
                                          text="и ещё [~alice] вот")]
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            table = app.query_one("#t-mentions", DataTable)
            assert table.row_count == 2
            row = [str(c) for c in table.get_row_at(0)]
            assert "X-1" in row[1] and row[2] == "Боб" and "глянь" in row[3]
            assert "●" in row[1]                      # непрочитанное помечено
            assert "●" not in str(table.get_row_at(1)[1])
            # бейдж вкладки считает непрочитанные, а не дельты
            assert "●1" in str(app._tabs.get_tab("tab-mentions").label)
            await pilot.press("q")

    asyncio.run(run())


def test_opening_mention_marks_it_read_and_loads_issue():
    """Заход внутрь: упоминание становится прочитанным, карточка задачи тянется из сети."""
    from jwu.cli.dashboard import IssueDetailScreen

    data = _dash_data()
    data.mentions = [_mention()]
    seen: list = []
    loaded: list = []

    def mark(ids):
        seen.append(ids)
        fresh = _dash_data()
        fresh.mentions = [_mention(seen=True)]
        return fresh

    def get_issue(key):
        loaded.append(key)
        return _issue(key)

    app = JwuDashboard(data, mentions_seen_fn=mark, issue_get_fn=get_issue,
                       jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-mentions"
            await pilot.pause()
            app.query_one("#t-mentions", DataTable).focus()
            await pilot.pause()
            assert loaded == []                      # до входа задачу не грузим
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert seen == [[1]]                     # прочитано ровно это упоминание
            assert loaded == ["X-1"]
            assert isinstance(app.screen, IssueDetailScreen)
            assert app.screen.issue.key == "X-1"

    asyncio.run(run())


def test_mentions_panel_lists_unread_and_c_marks_all_read():
    """Панель на вкладке упоминаний показывает непрочитанные; `c` — «прочитано»."""
    from textual.widgets import Static

    data = _dash_data()
    data.mentions = [_mention(), _mention(mid=2, comment_id="10", text="[~alice] второе")]
    calls: list = []

    def mark(ids):
        calls.append(ids)
        fresh = _dash_data()
        fresh.mentions = [_mention(seen=True), _mention(mid=2, comment_id="10", seen=True)]
        return fresh

    app = JwuDashboard(data, mentions_seen_fn=mark, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-mentions"
            await pilot.pause()
            panel = app.query_one("#changes", Static)
            rendered = str(panel.render())
            assert "X-1" in rendered and "глянь" in rendered and "второе" in rendered
            assert "прочитано" in rendered           # у упоминаний своя подпись действия
            app.query_one("#t-mentions", DataTable).focus()
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert calls == [None]                   # None = «все»
            assert "нет" in str(panel.render()).lower()

    asyncio.run(run())


def test_changes_panel_groups_by_task():
    """Панель изменений: одна сущность — один блок «ключ: название» + список того, что в ней."""
    from textual.widgets import Static

    from jwu.core.models import Delta

    data = _dash_data()  # активная вкладка — «Мои задачи» (A-1, A-2)
    data.deltas = [
        Delta(key="A-1", kind="new_comment", summary="Первая задача", detail="+3 комм."),
        Delta(key="A-1", kind="status_change", summary="Первая задача", detail="Open → In Progress"),
        Delta(key="A-2", kind="resolved", summary="Вторая задача", detail="Fixed"),
    ]
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test():
            lines = str(app.query_one("#changes", Static).render()).splitlines()
            body = [ln.strip() for ln in lines if ln.strip()]
            assert body[0].startswith("Изменения · Мои задачи (3)")
            # ключ появляется РОВНО один раз на сущность, изменения — отдельными строками
            assert sum(1 for ln in body if ln.startswith("A-1:")) == 1
            assert "A-1: Первая задача" in body
            assert "💬 +3 комм." in body
            assert "🔁 Open → In Progress" in body
            assert "A-2: Вторая задача" in body
            # голое значение дельты снабжается подписью, иначе строка — ребус
            assert "✅ решена: Fixed" in body

    asyncio.run(run())


def test_changes_panel_caps_groups_and_items():
    """Длинный список не растёт бесконечно: и групп, и строк внутри группы есть потолок."""
    from textual.widgets import Static

    from jwu.core.models import Delta

    data = _dash_data()
    data.mine = [_issue(f"A-{i}") for i in range(1, 13)]
    data.deltas = [
        Delta(key=f"A-{i}", kind="new_comment", summary=f"задача {i}", detail=f"+{i} комм.")
        for i in range(1, 13)
    ] + [
        Delta(key="A-1", kind="new_pr", summary="задача 1", detail=str(n))
        for n in range(10)
    ]
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test():
            body = [ln.strip() for ln in
                    str(app.query_one("#changes", Static).render()).splitlines() if ln.strip()]
            heads = [ln for ln in body if ln.startswith("A-")]
            assert len(heads) == app.MAX_CHANGE_GROUPS
            assert any("…ещё 4 шт." in ln for ln in body)          # 12 задач - 8 показанных
            assert any(ln.startswith("…ещё 6") for ln in body)     # 11 изменений A-1 - 5

    asyncio.run(run())


def _job_at(days_ago, job_id, title="работа"):
    from datetime import datetime, timedelta, timezone

    from jwu.core.models import Job

    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return Job(id=job_id, task_key=f"A-{job_id}", title=title, status="active", updated_at=ts)


def test_jobs_tab_splits_rows_by_day():
    """Между днями — строка-разделитель с датой; сами работы остаются кликабельными."""
    from jwu.core.models import Job

    data = _dash_data()
    data.jobs = [_job_at(0, 3), _job_at(0, 2), _job_at(1, 1)]
    app = JwuDashboard(data, job_get_fn=lambda i: None, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-jobs"
            await pilot.pause()
            table = app.query_one("#t-jobs", DataTable)
            # 3 работы + 2 заголовка дня + пустая строка между днями
            assert table.row_count == 6
            rows = app._rows["t-jobs"]
            assert [r is None for r in rows] == [True, False, False, True, True, False]
            assert [getattr(r, "id", None) for r in rows] == [None, 3, 2, None, None, 1]
            # заголовок дня — в колонке «Обновлено»: сначала словами, потом дата
            head = str(table.get_row_at(0)[1])
            assert head.startswith("─ Сегодня — ") and "." in head
            assert str(table.get_row_at(4)[1]).startswith("─ Вчера — ")
            assert str(table.get_row_at(3)[1]) == ""      # пустая строка отбивает день
            # курсор не застревает на разделителе — проезжает на ближайшую работу
            table.move_cursor(row=0)
            await pilot.pause()
            assert table.cursor_row == 1
            assert isinstance(app._selected_obj(), Job)
            table.move_cursor(row=3)     # пустая строка + заголовок дня, шли вниз
            await pilot.pause()
            assert table.cursor_row == 5
            await pilot.press("q")

    asyncio.run(run())


def test_jobs_day_split_off_when_sorted_by_other_column():
    """Сортировка не по времени → разделителей нет: «дальше другой день» было бы неправдой."""
    data = _dash_data()
    data.jobs = [_job_at(0, 2), _job_at(1, 1)]
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-jobs"
            await pilot.pause()
            # 2 работы + 2 заголовка дня + пустая строка между ними
            assert app.query_one("#t-jobs", DataTable).row_count == 5
            app._sort["t-jobs"] = (5, False)     # по «Title»
            app._render()
            await pilot.pause()
            table = app.query_one("#t-jobs", DataTable)
            assert table.row_count == 2
            assert all(r is not None for r in app._rows["t-jobs"])
            await pilot.press("q")

    asyncio.run(run())


def test_day_divider_reads_word_then_date():
    """Заголовок дня: сначала словами («Сегодня»), потом дата — и то и другое выделено."""
    from jwu.cli.dashboard import _day_color, _day_label

    data = _dash_data()
    data.jobs = [_job_at(0, 1), _job_at(1, 2), _job_at(5, 3)]
    app = JwuDashboard(data, jira_base="https://jira.test")

    assert _day_label(_job_at(0, 1).updated_at).startswith("Сегодня — ")
    assert _day_label(_job_at(1, 2).updated_at).startswith("Вчера — ")
    # свежесть считывается цветом: сегодня зелёным, вчера жёлтым, дальше — ровно
    assert _day_color(_job_at(0, 1).updated_at) == "green"
    assert _day_color(_job_at(1, 2).updated_at) == "yellow"
    assert _day_color(_job_at(5, 3).updated_at) == "cyan"

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-jobs"
            await pilot.pause()
            table = app.query_one("#t-jobs", DataTable)
            head = table.get_row_at(0)[1]
            # слово и дата — жирные и цветные, черта вокруг — приглушённая
            styles = [str(sp.style) for sp in head.spans]
            assert any("bold green" in st for st in styles)
            assert any("bold cyan" in st for st in styles)
            # черта смыкается с соседними колонками (ширина колонки = длине заголовка)
            assert head.cell_len == len(str(table.get_row_at(0)[1]))
            assert str(table.get_row_at(0)[0]).startswith("─")
            await pilot.press("q")

    asyncio.run(run())


def test_jobs_tab_survives_empty_list():
    """Пустой список работ не должен ронять расчёт ширины заголовка дня."""
    data = _dash_data()
    data.jobs = []
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-jobs"
            await pilot.pause()
            assert app.query_one("#t-jobs", DataTable).row_count == 0
            await pilot.press("q")

    asyncio.run(run())


def _screen_text(app) -> str:
    """Что реально нарисовано на экране (у Static с Group `render()` отдаёт обёртку)."""
    return "\n".join(
        "".join(seg.text for seg in strip)
        for strip in app.screen._compositor.render_strips()
    )


def _rule(rid=1, kind="constraint", title="Не пушить в develop", tag="", text=""):
    from jwu.core.models import WorkspaceRule

    return WorkspaceRule(id=rid, kind=kind, title=title, tag=tag, text=text,
                         updated_at="2026-07-30T10:00")


def test_rules_tab_opens_project_row():
    """«Правила» — содержимое контура, поэтому стоят в нижнем ряду, сразу за «Структурой»."""
    from jwu.cli.dashboard import GLOBAL_TABS, PROJECT_TABS

    assert GLOBAL_TABS == ("tab-workspaces",)          # наверху только выбор контура
    assert PROJECT_TABS[:2] == ("tab-structure", "tab-rules")

    data = _dash_data()
    data.rules = [_rule(), _rule(2, "howto", "Как поднять стенд", "legacy-бэкенд", "шаги")]
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-rules"
            await pilot.pause()
            table = app.query_one("#t-rules", DataTable)
            assert table.row_count == 2
            first = [str(c) for c in table.get_row_at(0)]
            assert "ЗАПРЕТ" in first[0] and first[1] == "общее"
            second = [str(c) for c in table.get_row_at(1)]
            assert second[1] == "legacy-бэкенд"
            assert second[2].endswith("…")          # есть подробности — смотри карточку
            assert "Правил: 2" in str(app.query_one("#rules-head").render())
            assert "запретов: 1" in str(app.query_one("#rules-head").render())
            await pilot.press("q")

    asyncio.run(run())


def test_rule_keys_are_scoped_to_its_tab():
    """N/e/D работают на «Правилах» и спрятаны на других вкладках (клавиши общие)."""
    data = _dash_data()
    data.rules = [_rule()]
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            assert app.check_action("new_rule", ()) is False     # «Мои задачи»
            assert app.check_action("delete_rule", ()) is False
            app._tabs.active = "tab-rules"
            await pilot.pause()
            assert app.check_action("new_rule", ()) is True
            assert app.check_action("edit_rule", ()) is True
            assert app.check_action("delete_rule", ()) is True
            assert app.check_action("new_workspace", ()) is False  # чужая вкладка
            await pilot.press("q")

    asyncio.run(run())


def test_rule_detail_shows_full_text():
    """В строке только суть; многострочная инструкция открывается по enter."""
    from jwu.cli.workspace_screens import RuleDetailScreen

    rule = _rule(2, "howto", "Как поднять стенд", "legacy-бэкенд",
                 "1. docker compose up\n2. ./manage.py migrate")
    data = _dash_data()
    data.rules = [rule]
    app = JwuDashboard(data, rule_get_fn=lambda i: rule, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test(size=(120, 30)) as pilot:
            app._tabs.active = "tab-rules"
            await pilot.pause()
            app.query_one("#t-rules", DataTable).focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RuleDetailScreen)
            body = _screen_text(app)
            assert "docker compose up" in body and "./manage.py migrate" in body
            assert "legacy-бэкенд" in body
            await pilot.press("escape")

    asyncio.run(run())


def test_creating_and_deleting_rule_from_tab():
    from jwu.cli.dashboard import ConfirmScreen
    from jwu.cli.workspace_screens import RuleEditScreen

    data = _dash_data()
    data.rules = [_rule()]
    calls: dict = {}
    app = JwuDashboard(
        data,
        memory_fn=lambda: data,
        rule_create_fn=lambda values: calls.__setitem__("created", values),
        rule_delete_fn=lambda rid: calls.__setitem__("deleted", rid),
        tags_fn=lambda: ["фронт", "legacy-бэкенд"],
        jira_base="https://jira.test",
    )

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-rules"
            await pilot.pause()
            app.query_one("#t-rules", DataTable).focus()
            await pilot.pause()

            await pilot.press("N")
            await pilot.pause()
            assert isinstance(app.screen, RuleEditScreen)
            app.screen.query_one("#rule-name").value = "Ревью до коммита"
            app.screen.query_one("#rule-tag").value = "фронт"
            app.screen.query_one("#rule-text").text = "и никак иначе"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert calls["created"] == {"kind": "info", "tag": "фронт",
                                        "title": "Ревью до коммита", "text": "и никак иначе"}

            await pilot.press("D")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await pilot.pause()
            assert calls["deleted"] == 1

    asyncio.run(run())


def test_rule_edit_screen_requires_a_title():
    """Правило без сути — это заметка ни о чём: не сохраняем и говорим почему."""
    from jwu.cli.workspace_screens import RuleEditScreen

    data = _dash_data()
    data.rules = []
    saved: list = []
    app = JwuDashboard(data, memory_fn=lambda: data,
                       rule_create_fn=lambda v: saved.append(v),
                       jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            app._tabs.active = "tab-rules"
            await pilot.pause()
            app.query_one("#t-rules", DataTable).focus()
            await pilot.pause()
            await pilot.press("N")
            await pilot.pause()
            await pilot.press("ctrl+s")          # суть не введена
            await pilot.pause()
            assert isinstance(app.screen, RuleEditScreen)   # модалка не закрылась
            assert saved == []

    asyncio.run(run())


# --- скрываемая панель, модалки поиска и легенды --------------------------- #


def test_changes_panel_hidden_by_default_and_toggled_by_b():
    from jwu.core.models import Delta

    data = _dash_data()
    data.deltas = [Delta(key="A-1", kind="new_comment", summary="s", detail="+1 комм.")]
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            col, splitter = app.query_one("#changes-col"), app.query_one("#splitter")
            assert col.display is False and splitter.display is False
            # даже скрытая панель наполнена — счётчик вкладки про изменения не врёт
            assert "●1" in str(app._tabs.get_tab("tab-mine").label)

            await pilot.press("b")
            await pilot.pause()
            assert col.display is True and splitter.display is True
            await pilot.press("b")
            await pilot.pause()
            assert col.display is False and splitter.display is False
            await pilot.press("q")

    asyncio.run(run())


def test_search_modal_opens_by_slash_and_cancels_cleanly():
    from jwu.cli.workspace_screens import TextPromptScreen

    calls: list = []
    data = DashboardData(user="alice", mine=[_issue("A-1")])
    app = JwuDashboard(data, issue_get_fn=lambda k: calls.append(k) or _issue(k),
                       jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            await pilot.press("/")
            await pilot.pause()
            assert isinstance(app.screen, TextPromptScreen)
            await pilot.press("escape")          # передумал — ничего не грузим
            await pilot.pause()
            assert not isinstance(app.screen, TextPromptScreen)
            assert calls == []
            await pilot.press("q")

    asyncio.run(run())


def test_search_says_so_when_jira_is_off():
    """Воркспейс без Jira: искать нечего — говорим прямо, а не открываем пустую модалку."""
    from jwu.cli.workspace_screens import TextPromptScreen

    data = DashboardData(user="alice", provider="local")
    app = JwuDashboard(data)                     # issue_get_fn не передан

    async def run() -> None:
        async with app.run_test() as pilot:
            notes: list = []
            app.notify = lambda *a, **k: notes.append(a)  # type: ignore[method-assign]
            await pilot.press("/")
            await pilot.pause()
            assert not isinstance(app.screen, TextPromptScreen)
            assert notes and "нет провайдера задач" in notes[0][0]
            await pilot.press("q")

    asyncio.run(run())


def test_legend_splits_page_keys_from_common():
    """Легенда: сверху клавиши текущей вкладки, ниже общие; клавиши — символами."""
    from jwu.cli.workspace_screens import LegendScreen

    data = _dash_data()
    data.jobs = [_job_at(0, 7)]
    app = JwuDashboard(data, job_get_fn=lambda i: None, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            page, common, where = app._legend_sections()
            assert where == "Мои задачи"
            assert ("enter", "Открыть карточку задачи") in page   # основное действие вкладки
            assert ("?", "Клавиши") in common and ("/", "Поиск задачи") in common
            assert not any(k == "question_mark" for k, _ in common)  # не имена, а символы
            # клавиши работ на вкладке задач не предлагаем
            assert not any("Удалить работу" in d for _, d in page + common)

            app._tabs.active = "tab-jobs"
            await pilot.pause()
            page, common, where = app._legend_sections()
            assert where == "Работы"
            assert ("D", "✕ Удалить работу") in page
            assert ("x", "Закрыть работу") in page
            assert ("?", "Клавиши") in common          # общие никуда не делись

            await pilot.press("?")
            await pilot.pause()
            assert isinstance(app.screen, LegendScreen)
            shown = _screen_text(app)
            assert "На этой странице — Работы" in shown and "Общие" in shown
            assert "Удалить работу" in shown
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, LegendScreen)

    asyncio.run(run())


def test_legend_on_detail_screen_shows_its_own_keys_once():
    """В карточке свои клавиши, и перекрытые ими общие не дублируются ниже."""
    app = JwuDashboard(_dash_data(), pr_detail_fn=_pr_detail_stub,
                       jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            await pilot.press("enter")           # карточка задачи
            await pilot.pause()
            page, common, where = app._legend_sections()
            assert where == "карточка задачи"
            assert ("esc / backspace", "← Назад") in page
            assert ("p", "Открыть PR") in page
            # `o` и `y` карточка перекрывает своими — в «Общих» их быть не должно
            page_keys = {k for k, _ in page}
            assert "o" in page_keys and "y" in page_keys
            assert not (page_keys & {k for k, _ in common})
            assert ("?", "Клавиши") in common
            await pilot.press("escape")

    asyncio.run(run())


def test_tab_heads_carry_state_not_key_hints():
    """Шапки вкладок — про состояние; перечень клавиш живёт только в легенде по «?»."""
    data = _dash_data()
    data.rules = [_rule()]
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            for selector in ("#ws-head", "#structure-head", "#rules-head"):
                head = str(app.query_one(selector).render())
                for hint in ("— добавить", "— удалить", "— править", "— переключиться",
                             "— раскрыть", "— отвязать", "— открыть"):
                    assert hint not in head, f"{selector}: {hint}"
            assert "Правил: 1" in str(app.query_one("#rules-head").render())
            await pilot.press("q")

    asyncio.run(run())


def test_empty_rules_tab_explains_itself():
    """Пустая вкладка правил объясняет, зачем она; когда правила есть — не мозолит глаза."""
    data = _dash_data()
    data.rules = []
    app = JwuDashboard(data, jira_base="https://jira.test")

    async def run() -> None:
        async with app.run_test() as pilot:
            head = str(app.query_one("#rules-head").render())
            assert "Правил нет" in head and "агент обязан знать" in head
            app.data.rules = [_rule()]
            app._render()
            await pilot.pause()
            head = str(app.query_one("#rules-head").render())
            assert "агент обязан знать" not in head and "Правил: 1" in head
            await pilot.press("q")

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# GitHub-контур в дашборде
# --------------------------------------------------------------------------- #


def _github_data(store, ws) -> DashboardData:
    store.use_workspace(ws.id)
    data = dashboard_from_memory(store)
    data.workspace = ws
    return data


def test_github_workspace_shows_task_and_pr_tabs_but_not_features(tmp_path):
    """У GitHub-контура задачи и PR приходят от одного провайдера — вкладки те же, что у Jira."""
    from jwu.core import workspaces

    store = Store(tmp_path / "state.db")
    ws = workspaces.create(store, "dndeck", provider="github")
    data = _github_data(store, ws)
    store.close()

    app = JwuDashboard(data)

    async def run() -> None:
        async with app.run_test() as pilot:
            visible = app._visible_tabs()
            assert "tab-mine" in visible and "tab-prs-review" in visible
            assert "tab-features" not in visible   # задачи ведутся в Issues
            await pilot.press("q")

    asyncio.run(run())


def test_footer_shows_provider_host_and_login(tmp_path):
    """Куда и под кем залогинены — видно всегда: контуров несколько, спутать нельзя."""
    from jwu.core import workspaces

    store = Store(tmp_path / "state.db")
    ws = workspaces.create(store, "dndeck", provider="github")
    data = _github_data(store, ws)
    data.user, data.display_name = "akotkov", "Артём"
    data.env_label = "dndeck @ github.com"
    store.close()

    app = JwuDashboard(data)
    block = app._user_block()
    assert "GitHub" in block                      # провайдер
    assert "dndeck @ github.com" in block         # хост и репозиторий
    assert "Артём (akotkov)" in block             # под кем
    assert "dndeck" in block                      # какой контур


def test_local_workspace_footer_has_no_phantom_login(tmp_path):
    from jwu.core import workspaces

    store = Store(tmp_path / "state.db")
    ws = workspaces.create(store, "home")
    data = _github_data(store, ws)
    store.close()

    block = JwuDashboard(data)._user_block()
    assert "👤" not in block and "локальный" in block


def test_switching_workspace_drops_previous_identity(tmp_path):
    """После перехода в другой контур в футере не должен висеть логин из прошлого."""
    from jwu.core import workspaces

    store = Store(tmp_path / "state.db")
    home = workspaces.create(store, "home")
    work = store.get_workspace_by_slug("work")
    store.use_workspace(work.id)
    work_data = dashboard_from_memory(store)
    work_data.user, work_data.display_name = "alice", "Alice"
    home_data = _github_data(store, home)
    store.close()

    app = JwuDashboard(work_data, workspace_switch_fn=lambda wid: home_data)

    async def run() -> None:
        async with app.run_test() as pilot:
            assert "alice" in app._user_block()
            app._workspace_switched(home_data)
            await pilot.pause()
            assert "alice" not in app._user_block()
            await pilot.press("q")

    asyncio.run(run())


def test_pr_task_key_reads_github_issue_number():
    from jwu.cli.dashboard import pr_task_key

    pr = PR(id=7, title="Fix save", source_branch="42-fix-save",
            project="akotkov", repository="dndeck")
    assert pr_task_key(pr, "akotkov") == "dndeck#42"
    # ключ Jira по-прежнему в приоритете — контуры Jira ничего не замечают
    jira_pr = PR(id=7, title="PROJ-9: fix", source_branch="PROJ-9-fix",
                 project="PROJ", repository="repo")
    assert pr_task_key(jira_pr) == "PROJ-9"
    # связи нет — колонка «Задача» останется пустой, а не покажет мусор
    assert pr_task_key(PR(id=7, title="Fix", source_branch="fix",
                          project="akotkov", repository="dndeck"), "akotkov") == ""
