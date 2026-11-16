"""История ревью по PR: находки всех кругов и всех ревьюеров, их судьба и выжимка для нового круга.

Каждый круг ревью начинался с нуля: отклонённое поднималось снова, а фикс прошлого замечания
ломал соседний сценарий, и ревьюер об этом не знал. Здесь копятся находки и то, чем они
кончились, а ``history`` отдаёт короткую выжимку: что отклонено и почему, что исправлено
(проверить регрессию), что ещё открыто.

Источник находки — ``side`` (``local`` — наше ревью, ``external`` — всё внешнее: люди,
ревью в CI) и свободная подпись ``label`` (имя агента, логин, «нейро #898») — только для
показа. Круг (``review_rounds``) — один проход одного ревьюера: запись ``kind=review`` в
журнале работы, коммент PR, круг, добавленный скиллом. ``full_pass`` — ревьюер каждый круг
смотрит весь PR заново: тогда находка, которой нет в его следующем круге, считается снятой.

Одна и та же проблема в разных кругах — цепочка ``same_as`` (сопоставление по словам и файлу
при добавлении круга, поправимо руками). Судьба (``fate``) ставится на находку; у цепочки
действует последняя поставленная.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:
    from .store import Store

SIDES = ("local", "external")
LEVELS = ("blocker", "critical", "major", "important", "minor", "info")
LEVEL_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}
LEVEL_ALIASES = {"b": "blocker", "i": "important", "m": "minor", "крит": "critical", "блокер": "blocker",
                 "важное": "important", "мелкое": "minor", "nit": "minor", "suggestion": "info"}
FATES = ("fixed", "rejected", "out_of_pr", "fabricated", "duplicate")
REJECTED = ("rejected", "out_of_pr", "fabricated")
FATE_RU = {"fixed": "исправлено", "rejected": "отклонено", "out_of_pr": "вне PR",
           "fabricated": "не подтвердилось по коду", "duplicate": "дубль"}
MAX_CHARS = 5000
MATCH_MIN = 0.4

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_rounds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    project      TEXT NOT NULL DEFAULT '',
    repo         TEXT NOT NULL DEFAULT '',
    pr_id        INTEGER NOT NULL,
    side         TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    full_pass    INTEGER NOT NULL DEFAULT 1,
    commit_sha   TEXT NOT NULL DEFAULT '',
    ts           TEXT NOT NULL,
    verdict      TEXT NOT NULL DEFAULT '',
    ref          TEXT NOT NULL DEFAULT '',
    source_key   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_rounds_pr ON review_rounds(workspace_id, pr_id, project, repo);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_rounds_src ON review_rounds(workspace_id, source_key)
    WHERE source_key != '';
CREATE TABLE IF NOT EXISTS review_findings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id     INTEGER NOT NULL,
    anchor       TEXT NOT NULL DEFAULT '',
    level        TEXT NOT NULL DEFAULT 'minor',
    text         TEXT NOT NULL,
    file         TEXT NOT NULL DEFAULT '',
    line         INTEGER,
    words        TEXT NOT NULL DEFAULT '',
    same_as      INTEGER,
    fate         TEXT NOT NULL DEFAULT '',
    fate_reason  TEXT NOT NULL DEFAULT '',
    fate_ref     TEXT NOT NULL DEFAULT '',
    fate_commit  TEXT NOT NULL DEFAULT '',
    fate_by      TEXT NOT NULL DEFAULT '',
    fate_at      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_review_findings_round ON review_findings(round_id);
"""


class ReviewError(ValueError):
    pass


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ts(value) -> str:
    """epoch ms | ISO → ISO UTC (для сортировки кругов)."""
    if isinstance(value, (int, float)) and value:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="seconds")
    return str(value or "") or _now()


# --------------------------------------------------------------------------- #
# Слова, файлы, уровни
# --------------------------------------------------------------------------- #

_CODE_EXT = ("py", "pyi", "js", "jsx", "ts", "tsx", "vue", "md", "rst", "txt", "go", "java", "kt", "rb",
             "php", "html", "htm", "css", "scss", "sql", "yml", "yaml", "json", "toml", "ini", "cfg", "sh",
             "xml", "swift", "rs", "c", "h", "cpp", "hpp", "cs", "conf", "j2", "jinja", "lua", "gradle")
_FILE_RE = re.compile(r"`?((?:[\w.-]+/)*[\w.-]+\.(?:" + "|".join(_CODE_EXT) + r"))(?::(\d+))?(?:[-–,]\d+)*`?",
                      re.IGNORECASE)
_WORD_RE = re.compile(r"[^\W\d_][\w]*", re.UNICODE)
_STOP = {"нет", "для", "при", "как", "что", "это", "или", "так", "его", "она", "они", "без", "уже", "все",
         "всё", "тоже", "если", "надо", "нужно", "the", "and", "for", "not", "with", "from", "this", "that"}


def find_file(text: str) -> tuple[str, Optional[int]]:
    """Первый путь вида ``dir/file.py:120`` (или ``file.py``) в тексте находки."""
    m = _FILE_RE.search(text or "")
    if not m:
        return "", None
    return m.group(1), (int(m.group(2)) if m.group(2) else None)


def words(text: str, file: str = "") -> set[str]:
    """Слова для сопоставления: без путей и коротких, обрезаны до 5 букв (грубый стем)."""
    plain = _FILE_RE.sub(" ", text or "")
    out = {w.lower()[:5] for w in _WORD_RE.findall(plain) if len(w) >= 3 and w.lower() not in _STOP}
    return out


def level_of(raw: str) -> str:
    key = (raw or "").strip().lower()
    if key in LEVEL_RANK:
        return key
    return LEVEL_ALIASES.get(key, "minor")


_IDENT_RE = re.compile(r"\b(?:_*[a-z][a-z0-9]*_[a-z0-9_]+|[A-Z][a-z0-9]+[A-Z]\w*|[a-z]+[A-Z]\w*)\b")


def identifiers(text: str) -> set[str]:
    """Имена из кода в тексте (snake_case, CamelCase, camelCase) — для отсева разных находок."""
    return {m.lower().strip("_") for m in _IDENT_RE.findall(_FILE_RE.sub(" ", text or ""))}


def similarity(text_a: str, text_b: str, file_a: str = "", file_b: str = "") -> float:
    """Похожесть двух находок, 0…1+: доля общих слов от меньшего набора.

    Разные файлы — не одна находка; есть имена из кода в обоих текстах, но ни одного общего —
    тоже. Меньше двух общих слов — не одна; ровно два — только если это почти все слова короткой.
    """
    if file_a and file_b and PurePosixPath(file_a).name != PurePosixPath(file_b).name:
        return 0.0
    ia, ib = identifiers(text_a), identifiers(text_b)
    if ia and ib and not ia & ib:
        return 0.0
    a, b = words(text_a), words(text_b)
    common = len(a & b)
    if common < 2 or not a or not b:
        return 0.0
    score = common / min(len(a), len(b))
    if common == 2 and score < 0.6:
        return 0.0
    if file_a and file_b:
        score += 0.1
    return score


# --------------------------------------------------------------------------- #
# Разбор записи kind=review журнала работы (формат скилла jwu-job-review)
# --------------------------------------------------------------------------- #

_ITEM_SPLIT = re.compile(r"\[([BIM])(\d+)\]")


def parse_local_review(text: str) -> dict:
    """Запись ``kind=review`` → {reviewer, pr, iteration, verdict, findings: [{anchor, level, text, file, line}]}.

    Шапка: «Ревьюер: <агент>; ветка …; PR #N; итерация k; вердикт: …; блокеры …». Замечания —
    ``[B1] …`` / ``[I1] …`` / ``[M1] …`` после «Замечания:», по строке или подряд в одной.
    """
    text = text or ""
    head, _, body = text.partition("Замечания:")
    out: dict = {"reviewer": "", "pr": None, "iteration": None, "verdict": "", "findings": []}
    m = re.search(r"Ревьюер:\s*([^;\n]+)", head)
    if m:
        out["reviewer"] = m.group(1).strip()
    m = re.search(r"PR\s*#(\d+)", head)
    if m:
        out["pr"] = int(m.group(1))
    m = re.search(r"итерация\s*(\d+)", head)
    if m:
        out["iteration"] = int(m.group(1))
    m = re.search(r"вердикт:\s*([^;\n]+)", head)
    if m:
        out["verdict"] = m.group(1).strip()
    parts = _ITEM_SPLIT.split(body)
    for letter, num, item in zip(parts[1::3], parts[2::3], parts[3::3]):
        item = " ".join(item.split()).strip(" ;-*")
        if not item:
            continue
        file, line = find_file(item)
        out["findings"].append({"anchor": f"{letter}{num}", "level": level_of(letter), "text": item,
                                "file": file, "line": line})
    return out


# --------------------------------------------------------------------------- #
# Запись
# --------------------------------------------------------------------------- #


@dataclass
class PRRef:
    pr_id: int
    project: str = ""
    repo: str = ""

    def where(self) -> tuple[str, list]:
        return ("r.workspace_id = ? AND r.pr_id = ? AND r.project = ? AND r.repo = ?",
                [None, self.pr_id, self.project or "", self.repo or ""])


def _pr_where(store: "Store", pr: PRRef) -> tuple[str, list]:
    sql, params = pr.where()
    params[0] = store.workspace_id
    return sql, params


def _findings(store: "Store", pr: PRRef) -> list[dict]:
    sql, params = _pr_where(store, pr)
    rows = store.conn.execute(
        "SELECT f.*, r.side, r.label, r.full_pass, r.commit_sha, r.ts, r.verdict, r.ref, r.id AS rid"
        f" FROM review_findings f JOIN review_rounds r ON r.id = f.round_id WHERE {sql}"
        " ORDER BY r.ts, r.id, f.id", params).fetchall()
    return [dict(r) for r in rows]


def add_round(store: "Store", pr: PRRef, *, side: str, label: str = "", findings: Iterable[dict] = (),
              commit: str = "", ts: str = "", verdict: str = "", ref: str = "", source_key: str = "",
              full_pass: bool = True, by: str = "") -> dict:
    """Добавить круг ревью с находками. Повторный ``source_key`` — ничего не делает (импорт идемпотентен).

    findings — [{level, text, file?, line?, anchor?, fate?, reason?, fate_ref?, fate_commit?}];
    file:line без поля берётся из текста. Каждая находка сопоставляется с прошлыми кругами
    этого PR: похожая — звено её цепочки (``same_as``).
    → {round_id, added, matched: [{id, same_as}], skipped: bool}
    """
    ensure_schema(store.conn)
    if side not in SIDES:
        raise ReviewError(f"side: {' | '.join(SIDES)}")
    if source_key:
        row = store.conn.execute("SELECT id FROM review_rounds WHERE workspace_id = ? AND source_key = ?",
                                 (store.workspace_id, source_key)).fetchone()
        if row:
            return {"round_id": row["id"], "added": 0, "matched": [], "skipped": True}
    ts = _ts(ts)
    earlier = [f for f in _findings(store, pr) if f["ts"] <= ts]
    cur = store.conn.execute(
        "INSERT INTO review_rounds (workspace_id, project, repo, pr_id, side, label, full_pass, commit_sha, ts,"
        " verdict, ref, source_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (store.workspace_id, pr.project or "", pr.repo or "", pr.pr_id, side, label.strip(), int(full_pass),
         commit or "", ts, verdict or "", ref or "", source_key or "", _now()))
    round_id = cur.lastrowid
    added, matched = 0, []
    for item in findings:
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            continue
        file, line = item.get("file") or "", item.get("line")
        if not file:
            file, line = find_file(text)
        ws = words(text, file)
        best, best_score = None, MATCH_MIN
        for prev in earlier:
            score = similarity(text, prev["text"], file, prev["file"])
            if score >= best_score:
                best, best_score = prev, score
        fate = item.get("fate") or ""
        if fate and fate not in FATES:
            raise ReviewError(f"fate: {' | '.join(FATES)}")
        fcur = store.conn.execute(
            "INSERT INTO review_findings (round_id, anchor, level, text, file, line, words, same_as, fate,"
            " fate_reason, fate_ref, fate_commit, fate_by, fate_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (round_id, item.get("anchor") or "", level_of(item.get("level") or "minor"), text, file,
             int(line) if line else None, " ".join(sorted(ws)), best["id"] if best else None, fate,
             item.get("reason") or "", item.get("fate_ref") or "", item.get("fate_commit") or "",
             (by or label) if fate else "", ts if fate else ""))
        added += 1
        if best:
            matched.append({"id": fcur.lastrowid, "same_as": best["id"]})
    store.conn.commit()
    return {"round_id": round_id, "added": added, "matched": matched, "skipped": False}


def set_fate(store: "Store", ids: Iterable[int], fate: str, *, reason: str = "", ref: str = "",
             commit: str = "", by: str = "", same_as: Optional[int] = None) -> list[int]:
    """Поставить судьбу находкам (``F12`` или 12). ``fate=""`` — снять. duplicate — с ``same_as``.

    rejected / out_of_pr / fabricated — только с доводом: без него следующий ревьюер не поймёт,
    почему не поднимать.
    """
    ensure_schema(store.conn)
    if fate and fate not in FATES:
        raise ReviewError(f"fate: {' | '.join(FATES)} (или пусто — снять)")
    if fate in REJECTED and not reason.strip():
        raise ReviewError(f"«{FATE_RU[fate]}» — только с доводом (reason): иначе его поднимут снова")
    if fate == "duplicate" and not same_as:
        raise ReviewError("duplicate — укажи same_as: номер находки, дублем которой эта является")
    done = []
    for raw in ids:
        fid = _fid(raw)
        row = store.conn.execute(
            "SELECT f.id FROM review_findings f JOIN review_rounds r ON r.id = f.round_id"
            " WHERE f.id = ? AND r.workspace_id = ?", (fid, store.workspace_id)).fetchone()
        if not row:
            raise ReviewError(f"находки F{fid} нет в этом воркспейсе")
        store.conn.execute(
            "UPDATE review_findings SET fate = ?, fate_reason = ?, fate_ref = ?, fate_commit = ?, fate_by = ?,"
            " fate_at = ? WHERE id = ?", (fate, reason.strip(), ref, commit, by, _now() if fate else "", fid))
        if same_as:
            link(store, fid, same_as)
        done.append(fid)
    store.conn.commit()
    return done


def link(store: "Store", fid, same_as) -> None:
    """Руками: находка ``fid`` — та же проблема, что ``same_as`` (или ``None`` — отвязать)."""
    fid = _fid(fid)
    target = _fid(same_as) if same_as else None
    if target is not None:
        seen, cur = {fid}, target
        while cur is not None:
            if cur in seen:
                raise ReviewError("такая привязка замыкает круг")
            seen.add(cur)
            row = store.conn.execute("SELECT same_as FROM review_findings WHERE id = ?", (cur,)).fetchone()
            if not row:
                raise ReviewError(f"находки F{cur} нет")
            cur = row["same_as"]
    store.conn.execute("UPDATE review_findings SET same_as = ? WHERE id = ?", (target, fid))
    store.conn.commit()


def _fid(raw) -> int:
    text = str(raw).strip().lstrip("Ff#")
    if not text.isdigit():
        raise ReviewError(f"номер находки — F12 или 12, а не «{raw}»")
    return int(text)


# --------------------------------------------------------------------------- #
# Наполнение из журнала работ и комментов PR
# --------------------------------------------------------------------------- #


def import_job_reviews(store: "Store", pr: PRRef) -> int:
    """Записи ``kind=review`` работ, привязанных к PR, → круги ``local`` (по одному на запись).

    Берутся только записи с шапкой скилла jwu-job-review («Ревьюер: …»); свободный текст пропускается.
    """
    ensure_schema(store.conn)
    added = 0
    for job in store.jobs_for_pr(pr.pr_id, pr.project, pr.repo):
        for rec in job.records:
            if rec.kind != "review":
                continue
            parsed = parse_local_review(rec.text)
            if not parsed["reviewer"]:      # свободный текст без шапки скилла — не круг ревью
                continue
            if parsed["pr"] is not None and parsed["pr"] != pr.pr_id:
                continue
            iteration = f" · итерация {parsed['iteration']}" if parsed["iteration"] else ""
            res = add_round(store, pr, side="local", label=parsed["reviewer"],
                            findings=parsed["findings"], commit=rec.commit, ts=rec.ts,
                            verdict=parsed["verdict"], ref=f"работа #{job.id}{iteration}",
                            source_key=f"job_record:{rec.id}")
            added += 0 if res["skipped"] else 1
    return added


def import_pr_comments(store: "Store", pr: PRRef, comments: list, me: str = "") -> int:
    """Треды PR → находки ``external`` (подпись — автор).

    Берутся треды верхнего уровня не от меня: на строку кода (file) или с задачами на них.
    Задачи треда все закрыты, а судьбы ещё нет — «исправлено» (по задачам PR).
    """
    ensure_schema(store.conn)
    me = (me or "").casefold()
    added = 0
    for c in comments:
        if getattr(c, "depth", 0) or (me and (c.author or "").casefold() == me):
            continue
        if not (c.file or c.tasks):
            continue
        text = " ".join((c.text or "").split())
        if not text:
            continue
        loc = f"{c.file}:{c.line}" if c.file and c.line else (c.file or "")
        res = add_round(store, pr, side="external", label=c.author or "?", full_pass=False,
                        findings=[{"text": text, "file": c.file, "line": c.line, "level": "important"}],
                        ts=_ts(c.created), ref=f"коммент {c.id}" + (f" ({loc})" if loc else ""),
                        source_key=f"pr_comment:{pr.project}/{pr.repo}#{pr.pr_id}:{c.id}")
        added += 0 if res["skipped"] else 1
        if c.tasks and all(t.resolved for t in c.tasks):
            row = store.conn.execute("SELECT id, fate FROM review_findings WHERE round_id = ?",
                                     (res["round_id"],)).fetchone()
            if row and not row["fate"]:
                store.conn.execute("UPDATE review_findings SET fate = 'fixed', fate_reason = ?, fate_by = ?,"
                                   " fate_at = ? WHERE id = ?",
                                   ("задачи на комменте закрыты", "jwu", _now(), row["id"]))
                store.conn.commit()
    return added


# --------------------------------------------------------------------------- #
# Цепочки и состояние
# --------------------------------------------------------------------------- #


@dataclass
class Issue:
    """Одна проблема через все круги: звенья цепочки ``same_as`` и что с ней сейчас."""
    members: list[dict] = field(default_factory=list)
    state: str = "open"                 # open | gone | fixed | reopened | rejected | out_of_pr | fabricated | duplicate
    fate_row: Optional[dict] = None     # находка, на которой стоит действующая судьба
    repeats_after: list[dict] = field(default_factory=list)   # звенья после судьбы
    gone_round: Optional[dict] = None   # для gone: первый круг того же ревьюера, где её уже нет

    @property
    def last(self) -> dict:
        return self.members[-1]

    @property
    def level(self) -> str:
        return min((m["level"] for m in self.members), key=lambda lvl: LEVEL_RANK.get(lvl, 9))


def issues(store: "Store", pr: PRRef) -> tuple[list[Issue], list[dict]]:
    """Находки PR → проблемы (цепочки) с состоянием; второе — круги PR."""
    ensure_schema(store.conn)
    rows = _findings(store, pr)
    by_id = {r["id"]: r for r in rows}

    def root(r: dict) -> int:
        seen = set()
        while r["same_as"] in by_id and r["id"] not in seen:
            seen.add(r["id"])
            r = by_id[r["same_as"]]
        return r["id"]

    groups: dict[int, list[dict]] = {}
    for r in rows:
        groups.setdefault(root(r), []).append(r)
    sql, params = _pr_where(store, pr)
    rounds = [dict(r) for r in store.conn.execute(
        f"SELECT r.* FROM review_rounds r WHERE {sql} ORDER BY r.ts, r.id", params).fetchall()]
    last_round: dict[tuple, int] = {}
    by_src: dict[tuple, list[dict]] = {}
    for rd in rounds:
        if rd["full_pass"]:
            last_round[(rd["side"], rd["label"])] = rd["id"]
            by_src.setdefault((rd["side"], rd["label"]), []).append(rd)

    out: list[Issue] = []
    for members in groups.values():
        issue = Issue(members=members)
        fated = [m for m in members if m["fate"]]
        if fated:
            issue.fate_row = max(fated, key=lambda m: m["fate_at"])
            fate = issue.fate_row["fate"]
            issue.repeats_after = [m for m in members if m["ts"] > issue.fate_row["ts"]
                                   and m["rid"] != issue.fate_row["rid"]]
            if fate == "fixed" and issue.repeats_after:
                issue.state = "reopened"
            else:
                issue.state = fate
        else:
            last = issue.last
            gone = last["full_pass"] and last_round.get((last["side"], last["label"])) != last["rid"]
            issue.state = "gone" if gone else "open"
            if gone:
                issue.gone_round = next((rd for rd in by_src[(last["side"], last["label"])]
                                         if (rd["ts"], rd["id"]) > (last["ts"], last["rid"])), None)
        out.append(issue)
    out.sort(key=lambda i: (LEVEL_RANK.get(i.level, 9), i.last["ts"]), reverse=False)
    return out, rounds


# --------------------------------------------------------------------------- #
# Выжимка
# --------------------------------------------------------------------------- #


def _day(ts: str) -> str:
    return ts[8:10] + "." + ts[5:7] if len(ts) >= 10 else ts


def _src(m: dict) -> str:
    return f"{m['side']} {m['label']}".strip() + f" {_day(m['ts'])}"


def _loc(m: dict) -> str:
    if not m["file"]:
        return ""
    return f" — `{m['file']}{':' + str(m['line']) if m['line'] else ''}`"


def _line(issue: Issue, extra: str = "") -> str:
    m = issue.last
    seen = len({(x["side"], x["label"], x["rid"]) for x in issue.members})
    times = f" · поднимали {seen}×" if seen > 1 else ""
    return f"- F{m['id']} [{issue.level}] {m['text']}{_loc(m)} · {_src(m)}{times}{extra}"


def _fate_tail(issue: Issue) -> str:
    f = issue.fate_row or {}
    bits = [FATE_RU.get(f.get("fate", ""), f.get("fate", ""))]
    if f.get("fate_commit"):
        bits.append(f["fate_commit"][:10])
    if f.get("fate_reason"):
        bits.append(f"«{f['fate_reason']}»")
    if f.get("fate_ref"):
        bits.append(f["fate_ref"])
    return " → " + ", ".join(b for b in bits if b)


def _gone_tail(issue: Issue) -> str:
    rd = issue.gone_round
    if not rd:
        return ""
    at = f" на {rd['commit_sha'][:10]}" if rd["commit_sha"] else ""
    return f" → нет в круге {_day(rd['ts'])}{at}"


def history(store: "Store", pr: PRRef, max_chars: int = MAX_CHARS) -> dict:
    """Выжимка для ревьюера: отклонено и почему · исправлено (проверить регрессию) · открыто.

    → {markdown, rounds, counts, rejected_repeats, findings: [{id, state, level, text, file, line, side, label}]}
    Объём ограничен ``max_chars``: не влезшее и мелкое в «исправлено»/«снято» — счётчиками.
    """
    cache = issues(store, pr)
    found, rounds = cache
    name = f"{pr.project}/{pr.repo} " if pr.project or pr.repo else ""
    if not rounds:
        md = (f"## История ревью PR {name}#{pr.pr_id}\n\nКругов ревью нет: ни записей ревью в работах по "
              "этому PR, ни замечаний в тредах.")
        return {"markdown": md, "rounds": 0, "counts": {}, "rejected_repeats": 0, "findings": []}

    counts: dict[str, int] = {}
    for i in found:
        counts[i.state] = counts.get(i.state, 0) + 1
    by_src: dict[tuple, list] = {}
    for rd in rounds:
        by_src.setdefault((rd["side"], rd["label"]), []).append(rd)
    src_line = " · ".join(
        f"{side} {label} ×{len(rds)} (последний {_day(rds[-1]['ts'])}"
        + (f", {rds[-1]['commit_sha'][:10]}" if rds[-1]["commit_sha"] else "")
        + (f", {rds[-1]['verdict']}" if rds[-1]["verdict"] else "") + ")"
        for (side, label), rds in by_src.items())
    rejected_repeats = sum(len(i.repeats_after) for i in found if i.state in REJECTED)

    head = [f"## История ревью PR {name}#{pr.pr_id}: {len(rounds)} круг(ов), {len(found)} проблем(ы)",
            "", f"Круги: {src_line}"]
    if rejected_repeats:
        head.append(f"Повтор отклонённого: {rejected_repeats} — не поднимать без нового довода.")
    diff = round_diff(store, pr, _cache=cache)
    cmp = compare(store, pr, _cache=cache)
    if diff:
        head.append(f"Последний внешний круг — {diff['summary']}.")
    if cmp:
        head.append(f"Сверка — {cmp['summary']}.")
    miss = misses_line(store)
    if miss:
        head.append(miss + ".")
    budget = max_chars - sum(len(x) + 1 for x in head) - 200

    sections: list[tuple[str, list[str], str]] = []
    rej = [i for i in found if i.state in REJECTED]
    sections.append(("### Отклонено — не поднимать без нового довода",
                     [_line(i, _fate_tail(i) + (f" · после этого поднимали ещё {len(i.repeats_after)}×"
                                                if i.repeats_after else "")) for i in rej], ""))
    reo = [i for i in found if i.state == "reopened"]
    sections.append(("### Было исправлено, но подняли снова — проверить регрессию",
                     [_line(i, _fate_tail(i)) for i in reo], ""))
    opn = [i for i in found if i.state == "open"]
    sections.append(("### Открыто", [_line(i) for i in opn], ""))
    fix = [i for i in found if i.state == "fixed"]
    fix_big = [i for i in fix if LEVEL_RANK.get(i.level, 9) < LEVEL_RANK["minor"]]
    sections.append(("### Исправлено на прошлых кругах — не сломано ли, нет ли регрессии от фикса",
                     [_line(i, _fate_tail(i)) for i in fix_big],
                     f"и ещё {len(fix) - len(fix_big)} мелких" if len(fix) > len(fix_big) else ""))
    gone = [i for i in found if i.state == "gone"]
    gone_big = [i for i in gone if LEVEL_RANK.get(i.level, 9) < LEVEL_RANK["minor"]]
    sections.append(("### Ушло по ходу кругов (тот же ревьюер больше не поднимал; судьбы нет)",
                     [_line(i, _gone_tail(i)) for i in gone_big],
                     f"и ещё {len(gone) - len(gone_big)} мелких" if len(gone) > len(gone_big) else ""))

    body: list[str] = []
    for title, lines, tail in sections:
        if not lines and not tail:
            continue
        chunk = ["", title]
        shown = 0
        for ln in lines:
            if budget - len(ln) - 1 < 0:
                break
            chunk.append(ln)
            budget -= len(ln) + 1
            shown += 1
        rest = len(lines) - shown
        tails = [t for t in (f"ещё {rest} не влезло" if rest else "", tail) if t]
        if tails:
            chunk.append(f"- … {'; '.join(tails)}")
        budget -= len(title) + 2
        body += chunk
    md = "\n".join(head + body)
    findings = [{"id": i.last["id"], "state": i.state, "level": i.level, "text": i.last["text"],
                 "file": i.last["file"], "line": i.last["line"], "side": i.last["side"],
                 "label": i.last["label"], "times": len(i.members)} for i in found]
    return {"markdown": md, "rounds": len(rounds), "counts": counts, "rejected_repeats": rejected_repeats,
            "last_external": diff, "compare": cmp, "findings": findings}


# --------------------------------------------------------------------------- #
# Круг против прошлого круга того же ревьюера; local против external; пропуски
# --------------------------------------------------------------------------- #

def _rounds_of(rounds: list[dict], side: str) -> list[dict]:
    return [r for r in rounds if r["side"] == side and r["full_pass"]]


def round_diff(store: "Store", pr: PRRef, round_id: Optional[int] = None,
               _cache: Optional[tuple] = None) -> Optional[dict]:
    """Круг (по умолчанию — последний внешний полный) против прошлого круга того же ревьюера:
    новое · повтор · повтор отклонённого · исчезло. None — таких кругов нет."""
    found, rounds = _cache or issues(store, pr)
    full = [r for r in rounds if r["full_pass"]]
    if round_id is None:
        ext = _rounds_of(rounds, "external")
        if not ext:
            return None
        rd = ext[-1]
    else:
        rd = next((r for r in full if r["id"] == round_id), None)
        if rd is None:
            return None
    same = [r for r in full if (r["side"], r["label"]) == (rd["side"], rd["label"]) and
            (r["ts"], r["id"]) < (rd["ts"], rd["id"])]
    prev = same[-1] if same else None
    out = {"round_id": rd["id"], "label": rd["label"], "side": rd["side"], "ts": rd["ts"],
           "commit": rd["commit_sha"], "ref": rd["ref"], "new": [], "repeat": [], "repeat_rejected": [],
           "gone": []}
    here: set[int] = set()
    for issue in {id(i): i for i in found}.values():
        mine = [m for m in issue.members if m["rid"] == rd["id"]]
        if not mine:
            continue
        here.add(id(issue))
        earlier = [m for m in issue.members if (m["ts"], m["rid"]) < (rd["ts"], rd["id"])]
        if any(m["fate"] in REJECTED for m in earlier):
            out["repeat_rejected"].append(mine[0]["id"])
        elif earlier:
            out["repeat"].append(mine[0]["id"])
        else:
            out["new"].append(mine[0]["id"])
    if prev:
        for issue in {id(i): i for i in found}.values():
            if id(issue) not in here and any(m["rid"] == prev["id"] for m in issue.members):
                out["gone"].append(issue.members[-1]["id"])
    out["summary"] = (f"{rd['side']} {rd['label']} {_day(rd['ts'])}: новых {len(out['new'])}, "
                      f"повтор {len(out['repeat'])}, повтор отклонённого {len(out['repeat_rejected'])}"
                      + (f", исчезло с прошлого круга {len(out['gone'])}" if prev else ""))
    return out


def compare(store: "Store", pr: PRRef, external_round_id: Optional[int] = None,
            _cache: Optional[tuple] = None) -> Optional[dict]:
    """Наше ревью против внешнего на том же коде: совпало · только local · только external.

    Внешний круг — последний полный (или указанный); локальные — все полные круги до него на
    том же коммите, а если коммиты не записаны или не совпали — последний круг каждого нашего
    ревьюера до него. Только external делится на «по делу» и отклонённое (вне PR, не
    подтвердилось, отклонено). None — сравнивать не с чем.
    """
    found, rounds = _cache or issues(store, pr)
    ext = _rounds_of(rounds, "external")
    if external_round_id is not None:
        ext = [r for r in ext if r["id"] == external_round_id]
    if not ext:
        return None
    rd = ext[-1]
    before = [r for r in _rounds_of(rounds, "local") if (r["ts"], r["id"]) <= (rd["ts"], rd["id"])]
    if not before:
        return None
    commit = (rd["commit_sha"] or "")[:10]
    same_commit = [r for r in before if commit and (r["commit_sha"] or "")[:10] == commit]
    if same_commit:
        local = same_commit
    else:
        last: dict[str, dict] = {}
        for r in before:
            last[r["label"]] = r
        local = list(last.values())
    local_ids = {r["id"] for r in local}
    both, only_local, only_ext, ext_rejected = [], [], [], []
    for issue in {id(i): i for i in found}.values():
        in_ext = [m for m in issue.members if m["rid"] == rd["id"]]
        # «видел ли наш ревьюер»: звено цепочки в выбранных локальных кругах или раньше них
        in_local = [m for m in issue.members if m["rid"] in local_ids or
                    (m["side"] == "local" and (m["ts"], m["rid"]) <= (rd["ts"], rd["id"]))]
        if in_ext and in_local:
            both.append(in_ext[0]["id"])
        elif in_ext:
            (ext_rejected if issue.state in REJECTED or issue.state == "duplicate" else only_ext).append(in_ext[0]["id"])
        elif any(m["rid"] in local_ids for m in issue.members):
            only_local.append(issue.members[-1]["id"])
    out = {"external_round": rd["id"], "external": f"{rd['label']} {_day(rd['ts'])}",
           "local_rounds": sorted(local_ids), "local": ", ".join(sorted({r["label"] for r in local})),
           "same_commit": bool(same_commit), "matched": both, "only_local": only_local,
           "only_external": only_ext, "only_external_rejected": ext_rejected}
    out["summary"] = (f"local ({out['local']}) против external {out['external']}"
                      + ("" if same_commit else " (коммиты не сверены — по последним кругам)")
                      + f": совпало {len(both)}, только local {len(only_local)}, только external "
                        f"{len(only_ext)}" + (f" (+{len(ext_rejected)} отклонённых)" if ext_rejected else ""))
    return out


CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("длина строк и форматирование", ("длин строк", "длиннее", "символов", "line length", "отступ", "pep8",
                                      "форматир", "пробел")),
    ("документация", ("readme", "docstring", "докстр", "документ", "описани", "комментари", ".md", ".rst")),
    ("нейминг", ("нейминг", "назван", "переимен", "имя ", "имени", "имена", "naming", "rename")),
    ("тесты", ("тест", "test", "покрыт")),
    ("безопасность", ("безопас", "xss", "инъекц", "csrf", "секрет", "утечк", "auth", "авториз", "токен")),
    ("права и окружение", ("права", "прав ", "владел", "chmod", "owner", "permission", "окружени", "стенд",
                           "конфиг", "nginx", "docker")),
    ("конкурентность", ("гонк", "race", "лок ", "лока", "lock", "параллел", "конкурент", "транзакц", "атомарн")),
    ("производительность", ("n+1", "производител", "медлен", "в цикле", "индекс", "perf")),
    ("переводы", ("перевод", "локализ", "i18n", "translation")),
    ("правила проекта", ("правил", "конвенц", "соглашени")),
]


def category(text: str, file: str = "") -> str:
    hay = f" {(text or '').lower()} {(file or '').lower()} "
    for name, keys in CATEGORIES:
        if any(k in hay for k in keys):
            return name
    return "логика и баги"


def misses(store: "Store", days: int = 30, examples: int = 3) -> dict:
    """Что нашёл внешний ревьюер, а наш на том же коде пропустил — по всем PR воркспейса за ``days``.

    Отклонённое, «вне PR», «не подтвердилось» и дубли в пропуски не идут.
    → {prs, compared, matched, only_local, only_external, categories: [{name, count, examples}], markdown}
    """
    ensure_schema(store.conn)
    since = datetime.now(timezone.utc).timestamp() - days * 86400
    since_iso = datetime.fromtimestamp(since, timezone.utc).isoformat(timespec="seconds")
    prs = store.conn.execute(
        "SELECT DISTINCT project, repo, pr_id FROM review_rounds WHERE workspace_id = ? AND side = 'external'"
        " AND full_pass = 1 AND ts >= ?", (store.workspace_id, since_iso)).fetchall()
    totals = {"matched": 0, "only_local": 0, "only_external": 0}
    cats: dict[str, list[dict]] = {}
    compared = 0
    for row in prs:
        ref = PRRef(row["pr_id"], row["project"], row["repo"])
        cache = issues(store, ref)
        index = {m["id"]: m for i in cache[0] for m in i.members}
        owner = {m["id"]: n for n, i in enumerate(cache[0]) for m in i.members}
        seen: set[int] = set()
        for rd in _rounds_of(cache[1], "external"):
            if rd["ts"] < since_iso:
                continue
            cmp = compare(store, ref, rd["id"], _cache=cache)
            if not cmp:
                continue
            compared += 1
            totals["matched"] += len(cmp["matched"])
            totals["only_local"] += len(cmp["only_local"])
            for fid in cmp["only_external"]:
                m = index[fid]
                root = owner[fid]
                if root in seen:           # одна проблема в нескольких внешних кругах — один пропуск
                    continue
                seen.add(root)
                totals["only_external"] += 1
                cats.setdefault(category(m["text"], m["file"]), []).append(
                    {"pr": ref.pr_id, "repo": ref.repo, "id": fid, "level": m["level"], "text": m["text"],
                     "file": m["file"]})
    ranked = sorted(cats.items(), key=lambda kv: -len(kv[1]))
    out = {"days": days, "prs": len(prs), "compared": compared, **totals,
           "categories": [{"name": k, "count": len(v), "examples": v[:examples]} for k, v in ranked]}
    lines = [f"## Пропуски нашего ревью за {days} дн.: PR {len(prs)}, сравнено кругов {compared}", "",
             f"Совпало {totals['matched']} · только local {totals['only_local']} · только external "
             f"(по делу) {totals['only_external']}."]
    if not compared:
        lines.append("\nСравнивать не с чем: нужны внешний полный круг и наш круг до него по тому же PR.")
    for c in out["categories"]:
        lines += ["", f"### {c['name']} — {c['count']}"]
        lines += [f"- {e['repo']}#{e['pr']} F{e['id']} [{e['level']}] {e['text'][:160]}" for e in c["examples"]]
    if out["categories"]:
        lines += ["", "Предложение: дополнить описание агента-ревьюера проверками по верхним категориям "
                      "(примеры выше). Правит пользователь."]
    out["markdown"] = "\n".join(lines)
    return out


def misses_line(store: "Store", days: int = 30) -> str:
    """Одна строка для выжимки: верхние категории пропусков нашего ревью по воркспейсу."""
    try:
        rep = misses(store, days, examples=0)
    except Exception:  # noqa: BLE001 — строка-подсказка, не повод ронять выжимку
        return ""
    top = [f"{c['name']} {c['count']}" for c in rep["categories"][:4]]
    return f"Чаще всего наше ревью пропускает ({days} дн.): " + ", ".join(top) if top else ""


def brief(store: "Store", pr: PRRef) -> Optional[dict]:
    """Коротко для сборок и доски, без сети: последний внешний круг против прошлого. None — кругов нет."""
    ensure_schema(store.conn)
    cache = issues(store, pr)
    if not cache[1]:
        return None
    diff = round_diff(store, pr, _cache=cache)
    open_n = sum(1 for i in cache[0] if i.state in ("open", "reopened"))
    out = {"open": open_n, "rounds": len(cache[1]),
           "repeat_rejected": len(diff["repeat_rejected"]) if diff else 0,
           "last_external": diff["summary"] if diff else ""}
    return out
