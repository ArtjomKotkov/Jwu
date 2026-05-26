import httpx
import pytest
import respx

from jwu.core.bitbucket import BitbucketClient
from jwu.core.jira import JiraClient, JiraError

from .fixtures import (
    bitbucket_activities_raw,
    bitbucket_commits_raw,
    bitbucket_dashboard_raw,
    bitbucket_merge_raw,
    bitbucket_pr_raw,
    dev_status_branch_raw,
    dev_status_pr_raw,
    dev_status_repo_raw,
    jira_issue_raw,
    jira_search_raw,
)

JIRA = "https://jira.test"
BB = "https://git.test"


@respx.mock
def test_jira_search_paginates():
    page1 = [jira_issue_raw(key=f"PROJ-{i}") for i in range(50)]
    page2 = [jira_issue_raw(key="PROJ-50")]
    route = respx.get(f"{JIRA}/rest/api/2/search")
    route.side_effect = [
        httpx.Response(200, json=jira_search_raw(page1, total=51)),
        httpx.Response(200, json=jira_search_raw(page2, total=51, start_at=50)),
    ]
    with JiraClient(JIRA, "tok") as jira:
        issues = jira.search("project = PROJ")
    assert len(issues) == 51
    assert route.call_count == 2


@respx.mock
def test_jira_issue_with_dev_status():
    respx.get(f"{JIRA}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(200, json=jira_issue_raw(comments=[{"id": 1, "body": "c"}]))
    )
    dev = respx.get(f"{JIRA}/rest/dev-status/1.0/issue/detail")
    dev.side_effect = [
        httpx.Response(200, json=dev_status_branch_raw()),     # dataType=branch → ветки
        httpx.Response(200, json=dev_status_repo_raw()),       # dataType=repository → коммиты
        httpx.Response(200, json=dev_status_pr_raw()),         # dataType=pullrequest → PR
    ]
    with JiraClient(JIRA, "tok") as jira:
        issue = jira.issue("PROJ-1")
    assert issue.comments[0].body == "c"
    assert [b.name for b in issue.branches] == ["PROJ-1-fix"]
    assert [p.id for p in issue.pull_requests] == ["#42"]


@respx.mock
def test_jira_download_attachment_streams_to_file(tmp_path):
    url = f"{JIRA}/secure/attachment/9/bug.png"
    respx.get(url).mock(return_value=httpx.Response(200, content=b"\x89PNG\r\nDATA"))
    dest = tmp_path / "sub" / "bug.png"
    with JiraClient(JIRA, "tok") as jira:
        out = jira.download_attachment(url, dest)
    assert out == dest
    assert dest.read_bytes() == b"\x89PNG\r\nDATA"  # каталог создан, файл записан


@respx.mock
def test_jira_download_attachment_error_raises(tmp_path):
    url = f"{JIRA}/secure/attachment/9/missing.png"
    respx.get(url).mock(return_value=httpx.Response(404, text="gone"))
    with JiraClient(JIRA, "tok") as jira:
        with pytest.raises(JiraError) as exc:
            jira.download_attachment(url, tmp_path / "x.png")
    assert exc.value.status_code == 404


@respx.mock
def test_jira_401_raises():
    respx.get(f"{JIRA}/rest/api/2/myself").mock(return_value=httpx.Response(401, text="nope"))
    with JiraClient(JIRA, "bad") as jira:
        with pytest.raises(JiraError) as exc:
            jira.myself()
    assert exc.value.status_code == 401


@respx.mock
def test_jira_dev_status_failure_is_non_fatal():
    respx.get(f"{JIRA}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(200, json=jira_issue_raw())
    )
    respx.get(f"{JIRA}/rest/dev-status/1.0/issue/detail").mock(
        return_value=httpx.Response(404, text="no plugin")
    )
    with JiraClient(JIRA, "tok") as jira:
        issue = jira.issue("PROJ-1")  # не должно падать
    assert issue.pull_requests == []


@respx.mock
def test_bitbucket_dashboard_and_merge():
    respx.get(f"{BB}/rest/api/1.0/dashboard/pull-requests").mock(
        return_value=httpx.Response(200, json=bitbucket_dashboard_raw([bitbucket_pr_raw()]))
    )
    respx.get(
        f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42/merge"
    ).mock(return_value=httpx.Response(200, json=bitbucket_merge_raw(conflicted=True)))
    with BitbucketClient(BB, "tok") as bb:
        prs = bb.dashboard_prs("review")
        assert prs[0].id == 42
        status = bb.merge_status("PROJ", "repo", 42)
        prs[0].apply_merge_status(status)
    assert prs[0].conflicted is True


@respx.mock
def test_bitbucket_pr_comments_with_diff_context():
    respx.get(
        f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/350/activities"
    ).mock(return_value=httpx.Response(200, json=bitbucket_activities_raw()))
    with BitbucketClient(BB, "tok") as bb:
        comments = bb.pr_comments("PROJ", "repo", 350)
    # верхний коммент + ответ, в хронологическом порядке (родитель раньше ответа)
    assert [c.depth for c in comments] == [0, 1]
    top = comments[0]
    assert top.file == "README.md" and top.line == 228
    assert top.context == [" ## заголовок", "+новая строка"]
    assert comments[1].text == "ответ"


@respx.mock
def test_bitbucket_pr_comments_newest_thread_first():
    # activities новыми сверху: первой идёт самая свежая активность
    activities = {
        "isLastPage": True,
        "values": [
            {
                "action": "COMMENTED",
                "comment": {"id": 200, "text": "свежий", "author": {"name": "a"}, "comments": []},
            },
            {
                "action": "COMMENTED",
                "comment": {"id": 100, "text": "старый", "author": {"name": "b"}, "comments": []},
            },
        ],
    }
    respx.get(
        f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/350/activities"
    ).mock(return_value=httpx.Response(200, json=activities))
    with BitbucketClient(BB, "tok") as bb:
        comments = bb.pr_comments("PROJ", "repo", 350)
    # свежий тред первым (как в activities), без разворота
    assert [c.text for c in comments] == ["свежий", "старый"]


def test_anchor_index_matches_line_numbers():
    from jwu.core.bitbucket import _anchor_index, _diff_lines

    diff = {"hunks": [{"segments": [
        {"type": "CONTEXT", "lines": [{"line": "a", "source": 10, "destination": 10}]},
        {"type": "ADDED", "lines": [{"line": "b", "source": 10, "destination": 11}]},
    ]}]}
    lines = _diff_lines(diff)
    assert _anchor_index(lines, {"line": 11, "fileType": "TO"}) == 1
    assert _anchor_index(lines, {"line": 10, "fileType": "FROM"}) == 0
    assert _anchor_index(lines, {"line": 999}) == -1


@respx.mock
def test_bitbucket_latest_and_commits():
    respx.get(
        f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/350/commits"
    ).mock(return_value=httpx.Response(200, json=bitbucket_commits_raw()))
    with BitbucketClient(BB, "tok") as bb:
        assert bb.latest_commit("PROJ", "repo", 350).startswith("abc123def")
        commits = bb.pr_commits("PROJ", "repo", 350)
    assert commits[0]["id"] == "abc123def"
