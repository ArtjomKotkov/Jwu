"""Песочница jwu_query: выполняет скрипт агента в отдельном процессе.

Запускается как ``python -I codemode_runner.py`` с пустым окружением и jwu НЕ импортирует.
Разговаривает с MCP-сервером строками JSON: stdin — код и ответы на вызовы, stdout —
вызовы API и итог. Объект ``jwu`` в скрипте только пересылает вызовы: что разрешено,
решает сервер (белый список методов чтения).

Что скрипту нельзя (audit hook, ставится до запуска кода): открывать файлы, импортировать
то, что не загружено заранее, запускать процессы, сеть, менять файлы и папки. Это барьер
против обхода проверок записи jwu (у агента и так есть Bash), а не изоляция от злонамеренного
кода. Плюс лимит процессорного времени.
"""

from __future__ import annotations

# Всё, что скрипту можно импортировать, — загружается заранее: после хука новых импортов нет.
import ast
import bisect
import collections
import copy
import datetime
import decimal
import fractions
import functools
import heapq
import io
import itertools
import json
import math
import operator
import pprint
import random
import re
import statistics
import string
import sys
import textwrap
import time
import traceback
import typing

try:
    import resource
except ImportError:  # pragma: no cover — не POSIX
    resource = None

MAX_STDOUT = 5000
FILENAME = "<jwu_query>"

_BLOCKED_PREFIXES = ("subprocess.", "os.", "socket.", "ctypes.", "shutil.", "pty.", "winreg.", "msvcrt.",
                     "urllib.", "http.", "ftplib.", "smtplib.", "webbrowser.", "sqlite3.", "glob.")


def _hook(event: str, args: tuple) -> None:
    if event == "import":
        if args and args[0] not in sys.modules:
            raise ImportError(f"jwu_query: импорт «{args[0]}» недоступен — только заранее загруженное "
                              "(json, re, math, datetime, collections, itertools, statistics …)")
        return
    if event == "open":
        raise PermissionError("jwu_query: файлы недоступны — данные только через jwu.*")
    if event.startswith(_BLOCKED_PREFIXES):
        raise PermissionError(f"jwu_query: «{event}» недоступно — только вычисления и jwu.*")


class Obj(dict):
    """Ответ API: словарь с доступом через точку (pr.id, task.status)."""

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError:
            keys = ", ".join(list(self)[:25])
            raise AttributeError(f"нет поля «{name}»; есть: {keys}") from None


class ApiError(Exception):
    """Вызов jwu.* не удался (нет такого метода, неверные аргументы, ошибка источника)."""


class _Api:
    def __init__(self, proto_in, proto_out, max_calls: int) -> None:
        self._in, self._out, self._max = proto_in, proto_out, max_calls
        self._n = 0

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            self._n += 1
            if self._n > self._max:
                raise ApiError(f"больше {self._max} вызовов jwu.* за скрипт")
            self._out.write(json.dumps({"t": "call", "id": self._n, "name": name, "args": list(args),
                                        "kwargs": kwargs}, ensure_ascii=False, default=str) + "\n")
            self._out.flush()
            line = self._in.readline()
            if not line:
                raise ApiError("сервер jwu не ответил")
            reply = json.loads(line, object_hook=Obj)
            if not reply.get("ok"):
                raise ApiError(reply.get("error") or "ошибка")
            return reply.get("result")

        call.__name__ = name
        return call

    def __dir__(self):
        return ["help: jwu_query_help()"]


def _split_last_expr(code: str):
    """Код → (тело, последнее выражение или None): значение последнего выражения — итог."""
    tree = ast.parse(code, FILENAME, "exec")
    last = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = ast.Expression(tree.body.pop().value)
    return compile(tree, FILENAME, "exec"), (compile(last, FILENAME, "eval") if last else None)


def _error(exc: BaseException) -> str:
    """Ошибка скрипта: тип, текст и строка скрипта, где упало (без внутренностей песочницы)."""
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == FILENAME]
    where = f" (строка {frames[-1].lineno})" if frames else ""
    if isinstance(exc, SyntaxError) and exc.lineno:
        where = f" (строка {exc.lineno})"
    return f"{type(exc).__name__}{where}: {exc}"


def main() -> None:
    proto_in, proto_out = sys.stdin, sys.stdout
    start = json.loads(proto_in.readline() or "{}")
    if resource is not None:
        cpu = int(start.get("cpu_seconds") or 20)
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        except (ValueError, OSError):
            pass
    captured = io.StringIO()
    sys.stdout = captured
    api = _Api(proto_in, proto_out, int(start.get("max_calls") or 200))
    ns = {"__name__": "__jwu_query__", "jwu": api, "ApiError": ApiError, "json": json, "re": re,
          "math": math, "datetime": datetime, "collections": collections, "itertools": itertools,
          "statistics": statistics, "Counter": collections.Counter, "defaultdict": collections.defaultdict}
    out: dict = {"t": "done", "ok": True}
    try:
        body, last = _split_last_expr(start.get("code") or "")
        sys.addaudithook(_hook)
        exec(body, ns)
        value = eval(last, ns) if last is not None else None
        out["result"] = ns["result"] if "result" in ns else value
    except BaseException as exc:  # noqa: BLE001 — любая ошибка скрипта уходит агенту текстом
        out.update(ok=False, error=_error(exc))
    printed = captured.getvalue()
    if len(printed) > MAX_STDOUT:
        printed = printed[:MAX_STDOUT] + f"\n… обрезано, всего {len(printed)} символов"
    out["stdout"] = printed
    try:
        text = json.dumps(out, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        out.pop("result", None)
        out.update(ok=False, error=f"итог не сериализуется в JSON: {exc}")
        text = json.dumps(out, ensure_ascii=False, default=str)
    proto_out.write(text + "\n")
    proto_out.flush()


if __name__ == "__main__":
    main()
