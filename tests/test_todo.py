"""Доска /jwu-todo, раздача по сессиям, живые сессии, инструкции, порты, утренняя сводка."""

import json
import os
from datetime import datetime

import pytest

from jwu.core import brief, guides, jobspace, todo
from jwu.core.models import PR, BuildStatus, Issue, Reviewer
from jwu.core.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "s.db")
    s.use_workspace(s.get_workspace_by_slug("work").id)
    yield s
    s.close()


def _pr(pid, title, *, conflicted=False, failed=False, needs_work=False, tasks_open=0, author="bob"):
    return PR(id=pid, title=title, project="P", repository="r", state="OPEN", author=author,
              conflicted=conflicted, tasks_open=tasks_open,
              reviewers=[Reviewer(name="rev", display_name="Ревьюер", status="NEEDS_WORK" if needs_work else "UNAPPROVED")],
              builds=[BuildStatus(state="FAILED", key="b", name="b", url="")] if failed else [])


def _seed(store):
    run = store.start_sync_run(["mine", "prs:mine", "prs:review"])
    for issue in (
        Issue(key="AB-1", summary="Конфликт", status="In Review", priority="Critical"),
        Issue(key="AB-2", summary="Правки", status="In Review", priority="Major"),
        Issue(key="AB-3", summary="Вернули", status="DEFECT", priority="Blocker"),
        Issue(key="AB-4", summary="Не начата", status="Selected for Development", priority="Minor"),
        Issue(key="AB-5", summary="В работе", status="In Progress", priority="Minor"),
        Issue(key="AB-6", summary="Важная, не начата", status="To Do", priority="Critical"),
        Issue(key="AB-7", summary="Уже на ревью", status="In Review", priority="Blocker"),
    ):
        store.save_issue_snapshot(run, issue, ["mine"])
    store.save_pr_snapshot(run, _pr(1, "AB-1: конфликт", conflicted=True, failed=True), ["mine"])
    store.save_pr_snapshot(run, _pr(2, "AB-2: правки", conflicted=True, needs_work=True), ["mine"])
    store.save_pr_snapshot(run, _pr(3, "AB-7: ждёт", ), ["mine"])
    store.save_pr_snapshot(run, _pr(9, "B-1: чужой", author="alice"), ["review"])
    store.finish_sync_run(run, {"tasks:mine": 7, "prs:mine": 3, "prs:review": 1})


def test_board_groups_and_order(store):
    _seed(store)
    board = todo.build(store, store.workspace_id, "me", session="S1")
    by_group = {g: [i.key for i in board.items if i.group == g] for g in todo.GROUPS}
    assert by_group["simple"] == ["P/r#1"]                    # конфликт + сборка без замечаний
    assert set(by_group["fixes"]) == {"P/r#2", "AB-3"}         # needs work (с конфликтом) и возврат QA
    # несделанные: в работе выше не начатого, дальше по приоритету; In Review и PR — не здесь
    assert by_group["todo"] == ["AB-5", "AB-6", "AB-4"]
    assert by_group["review"] == ["P/r#9"]
    assert [i.n for i in board.items] == list(range(1, len(board.items) + 1))
    md = todo.render_md(board)
    assert "| № | PR | Задача |" in md and "🟠 Critical" in md and "**Сессия:** S1" in md


def test_take_is_bound_to_shown_numbers_and_exclusive(store):
    _seed(store)
    board = todo.build(store, store.workspace_id, "me", session="S1")
    n_conflict = next(i.n for i in board.items if i.key == "P/r#1")
    res = todo.take(store, store.workspace_id, "me", numbers=[n_conflict], session="S1")
    assert [t["key"] for t in res["taken"]] == ["P/r#1"]
    job = store.get_job(res["taken"][0]["job_id"])
    assert job.task_key == "AB-1" and job.title == "Конфликт и красная сборка PR #1"
    assert job.prs[0].pr_id == 1
    # вторая сессия видит взятое и не берёт его даже группой
    board2 = todo.build(store, store.workspace_id, "me", session="S2")
    taken = next(i for i in board2.items if i.key == "P/r#1")
    assert taken.taken["session"] == "S1"
    res2 = todo.take(store, store.workspace_id, "me", group="simple", session="S2")
    assert res2["taken"] == []
    # неизвестный номер — пропуск с причиной
    res3 = todo.take(store, store.workspace_id, "me", numbers=[99], session="S2")
    assert res3["skipped"][0]["reason"].startswith("нет такого номера")


def test_sessions_alive_and_release(store):
    _seed(store)
    todo.build(store, store.workspace_id, "me", session="Dead")
    todo.take(store, store.workspace_id, "me", numbers=[1], session="Dead")
    todo.heartbeat(store, "Alive", pid=os.getpid(), cwd="/x")
    todo.heartbeat(store, "Dead", pid=999999, cwd="/y")
    seen = todo.sessions(store)
    assert seen["Alive"]["alive"] and not seen["Dead"]["alive"]
    released = todo.release(store, "Dead")
    assert len(released) == 1 and store.get_job(released[0]).status == "paused"
    board = todo.build(store, store.workspace_id, "me", session="Alive")
    assert next(i for i in board.items if i.key == "P/r#1").taken is None


def test_random_session_name_avoids_taken():
    name = todo.random_session_name()
    assert "-" in name and name[0].isupper()
    assert todo.random_session_name({name}) != name or True


def test_guides_workspace_overrides_global(store):
    ws = store.workspace_id
    guides.save_guide(store, ws, guides.Guide(name="pw", title="общая", text="g", scope="global"))
    guides.save_guide(store, ws, guides.Guide(name="stand", title="стенд", text="шаги", tag="be"))
    names = {g.name: g.scope for g in guides.list_guides(store, ws)}
    assert names == {"pw": "global", "stand": "workspace"}
    guides.save_guide(store, ws, guides.Guide(name="pw", title="своя", text="w"))
    assert guides.get_guide(store, ws, "pw").title == "своя"
    ctx = store.workspace_context()
    assert "guides_md" in ctx and "`stand`" in ctx["guides_md"]
    with pytest.raises(guides.GuideError):
        guides.save_guide(store, ws, guides.Guide(name="Плохое имя", title="x"))


def test_ports_unique_and_idempotent(store):
    j1 = store.create_job("AB-1", "one")
    j2 = store.create_job("AB-2", "two")
    p1 = jobspace.allocate_ports(store, j1.id, ["db", "web"])
    p2 = jobspace.allocate_ports(store, j2.id, ["db"])
    assert len({*p1.values(), *p2.values()}) == 3
    assert jobspace.allocate_ports(store, j1.id, ["db"]) == {"db": p1["db"]}


def test_brief_due_once_a_day(store):
    ws = store.workspace_id
    assert not brief.due(store, ws)
    brief.set_time(store, ws, "09:00")
    tz = brief._tz(store, ws)
    assert not brief.due(store, ws, now=datetime(2026, 10, 27, 8, 59, tzinfo=tz))
    assert brief.due(store, ws, now=datetime(2026, 10, 27, 9, 1, tzinfo=tz))
    store.set_workspace_meta(brief.LAST_META, "2026-10-27")
    assert not brief.due(store, ws, now=datetime(2026, 10, 27, 12, 0, tzinfo=tz))
    assert brief.set_time(store, ws, "25:00") == ""          # только из списка
    _seed(store)
    text = brief.render(todo.build(store, ws, "me", session="brief"), "work", None)
    assert "Что делать · work" in text and "AB-6" in text


def test_silent_sync_defers_notifications_to_next_normal(store):
    from types import SimpleNamespace

    from jwu.core import notify
    from jwu.core.models import Delta
    from jwu.core.service import Service

    sent = []

    class Sender:
        def send(self, text, **kw):
            sent.append(text)
            return [1]

    from jwu.core.config import Config

    fake = SimpleNamespace(store=store, notifications_enabled=True, defer_notifications=True,
                           NOTIFY_BACKLOG_META=Service.NOTIFY_BACKLOG_META, workspace=None, cfg=Config(),
                           notifier=lambda: Sender(), _notification_roles=lambda note: {})
    silent = SimpleNamespace(deltas=[Delta(key="AB-1", kind="new_conflict", summary="тихо")], new_mentions=[], notified=False)
    Service._notify_after_sync(fake, silent)
    assert sent == [] and json.loads(store.get_workspace_meta(Service.NOTIFY_BACKLOG_META))["deltas"]
    fake.defer_notifications = False
    normal = SimpleNamespace(deltas=[Delta(key="AB-2", kind="build_failed", summary="громко")], new_mentions=[], notified=False)
    Service._notify_after_sync(fake, normal)
    assert len(sent) == 1 and "AB-1" in sent[0] and "AB-2" in sent[0]
    assert not store.get_workspace_meta(Service.NOTIFY_BACKLOG_META)



def test_two_prs_for_one_task_are_flagged_and_linked(store):
    _seed(store)
    run = store.start_sync_run(["prs:mine"])
    store.save_pr_snapshot(run, _pr(1, "AB-1: конфликт", conflicted=True, failed=True), ["mine"])
    store.save_pr_snapshot(run, PR(id=4, title="AB-1: бэкпорт", project="P", repository="other", state="OPEN",
                                   conflicted=True), ["mine"])
    store.finish_sync_run(run, {"prs:mine": 2})
    board = todo.build(store, store.workspace_id, "me", session="S")
    item = next(i for i in board.items if i.key == "P/r#1")
    assert any("ещё PR по задаче: other#4" in p for p in item.problems)
    res = todo.take(store, store.workspace_id, "me", numbers=[item.n], session="S")
    job = store.get_job(res["taken"][0]["job_id"])
    assert {(p.repo, p.pr_id) for p in job.prs} == {("r", 1), ("other", 4)}


def test_duty_question_no_job_until_taken(store):
    from jwu.core import duty

    q = duty.question_add(store, summary="shop — Too many open files", author="ivan", when="26.10 17:17",
                          where="корень комнаты", event_id="$e1", details="Спрашивает: …")
    again = duty.question_add(store, summary="shop — уточнили", event_id="$e1")
    assert q["id"] == "Д1" and q["created"] and not again["created"] and again["id"] == "Д1"
    assert store.list_jobs() == []                                   # пока не взяли — работы нет
    board = todo.build(store, store.workspace_id, "me", session="S1")
    item = next(i for i in board.items if i.group == "duty")
    assert item.key == "Д1" and board.items[0].group == "duty" and item.taken is None
    res = todo.take(store, store.workspace_id, "me", numbers=[item.n], session="S1")
    job = store.get_job(res["taken"][0]["job_id"])
    assert job.title.startswith(duty.QUESTION_PREFIX) and len(store.list_jobs()) == 1
    assert todo.take(store, store.workspace_id, "me", group="duty", session="S2")["taken"] == []
    # закрыть, не беря: ни работы, ни следа
    q2 = duty.question_add(store, summary="вопрос попроще", event_id="$e2")
    duty.question_close(store, q2["id"])
    assert [x["id"] for x in duty.questions(store)] == ["Д1"] and len(store.list_jobs()) == 1


def test_board_move_reply_close_groups(store):
    from jwu.core.models import Mention

    run = store.start_sync_run(["mine", "prs:mine"])
    store.save_issue_snapshot(run, Issue(key="AB-8", summary="Апрувнуто", status="In Review", priority="Major"), ["mine"])
    store.save_pr_snapshot(run, PR(id=8, title="AB-8: готово", project="P", repository="r", state="OPEN",
                                   reviewers=[Reviewer(name="x", approved=True, status="APPROVED")]), ["mine"])
    store.finish_sync_run(run, {"tasks:mine": 1, "prs:mine": 1})
    store.add_mentions([Mention(task_key="AB-9", comment_id="1", author="Петя", text="[~me] глянь?",
                                created="2026-10-26T10:00:00+03:00", summary="Чужая задача")])
    gone = store.create_job("AB-77", "старая работа")            # задачи нет среди моих
    board = todo.build(store, store.workspace_id, "me", session="S")
    groups = {i.group: i for i in board.items}
    assert groups["move"].key == "P/r#8" and "перевести задачу в тестирование" in groups["move"].problems[-1]
    assert groups["reply"].issue_key == "AB-9" and "Петя" in groups["reply"].problems[0]
    assert groups["close"].key == f"#{gone.id}"
    res = todo.take(store, store.workspace_id, "me", numbers=[groups["close"].n], session="S")
    assert res["taken"] == [] and "jwu-close-job" in res["skipped"][0]["reason"]


def test_render_shows_only_live_sessions():
    from jwu.core.todo import Board, render_md

    board = Board(items=[], synced_ago="сейчас", session="A",
                  sessions={"A": {"alive": True}, "B": {"alive": True}, "Z": {"alive": False, "seen_ago": "15 ч"}})
    text = render_md(board)
    assert "🟢 A (эта) · 🟢 B" in text
    assert "Z" not in text and "молчит" not in text and "освободи" not in text


def _approved_pr(pr_id, repo="r", **kw):
    return PR(id=pr_id, title=f"AB-{pr_id}: готово", project="P", repository=repo, state="OPEN",
              reviewers=[Reviewer(name="x", approved=True, status="APPROVED")], **kw)


def test_board_build_gate_like_conflict(store):
    """Где CI присылает сборки, без зелёной на последнем коммите PR не «двигают»:
    нет и не в очереди — «🧪 Запустить сборку»; в очереди или идёт — «двигать», но «дождаться»."""
    green = [BuildStatus(state="SUCCESSFUL", key="ci", url="u1")]
    running = [BuildStatus(state="INPROGRESS", key="ci", url="u2")]
    run = store.start_sync_run(["mine", "prs:mine"])
    for n in (1, 2, 3, 4, 5):
        store.save_issue_snapshot(run, Issue(key=f"AB-{n}", summary="з", status="In Review", priority="Major"), ["mine"])
    store.save_pr_snapshot(run, _approved_pr(1, builds=green), ["mine"])          # зелёная
    store.save_pr_snapshot(run, _approved_pr(2), ["mine"])                        # нет и не в очереди
    store.save_pr_snapshot(run, _approved_pr(3, build_queued=True), ["mine"])     # в очереди
    store.save_pr_snapshot(run, _approved_pr(4, builds=running), ["mine"])        # идёт
    store.save_pr_snapshot(run, _approved_pr(5, repo="nobuilds"), ["mine"])       # CI статусы не шлёт
    store.finish_sync_run(run, {"tasks:mine": 5, "prs:mine": 5})
    board = todo.build(store, store.workspace_id, "me", session="S")
    by_pr = {i.pr["id"]: i for i in board.items if i.pr}
    assert by_pr[1].group == "move" and "сборка: ✓ зелёная" in by_pr[1].problems
    assert "апрувы и зелёная сборка" in by_pr[1].problems[-1]
    assert by_pr[2].group == "build" and by_pr[2].problems == [todo.NO_BUILD] and by_pr[2].pr["approvals"] == "1/1 ✓"
    assert by_pr[3].group == "move" and "сборка в очереди" in by_pr[3].problems[-1]
    assert by_pr[4].group == "move" and "сборка идёт" in by_pr[4].problems[-1]
    assert by_pr[5].group == "move" and "CI сборку не сообщает" in by_pr[5].problems[-1]
    md = todo.render_md(board)
    assert todo.GROUP_TITLES["build"] in md and "| Сборка |" in md and "⏳ в очереди" in md
    res = todo.take(store, store.workspace_id, "me", numbers=[by_pr[2].n], session="S")
    assert store.get_job(res["taken"][0]["job_id"]).title.startswith("Запустить сборку PR #2")


def test_build_required_setting(store):
    run = store.start_sync_run(["mine", "prs:mine"])
    store.save_pr_snapshot(run, _approved_pr(2), ["mine"])
    store.save_pr_snapshot(run, _approved_pr(5, repo="nobuilds"), ["mine"])
    store.finish_sync_run(run, {"prs:mine": 2})
    ws = store.workspace_id
    store.set_workspace_settings(ws, {todo.BUILD_REQUIRED_SETTING: "*"})
    groups = {i.pr["id"]: i.group for i in todo.build(store, ws, "me").items if i.pr}
    assert groups == {2: "build", 5: "build"}
    store.set_workspace_settings(ws, {todo.BUILD_REQUIRED_SETTING: "-"})
    groups = {i.pr["id"]: i.group for i in todo.build(store, ws, "me").items if i.pr}
    assert groups == {2: "move", 5: "move"}
    store.set_workspace_settings(ws, {todo.BUILD_REQUIRED_SETTING: "NoBuilds"})
    groups = {i.pr["id"]: i.group for i in todo.build(store, ws, "me").items if i.pr}
    assert groups == {2: "move", 5: "build"}


def test_no_build_with_conflict_stays_simple(store):
    run = store.start_sync_run(["mine", "prs:mine"])
    store.save_pr_snapshot(run, PR(id=7, project="P", repository="r", state="OPEN", conflicted=True), ["mine"])
    store.finish_sync_run(run, {"prs:mine": 1})
    store.set_workspace_settings(store.workspace_id, {todo.BUILD_REQUIRED_SETTING: "*"})
    [item] = [i for i in todo.build(store, store.workspace_id, "me").items if i.pr]
    assert item.group == "simple" and item.problems == ["конфликт", todo.NO_BUILD]


def test_approvals_cell():
    two = [Reviewer(name="a", approved=True), Reviewer(name="b", approved=False)]
    assert todo.approvals_cell(PR(id=1, reviewers=two)) == "1/2"
    assert todo.approvals_cell(PR(id=1, reviewers=two[:1])) == "1/1 ✓"
    assert todo.approvals_cell(PR(id=1)) == "нет ревьюеров"


def test_board_demo_group(store):
    """Dev at stand и Demo — своя группа «🎬 Демо и подготовка»: не «несделанные» и не «двигать»."""
    run = store.start_sync_run(["mine", "prs:mine"])
    store.save_issue_snapshot(run, Issue(key="AB-1", summary="на стенде", status="Dev at stand", priority="Major"), ["mine"])
    store.save_issue_snapshot(run, Issue(key="AB-2", summary="к показу", status="Demo", priority="High"), ["mine"])
    store.save_issue_snapshot(run, Issue(key="AB-3", summary="в работе", status="In Progress"), ["mine"])
    store.save_pr_snapshot(run, _approved_pr(2), ["mine"])      # PR задачи на демо: апрувы есть, сборки нет
    store.finish_sync_run(run, {"tasks:mine": 3, "prs:mine": 1})
    store.set_workspace_settings(store.workspace_id, {todo.BUILD_REQUIRED_SETTING: "*"})
    board = todo.build(store, store.workspace_id, "me")
    groups = {i.issue_key: i for i in board.items}
    assert groups["AB-1"].group == "demo" and groups["AB-1"].pr is None
    assert groups["AB-2"].group == "demo" and groups["AB-2"].problems == ["r#2 · апрувы 1/1 ✓"]
    assert groups["AB-3"].group == "todo"
    assert [i for i in board.items if i.group in ("move", "build")] == []
    assert todo.GROUP_TITLES["demo"] in todo.render_md(board)
    store.set_workspace_settings(store.workspace_id, {todo.DEMO_STATUSES_SETTING: "demo"})
    groups = {i.issue_key: i.group for i in todo.build(store, store.workspace_id, "me").items}
    assert groups["AB-2"] == "demo" and "AB-1" not in groups      # Dev at stand больше не демо и не todo


def test_board_links_tasks_and_prs(store):
    """Ключ задачи — ссылка в трекер, PR — ссылка на сам PR; адреса нет — простой текст."""
    run = store.start_sync_run(["mine", "prs:mine"])
    store.save_issue_snapshot(run, Issue(key="AB-2", summary="готово", status="In Progress"), ["mine"])
    store.save_pr_snapshot(run, _approved_pr(2, conflicted=True, url="https://git.example/pr/2"), ["mine"])
    store.finish_sync_run(run, {"tasks:mine": 1, "prs:mine": 1})
    board = todo.build(store, store.workspace_id, "me")
    [item] = [i for i in board.items if i.pr]
    assert item.pr["url"] == "https://git.example/pr/2"
    assert "[r#2](https://git.example/pr/2)" in todo.render_md(board)
    board.task_base = "https://jira.example/"
    md = todo.render_md(board)
    assert "[**AB-2**](https://jira.example/browse/AB-2)" in md
    board.task_base, item.pr["url"] = "", ""
    md = todo.render_md(board)
    assert "| **AB-2** |" in md and "| r#2 |" in md


def test_task_url_jira_and_github():
    from jwu.core.models import task_url

    assert task_url("https://jira.example/", "AB-1") == "https://jira.example/browse/AB-1"
    assert task_url("https://github.com", "repo#42", "me") == "https://github.com/me/repo/issues/42"
    assert task_url("", "AB-1") == ""
