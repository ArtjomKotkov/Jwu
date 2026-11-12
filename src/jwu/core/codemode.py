"""Code Mode: агент пишет короткий Python-скрипт против API jwu вместо цепочки MCP-вызовов.

Скрипт выполняется в отдельном процессе (``codemode_runner``) без файлов, сети и импорта jwu;
объект ``jwu`` в нём пересылает вызовы сюда, а здесь исполняются только методы ЧТЕНИЯ из
белого списка (``API``) — те же функции, что у MCP-инструментов, с той же логикой. Запись
наружу (комменты, пуши, переходы, ворклоги) — по-прежнему отдельные инструменты с «да».
В контекст агента приходит только итог скрипта.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

RUNNER = Path(__file__).with_name("codemode_runner.py")
TIMEOUT = 90.0          # стена на весь скрипт, с
CPU_SECONDS = 20        # процессорное время самого скрипта (сетевые вызовы jwu — не его)
MAX_CALLS = 200
MAX_RESULT = 20000      # символов итога в ответе агенту


@dataclass(frozen=True)
class Method:
    name: str                         # как зовётся в скрипте: jwu.<name>(...)
    tool: str                         # MCP-инструмент, который его исполняет
    forced: dict = field(default_factory=dict)   # параметры, которые скрипт не может поменять


# Только чтение. Параметры, ведущие к записи (скачать, сохранить), прибиты в forced.
API: list[Method] = [
    Method("paths", "jwu_workspace_paths"),
    Method("rules", "jwu_rules"),
    Method("guides", "jwu_guides"),
    Method("guide", "jwu_guide"),
    Method("tasks", "jwu_tasks"),
    Method("task", "jwu_task"),
    Method("attachments", "jwu_attachments", {"download": False, "dest": None}),
    Method("similar", "jwu_issue_similar"),
    Method("task_branches", "jwu_task_branches"),
    Method("branches", "jwu_branches"),
    Method("prs", "jwu_prs"),
    Method("pr", "jwu_pr"),
    Method("pr_diff", "jwu_pr_diff"),
    Method("pr_tasks", "jwu_pr_tasks"),
    Method("pr_people", "jwu_pr_people"),
    Method("review_queue", "jwu_review_queue"),
    Method("builds", "jwu_builds"),
    Method("build", "jwu_build"),
    Method("jobs", "jwu_jobs"),
    Method("job", "jwu_job_handoff", {"save": False}),
    Method("job_resources", "jwu_job_resources"),
    Method("notes", "jwu_notes"),
    Method("context", "jwu_context"),
    Method("mentions", "jwu_mentions"),
    Method("changes", "jwu_changes"),
    Method("features", "jwu_features"),
    Method("queue", "jwu_queue"),
    Method("workers", "jwu_worker_list"),
    Method("worklogs", "jwu_worklogs"),
    Method("confluence_search", "jwu_confluence_search"),
    Method("confluence_page", "jwu_confluence_page"),
    Method("zephyr_search", "jwu_zephyr_search"),
    Method("zephyr_case", "jwu_zephyr_case"),
    Method("disk", "jwu_disk"),
    Method("thresholds", "jwu_thresholds"),
    Method("ssh_servers", "jwu_ssh_servers"),
]
BY_NAME = {m.name: m for m in API}

Resolver = Callable[[str], Callable[..., Awaitable[Any]]]


def _fn(resolve: Resolver, method: Method):
    fn = resolve(method.tool)
    return getattr(fn, "fn", fn)


def _params(fn, method: Method) -> list[inspect.Parameter]:
    return [p for p in inspect.signature(fn).parameters.values()
            if p.name != "workspace" and p.name not in method.forced]


def signature(resolve: Resolver, method: Method) -> str:
    parts = []
    for p in _params(_fn(resolve, method), method):
        parts.append(p.name if p.default is inspect.Parameter.empty else f"{p.name}={p.default!r}")
    return f"jwu.{method.name}({', '.join(parts)})"


def help_md(resolve: Resolver) -> str:
    """Шпаргалка API для агента: методы, сигнатуры, что возвращают (первая строка описания)."""
    lines = [
        "# jwu_query — API скрипта (только чтение)",
        "",
        "Скрипт на Python. Объект `jwu` — методы ниже (позиционно или по имени); ответы — словари и "
        "списки, поля доступны через точку (`pr.id`). Итог — переменная `result` или значение "
        "последнего выражения; `print` тоже вернётся. Доступны json, re, math, datetime, collections, "
        "itertools, statistics, Counter, defaultdict. Нет: файлов, сети, импорта прочего, записи — "
        "запись делай отдельными инструментами jwu_* после «да». Ошибка вызова — исключение ApiError.",
        f"Лимиты: {MAX_CALLS} вызовов, {int(TIMEOUT)} с на скрипт, итог до {MAX_RESULT} символов — "
        "возвращай выжимку, а не сырые ответы.",
        "",
    ]
    for method in API:
        fn = _fn(resolve, method)
        doc = (fn.__doc__ or "").strip().splitlines()
        lines.append(f"- `{signature(resolve, method)}` — {doc[0] if doc else ''}")
    lines += ["", "Пример:", "```python",
              "rows = []",
              "for p in jwu.prs('mine', with_conflicts=False, with_builds=False):",
              "    ci = jwu.builds(p.id, p.project, p.repository)",
              "    rows.append((p.id, ci.build_state, (ci.autotests or {}).get('summary')))",
              "rows",
              "```"]
    return "\n".join(lines)


def _bind(fn, method: Method, args: list, kwargs: dict, workspace: Optional[str]) -> dict:
    params = _params(fn, method)
    names = [p.name for p in params]
    if len(args) > len(names):
        raise ValueError(f"jwu.{method.name}: лишние позиционные аргументы (есть: {', '.join(names)})")
    bound = dict(zip(names, args))
    for key, value in kwargs.items():
        if key not in names:
            raise ValueError(f"jwu.{method.name}: нет параметра «{key}» (есть: {', '.join(names) or '—'})")
        if key in bound:
            raise ValueError(f"jwu.{method.name}: «{key}» передан дважды")
        bound[key] = value
    missing = [p.name for p in params if p.default is inspect.Parameter.empty and p.name not in bound]
    if missing:
        raise ValueError(f"jwu.{method.name}: не хватает {', '.join(missing)}")
    if "workspace" in inspect.signature(fn).parameters:
        bound["workspace"] = workspace
    return {**bound, **method.forced}


def _plain(value: Any) -> Any:
    """Ответ инструмента → JSON (модели, даты — строками)."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _brief(args: list, kwargs: dict) -> str:
    text = ", ".join([repr(a) for a in args] + [f"{k}={v!r}" for k, v in kwargs.items()])
    return text if len(text) <= 80 else text[:77] + "…"


async def run(code: str, resolve: Resolver, workspace: Optional[str] = None, *,
              timeout: float = TIMEOUT, max_calls: int = MAX_CALLS) -> dict:
    """Выполнить скрипт: → {ok, result, stdout, error, calls: [{name, args, chars}], seconds}."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(RUNNER),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8", "LANG": "C.UTF-8"},
        limit=16 * 1024 * 1024,
    )
    calls: list[dict] = []

    async def send(obj: dict) -> None:
        proc.stdin.write((json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode())
        await proc.stdin.drain()

    async def converse() -> dict:
        await send({"code": code, "max_calls": max_calls, "cpu_seconds": CPU_SECONDS})
        while True:
            line = await proc.stdout.readline()
            if not line:
                err = (await proc.stderr.read()).decode(errors="replace").strip()
                return {"ok": False, "error": "песочница завершилась без итога"
                        + (f": {err.splitlines()[-1]}" if err else " (лимит процессорного времени?)")}
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("t") == "done":
                return msg
            if msg.get("t") != "call":
                continue
            name, args, kwargs = msg.get("name", ""), msg.get("args") or [], msg.get("kwargs") or {}
            entry = {"name": name, "args": _brief(args, kwargs)}
            calls.append(entry)
            method = BY_NAME.get(name)
            try:
                if method is None:
                    raise ValueError(f"нет метода jwu.{name}; список — jwu_query_help()")
                fn = _fn(resolve, method)
                result = _plain(await fn(**_bind(fn, method, args, kwargs, workspace)))
                reply = {"t": "ret", "id": msg.get("id"), "ok": True, "result": result}
                entry["chars"] = len(json.dumps(result, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001 — ошибку получает скрипт, а не сервер
                reply = {"t": "ret", "id": msg.get("id"), "ok": False, "error": f"{type(exc).__name__}: {exc}"}
                entry["error"] = str(exc)[:200]
            await send(reply)

    try:
        out = await asyncio.wait_for(converse(), timeout)
    except asyncio.TimeoutError:
        out = {"ok": False, "error": f"скрипт не уложился в {int(timeout)} с"}
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    out.pop("t", None)
    if "result" in out:
        text = json.dumps(out["result"], ensure_ascii=False, default=str)
        if len(text) > MAX_RESULT:
            out["result"] = text[:MAX_RESULT]
            out["truncated"] = f"итог {len(text)} символов, показано {MAX_RESULT} — верни выжимку"
    out["calls"] = calls
    out["seconds"] = round(loop.time() - started, 1)
    return out
