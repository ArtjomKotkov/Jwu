import pytest

from jwu.core.models import Issue, Job, JobPRLink, JobRecord, PR, classify_attachment

from .fixtures import (
    bitbucket_merge_raw,
    bitbucket_pr_raw,
    dev_status_branch_raw,
    dev_status_pr_raw,
    dev_status_repo_raw,
    jira_issue_raw,
)


def test_issue_from_jira_parses_core_fields_and_comments():
    raw = jira_issue_raw(comments=[{"id": 1, "body": "первый"}, {"id": 2, "body": "второй"}])
    issue = Issue.from_jira(raw)
    assert issue.key == "PROJ-1"
    assert issue.status == "In Progress"
    assert issue.assignee == "Alice"
    assert issue.priority == "High"
    # комментарии задач — в хронологическом порядке (старые сверху)
    assert [c.body for c in issue.comments] == ["первый", "второй"]


def test_issue_parses_attachments_with_kind_and_url():
    raw = jira_issue_raw(attachments=[
        {"id": 1, "filename": "bug.PNG", "mime": "image/png", "size": 2048},
        {"id": 2, "filename": "server.log", "size": 100},
        {"id": 3, "filename": "demo.mp4", "mime": "video/mp4"},
        {"id": 4, "filename": "report.pdf"},
        {"id": 5, "filename": "dump.zip"},
    ])
    issue = Issue.from_jira(raw)
    assert [a.kind for a in issue.attachments] == ["image", "log", "video", "doc", "archive"]
    img = issue.attachments[0]
    assert img.filename == "bug.PNG" and img.size == 2048
    assert img.url.endswith("/1/bug.PNG")
    # computed field попадает в JSON для скилла
    assert img.model_dump()["kind"] == "image"


@pytest.mark.parametrize("filename,mime,expected", [
    ("a.jpeg", "", "image"),
    ("trace.TXT", "", "log"),
    ("data.json", "", "log"),
    ("clip.mov", "", "video"),
    ("doc.docx", "", "doc"),
    ("src.tar.gz", "", "archive"),
    ("weird", "image/gif", "image"),
    ("weird", "text/plain", "log"),
    ("weird", "", "other"),
])
def test_classify_attachment(filename, mime, expected):
    assert classify_attachment(filename, mime) == expected


def test_issue_links_direction():
    issue = Issue.from_jira(jira_issue_raw())
    assert len(issue.links) == 1
    link = issue.links[0]
    assert link.direction == "outward"
    assert link.key == "PROJ-2"


def test_apply_dev_status_merges_branches_commits_prs():
    issue = Issue.from_jira(jira_issue_raw())
    merged = {"branches": [], "repositories": [], "pullRequests": []}
    for src in (dev_status_branch_raw(), dev_status_repo_raw(), dev_status_pr_raw()):
        for entry in src["detail"]:
            merged["branches"].extend(entry.get("branches", []))
            merged["repositories"].extend(entry["repositories"])
            merged["pullRequests"].extend(entry["pullRequests"])
    issue.apply_dev_status(merged)
    assert [b.name for b in issue.branches] == ["PROJ-1-fix"]
    assert [c.id for c in issue.commits] == ["abc123"]
    assert [p.id for p in issue.pull_requests] == ["#42"]


def test_pr_from_bitbucket_and_merge_status():
    pr = PR.from_bitbucket(bitbucket_pr_raw())
    assert pr.id == 42
    assert pr.project == "PROJ"
    assert pr.repository == "repo"
    assert pr.reviewers[0].approved is True
    pr.apply_merge_status(bitbucket_merge_raw(conflicted=True, can_merge=False))
    assert pr.conflicted is True
    assert pr.can_merge is False


def test_pr_merge_status_infers_conflict_from_vetoes():
    pr = PR.from_bitbucket(bitbucket_pr_raw())
    pr.apply_merge_status({
        "canMerge": False,
        "vetoes": [{"summaryMessage": "This pull request has conflicts"}],
    })
    assert pr.conflicted is True


def test_job_roundtrip_serialization():
    job = Job(
        id=1, task_key="PROJ-399", title="dev-сервер", status="active",
        created_at="2026-05-21T00:00:00+00:00", updated_at="2026-05-21T00:00:00+00:00",
        records=[JobRecord(id=1, job_id=1, kind="phase", text="мердж", status="done", ts="t")],
        prs=[JobPRLink(pr_id=334, project="PROJ", repo="repo")],
    )
    dumped = job.model_dump()
    restored = Job.model_validate(dumped)
    assert restored.task_key == "PROJ-399"
    assert restored.records[0].kind == "phase"
    assert restored.prs[0].pr_id == 334


def test_job_defaults():
    job = Job(task_key="X-1")
    assert job.status == "active"
    assert job.records == [] and job.prs == []
    rec = JobRecord(job_id=1, text="t")
    assert rec.kind == "note" and rec.status is None
