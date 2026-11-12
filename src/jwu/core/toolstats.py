"""Метрики MCP-инструментов: что агент зовёт, сколько весят ответы, где помог jwu_query.

Пишет обёртка инструментов в ``mcp_server`` — без аргументов и содержимого (там ключи задач и
рабочие тексты), только имя, сессия, воркспейс, время, размер ответа и успех. Для jwu_query —
ещё вызовы внутри скрипта и вес их сырых ответов: разница с итогом и есть экономия.

Записи копятся в памяти сервера и раз в минуту ложатся в БД своим соединением; хранятся
``KEEP_DAYS`` дней (чистит демон). Отчёт — ``jwu stats tools``.
"""

from __future__ import annotations

import os
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .store import Store

KEEP_DAYS = 30
FLUSH_EVERY = 60          # с
CHAIN_WINDOW = 60         # с: чтения одной сессии ближе этого — одна цепочка
CHAIN_MIN = 3             # столько читающих вызовов подряд мимо jwu_query — «упущенная цепочка»

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_usage (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    session      TEXT NOT NULL DEFAULT '',
    workspace    TEXT NOT NULL DEFAULT '',
    tool         TEXT NOT NULL,
    ms           INTEGER NOT NULL DEFAULT 0,
    chars        INTEGER NOT NULL DEFAULT 0,
    ok           INTEGER NOT NULL DEFAULT 1,
    inner_calls  INTEGER NOT NULL DEFAULT 0,
    raw_chars    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tool_usage_ts ON tool_usage(ts);
"""

_buffer: list[tuple] = []
_lock = threading.Lock()
_flusher_started = False


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def enabled() -> bool:
    """``JWU_TOOLSTATS=0`` выключает метрики (так делают тесты: их вызовы не пишутся в настоящую БД)."""
    return os.environ.get("JWU_TOOLSTATS", "1") != "0"


def record(tool: str, *, session: str, workspace: str, ms: int, chars: int, ok: bool,
           inner_calls: int = 0, raw_chars: int = 0) -> None:
    """Запомнить вызов (в памяти; в БД — при сбросе)."""
    if not enabled():
        return
    with _lock:
        _buffer.append((_iso(), session, workspace, tool, int(ms), int(chars), int(ok), int(inner_calls),
                        int(raw_chars)))


def flush(store: "Store") -> int:
    """Записать накопленное в БД. → сколько строк."""
    with _lock:
        rows, _buffer[:] = list(_buffer), []
    if not rows:
        return 0
    ensure_schema(store.conn)
    store.conn.executemany(
        "INSERT INTO tool_usage (ts, session, workspace, tool, ms, chars, ok, inner_calls, raw_chars)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    store.conn.commit()
    return len(rows)


def start_flusher(open_store) -> None:
    """Фоновый сброс раз в минуту (``open_store()`` — своё соединение на каждый сброс)."""
    global _flusher_started
    if _flusher_started:
        return
    _flusher_started = True

    def loop() -> None:
        while True:
            time.sleep(FLUSH_EVERY)
            flush_now(open_store)

    threading.Thread(target=loop, name="jwu-toolstats", daemon=True).start()


def flush_now(open_store) -> None:
    try:
        own = open_store()
        try:
            flush(own)
        finally:
            own.close()
    except Exception:  # noqa: BLE001 — метрики не критичны
        pass


def prune(store: "Store", days: int = KEEP_DAYS) -> int:
    ensure_schema(store.conn)
    cur = store.conn.execute("DELETE FROM tool_usage WHERE ts < ?",
                             (_iso(datetime.now(timezone.utc) - timedelta(days=days)),))
    store.conn.commit()
    return cur.rowcount or 0


def clear(store: "Store") -> int:
    """Начать замер с нуля: удалить все записи."""
    ensure_schema(store.conn)
    cur = store.conn.execute("DELETE FROM tool_usage")
    store.conn.commit()
    return cur.rowcount or 0


def _is_read(tool: str) -> bool:
    """Читающий инструмент — то, что jwu_query может заменить (его белый список)."""
    from .codemode import API

    return tool in {m.tool for m in API}


def report(store: "Store", days: int = 7, workspace: str = "") -> dict:
    """Сводка за ``days`` дней: топ инструментов, jwu_query, упущенные цепочки, сессии."""
    ensure_schema(store.conn)
    since = _iso(datetime.now(timezone.utc) - timedelta(days=days))
    rows = store.conn.execute(
        "SELECT ts, session, workspace, tool, ms, chars, ok, inner_calls, raw_chars FROM tool_usage"
        " WHERE ts >= ? AND (? = '' OR workspace = ?) ORDER BY session, ts, id",
        (since, workspace, workspace)).fetchall()
    tools: dict[str, dict] = defaultdict(lambda: {"calls": 0, "chars": 0, "ms": 0, "errors": 0})
    sessions: dict[str, dict] = defaultdict(lambda: {"calls": 0, "chars": 0, "query": 0})
    query = {"calls": 0, "errors": 0, "inner_calls": 0, "raw_chars": 0, "chars": 0}
    for r in rows:
        t = tools[r["tool"]]
        t["calls"] += 1
        t["chars"] += r["chars"]
        t["ms"] += r["ms"]
        t["errors"] += 0 if r["ok"] else 1
        s = sessions[r["session"] or "?"]
        s["calls"] += 1
        s["chars"] += r["chars"]
        if r["tool"] == "jwu_query":
            s["query"] += 1
            query["calls"] += 1
            query["errors"] += 0 if r["ok"] else 1
            query["inner_calls"] += r["inner_calls"]
            query["raw_chars"] += r["raw_chars"]
            query["chars"] += r["chars"]
    query["saved_chars"] = max(0, query["raw_chars"] - query["chars"])

    # упущенные цепочки: ≥ CHAIN_MIN читающих вызовов одной сессии подряд, без jwu_query между
    missed: Counter = Counter()
    missed_examples: list[dict] = []
    run: list = []

    def close_run() -> None:
        if len(run) >= CHAIN_MIN:
            key = " → ".join(dict.fromkeys(r["tool"] for r in run))
            missed[key] += 1
            if len(missed_examples) < 10:
                missed_examples.append({"session": run[0]["session"], "at": run[0]["ts"], "calls": len(run),
                                        "chars": sum(r["chars"] for r in run), "tools": key})
        run.clear()

    prev = None
    for r in rows:
        ts = datetime.fromisoformat(r["ts"])
        same = (prev is not None and r["session"] == prev["session"]
                and (ts - datetime.fromisoformat(prev["ts"])).total_seconds() <= CHAIN_WINDOW)
        if not same or not _is_read(r["tool"]):
            close_run()
        if _is_read(r["tool"]):
            run.append(r)
        prev = r
    close_run()

    top = sorted(({"tool": k, **v} for k, v in tools.items()), key=lambda x: -x["chars"])
    return {"days": days, "calls": len(rows), "chars": sum(r["chars"] for r in rows),
            "tools": top, "query": query,
            "missed_chains": [{"tools": k, "times": n} for k, n in missed.most_common(10)],
            "missed_examples": missed_examples,
            "sessions": sorted(({"session": k, **v} for k, v in sessions.items()), key=lambda x: -x["chars"])}


def render(rep: dict, limit: int = 15) -> str:
    """Отчёт текстом (markdown) — для CLI."""
    tok = lambda n: f"~{n // 3:,}".replace(",", " ")  # noqa: E731 — грубо: 3 символа на токен
    L = [f"## Инструменты jwu за {rep['days']} дн.: {rep['calls']} вызовов, ответы {tok(rep['chars'])} ток.", ""]
    if not rep["calls"]:
        return L[0] + "\n\nЗаписей нет: метрики пишет MCP-сервер jwu 3.2.1+ (перезапусти сессии)."
    L += ["| инструмент | вызовов | ответы, ток. | ср. мс | ошибок |", "|---|---:|---:|---:|---:|"]
    for t in rep["tools"][:limit]:
        L.append(f"| {t['tool']} | {t['calls']} | {tok(t['chars'])} | {t['ms'] // max(1, t['calls'])} | {t['errors']} |")
    q = rep["query"]
    L += ["", f"**jwu_query:** {q['calls']} скриптов ({q['errors']} с ошибкой), внутри {q['inner_calls']} вызовов; "
              f"сырые ответы {tok(q['raw_chars'])} → в контекст {tok(q['chars'])} ток., сэкономлено {tok(q['saved_chars'])}."]
    if rep["missed_chains"]:
        L += ["", f"**Упущенные цепочки** ({CHAIN_MIN}+ чтений подряд мимо jwu_query):"]
        L += [f"- {m['times']}× {m['tools']}" for m in rep["missed_chains"]]
    else:
        L += ["", "Упущенных цепочек нет."]
    if rep["sessions"]:
        L += ["", "**Сессии:** " + ", ".join(
            f"{s['session']} — {s['calls']} выз., {tok(s['chars'])} ток., query {s['query']}" for s in rep["sessions"][:8])]
    return "\n".join(L)
