"""Code Mode (jwu_query): песочница, белый список, привязка аргументов, итог и ошибки."""

import asyncio

import pytest

from jwu.core import codemode


async def fake_task(key: str, workspace=None) -> dict:
    return {"key": key, "status": "Open", "ws": workspace, "comments": [{"author": "a", "body": "x" * 50}]}


async def fake_attachments(key: str, download: bool = False, kinds=None, dest=None, workspace=None) -> dict:
    return {"key": key, "download": download, "dest": dest}


async def fake_jobs(status=None, full: bool = False, workspace=None) -> list:
    return [{"id": i, "status": status} for i in (1, 2, 3)]


TOOLS = {"jwu_task": fake_task, "jwu_attachments": fake_attachments, "jwu_jobs": fake_jobs}


def run(code: str, **kw) -> dict:
    return asyncio.run(codemode.run(code, TOOLS.__getitem__, "work", **kw))


def test_result_last_expression_print_and_attr_access():
    out = run("t = jwu.task('AB-1')\nprint(t.status)\n[t.key, len(t.comments[0].body), t.ws]")
    assert out["ok"] and out["result"] == ["AB-1", 50, "work"] and out["stdout"] == "Open\n"
    assert out["calls"][0]["name"] == "task" and out["calls"][0]["chars"] > 0
    assert run("result = {'n': sum(j.id for j in jwu.jobs(status='active'))}\n1")["result"] == {"n": 6}


def test_forced_params_cannot_be_changed():
    assert run("jwu.attachments('AB-1')")["result"] == {"key": "AB-1", "download": False, "dest": None}
    out = run("jwu.attachments('AB-1', download=True)")
    assert not out["ok"] and "нет параметра «download»" in out["error"]


def test_unknown_method_bad_args_and_script_errors():
    assert "нет метода jwu.push" in run("jwu.push()")["error"]
    assert "не хватает key" in run("jwu.task()")["error"]
    assert "лишние позиционные" in run("jwu.task('A', 'B')")["error"]
    out = run("x = 1\ny = 1/0")
    assert out["error"].startswith("ZeroDivisionError (строка 2)")
    assert "строка 1" in run("def (:")["error"]
    assert run("try:\n    jwu.task()\nexcept ApiError:\n    result = 'поймал'")["result"] == "поймал"


@pytest.mark.parametrize("code, word", [
    ("open('/etc/hosts').read()", "файлы недоступны"),
    ("import os\nos.system('true')", "os.system"),
    ("import subprocess", "импорт «subprocess»"),
    ("import socket", "импорт «socket»"),
    ("import os\nos.listdir('/')", "os.listdir"),
    ("import os\nos.remove('/tmp/x')", "os.remove"),
    ("import jwu", "импорт «jwu»"),
])
def test_sandbox_blocks(code, word):
    out = run(code)
    assert not out["ok"] and word in out["error"]


def test_env_is_empty_of_secrets(monkeypatch):
    monkeypatch.setenv("JIRA_TOKEN", "secret")
    out = run("import os\nsorted(os.environ)")
    assert "JIRA_TOKEN" not in out["result"]


def test_limits_timeout_calls_and_result_size():
    assert "не уложился" in run("while True:\n    pass", timeout=1.5)["error"]
    assert "больше 2 вызовов" in run("for _ in range(3):\n    jwu.task('A')", max_calls=2)["error"]
    big = run("'x' * (codemode_max + 10)".replace("codemode_max", str(codemode.MAX_RESULT)))
    assert big["truncated"] and len(big["result"]) == codemode.MAX_RESULT


def test_help_lists_methods_with_signatures():
    resolve = lambda name: TOOLS.get(name, fake_jobs)  # noqa: E731
    md = codemode.help_md(resolve)
    assert "`jwu.task(key)`" in md and "`jwu.attachments(key, kinds=None)`" in md


def test_api_tools_exist_and_are_read_only():
    from jwu import mcp_server

    for method in codemode.API:
        assert hasattr(mcp_server, method.tool), method.tool
        doc = (getattr(mcp_server, method.tool).__doc__ or "")
        assert "ВНЕШНЯЯ ЗАПИСЬ" not in doc, method.tool
