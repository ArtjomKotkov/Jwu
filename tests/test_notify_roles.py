"""Роль в уведомлениях: «МОЁ» / «УЧАСТВУЮ», кто перевёл статус, на ком задача (JWU-49)."""

import httpx
import respx

from jwu.core import notify
from jwu.core.jira import JiraClient
from jwu.core.models import Delta, Issue, Mention, PR

JIRA = "https://jira.test"
ME = ("akotkov", "Артём Котков")


def _issues():
    return {
        "WM1WIN-243": Issue(key="WM1WIN-243", assignee="Артём Котков", reporter="QA"),
        "WEBIMCORE-12728": Issue(key="WEBIMCORE-12728", assignee="Иван Иванов", reporter="QA"),
        "WMDJANGOCHAT-336": Issue(key="WMDJANGOCHAT-336", assignee="Пётр Петров", reporter="Артём Котков"),
        "PROJ-1": Issue(key="PROJ-1", assignee="Коллега"),
        "PROJ-2": Issue(key="PROJ-2", assignee="Коллега"),
        "PROJ-3": Issue(key="PROJ-3", assignee=""),
    }


def _prs():
    mine = [PR(id=10, project="P", repository="r", source_branch="feature/PROJ-1-x")]
    review = [PR(id=20, project="P", repository="r", source_branch="bugfix/PROJ-2-y")]
    return mine, review


def test_role_priority():
    mine, review = _prs()
    roles = notify.resolve_roles(
        ["WM1WIN-243", "PROJ-1", "WMDJANGOCHAT-336", "PROJ-2", "PROJ-3", "WEBIMCORE-12728",
         "P/r#10", "P/r#20"],
        me=ME, issues=_issues(), my_prs=mine, review_prs=review, mention_keys=["PROJ-3"])
    got = {k: (r.label, r.mine) for k, r in roles.items()}
    assert got == {
        "WM1WIN-243": (notify.ROLE_ASSIGNEE, True),
        "PROJ-1": (notify.ROLE_MY_PR, True),            # задача моего PR
        "WMDJANGOCHAT-336": (notify.ROLE_REPORTER, False),
        "PROJ-2": (notify.ROLE_REVIEWER, False),         # задача PR на моём ревью
        "PROJ-3": (notify.ROLE_MENTIONED, False),
        "WEBIMCORE-12728": (notify.ROLE_WATCHER, False),
        "P/r#10": (notify.ROLE_MY_PR, True),
        "P/r#20": (notify.ROLE_REVIEWER, False),
    }
    assert roles["WM1WIN-243"].assignee_is_me and roles["WEBIMCORE-12728"].assignee == "Иван Иванов"


def test_fresh_assignee_from_status_wins():
    roles = notify.resolve_roles(["WEBIMCORE-12728"], me=ME, issues=_issues(),
                                 status={"WEBIMCORE-12728": {"actor": "Aleksandra Golovko",
                                                             "assignee": "Артём Котков"}})
    role = roles["WEBIMCORE-12728"]
    assert role.mine and role.label == notify.ROLE_ASSIGNEE and role.actor == "Aleksandra Golovko"


def _note():
    deltas = [
        Delta(key="WM1WIN-243", kind="returned_from_testing", summary="Проблема при выгрузке статистики",
              detail="WEBIM TESTING → DEFECT"),
        Delta(key="WEBIMCORE-12728", kind="qa_comment", summary="Баг хронологии сообщений",
              detail="+1 комм. от Aleksandra Golovko"),
    ]
    mentions = [Mention(task_key="WMDJANGOCHAT-336", author="Roman Gomelauri", text="[~akotkov] Стенд готов")]
    note = notify.build_notification("work", deltas, mentions)
    note.roles = notify.resolve_roles(
        ["WM1WIN-243", "WEBIMCORE-12728", "WMDJANGOCHAT-336"], me=ME, issues=_issues(),
        mention_keys=["WMDJANGOCHAT-336"],
        status={"WM1WIN-243": {"actor": "Мария QA", "assignee": "Артём Котков"}})
    return note


def test_format_two_blocks_with_role_lines():
    text = notify.format_message(_note())
    mine, part = text.index("━━ МОЁ ━━"), text.index("━━ УЧАСТВУЮ ━━")
    assert mine < text.index("WM1WIN-243") < part < text.index("WEBIMCORE-12728")
    assert "👤 исполнитель · перевёл Мария QA · на мне" in text
    assert "👁 наблюдаю · исп. Иван Иванов" in text
    assert "✍️ автор задачи · исп. Пётр Петров" in text   # упоминание в задаче, которую я завёл
    # внутри блока — прежние секции по виду события
    assert text.index("↩️ ВЕРНУЛИ С ТЕСТОВ") < part and text.index("📣 УПОМИНАНИЯ") > part


def test_status_change_unassigned_and_no_block_when_empty():
    note = notify.build_notification("work", [Delta(key="PROJ-3", kind="status_change", summary="t",
                                                    detail="Open → In Progress")],
                                     kinds=("status_change",))
    note.roles = notify.resolve_roles(["PROJ-3"], me=ME, issues=_issues(),
                                      status={"PROJ-3": {"actor": "Лид", "assignee": ""}})
    text = notify.format_message(note)
    assert "👁 наблюдаю · перевёл Лид · не назначена" in text
    assert "━━ МОЁ ━━" not in text and "━━ УЧАСТВУЮ ━━" in text


def test_without_roles_old_format():
    note = _note()
    note.roles = {}
    text = notify.format_message(note)
    assert "━━" not in text and "исполнитель" not in text and "WM1WIN-243" in text


@respx.mock
def test_jira_status_actor():
    respx.get(f"{JIRA}/rest/api/2/issue/WM1WIN-243").mock(return_value=httpx.Response(200, json={
        "fields": {"assignee": {"displayName": "Артём Котков"}},
        "changelog": {"histories": [
            {"author": {"displayName": "Старый"}, "created": "2026-09-20",
             "items": [{"field": "status", "fromString": "Open", "toString": "WEBIM TESTING"}]},
            {"author": {"displayName": "Мария QA"}, "created": "2026-09-25",
             "items": [{"field": "status", "fromString": "WEBIM TESTING", "toString": "DEFECT"}]},
            {"author": {"displayName": "Кто-то"}, "created": "2026-09-26",
             "items": [{"field": "assignee", "toString": "Артём Котков"}]},
        ]}}))
    client = JiraClient(JIRA, "tok")
    try:
        got = client.status_actor("WM1WIN-243")
    finally:
        client.close()
    assert got == {"actor": "Мария QA", "from": "WEBIM TESTING", "to": "DEFECT", "at": "2026-09-25",
                   "assignee": "Артём Котков"}
    assert respx.calls.last.request.url.params["expand"] == "changelog"
