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


def test_analyses_roundtrip(store):
    a1 = store.save_analysis("план 1", "День 1")
    a2 = store.save_analysis("план 2", "День 2")
    assert a2.id > a1.id
    lst = store.list_analyses()
    assert [a.id for a in lst] == [a2.id, a1.id]  # новые сверху
    assert lst[0].content == ""  # список без content
    assert store.get_analysis(a1.id).content == "план 1"
    assert store.get_analysis().id == a2.id  # последний
    assert store.get_analysis(999) is None


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
