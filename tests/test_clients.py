import json

import httpx
import pytest
import respx

from jwu.core.bitbucket import BitbucketClient
from jwu.core.jira import (
    JiraClient,
    JiraError,
    build_create_fields,
    check_create_fields,
)

from .fixtures import (
    bitbucket_activities_raw,
    bitbucket_commits_raw,
    bitbucket_dashboard_raw,
    bitbucket_merge_raw,
    bitbucket_pr_raw,
    dev_status_branch_raw,
    dev_status_pr_raw,
    dev_status_repo_raw,
    jira_createmeta_raw,
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
    assert issue.dev_ok is True  # все три dataType ответили — dev-данные достоверны


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
def test_jira_add_worklog_posts_time_and_comment():
    route = respx.post(f"{JIRA}/rest/api/2/issue/PROJ-7/worklog").mock(
        return_value=httpx.Response(201, json={"id": "10001", "timeSpent": "2h 30m"})
    )
    with JiraClient(JIRA, "tok") as jira:
        res = jira.add_worklog("PROJ-7", "2h 30m", comment="перенёс фикс stat v2")
    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"timeSpent": "2h 30m", "comment": "перенёс фикс stat v2"}
    assert res["timeSpent"] == "2h 30m"


@respx.mock
def test_jira_add_worklog_error_raises():
    respx.post(f"{JIRA}/rest/api/2/issue/PROJ-7/worklog").mock(
        return_value=httpx.Response(400, text="bad time")
    )
    with JiraClient(JIRA, "tok") as jira:
        with pytest.raises(JiraError) as exc:
            jira.add_worklog("PROJ-7", "не время")
    assert exc.value.status_code == 400


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
    assert issue.dev_ok is False  # dev-status упал → pr/branches недостоверны


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


@respx.mock
def test_bitbucket_my_review_at_latest_action_by_user():
    activities = {
        "isLastPage": True,
        "values": [
            {"action": "APPROVED", "user": {"name": "me"}, "createdDate": 3000},
            {"action": "REVIEWED", "user": {"name": "me"}, "createdDate": 1000},
            {"action": "APPROVED", "user": {"name": "other"}, "createdDate": 5000},
            {"action": "COMMENTED", "user": {"name": "me"}, "createdDate": 9000},
        ],
    }
    respx.get(
        f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/77/activities"
    ).mock(return_value=httpx.Response(200, json=activities))
    with BitbucketClient(BB, "tok") as bb:
        # самое свежее ревью-действие пользователя (апрув 3000), а не его коммент (9000)
        # и не апрув другого ревьюера (5000)
        assert bb.my_review_at("PROJ", "repo", 77, "me") == 3000
        assert bb.my_review_at("PROJ", "repo", 77, "nobody") is None


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


@respx.mock
def test_jira_create_issue_posts_fields_and_returns_link():
    route = respx.post(f"{JIRA}/rest/api/2/issue").mock(
        return_value=httpx.Response(201, json={"id": "10500", "key": "PROJ-777"})
    )
    fields = build_create_fields(
        "PROJ", "Падает экспорт", description="Шаги:\n# открыть\n# нажать",
        issuetype="Bug", priority="Major", assignee="bob",
        labels=["duty"], components=["core"], fix_versions=["12.5"], parent="PROJ-1",
    )
    with JiraClient(JIRA, "tok") as jira:
        res = jira.create_issue(fields)
    sent = json.loads(route.calls.last.request.content)["fields"]
    assert sent["project"] == {"key": "PROJ"}
    assert sent["issuetype"] == {"name": "Bug"}
    assert sent["assignee"] == {"name": "bob"}   # Server: логин, не accountId
    assert sent["components"] == [{"name": "core"}]
    assert sent["description"].startswith("Шаги:")           # wiki-текст, не ADF
    assert res == {"key": "PROJ-777", "id": "10500",
                   "url": f"{JIRA}/browse/PROJ-777"}


@respx.mock
def test_jira_create_issue_shows_jira_errors_verbatim():
    """400 от Jira приходит с errors/errorMessages — без них диагностировать нечего."""
    respx.post(f"{JIRA}/rest/api/2/issue").mock(return_value=httpx.Response(
        400,
        json={"errorMessages": ["Field 'foo' is unknown"],
              "errors": {"customfield_10500": "Отдел обязателен"}},
    ))
    with JiraClient(JIRA, "tok") as jira:
        with pytest.raises(JiraError) as exc:
            jira.create_issue(build_create_fields("PROJ", "тема"))
    assert exc.value.status_code == 400
    assert "Field 'foo' is unknown" in str(exc.value)
    assert "customfield_10500: Отдел обязателен" in str(exc.value)


@respx.mock
def test_jira_create_meta_returns_project_block():
    respx.get(f"{JIRA}/rest/api/2/issue/createmeta").mock(
        return_value=httpx.Response(200, json=jira_createmeta_raw()))
    with JiraClient(JIRA, "tok") as jira:
        meta = jira.create_meta("PROJ")
    assert meta["key"] == "PROJ"
    assert [t["name"] for t in meta["issuetypes"]] == ["Task", "Bug"]


@respx.mock
def test_jira_create_meta_empty_for_unavailable_project():
    respx.get(f"{JIRA}/rest/api/2/issue/createmeta").mock(
        return_value=httpx.Response(200, json={"projects": []}))
    with JiraClient(JIRA, "tok") as jira:
        assert jira.create_meta("NOPE") == {}


@respx.mock
def test_jira_link_issues_posts_type_and_direction():
    route = respx.post(f"{JIRA}/rest/api/2/issueLink").mock(
        return_value=httpx.Response(201, content=b""))
    with JiraClient(JIRA, "tok") as jira:
        res = jira.link_issues("PROJ-2", "PROJ-1", "Blocks")
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"type": {"name": "Blocks"},
                    "inwardIssue": {"key": "PROJ-2"},
                    "outwardIssue": {"key": "PROJ-1"}}
    assert res["type"] == "Blocks"


def test_check_create_fields_passes_when_required_are_filled():
    meta = jira_createmeta_raw()["projects"][0]
    check = check_create_fields(meta, build_create_fields("PROJ", "тема", issuetype="task"))
    assert check["ok"] is True and check["problems"] == []
    assert check["issuetype"] == "Task"  # регистр приводится к тому, что знает Jira


def test_check_create_fields_reports_missing_required_field():
    meta = jira_createmeta_raw(required_extra={"customfield_10500": "Отдел"})["projects"][0]
    check = check_create_fields(meta, build_create_fields("PROJ", "тема"))
    assert check["ok"] is False
    assert any("«Отдел» (customfield_10500)" in p for p in check["problems"])
    assert "Отдел" in check["required"]


def test_check_create_fields_reports_unknown_type_and_value():
    meta = jira_createmeta_raw()["projects"][0]
    by_type = check_create_fields(meta, build_create_fields("PROJ", "тема", issuetype="Эпик"))
    assert by_type["ok"] is False
    assert "Task, Bug" in by_type["problems"][0]

    by_value = check_create_fields(
        meta, build_create_fields("PROJ", "тема", priority="Ultra"))
    assert by_value["ok"] is False
    assert any("Major, Blocker" in p for p in by_value["problems"])


def test_check_create_fields_reports_unavailable_project():
    check = check_create_fields({}, build_create_fields("NOPE", "тема"))
    assert check["ok"] is False
    assert "NOPE" in check["problems"][0]


@respx.mock
def test_jira_transitions_and_do_transition():
    respx.get(f"{JIRA}/rest/api/2/issue/PROJ-1/transitions").mock(
        return_value=httpx.Response(200, json={"transitions": [
            {"id": "31", "name": "In Progress", "to": {"name": "In Progress"}},
            {"id": "41", "name": "Done", "to": {"name": "Closed"}},
        ]}))
    route = respx.post(f"{JIRA}/rest/api/2/issue/PROJ-1/transitions").mock(
        return_value=httpx.Response(204, content=b""))
    with JiraClient(JIRA, "tok") as jira:
        items = jira.transitions("PROJ-1")
        jira.do_transition("PROJ-1", "41")
    assert [t["name"] for t in items] == ["In Progress", "Done"]
    assert items[1]["to"] == "Closed"
    assert json.loads(route.calls.last.request.content) == {"transition": {"id": "41"}}


@respx.mock
def test_jira_add_attachment_sends_multipart_with_csrf_header(tmp_path):
    """Без X-Atlassian-Token: no-check Jira отвергает загрузку — заголовок обязателен."""
    route = respx.post(f"{JIRA}/rest/api/2/issue/PROJ-1/attachments").mock(
        return_value=httpx.Response(200, json=[{"id": "9", "filename": "log.har",
                                                "size": 12}]))
    src = tmp_path / "log.har"
    src.write_text("{}" * 6, encoding="utf-8")
    with JiraClient(JIRA, "tok") as jira:
        created = jira.add_attachment("PROJ-1", src)
    request = route.calls.last.request
    assert request.headers["X-Atlassian-Token"] == "no-check"
    assert b'name="file"; filename="log.har"' in request.content
    assert created[0]["filename"] == "log.har"


@respx.mock
def test_jira_add_attachment_error_raises(tmp_path):
    respx.post(f"{JIRA}/rest/api/2/issue/PROJ-1/attachments").mock(
        return_value=httpx.Response(413, json={"errorMessages": ["Файл слишком большой"]}))
    src = tmp_path / "big.zip"
    src.write_bytes(b"x" * 10)
    with JiraClient(JIRA, "tok") as jira:
        with pytest.raises(JiraError) as exc:
            jira.add_attachment("PROJ-1", src)
    assert exc.value.status_code == 413
    assert "Файл слишком большой" in str(exc.value)


@respx.mock
def test_jira_session_relogin_on_expired_session():
    """Сессия протухла у долгоживущего процесса: логинимся заново и повторяем запрос."""
    login = respx.post(f"{JIRA}/rest/auth/1/session").mock(
        return_value=httpx.Response(200, json={"session": {"name": "JSESSIONID"}}))
    route = respx.get(f"{JIRA}/rest/api/2/myself")
    route.side_effect = [
        httpx.Response(401, text="session expired"),
        httpx.Response(200, json={"name": "akotkov"}),
    ]
    with JiraClient(JIRA, session_login=("akotkov", "pw")) as jira:
        assert jira.myself()["name"] == "akotkov"
    assert login.call_count == 2      # первый — конструктор, второй — после 401
    assert route.call_count == 2


@respx.mock
def test_jira_session_relogin_happens_only_once():
    """Если и после перелогина 401 — дело в кредах: второй круг только зациклит."""
    respx.post(f"{JIRA}/rest/auth/1/session").mock(
        return_value=httpx.Response(200, json={}))
    route = respx.get(f"{JIRA}/rest/api/2/myself").mock(
        return_value=httpx.Response(401, text="nope"))
    with JiraClient(JIRA, session_login=("akotkov", "pw")) as jira:
        with pytest.raises(JiraError) as exc:
            jira.myself()
    assert route.call_count == 2
    assert "сессия Jira не восстановилась" in str(exc.value)


@respx.mock
def test_jira_pat_401_says_how_to_fix():
    """PAT перелогинить нечем — сообщение должно говорить, что делать."""
    respx.get(f"{JIRA}/rest/api/2/myself").mock(return_value=httpx.Response(401, text="x"))
    with JiraClient(JIRA, "tok") as jira:
        with pytest.raises(JiraError) as exc:
            jira.myself()
    assert "jwu configure --jira-token" in str(exc.value)


def test_check_create_fields_resolves_names_to_ids():
    """Инстанс с несколькими схемами не резолвит имя типа — ссылаемся по id из createmeta."""
    meta = jira_createmeta_raw()["projects"][0]
    check = check_create_fields(meta, build_create_fields(
        "PROJ", "тема", issuetype="bug", priority="Major", components=["core"],
        labels=["duty"], assignee="bob"))
    assert check["ok"] is True
    sent = check["fields"]
    assert sent["issuetype"] == {"id": "10001"}          # Bug — второй тип в фикстуре
    assert sent["priority"] == {"id": "3"}
    assert sent["components"] == [{"id": "55"}]
    # поля без allowedValues трогать нечем — уходят как были
    assert sent["labels"] == ["duty"]
    assert sent["assignee"] == {"name": "bob"}
    assert check["issuetype"] == "Bug"                   # человекочитаемое имя для показа


def test_check_create_fields_keeps_name_when_meta_has_no_id():
    """id из createmeta не пришёл — остаёмся на имени, а не отправляем пустую ссылку."""
    meta = jira_createmeta_raw()["projects"][0]
    for t in meta["issuetypes"]:
        t.pop("id")
    check = check_create_fields(meta, build_create_fields("PROJ", "тема"))
    assert check["ok"] is True
    assert check["fields"]["issuetype"] == {"name": "Task"}


def test_check_create_fields_unknown_value_keeps_payload_unsent():
    """Имя не резолвится — это problems, а не «ok»: dry-run обязан падать там же, где отправка."""
    meta = jira_createmeta_raw()["projects"][0]
    check = check_create_fields(meta, build_create_fields(
        "PROJ", "тема", priority="Ultra"))
    assert check["ok"] is False
    assert any("Ultra" in p for p in check["problems"])
    assert check["fields"]["priority"] == {"name": "Ultra"}   # ничего не выдумываем
