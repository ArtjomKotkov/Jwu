"""Zephyr Scale через клиент Jira: чтение, поля, папки, подсказка папки, запись и привязка."""

import json

import httpx
import pytest
import respx

from jwu.core import zephyr
from jwu.core.jira import JiraClient

HOST = "https://jira.test"
BASE = f"{HOST}/rest"


@pytest.fixture()
def client():
    j = JiraClient(HOST, token="t")
    yield zephyr.ZephyrClient(j, "QA")
    j.close()


def _mock_read():
    respx.get(f"{BASE}/tests/1.0/project").mock(return_value=httpx.Response(200, json=[
        {"id": 7, "key": "QA", "name": "Кейсы"}, {"id": 8, "key": "APP", "name": "Задачи"}]))
    respx.get(f"{BASE}/atm/1.0/issuelink/APP-1/testcases").mock(return_value=httpx.Response(200, json=[
        {"key": "QA-T1"}, {"key": "QA-T2"}, {"key": "OTHER-T9"}]))
    for key, folder, proj in (("QA-T1", "/Чат/Вложения", "QA"), ("QA-T2", "/Чат/Вложения", "QA"),
                              ("OTHER-T9", "/x", "OTHER")):
        respx.get(f"{BASE}/atm/1.0/testcase/{key}").mock(return_value=httpx.Response(200, json={
            "key": key, "name": key, "folder": folder, "projectKey": proj, "issueLinks": ["APP-1"]}))


@respx.mock
def test_for_issue_suggest_folder_and_fields(client):
    _mock_read()
    respx.get(f"{BASE}/tests/1.0/project/7/testcasestatus").mock(return_value=httpx.Response(200, json=[
        {"name": "Draft", "isDefault": True}, {"name": "Approved"}]))
    respx.get(f"{BASE}/tests/1.0/project/7/testcasepriority").mock(return_value=httpx.Response(200, json=[
        {"name": "High", "description": "смоук"}]))
    respx.get(f"{BASE}/tests/1.0/project/7/customfields/testcase").mock(return_value=httpx.Response(200, json=[
        {"name": "Автоматизация", "type": "SINGLE_CHOICE_SELECT_LIST", "required": True,
         "options": [{"name": "To Do"}, {"name": "Old", "archived": True}]},
        {"name": "Старое", "archived": True}]))
    respx.get(f"{BASE}/tests/1.0/project/7/foldertree/testcase").mock(return_value=httpx.Response(200, json={
        "children": [{"name": "Чат", "itemsCount": 3, "children": [{"name": "Вложения", "itemsCount": 2, "children": []}]}]}))
    assert [c["key"] for c in client.for_issue("APP-1")] == ["QA-T1", "QA-T2", "OTHER-T9"]
    assert client.suggest_folder("APP-1") == [{"path": "/Чат/Вложения", "cases": 2}]   # только проект кейсов
    f = client.fields()
    assert f["statuses"][0] == {"name": "Draft", "default": True}
    assert f["custom_fields"] == [{"name": "Автоматизация", "type": "SINGLE_CHOICE_SELECT_LIST",
                                   "required": True, "options": ["To Do"]}]
    assert client.folders() == [{"path": "/Чат", "count": 3}, {"path": "/Чат/Вложения", "count": 2}]


@respx.mock
def test_create_link_and_folder(client):
    created = respx.post(f"{BASE}/atm/1.0/testcase").mock(return_value=httpx.Response(201, json={"key": "QA-T5"}))
    res = client.create(name="Вложение уходит оператору", steps=[{"step": "Отправить файл", "expected": "Файл виден"}],
                        folder="/Чат", issueLinks=["APP-1"], labels=[], customFields={})
    body = json.loads(created.calls[0].request.content)
    assert res["key"] == "QA-T5" and body["projectKey"] == "QA" and body["issueLinks"] == ["APP-1"]
    assert body["testScript"] == {"type": "STEP_BY_STEP", "steps": [
        {"description": "Отправить файл", "expectedResult": "Файл виден"}]}
    assert "labels" not in body and "customFields" not in body          # пустое не шлём
    respx.get(f"{BASE}/atm/1.0/testcase/QA-T5").mock(return_value=httpx.Response(200, json={"issueLinks": ["APP-1"]}))
    put = respx.put(f"{BASE}/atm/1.0/testcase/QA-T5").mock(return_value=httpx.Response(200))
    assert client.link("QA-T5", ["APP-1"])["changed"] is False and not put.calls
    assert client.link("QA-T5", ["OTHER-2"])["issueLinks"] == ["APP-1", "OTHER-2"]
    assert json.loads(put.calls[0].request.content) == {"issueLinks": ["APP-1", "OTHER-2"]}
    folder = respx.post(f"{BASE}/atm/1.0/folder").mock(return_value=httpx.Response(201, json={"id": 1}))
    client.create_folder("Чат/Новое/")
    assert json.loads(folder.calls[0].request.content) == {"projectKey": "QA", "name": "/Чат/Новое", "type": "TEST_CASE"}
    with pytest.raises(zephyr.ZephyrError):
        client.update("QA-T5")
    assert not hasattr(client, "delete")                                # удаления нет намеренно


def test_no_project_and_bad_steps():
    c = zephyr.ZephyrClient(JiraClient(HOST, token="t"), "")
    with pytest.raises(zephyr.ZephyrError, match="jwu zephyr setup"):
        c._key(None)
    with pytest.raises(zephyr.ZephyrError):
        zephyr.steps_from([{"expected": "x"}])
