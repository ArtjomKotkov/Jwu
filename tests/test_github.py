"""Тесты GitHub-провайдера: разбор задач/PR, сборки Actions и связь с сервисом."""

import httpx
import pytest
import respx

from jwu.core.config import Config
from jwu.core.github import GitHubClient, GitHubError, parse_actions_url
from jwu.core.models import BuildStatus, gh_ms, github_key, parse_github_key
from jwu.core.service import Service
from jwu.core.store import Store

GH = "https://api.github.com"
OWNER = "akotkov"
REPO = "dndeck"


# --------------------------------------------------------------------------- #
# Фикстуры сырых ответов
# --------------------------------------------------------------------------- #


def gh_issue_raw(number=42, title="Колода не сохраняется", state="open",
                 state_reason=None, labels=("bug",), assignee="akotkov"):
    return {
        "number": number,
        "title": title,
        "state": state,
        "state_reason": state_reason,
        "body": "Описание задачи",
        "user": {"login": "reporter"},
        "assignee": {"login": assignee} if assignee else None,
        "assignees": [{"login": assignee}] if assignee else [],
        "labels": [{"name": lb} for lb in labels],
        "created_at": "2026-07-01T10:00:00Z",
        "updated_at": "2026-07-20T12:30:00Z",
        "repository_url": f"{GH}/repos/{OWNER}/{REPO}",
        "html_url": f"https://github.com/{OWNER}/{REPO}/issues/{number}",
    }


def gh_pr_raw(number=7, state="open", merged_at=None, mergeable=True,
              mergeable_state="clean", head="42-fix-save", base="main"):
    repo = {"name": REPO, "owner": {"login": OWNER}}
    return {
        "number": number,
        "title": f"Fix save (#42)",
        "body": "Описание PR",
        "state": state,
        "merged_at": merged_at,
        "user": {"login": OWNER},
        "head": {"ref": head, "sha": "abc1234def", "repo": repo},
        "base": {"ref": base, "repo": repo},
        "html_url": f"https://github.com/{OWNER}/{REPO}/pull/{number}",
        "created_at": "2026-07-10T09:00:00Z",
        "updated_at": "2026-07-21T18:00:00Z",
        "requested_reviewers": [{"login": "bob"}],
        "comments": 2,
        "review_comments": 3,
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
    }


def gh_search_raw(items):
    return {"total_count": len(items), "incomplete_results": False, "items": items}


def _client(**kwargs) -> GitHubClient:
    kwargs.setdefault("owner", OWNER)
    kwargs.setdefault("repos", [REPO])
    return GitHubClient(GH, "tok", **kwargs)


# --------------------------------------------------------------------------- #
# Ключи задач
# --------------------------------------------------------------------------- #


def test_github_key_omits_own_owner_and_keeps_foreign():
    assert github_key(REPO, 42, owner=OWNER, default_owner=OWNER) == "dndeck#42"
    assert github_key("lib", 7, owner="other", default_owner=OWNER) == "other/lib#7"


@pytest.mark.parametrize("key,expected", [
    ("dndeck#42", (OWNER, "dndeck", 42)),
    ("other/lib#7", ("other", "lib", 7)),
    ("#42", (OWNER, REPO, 42)),
    ("42", (OWNER, REPO, 42)),
])
def test_parse_github_key_fills_defaults(key, expected):
    assert parse_github_key(key, default_owner=OWNER, default_repo=REPO) == expected


def test_parse_github_key_ignores_jira_keys():
    """Ключ Jira не должен опознаваться как GitHub — иначе ссылки уедут не туда."""
    assert parse_github_key("PROJ-123", default_owner=OWNER, default_repo=REPO) is None


# --------------------------------------------------------------------------- #
# Задачи
# --------------------------------------------------------------------------- #


@respx.mock
def test_search_scopes_by_repo_and_skips_prs():
    """Поиск сужается до репозиториев контура, а PR (они же issue у GitHub) отсеиваются."""
    route = respx.get(f"{GH}/search/issues").mock(
        return_value=httpx.Response(200, json=gh_search_raw([
            gh_issue_raw(number=42),
            {**gh_issue_raw(number=7), "pull_request": {"url": "..."}},
        ]))
    )
    with _client() as gh:
        issues = gh.search("is:issue is:open assignee:@me")
    assert [i.key for i in issues] == ["dndeck#42"]
    assert f"repo:{OWNER}/{REPO}" in route.calls[0].request.url.params["q"]


@respx.mock
def test_search_scopes_by_owner_type_when_repos_not_listed():
    respx.get(f"{GH}/users/{OWNER}").mock(
        return_value=httpx.Response(200, json={"type": "Organization"}))
    route = respx.get(f"{GH}/search/issues").mock(
        return_value=httpx.Response(200, json=gh_search_raw([])))
    with _client(repos=[]) as gh:
        gh.search("is:issue")
    assert f"org:{OWNER}" in route.calls[0].request.url.params["q"]


@respx.mock
def test_issue_maps_state_labels_and_comments():
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/issues/42").mock(
        return_value=httpx.Response(200, json=gh_issue_raw(
            state="closed", state_reason="not_planned", labels=("bug", "ui"))))
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/issues/42/comments").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "user": {"login": "bob"}, "body": "@akotkov глянь",
             "created_at": "2026-07-19T10:00:00Z", "updated_at": "2026-07-19T10:00:00Z"},
        ]))
    with _client() as gh:
        issue = gh.issue("dndeck#42", with_dev=False)
    assert issue.key == "dndeck#42"
    assert issue.status == "Не будет сделана"      # closed + not_planned
    assert issue.labels == ["bug", "ui"]
    assert issue.assignee == OWNER
    # время приводится к форме, которую понимает datetime.fromisoformat на 3.10
    assert issue.updated == "2026-07-20T12:30:00+00:00"
    assert [c.author for c in issue.comments] == ["bob"]


@respx.mock
def test_issue_dev_panel_collects_linked_prs_and_branch():
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/issues/42").mock(
        return_value=httpx.Response(200, json=gh_issue_raw()))
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/issues/42/comments").mock(
        return_value=httpx.Response(200, json=[]))
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/issues/42/timeline").mock(
        return_value=httpx.Response(200, json=[
            {"event": "cross-referenced", "source": {"issue": {
                "number": 7, "title": "Fix save", "state": "open",
                "pull_request": {"merged_at": None},
                "html_url": f"https://github.com/{OWNER}/{REPO}/pull/7",
            }}},
            {"event": "labeled"},
        ]))
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/branches").mock(
        return_value=httpx.Response(200, json=[
            {"name": "42-fix-save"}, {"name": "main"},
        ]))
    with _client() as gh:
        issue = gh.issue("dndeck#42")
    assert issue.dev_ok is True
    assert [(p.id, p.status) for p in issue.pull_requests] == [("7", "OPEN")]
    assert [b.name for b in issue.branches] == ["42-fix-save"]


@respx.mock
def test_dev_panel_failure_marks_dev_not_ok():
    """Сбой ленты событий не должен выглядеть как «связанных PR нет»."""
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/issues/42").mock(
        return_value=httpx.Response(200, json=gh_issue_raw()))
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/issues/42/comments").mock(
        return_value=httpx.Response(200, json=[]))
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/issues/42/timeline").mock(
        return_value=httpx.Response(500, text="boom"))
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/branches").mock(
        return_value=httpx.Response(200, json=[]))
    with _client() as gh:
        issue = gh.issue("dndeck#42")
    assert issue.dev_ok is False and issue.pull_requests == []


def test_worklog_refuses_clearly():
    """В GitHub таймтрекера нет — молча делать вид, что записали, нельзя."""
    with _client() as gh:
        with pytest.raises(GitHubError, match="таймтрекера"):
            gh.add_worklog("dndeck#42", "1h")
        assert gh.worklogs("dndeck#42") == []


# --------------------------------------------------------------------------- #
# Pull requests
# --------------------------------------------------------------------------- #


def _mock_pr(number=7, **kwargs):
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/pulls/{number}").mock(
        return_value=httpx.Response(200, json=gh_pr_raw(number=number, **kwargs)))
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/pulls/{number}/reviews").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "user": {"login": "bob"}, "state": "CHANGES_REQUESTED",
             "body": "поправь", "submitted_at": "2026-07-21T10:00:00Z"},
            {"id": 2, "user": {"login": "carol"}, "state": "APPROVED",
             "body": "", "submitted_at": "2026-07-21T11:00:00Z"},
        ]))


@respx.mock
def test_pr_maps_branches_state_and_reviewers():
    _mock_pr()
    with _client() as gh:
        pr = gh.pr(OWNER, REPO, 7)
    assert (pr.id, pr.project, pr.repository) == (7, OWNER, REPO)
    assert (pr.source_branch, pr.target_branch) == ("42-fix-save", "main")
    assert pr.state == "OPEN" and pr.latest_commit == "abc1234def"
    assert pr.comment_count == 5           # обсуждение + инлайн-замечания
    statuses = {r.name: r.status for r in pr.reviewers}
    assert statuses == {"bob": "NEEDS_WORK", "carol": "APPROVED"}
    assert pr.conflicted is False and pr.can_merge is True


@respx.mock
def test_pr_conflict_detected_from_mergeable_state():
    _mock_pr(mergeable=False, mergeable_state="dirty")
    with _client() as gh:
        pr = gh.pr(OWNER, REPO, 7)
    assert pr.conflicted is True and pr.can_merge is False


@respx.mock
def test_merged_and_declined_states():
    _mock_pr(state="closed", merged_at="2026-07-22T10:00:00Z")
    with _client() as gh:
        assert gh.pr(OWNER, REPO, 7).state == "MERGED"
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/pulls/8").mock(
        return_value=httpx.Response(200, json=gh_pr_raw(number=8, state="closed")))
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/pulls/8/reviews").mock(
        return_value=httpx.Response(200, json=[]))
    with _client() as gh:
        assert gh.pr(OWNER, REPO, 8).state == "DECLINED"


@respx.mock
def test_dashboard_prs_reads_full_cards_after_search():
    """Поиск отдаёт PR без веток и ревьюверов — карточку дочитываем по каждому."""
    respx.get(f"{GH}/search/issues").mock(
        return_value=httpx.Response(200, json=gh_search_raw([{
            "number": 7, "repository_url": f"{GH}/repos/{OWNER}/{REPO}",
            "pull_request": {"url": "..."},
        }])))
    _mock_pr()
    with _client() as gh:
        prs = gh.dashboard_prs("review")
    assert [(p.id, p.source_branch) for p in prs] == [(7, "42-fix-save")]


@respx.mock
def test_dashboard_prs_survives_unavailable_repo():
    """Один недоступный репозиторий не должен обнулять весь список."""
    respx.get(f"{GH}/search/issues").mock(
        return_value=httpx.Response(200, json=gh_search_raw([
            {"number": 7, "repository_url": f"{GH}/repos/{OWNER}/{REPO}",
             "pull_request": {}},
            {"number": 9, "repository_url": f"{GH}/repos/{OWNER}/secret",
             "pull_request": {}},
        ])))
    _mock_pr()
    respx.get(f"{GH}/repos/{OWNER}/secret/pulls/9").mock(
        return_value=httpx.Response(404, text="Not Found"))
    with _client() as gh:
        assert [p.id for p in gh.dashboard_prs("mine")] == [7]


@respx.mock
def test_pr_comments_thread_inline_replies_with_diff_context():
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/issues/7/comments").mock(
        return_value=httpx.Response(200, json=[
            {"id": 100, "user": {"login": "bob"}, "body": "общий коммент",
             "created_at": "2026-07-21T09:00:00Z"},
        ]))
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/pulls/7/reviews").mock(
        return_value=httpx.Response(200, json=[
            {"id": 200, "user": {"login": "bob"}, "state": "CHANGES_REQUESTED",
             "body": "нужно поправить", "submitted_at": "2026-07-21T10:00:00Z"},
            {"id": 201, "user": {"login": "carol"}, "state": "APPROVED",
             "body": "", "submitted_at": "2026-07-21T11:00:00Z"},
        ]))
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/pulls/7/comments").mock(
        return_value=httpx.Response(200, json=[
            {"id": 300, "user": {"login": "bob"}, "body": "тут утечка",
             "created_at": "2026-07-21T10:05:00Z", "path": "deck.py", "line": 12,
             "diff_hunk": "@@ -1,3 +1,4 @@\n ctx = open(p)\n+    return ctx"},
            {"id": 301, "user": {"login": OWNER}, "body": "починил",
             "created_at": "2026-07-21T10:30:00Z", "path": "deck.py", "line": 12,
             "in_reply_to_id": 300, "diff_hunk": "@@ -1,3 +1,4 @@\n ctx = open(p)"},
        ]))
    with _client() as gh:
        comments = gh.pr_comments(OWNER, REPO, 7)
    # отзыв без текста — это статус, а не комментарий: его в ленте быть не должно
    assert "201" not in [c.id for c in comments]
    inline = next(c for c in comments if c.id == "300")
    assert (inline.file, inline.line, inline.depth) == ("deck.py", 12, 0)
    # префиксы диффа сохраняются — как и у Bitbucket, по ним видно, что менялось
    assert inline.context == [" ctx = open(p)", "+    return ctx"]
    assert inline.anchor_idx == len(inline.context) - 1  # якорь — последняя строка hunk
    reply = next(c for c in comments if c.id == "301")
    assert reply.depth == 1 and reply.context == []
    assert next(c for c in comments if c.id == "200").text.startswith("🔴 ")


@respx.mock
def test_my_review_at_takes_latest_own_review():
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/pulls/7/reviews").mock(
        return_value=httpx.Response(200, json=[
            {"user": {"login": OWNER}, "state": "CHANGES_REQUESTED",
             "submitted_at": "2026-07-20T10:00:00Z"},
            {"user": {"login": OWNER}, "state": "APPROVED",
             "submitted_at": "2026-07-21T10:00:00Z"},
            {"user": {"login": "bob"}, "state": "APPROVED",
             "submitted_at": "2026-07-22T10:00:00Z"},
            {"user": {"login": OWNER}, "state": "COMMENTED",
             "submitted_at": "2026-07-23T10:00:00Z"},   # коммент — не ревью-действие
        ]))
    with _client() as gh:
        ts = gh.my_review_at(OWNER, REPO, 7, OWNER)
    assert ts == gh_ms("2026-07-21T10:00:00Z")  # последнее МОЁ ревью-действие


# --------------------------------------------------------------------------- #
# Сборки (GitHub Actions)
# --------------------------------------------------------------------------- #


ACTIONS_URL = f"https://github.com/{OWNER}/{REPO}/actions/runs/555/job/777"


def test_parse_actions_url():
    assert parse_actions_url(ACTIONS_URL) == (555, 777)
    assert parse_actions_url(f"https://github.com/{OWNER}/{REPO}/actions/runs/555") == (555, None)
    assert parse_actions_url("https://codecov.io/gh/x/y") is None


@respx.mock
def test_build_statuses_merge_checks_and_commit_statuses():
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/commits/abc1234def/check-runs").mock(
        return_value=httpx.Response(200, json={"check_runs": [
            {"id": 777, "name": "tests", "status": "completed", "conclusion": "failure",
             "html_url": ACTIONS_URL, "started_at": "2026-07-21T10:00:00Z",
             "output": {"title": "3 упавших теста"}},
            {"id": 778, "name": "lint", "status": "in_progress", "conclusion": None,
             "html_url": ACTIONS_URL, "started_at": "2026-07-21T10:00:00Z"},
            {"id": 779, "name": "docs", "status": "completed", "conclusion": "skipped",
             "html_url": ACTIONS_URL},
        ]}))
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/commits/abc1234def/status").mock(
        return_value=httpx.Response(200, json={"statuses": [
            {"state": "success", "context": "codecov/patch", "description": "88%",
             "target_url": "https://codecov.io/x", "updated_at": "2026-07-21T10:10:00Z"},
        ]}))
    with _client() as gh:
        builds = gh.build_statuses("abc1234def", project=OWNER, repo=REPO)
    assert [(b.name, b.state) for b in builds] == [
        ("tests", "FAILED"),
        ("lint", "INPROGRESS"),
        ("docs", "SUCCESSFUL"),          # skipped — это не поломка
        ("codecov/patch", "SUCCESSFUL"),
    ]


@respx.mock
def test_build_report_collects_failed_steps_and_log_tail():
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/actions/runs/555").mock(
        return_value=httpx.Response(200, json={
            "id": 555, "name": "CI", "run_number": 159, "status": "completed",
            "conclusion": "failure", "head_sha": "abc1234def", "head_branch": "42-fix-save",
        }))
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/actions/runs/555/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [
            {"id": 777, "name": "tests", "conclusion": "failure", "steps": [
                {"number": 1, "name": "checkout", "conclusion": "success"},
                {"number": 2, "name": "pytest", "conclusion": "failure",
                 "completed_at": "2026-07-21T10:05:00Z"},
            ]},
            {"id": 778, "name": "lint", "conclusion": "success", "steps": []},
        ]}))
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/actions/jobs/777/logs").mock(
        return_value=httpx.Response(200, text="...tail of the log..."))
    with _client() as gh:
        report = gh.build_report(OWNER, REPO, BuildStatus(
            state="FAILED", name="tests", url=ACTIONS_URL))
    assert report.ci == "github-actions" and report.details_available is True
    assert report.jenkins_available is False      # разбор не Jenkins-овый
    assert (report.result, report.building) == ("FAILURE", False)
    assert (report.sha, report.branch, report.number) == ("abc1234def", "42-fix-save", 159)
    assert report.summary == {"fail": 1, "passed": 1, "skip": 0}
    assert [(f.class_name, f.name) for f in report.failures] == [("tests", "pytest")]
    assert report.console_tail == "...tail of the log..."


@respx.mock
def test_build_report_degrades_for_external_ci():
    """Внешний CI (не Actions) — отдаём статус и ссылку, а не выдумываем детали."""
    with _client() as gh:
        report = gh.build_report(OWNER, REPO, BuildStatus(
            state="FAILED", name="codecov", url="https://codecov.io/gh/x/y"))
    assert report.details_available is False
    assert "внешний CI" in report.note.lower() or "внешний CI" in report.note


@respx.mock
def test_missing_logs_are_not_an_error():
    """Логи Actions протухают по ретенции — это норма, а не сбой разбора."""
    respx.get(f"{GH}/repos/{OWNER}/{REPO}/actions/jobs/777/logs").mock(
        return_value=httpx.Response(404, text="Not Found"))
    with _client() as gh:
        assert gh.job_log_tail(OWNER, REPO, 777) == ""


@respx.mock
def test_rate_limit_reported_separately_from_permissions():
    respx.get(f"{GH}/user").mock(return_value=httpx.Response(
        403, headers={"x-ratelimit-remaining": "0"}, text="rate limited"))
    with _client() as gh:
        with pytest.raises(GitHubError, match="лимит запросов"):
            gh.ping()


# --------------------------------------------------------------------------- #
# Связка с сервисным слоем
# --------------------------------------------------------------------------- #


def _github_cfg() -> Config:
    cfg = Config()
    cfg.github.api_url = GH
    cfg.github.owner = OWNER
    cfg.github.repos = REPO
    cfg.github.username = OWNER
    return cfg


def _service(tmp_path) -> Service:
    gh = _client()
    return Service(_github_cfg(), gh, gh, Store(tmp_path / "state.db"))


def test_service_reports_github_provider_and_defaults(tmp_path):
    svc = _service(tmp_path)
    assert svc.provider == "github"
    assert svc.jira is None and svc.bitbucket is None and svc.github is not None
    assert svc.default_pr_ref() == (OWNER, REPO)
    svc.close()


def test_service_marker_and_views_follow_provider(tmp_path):
    """Упоминание в GitHub — это @login, а не [~login]; выборки тоже свои."""
    svc = _service(tmp_path)
    assert svc._mention_marker(OWNER) == f"@{OWNER}"
    assert "assignee:@me" in svc._views()["mine"]
    svc.close()


@pytest.mark.parametrize("branch,title,expected", [
    ("42-fix-save", "Fix save", "dndeck#42"),
    ("feature/42-fix", "Fix save", "dndeck#42"),
    ("fix-save", "Fix save (#42)", "dndeck#42"),
    ("fix-save", "Fix save", ""),
])
def test_task_key_from_pr_reads_issue_number(tmp_path, branch, title, expected):
    """PR связывается с задачей по номеру issue: из ветки, иначе из заголовка."""
    from jwu.core.models import PR

    svc = _service(tmp_path)
    pr = PR(id=7, title=title, source_branch=branch, project=OWNER, repository=REPO)
    assert svc._task_key_from_pr(pr) == expected
    svc.close()


@respx.mock
def test_sync_keeps_prs_of_different_repos_apart(tmp_path):
    """Нумерация PR в GitHub своя в каждом репозитории — #1 из двух репо не должны слипнуться."""
    from jwu.core.models import PR

    svc = _service(tmp_path)
    store = svc.store
    run = store.start_sync_run(["prs:mine"])
    for repo in ("dndeck", "dndeck-ui"):
        store.save_pr_snapshot(
            run, PR(id=1, title=f"PR из {repo}", project=OWNER, repository=repo,
                    comment_count=0),
            ["mine"],
        )
    store.finish_sync_run(run, {"prs:mine": 2})
    titles = sorted(p.title for p in store.latest_prs("mine"))
    assert titles == ["PR из dndeck", "PR из dndeck-ui"]
    svc.close()


@respx.mock
def test_attachment_from_foreign_host_goes_without_token(tmp_path):
    """Токен уходит только на свой хост: вложения GitHub лежат на чужих и авторизации не требуют."""
    route = respx.get("https://user-images.githubusercontent.com/pic.png").mock(
        return_value=httpx.Response(200, content=b"PNG"))
    with _client() as gh:
        dest = gh.download_attachment(
            "https://user-images.githubusercontent.com/pic.png", tmp_path / "pic.png")
    assert dest.read_bytes() == b"PNG"
    assert "authorization" not in route.calls[0].request.headers


@respx.mock
def test_search_422_explains_owner_and_token_scope():
    """422 от поиска — это всегда «не туда смотрим»; сообщение GitHub об этом молчит."""
    respx.get(f"{GH}/search/issues").mock(return_value=httpx.Response(
        422, json={"message": "Validation Failed", "errors": [
            {"message": "The listed users and repositories cannot be searched…"}]}))
    with _client() as gh:
        with pytest.raises(GitHubError, match="не нашёл, где искать") as exc:
            gh.search("is:issue assignee:@me")
    assert REPO in str(exc.value)          # что именно искали
    assert "resource owner" in str(exc.value)   # и куда смотреть в настройках токена
