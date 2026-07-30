import pytest

from jwu.core.models import Comment, Delta, DevPullRequest, Issue, PR, Reviewer
from jwu.core.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


def _issue(key="PROJ-1", status="Open", resolution="", comments=(), prs=(), dev_ok=True):
    return Issue(
        key=key,
        summary="S",
        status=status,
        resolution=resolution,
        comments=[Comment(id=str(c)) for c in comments],
        pull_requests=[DevPullRequest(id=str(p)) for p in prs],
        dev_ok=dev_ok,
    )


def test_new_issue_delta_on_first_sight(store):
    run = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run, _issue())
    deltas = store.compute_changes(run)
    assert any(d.kind == "new_issue" for d in deltas)


def test_status_change_and_new_comment_deltas(store):
    run1 = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run1, _issue(status="Open", comments=[1]))
    store.compute_changes(run1)

    run2 = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run2, _issue(status="In Progress", comments=[1, 2]))
    deltas = store.compute_changes(run2)
    kinds = {d.kind for d in deltas}
    assert "status_change" in kinds
    assert "new_comment" in kinds
    assert "new_issue" not in kinds


def test_resolved_and_new_pr_deltas(store):
    run1 = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run1, _issue(resolution="", prs=[]))
    store.compute_changes(run1)

    run2 = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run2, _issue(resolution="Fixed", prs=["#42"]))
    deltas = store.compute_changes(run2)
    kinds = {d.kind for d in deltas}
    assert "resolved" in kinds
    assert "new_pr" in kinds


def test_dev_status_failure_does_not_emit_phantom_new_pr(store):
    """Сбой dev-status (dev_ok=False, pr_ids пусты) не должен порождать new_pr ни на
    сбойном синке, ни на восстановлении: PR уже видели, они не новые."""
    run1 = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run1, _issue(prs=["#42"]))
    store.compute_changes(run1)

    # синк со сбоем dev-status: pr_ids схлопнулись, но снапшот помечен недостоверным
    run2 = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run2, _issue(prs=[], dev_ok=False))
    assert not any(d.kind == "new_pr" for d in store.compute_changes(run2))

    # dev-status восстановился: тот же #42 не должен выглядеть новым
    run3 = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run3, _issue(prs=["#42"]))
    assert not any(d.kind == "new_pr" for d in store.compute_changes(run3))

    # а вот реально новый PR после восстановления — ловим
    run4 = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run4, _issue(prs=["#42", "#99"]))
    new_pr = [d for d in store.compute_changes(run4) if d.kind == "new_pr"]
    assert len(new_pr) == 1 and new_pr[0].detail == "#99"


def test_new_conflict_delta(store):
    pr_ok = PR(id=7, title="t", project="P", repository="r", conflicted=False)
    pr_bad = PR(id=7, title="t", project="P", repository="r", conflicted=True)

    run1 = store.start_sync_run(["review"])
    store.save_pr_snapshot(run1, pr_ok)
    assert not [d for d in store.compute_changes(run1) if d.kind == "new_conflict"]

    run2 = store.start_sync_run(["review"])
    store.save_pr_snapshot(run2, pr_bad)
    deltas = store.compute_changes(run2)
    assert any(d.kind == "new_conflict" for d in deltas)


def test_pr_signature_deltas(store):
    pr1 = PR(id=9, project="P", repository="r", title="t", comment_count=1,
             latest_commit="aaa", reviewers=[Reviewer(name="rev", approved=False)])
    run1 = store.start_sync_run(["prs:review"])
    store.save_pr_snapshot(run1, pr1, ["review"])
    assert store.compute_changes(run1) == []  # первый раз — без шума

    pr2 = PR(id=9, project="P", repository="r", title="t", comment_count=3,
             latest_commit="bbb", reviewers=[Reviewer(name="rev", approved=True)])
    run2 = store.start_sync_run(["prs:review"])
    store.save_pr_snapshot(run2, pr2, ["review"])
    kinds = {d.kind for d in store.compute_changes(run2)}
    assert {"new_pr_comment", "new_pr_commit", "reviewer_approved"} <= kinds


def test_closed_issue_disappears_and_emits_gone(store):
    """Задача, переставшая приходить из Jira (закрыта/сменила статус), уходит из
    списка вкладки и порождает дельту gone — а не висит по устаревшему снапшоту."""
    run1 = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run1, _issue("PROJ-1"), ["mine"])
    store.finish_sync_run(run1, {"tasks:mine": 1})
    store.compute_changes(run1)
    assert [i.key for i in store.latest_issues("mine")] == ["PROJ-1"]

    # следующий синк mine: PROJ-1 больше не пришёл (вкладка надёжно пуста)
    run2 = store.start_sync_run(["mine"])
    store.finish_sync_run(run2, {"tasks:mine": 0})
    deltas = store.compute_changes(run2)
    gone = [d for d in deltas if d.kind == "gone"]
    assert len(gone) == 1
    assert gone[0].key == "PROJ-1" and gone[0].section == "mine"
    assert store.latest_issues("mine") == []  # пропала из списка


def test_merged_pr_disappears_and_emits_pr_gone(store):
    pr = PR(id=5, project="P", repository="r", title="фикс")
    run1 = store.start_sync_run(["prs:mine"])
    store.save_pr_snapshot(run1, pr, ["mine"])
    store.finish_sync_run(run1, {"prs:mine": 1})
    store.compute_changes(run1)
    assert [p.id for p in store.latest_prs("mine")] == [5]

    run2 = store.start_sync_run(["prs:mine"])
    store.finish_sync_run(run2, {"prs:mine": 0})
    deltas = store.compute_changes(run2)
    gone = [d for d in deltas if d.kind == "pr_gone"]
    assert len(gone) == 1
    assert gone[0].key == "P/r#5" and gone[0].section == "prs_mine"
    assert store.latest_prs("mine") == []


def test_fetch_failure_does_not_wipe_tab_or_emit_gone(store):
    """Сбой фетча вкладки (нет ключа в counts) не должен затирать список и не должен
    порождать ложный gone — членство откатывается к прошлому надёжному синку."""
    run1 = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run1, _issue("PROJ-1"), ["mine"])
    store.finish_sync_run(run1, {"tasks:mine": 1})
    store.compute_changes(run1)

    # синк, где фетч mine упал: прогон записан, но counts без tasks:mine и снапшота нет
    run2 = store.start_sync_run(["mine"])
    store.finish_sync_run(run2, {})
    deltas = store.compute_changes(run2)
    assert not any(d.kind == "gone" for d in deltas)
    assert [i.key for i in store.latest_issues("mine")] == ["PROJ-1"]  # не затёрли


def test_gone_detection_ignores_mentions_view(store):
    """Исчезновение считается только по «моим задачам».

    Упоминания больше не задачи в выборке, а отдельные записи (таблица mentions),
    поэтому старый снапшот со вью «mentions» не должен удерживать задачу «живой».
    """
    run1 = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run1, _issue("PROJ-1"), ["mine", "mentions"])
    store.finish_sync_run(run1, {"tasks:mine": 1})
    store.compute_changes(run1)

    run2 = store.start_sync_run(["mine"])
    store.finish_sync_run(run2, {"tasks:mine": 0})
    deltas = store.compute_changes(run2)
    assert [(d.key, d.kind, d.section) for d in deltas] == [("PROJ-1", "gone", "mine")]


def test_gone_delta_survives_pending_roundtrip(store):
    """section дельты gone не теряется при сохранении в накопитель."""
    store.add_pending_changes(1, [Delta(key="A-1", kind="gone", section="mine")])
    restored = store.pending_changes()
    assert restored[0].kind == "gone" and restored[0].section == "mine"


def test_analyses_are_gone(store):
    """«Анализы» убраны из продукта: ни таблицы, ни методов стора не осталось."""
    assert not hasattr(store, "save_analysis")
    tables = {r["name"] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "analyses" not in tables


def test_delete_job_removes_records_and_links(store):
    j = store.create_job("WM-1", "dev")
    store.add_job_record(j.id, "фаза 1", kind="phase")
    store.link_job_pr(j.id, 42, project="P", repo="r")
    assert store.get_job(j.id) is not None
    store.delete_job(j.id)
    assert store.get_job(j.id) is None
    assert store.jobs_for_task("WM-1") == []
    assert store.jobs_for_pr(42) == []


def test_pending_changes_accumulate_and_clear(store):
    store.add_pending_changes(1, [Delta(key="A-1", kind="new_comment", summary="s")])
    store.add_pending_changes(2, [Delta(key="A-2", kind="new_pr", summary="t")])
    assert [d.key for d in store.pending_changes()] == ["A-1", "A-2"]  # копятся между синками
    store.clear_pending_changes()
    assert store.pending_changes() == []


def test_notes_roundtrip(store):
    store.add_note("PROJ-1", "перенёс фикс в release-10.7")
    notes = store.get_notes("PROJ-1")
    assert len(notes) == 1
    assert notes[0].text == "перенёс фикс в release-10.7"
    assert notes[0].author == "claude"


def test_job_create_record_link_and_get(store):
    job = store.create_job("PROJ-399", title="dev-сервер")
    assert job.id > 0 and job.status == "active"

    store.add_job_record(job.id, "мердж develop", kind="phase", status="done")
    store.add_job_record(job.id, "Lazorin: убрать свой сервер", kind="remark")
    store.link_job_pr(job.id, 334, project="PROJ", repo="repo")
    store.link_job_pr(job.id, 334, project="PROJ", repo="repo")  # idempotent

    full = store.get_job(job.id)
    assert [r.kind for r in full.records] == ["phase", "remark"]
    assert full.records[0].status == "done"
    assert len(full.prs) == 1 and full.prs[0].pr_id == 334
    assert full.updated_at >= full.created_at


def test_job_filters_and_status(store):
    j1 = store.create_job("A-1", "j1")
    j2 = store.create_job("A-1", "j2")          # та же задача -> 2 работы
    j3 = store.create_job("B-2", "j3")
    store.link_job_pr(j2.id, 50, project="P", repo="r")
    store.set_job_status(j1.id, "done")

    assert {j.id for j in store.jobs_for_task("A-1")} == {j1.id, j2.id}
    assert [j.id for j in store.list_jobs(task_key="A-1", status="active")] == [j2.id]
    assert {j.id for j in store.jobs_for_pr(50)} == {j2.id}
    assert {j.id for j in store.list_jobs(status="active")} == {j2.id, j3.id}
    assert store.get_job(j1.id).status == "done"


def test_get_job_missing_returns_none(store):
    assert store.get_job(999) is None


def test_jobs_for_pr_distinguishes_project_repo(store):
    j1 = store.create_job("A-1")
    j2 = store.create_job("A-2")
    store.link_job_pr(j1.id, 100, project="P1", repo="r1")
    store.link_job_pr(j2.id, 100, project="P2", repo="r2")
    assert {j.id for j in store.jobs_for_pr(100)} == {j1.id, j2.id}                 # без фильтра — оба
    assert [j.id for j in store.jobs_for_pr(100, project="P1", repo="r1")] == [j1.id]
    assert [j.id for j in store.jobs_for_pr(100, project="P2", repo="r2")] == [j2.id]


def test_mentions_dedup_and_seen(store):
    from jwu.core.models import Mention

    def m(comment_id, text="[~alice] глянь"):
        return Mention(task_key="A-1", comment_id=comment_id, author="Боб", text=text,
                       created=f"2026-05-2{comment_id}T10:00", summary="Задача")

    assert [x.comment_id for x in store.add_mentions([m("1"), m("2")])] == ["1", "2"]
    # тот же комментарий второй раз — не новая запись
    assert store.add_mentions([m("1"), m("3")]) and \
        [x.comment_id for x in store.add_mentions([m("1")])] == []
    assert {x.comment_id for x in store.list_mentions()} == {"1", "2", "3"}
    assert [x.created for x in store.list_mentions()] == sorted(
        (x.created for x in store.list_mentions()), reverse=True)  # свежие сверху

    ids = [x.id for x in store.list_mentions()]
    assert len(store.unseen_mentions()) == 3
    store.mark_mentions_seen([ids[0]])
    assert len(store.unseen_mentions()) == 2
    store.mark_mentions_seen(None)
    assert store.unseen_mentions() == []


def test_mention_scan_state_roundtrip(store):
    assert store.mention_scan_state() == {}
    store.set_mention_scan("A-1", "2026-05-20T10:00")
    store.set_mention_scan("A-1", "2026-05-21T10:00")  # перезапись, не дубль
    assert store.mention_scan_state() == {"A-1": "2026-05-21T10:00"}


# --- чистка старых снапшотов ---------------------------------------------- #


def _age_run(store, run_id, days):
    """Состарить прогон и его снапшоты на N дней (чистка смотрит на даты)."""
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    store.conn.execute("UPDATE sync_runs SET started_at = ? WHERE id = ?", (old, run_id))
    for table in ("issue_snapshots", "pr_snapshots"):
        store.conn.execute(
            f"UPDATE {table} SET fetched_at = ? WHERE sync_run_id = ?", (old, run_id))
    store.conn.commit()


def _history(store, count=5, *, days_old=60):
    """Несколько состаренных прогонов с одной задачей и одним PR в каждом."""
    runs = []
    for n in range(count):
        run = store.start_sync_run(["mine", "prs:mine"])
        store.save_issue_snapshot(run, _issue(comments=list(range(n + 1))), ["mine"])
        store.save_pr_snapshot(run, PR(id=7, project="P", repository="r"), ["mine"])
        store.finish_sync_run(run, {"tasks:mine": 1, "prs:mine": 1})
        store.compute_changes(run)
        _age_run(store, run, days_old)
        runs.append(run)
    return runs


def test_prune_keeps_latest_snapshot_of_every_entity(store):
    runs = _history(store, count=5)
    report = store.prune_snapshots(days=30, dry_run=False)

    assert report.issue_snapshots > 0 and report.pr_snapshots > 0
    # по одной живой записи на сущность — ими считаются дельты следующего синка
    assert store.conn.execute(
        "SELECT COUNT(*) FROM issue_snapshots").fetchone()[0] == 1
    assert store.conn.execute(
        "SELECT COUNT(*) FROM pr_snapshots").fetchone()[0] == 1
    # выжил именно последний снапшот, а не какой попало
    kept = store.conn.execute("SELECT sync_run_id FROM issue_snapshots").fetchone()[0]
    assert kept == runs[-1]
    # и данные вкладок после чистки на месте
    assert [i.key for i in store.latest_issues("mine")] == ["PROJ-1"]
    assert [p.id for p in store.latest_prs("mine")] == [7]


def test_prune_does_not_resurrect_new_or_gone_deltas(store):
    """Главное свойство чистки: следующий синк после неё должен быть таким же тихим."""
    _history(store, count=5)
    store.prune_snapshots(days=30, dry_run=False)

    run = store.start_sync_run(["mine", "prs:mine"])
    store.save_issue_snapshot(run, _issue(comments=[0, 1, 2, 3, 4]), ["mine"])
    store.save_pr_snapshot(run, PR(id=7, project="P", repository="r"), ["mine"])
    store.finish_sync_run(run, {"tasks:mine": 1, "prs:mine": 1})
    assert store.compute_changes(run) == []


def test_prune_keeps_last_reliable_run_for_gone_detection(store):
    """Прогон, по которому считается «ушла из выборки», удалять нельзя."""
    _history(store, count=4)
    store.prune_snapshots(days=30, dry_run=False)

    # задача перестала приходить → ждём ровно одну gone, а не тишину и не дубли
    run = store.start_sync_run(["mine", "prs:mine"])
    store.finish_sync_run(run, {"tasks:mine": 0, "prs:mine": 0})
    deltas = store.compute_changes(run)
    assert [(d.key, d.kind) for d in deltas] == [("PROJ-1", "gone"), ("P/r#7", "pr_gone")]


def test_prune_keeps_last_reliable_dev_snapshot(store):
    """Свежайший снапшот с достоверной dev-панелью — база для new_pr, его не трогаем."""
    run1 = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run1, _issue(prs=[42], dev_ok=True), ["mine"])
    store.finish_sync_run(run1, {"tasks:mine": 1})
    store.compute_changes(run1)
    _age_run(store, run1, 60)

    # свежий снапшот есть, но dev-панель в нём сбойная (pr_ids пустые)
    run2 = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run2, _issue(prs=[], dev_ok=False), ["mine"])
    store.finish_sync_run(run2, {"tasks:mine": 1})
    store.compute_changes(run2)
    _age_run(store, run2, 60)

    store.prune_snapshots(days=30, dry_run=False)

    # dev-status снова ответил тем же PR — «новым» он выглядеть не должен
    run3 = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run3, _issue(prs=[42], dev_ok=True), ["mine"])
    store.finish_sync_run(run3, {"tasks:mine": 1})
    assert not any(d.kind == "new_pr" for d in store.compute_changes(run3))


def test_prune_spares_fresh_snapshots(store):
    """Свежие снапшоты не трогаем вовсе — чистка идёт строго по возрасту."""
    _history(store, count=3, days_old=3)
    before = store.conn.execute("SELECT COUNT(*) FROM issue_snapshots").fetchone()[0]
    report = store.prune_snapshots(days=30, dry_run=False)
    assert report.total == 0
    assert store.conn.execute(
        "SELECT COUNT(*) FROM issue_snapshots").fetchone()[0] == before


def test_prune_dry_run_counts_but_changes_nothing(store):
    _history(store, count=5)
    before = store.conn.execute("SELECT COUNT(*) FROM issue_snapshots").fetchone()[0]

    dry = store.prune_snapshots(days=30, dry_run=True)
    assert dry.dry_run is True and dry.issue_snapshots > 0
    assert store.conn.execute(
        "SELECT COUNT(*) FROM issue_snapshots").fetchone()[0] == before

    # сухой прогон обещает ровно то, что потом и делает
    real = store.prune_snapshots(days=30, dry_run=False)
    assert (real.issue_snapshots, real.pr_snapshots, real.sync_runs) == \
        (dry.issue_snapshots, dry.pr_snapshots, dry.sync_runs)


def test_prune_all_workspaces_restores_scope(store):
    from jwu.core import workspaces

    home = workspaces.create(store, "home")
    store.use_workspace(home.id)
    _history(store, count=3)
    store.use_workspace(store.get_workspace_by_slug("work").id)
    _history(store, count=3)

    reports = store.prune_all_workspaces(days=30, dry_run=False)
    assert set(reports) == {"work", "home"}
    assert all(r.total > 0 for r in reports.values())
    # скоуп соединения после обхода вернулся туда, где был
    assert store.workspace_id == store.get_workspace_by_slug("work").id


def test_vacuum_reclaims_space(store):
    _history(store, count=30)
    store.prune_snapshots(days=30, dry_run=False)
    assert store.free_ratio() > 0
    store.vacuum()
    assert store.free_ratio() == 0
    # соединение осталось рабочим (VACUUM трогает isolation_level)
    assert store.list_workspaces()


# --- правила воркспейса --------------------------------------------------- #


def test_rules_crud_and_scoping(store):
    ban = store.add_rule("Не пушить в develop", kind="constraint")
    how = store.add_rule("Как поднять стенд", text="1. docker compose up",
                         kind="howto", tag="legacy-бэкенд")
    store.add_rule("Сборка только pnpm", kind="convention", tag="фронт")

    assert [r.id for r in store.list_rules()] == sorted(r.id for r in store.list_rules())
    assert {r.title for r in store.list_rules(kind="constraint")} == {"Не пушить в develop"}

    # tag="" — только общие; tag="x" — общие И правила этого тега (общие действуют везде)
    assert [r.id for r in store.list_rules(tag="")] == [ban.id]
    assert {r.id for r in store.list_rules(tag="legacy-бэкенд")} == {ban.id, how.id}

    store.update_rule(how.id, title="Как поднять стенд локально", kind="info")
    fresh = store.get_rule(how.id)
    assert fresh.title == "Как поднять стенд локально" and fresh.kind == "info"
    assert fresh.text == "1. docker compose up"        # не затёрли тем, что не передали
    assert fresh.updated_at >= how.updated_at

    store.delete_rule(how.id)
    assert store.get_rule(how.id) is None
    assert len(store.list_rules()) == 2


def test_rule_kind_is_validated(store):
    with pytest.raises(ValueError, match="Неизвестный тип"):
        store.add_rule("x", kind="nope")
    rule = store.add_rule("x")
    assert rule.kind == "info"                          # дефолт — справка
    with pytest.raises(ValueError, match="Неизвестный тип"):
        store.update_rule(rule.id, kind="nope")
    with pytest.raises(ValueError, match="Неизвестное поле"):
        store.update_rule(rule.id, nonsense="x")


def test_rules_markdown_reads_as_a_briefing(store):
    """Правила отдаются текстом: их читают и исполняют, а не парсят."""
    store.add_rule("Не пушить в develop", text="совсем никогда", kind="constraint")
    store.add_rule("Как поднять стенд", text="шаг один\nшаг два",
                   kind="howto", tag="legacy-бэкенд")

    md = store.rules_markdown()
    assert "#1 ⛔ ЗАПРЕТ — Не пушить в develop" in md
    assert "совсем никогда" in md                 # общее правило — целиком
    assert "[#legacy-бэкенд]" in md               # правило тега — только заголовком
    assert "шаг один" not in md
    # многострочный текст остаётся многострочным, а не \n-эскейпом
    assert "\\n" not in md

    # с тегом приезжает и его полный текст
    scoped = store.rules_markdown(tag="legacy-бэкенд")
    assert "Только для #legacy-бэкенд" in scoped
    assert "шаг один" in scoped and "шаг два" in scoped
    assert "совсем никогда" in scoped             # общие никуда не делись

    assert store.rules_markdown(tag="нет-такого").count("Только для") == 0


def test_rules_markdown_is_empty_without_rules(store):
    assert store.rules_markdown() == ""


def test_rules_are_isolated_and_removed_with_workspace(store):
    from jwu.core import workspaces

    home = workspaces.create(store, "home")
    store.use_workspace(home.id)
    store.add_rule("домашнее правило")
    work = store.get_workspace_by_slug("work")
    store.use_workspace(work.id)
    store.add_rule("рабочее правило")

    assert [r.title for r in store.list_rules()] == ["рабочее правило"]
    store.use_workspace(home.id)
    assert [r.title for r in store.list_rules()] == ["домашнее правило"]

    store.delete_workspace(home.id)
    assert store.conn.execute(
        "SELECT COUNT(*) FROM workspace_rules WHERE workspace_id = ?",
        (home.id,),
    ).fetchone()[0] == 0
