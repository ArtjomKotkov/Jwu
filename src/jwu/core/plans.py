"""Планы воркспейса: дерево шагов с зависимостями, ожиданиями, доказательствами и журналом.

План связывает работы, очередь и фичи и отвечает на «что дальше, что ждёт меня, что уже
сделано и чем это проверено». Шаг знает, чего ждёт: других шагов (``after``, в том числе из
другого плана) и прочего (``waits``: вопрос, владелец, внешнее событие, ресурс). Состояния
«заблокирован» и «ждёт владельца» jwu вычисляет сам — руками их не ставят.

Правила, которые держит jwu (а не память сессии):

1. Нет ``done`` без доказательства: хотя бы одно, плюс виды из ``needs`` шага (``code`` —
   tests или build, ``ui`` — browser или screenshot). Ссылки доказательств проверяются:
   файл существует, коммит есть в репозитории воркспейса, у работы есть прогон тестов.
2. Нет ``done`` с остатком: непустой ``leftovers`` — отказ, пока каждый пункт не стал шагом.
3. Зависимости: в ``doing`` — только когда всё из ``after`` сделано и ожидания сняты;
   цикл в ``after`` — отказ; откат сделанного шага, на котором уже строят, — только с force.

Предложение целиком — docs/proposals/plans.md.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .store import Store

PLAN_STATUSES = ("active", "paused", "done", "dropped")
STEP_STATUSES = ("todo", "doing", "review", "done", "dropped")     # ставятся руками
DERIVED = ("blocked", "waiting_owner")                              # вычисляются
EXECUTORS_FIXED = ("main", "owner")                                 # плюс agent:<имя>
WHERE = ("none", "branch", "main", "stand", "prod")
EVIDENCE_KINDS = ("tests", "build", "stand", "browser", "screenshot", "review", "owner")
NEEDS_ALIASES = {"code": {"tests", "build"}, "ui": {"browser", "screenshot"}}
REF_REQUIRED = {"tests", "build", "stand", "browser", "screenshot"}  # без ссылки — «на слово»
WAIT_KINDS = ("question", "owner", "external", "resource")
OWNER_WAITS = ("question", "owner")
LINK_KEYS = ("jobs", "job_id", "worktree", "commits", "migrations", "issue", "files")   # job_id → jobs
OPEN = ("todo", "doing", "review")

ICON = {"todo": "⬜", "doing": "🔄", "review": "👀", "done": "✅", "dropped": "✖️",
        "blocked": "⛔", "waiting_owner": "🙋"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    num          INTEGER NOT NULL,
    title        TEXT NOT NULL,
    goal         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active',
    priority     INTEGER NOT NULL DEFAULT 0,
    focus        INTEGER NOT NULL DEFAULT 0,
    rules        TEXT NOT NULL DEFAULT '[]',
    needs        TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (workspace_id, num)
);
CREATE TABLE IF NOT EXISTS plan_steps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id       INTEGER NOT NULL,
    key           TEXT NOT NULL,
    title         TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '',
    parent_id     INTEGER,
    position      INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'todo',
    after         TEXT NOT NULL DEFAULT '[]',
    waits         TEXT NOT NULL DEFAULT '[]',
    together_with TEXT NOT NULL DEFAULT '[]',
    executor      TEXT NOT NULL DEFAULT 'main',
    model         TEXT NOT NULL DEFAULT '',
    est           TEXT NOT NULL DEFAULT '',
    spent         TEXT NOT NULL DEFAULT '',
    needs         TEXT NOT NULL DEFAULT '[]',
    evidence      TEXT NOT NULL DEFAULT '[]',
    links         TEXT NOT NULL DEFAULT '{}',
    where_        TEXT NOT NULL DEFAULT 'none',
    leftovers     TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL,
    started_at    TEXT NOT NULL DEFAULT '',
    finished_at   TEXT NOT NULL DEFAULT '',
    UNIQUE (plan_id, key)
);
CREATE INDEX IF NOT EXISTS idx_plan_steps_plan ON plan_steps(plan_id, position);
CREATE TABLE IF NOT EXISTS plan_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id  INTEGER NOT NULL,
    step_id  INTEGER,
    ts       TEXT NOT NULL,
    kind     TEXT NOT NULL,
    text     TEXT NOT NULL DEFAULT '',
    by       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_plan_events_plan ON plan_events(plan_id, id);
"""


class PlanError(ValueError):
    pass


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _j(value) -> str:
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Модель
# --------------------------------------------------------------------------- #


@dataclass
class Plan:
    id: int
    workspace_id: int
    num: int
    title: str
    goal: str = ""
    status: str = "active"
    priority: int = 0
    focus: bool = False
    rules: list = field(default_factory=list)
    needs: list = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    @property
    def key(self) -> str:
        return f"PLAN-{self.num}"

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "goal": self.goal, "status": self.status,
                "priority": self.priority, "focus": self.focus, "rules": self.rules, "needs": self.needs,
                "created_at": self.created_at, "updated_at": self.updated_at}


@dataclass
class Step:
    id: int
    plan_id: int
    key: str
    title: str
    detail: str = ""
    parent_id: Optional[int] = None
    position: int = 0
    status: str = "todo"
    after: list = field(default_factory=list)          # id шагов
    waits: list = field(default_factory=list)          # [{kind, text, resolved}]
    together_with: list = field(default_factory=list)  # id шагов
    executor: str = "main"
    model: str = ""
    est: str = ""
    spent: str = ""
    needs: list = field(default_factory=list)
    evidence: list = field(default_factory=list)       # [{kind, text, ref, at, by}]
    links: dict = field(default_factory=dict)
    where: str = "none"
    leftovers: list = field(default_factory=list)
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""


def _plan(row) -> Plan:
    return Plan(id=row["id"], workspace_id=row["workspace_id"], num=row["num"], title=row["title"],
                goal=row["goal"], status=row["status"], priority=row["priority"], focus=bool(row["focus"]),
                rules=json.loads(row["rules"] or "[]"), needs=json.loads(row["needs"] or "[]"),
                created_at=row["created_at"], updated_at=row["updated_at"])


def _step(row) -> Step:
    return Step(id=row["id"], plan_id=row["plan_id"], key=row["key"], title=row["title"], detail=row["detail"],
                parent_id=row["parent_id"], position=row["position"], status=row["status"],
                after=json.loads(row["after"] or "[]"), waits=json.loads(row["waits"] or "[]"),
                together_with=json.loads(row["together_with"] or "[]"), executor=row["executor"],
                model=row["model"], est=row["est"], spent=row["spent"], needs=json.loads(row["needs"] or "[]"),
                evidence=json.loads(row["evidence"] or "[]"), links=json.loads(row["links"] or "{}"),
                where=row["where_"], leftovers=json.loads(row["leftovers"] or "[]"), created_at=row["created_at"],
                started_at=row["started_at"], finished_at=row["finished_at"])


# --------------------------------------------------------------------------- #
# Чтение
# --------------------------------------------------------------------------- #


_PLAN_RE = re.compile(r"^\s*(?:PLAN-)?(\d+)\s*$", re.I)


def get_plan(store: "Store", key: str | int) -> Plan:
    ensure_schema(store.conn)
    m = _PLAN_RE.match(str(key))
    if not m:
        raise PlanError(f"План «{key}»: нужен ключ вида PLAN-3")
    row = store.conn.execute("SELECT * FROM plans WHERE workspace_id = ? AND num = ?",
                             (store.workspace_id, int(m.group(1)))).fetchone()
    if row is None:
        raise PlanError(f"Плана {str(key).upper()} в этом воркспейсе нет")
    return _plan(row)


def list_plans(store: "Store", status: Optional[str] = None) -> list[Plan]:
    ensure_schema(store.conn)
    rows = store.conn.execute(
        "SELECT * FROM plans WHERE workspace_id = ? AND (? IS NULL OR status = ?)"
        " ORDER BY focus DESC, priority DESC, num", (store.workspace_id, status, status)).fetchall()
    return [_plan(r) for r in rows]


def steps(store: "Store", plan: Plan) -> list[Step]:
    rows = store.conn.execute("SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY position, id",
                              (plan.id,)).fetchall()
    return [_step(r) for r in rows]


def _step_by_id(store: "Store", step_id: int) -> Optional[Step]:
    row = store.conn.execute("SELECT * FROM plan_steps WHERE id = ?", (step_id,)).fetchone()
    return _step(row) if row else None


def _plan_by_id(store: "Store", plan_id: int) -> Optional[Plan]:
    row = store.conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    return _plan(row) if row else None


def get_step(store: "Store", ref: str, plan: Optional[Plan] = None) -> tuple[Plan, Step]:
    """«PLAN-3/4а» — шаг по полному адресу; «4а» — в ``plan``."""
    ensure_schema(store.conn)
    ref = str(ref).strip()
    if "/" in ref:
        plan_key, step_key = ref.split("/", 1)
        plan = get_plan(store, plan_key)
    else:
        step_key = ref
    if plan is None:
        raise PlanError(f"Шаг «{ref}»: укажи план — PLAN-3/{ref}")
    row = store.conn.execute("SELECT * FROM plan_steps WHERE plan_id = ? AND key = ?",
                             (plan.id, step_key.strip())).fetchone()
    if row is None:
        raise PlanError(f"В {plan.key} нет шага «{step_key}»")
    return plan, _step(row)


def step_ref(store: "Store", step: Step, home: Optional[Plan] = None) -> str:
    """Адрес шага: «4а» внутри своего плана, «PLAN-2/5» — из чужого."""
    if home is not None and step.plan_id == home.id:
        return step.key
    plan = _plan_by_id(store, step.plan_id)
    return f"{plan.key}/{step.key}" if plan else f"#{step.id}"


def events(store: "Store", plan: Plan, *, since_id: int = 0, step: Optional[Step] = None,
           limit: int = 50) -> list[dict]:
    rows = store.conn.execute(
        "SELECT * FROM plan_events WHERE plan_id = ? AND id > ? AND (? IS NULL OR step_id = ?)"
        " ORDER BY id DESC LIMIT ?", (plan.id, since_id, step.id if step else None,
                                      step.id if step else None, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


# --------------------------------------------------------------------------- #
# Состояние шага: что держит
# --------------------------------------------------------------------------- #


def blockers(store: "Store", step: Step, home: Optional[Plan] = None) -> list[str]:
    """Чего шаг ждёт прямо сейчас — по-русски; пусто — ничего."""
    out = []
    for sid in step.after:
        dep = _step_by_id(store, sid)
        if dep is None:
            out.append(f"шаг #{sid} удалён — сними зависимость")
        elif dep.status == "dropped":
            out.append(f"отменённый шаг {step_ref(store, dep, home)} — сними зависимость или отмени и этот")
        elif dep.status != "done":
            out.append(f"шаг {step_ref(store, dep, home)} «{dep.title}» ({dep.status})")
    for w in step.waits:
        if not w.get("resolved"):
            out.append({"question": "ответ владельца", "owner": "владелец", "external": "внешнее",
                         "resource": "ресурс"}.get(w.get("kind"), w.get("kind", "?")) + f": {w.get('text', '')}")
    return out


def effective(store: "Store", step: Step, home: Optional[Plan] = None) -> str:
    """Статус для показа: открытый шаг с неснятыми ожиданиями — blocked или waiting_owner."""
    if step.status not in ("todo", "doing"):
        return step.status
    if any(not w.get("resolved") and w.get("kind") in OWNER_WAITS for w in step.waits):
        return "waiting_owner"
    if step.status == "todo" and blockers(store, step, home):
        return "blocked"
    return step.status


def _dependents(store: "Store", step: Step) -> list[Step]:
    rows = store.conn.execute("SELECT * FROM plan_steps WHERE after LIKE ?", (f"%{step.id}%",)).fetchall()
    return [s for s in map(_step, rows) if step.id in s.after]


def _children(store: "Store", step: Step) -> list[Step]:
    rows = store.conn.execute("SELECT * FROM plan_steps WHERE parent_id = ? ORDER BY position, id",
                              (step.id,)).fetchall()
    return [_step(r) for r in rows]


def _cycle(store: "Store", step_id: int, after: list[int]) -> Optional[list[int]]:
    """Путь, замыкающий круг, если ``step_id`` начнёт ждать ``after``."""
    stack = [(a, [step_id, a]) for a in after]
    seen = set()
    while stack:
        cur, path = stack.pop()
        if cur == step_id:
            return path
        if cur in seen:
            continue
        seen.add(cur)
        dep = _step_by_id(store, cur)
        for nxt in (dep.after if dep else []):
            stack.append((nxt, path + [nxt]))
    return None


# --------------------------------------------------------------------------- #
# Доказательства
# --------------------------------------------------------------------------- #


_SHA_RE = re.compile(r"^(?:commit:)?([0-9a-f]{7,40})$", re.I)


def _repo_paths(store: "Store") -> list[str]:
    try:
        return [p.path for p in store.workspace_paths(store.workspace_id)]
    except Exception:  # noqa: BLE001
        return []


def check_evidence(store: "Store", ev: dict) -> dict:
    """Проверить доказательство: вид, текст, ссылку. → нормализованное или PlanError."""
    kind = str(ev.get("kind") or "").strip()
    text = str(ev.get("text") or "").strip()
    ref = str(ev.get("ref") or "").strip()
    if kind not in EVIDENCE_KINDS:
        raise PlanError(f"доказательство: вид «{kind}» — бывает {', '.join(EVIDENCE_KINDS)}")
    if not text:
        raise PlanError(f"доказательство {kind}: нужен text — что именно проверено и с каким итогом")
    if kind in REF_REQUIRED and not ref:
        raise PlanError(f"доказательство {kind}: нужна ref — путь к файлу, коммит, job:<id> или ссылка; "
                        "без неё это «готово на слово»")
    if ref:
        if ref.startswith(("http://", "https://")):
            pass
        elif ref.startswith("job:"):
            try:
                job = store.get_job(int(ref[4:]))
            except ValueError:
                job = None
            if job is None:
                raise PlanError(f"доказательство {kind}: работы {ref} нет")
            if kind == "tests" and not any(r.kind == "test-pass" for r in job.records):
                raise PlanError(f"доказательство tests: в работе {ref} нет записи test-pass — "
                                "сначала запиши прогон тестов в журнал")
        elif _SHA_RE.match(ref):
            sha = _SHA_RE.match(ref).group(1)
            if not any(subprocess.run(["git", "-C", p, "cat-file", "-e", f"{sha}^{{commit}}"],
                                      capture_output=True).returncode == 0 for p in _repo_paths(store)):
                raise PlanError(f"доказательство {kind}: коммита {sha} нет в репозиториях воркспейса")
        else:
            path = Path(ref).expanduser()
            if not path.is_absolute():
                raise PlanError(f"доказательство {kind}: ref «{ref}» — абсолютный путь, коммит, job:<id> "
                                "или http(s)-ссылка")
            if not path.exists():
                raise PlanError(f"доказательство {kind}: файла {path} нет")
            ref = str(path)
    return {"kind": kind, "text": text, "ref": ref}


def _needs_met(needs: list, evidence: list) -> list[str]:
    have = {e["kind"] for e in evidence}
    missing = []
    for need in needs:
        options = NEEDS_ALIASES.get(need, {need})
        if not have & options:
            missing.append(need if need not in NEEDS_ALIASES else f"{need} ({' или '.join(sorted(options))})")
    return missing


# --------------------------------------------------------------------------- #
# Запись
# --------------------------------------------------------------------------- #


def _event(store: "Store", plan: Plan, step: Optional[Step], kind: str, text: str, by: str = "") -> None:
    store.conn.execute("INSERT INTO plan_events (plan_id, step_id, ts, kind, text, by) VALUES (?, ?, ?, ?, ?, ?)",
                       (plan.id, step.id if step else None, _now(), kind, text[:1000], by))


def _touch(store: "Store", plan: Plan) -> None:
    store.conn.execute("UPDATE plans SET updated_at = ? WHERE id = ?", (_now(), plan.id))


def _check_needs_list(needs: list) -> list:
    bad = [n for n in needs if n not in EVIDENCE_KINDS and n not in NEEDS_ALIASES]
    if bad:
        raise PlanError(f"needs: {', '.join(bad)} — бывает {', '.join(EVIDENCE_KINDS + tuple(NEEDS_ALIASES))}")
    return list(dict.fromkeys(needs))


def create_plan(store: "Store", title: str, goal: str = "", *, rules: Optional[list] = None,
                needs: Optional[list] = None, priority: int = 0, focus: bool = False, by: str = "") -> Plan:
    ensure_schema(store.conn)
    if not title.strip():
        raise PlanError("у плана нужен заголовок")
    num = (store.conn.execute("SELECT MAX(num) FROM plans WHERE workspace_id = ?",
                              (store.workspace_id,)).fetchone()[0] or 0) + 1
    now = _now()
    if focus:
        store.conn.execute("UPDATE plans SET focus = 0 WHERE workspace_id = ?", (store.workspace_id,))
    cur = store.conn.execute(
        "INSERT INTO plans (workspace_id, num, title, goal, priority, focus, rules, needs, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (store.workspace_id, num, title.strip(), goal.strip(), int(priority), int(focus),
         _j([r for r in (rules or []) if str(r).strip()]), _j(_check_needs_list(needs or [])), now, now))
    plan = _plan_by_id(store, cur.lastrowid)
    _event(store, plan, None, "plan", f"план создан: {plan.title}", by)
    store.conn.commit()
    return plan


def edit_plan(store: "Store", key: str, *, title: Optional[str] = None, goal: Optional[str] = None,
              status: Optional[str] = None, priority: Optional[int] = None, focus: Optional[bool] = None,
              rules: Optional[list] = None, needs: Optional[list] = None, by: str = "") -> Plan:
    plan = get_plan(store, key)
    changes = {}
    if title is not None:
        changes["title"] = title.strip() or plan.title
    if goal is not None:
        changes["goal"] = goal.strip()
    if status is not None:
        if status not in PLAN_STATUSES:
            raise PlanError(f"статус плана: {', '.join(PLAN_STATUSES)}")
        changes["status"] = status
    if priority is not None:
        changes["priority"] = int(priority)
    if focus is not None:
        if focus:
            store.conn.execute("UPDATE plans SET focus = 0 WHERE workspace_id = ?", (store.workspace_id,))
        changes["focus"] = int(focus)
    if rules is not None:
        changes["rules"] = _j([r for r in rules if str(r).strip()])
    if needs is not None:
        changes["needs"] = _j(_check_needs_list(needs))
    if changes:
        sets = ", ".join(f"{k} = ?" for k in changes)
        store.conn.execute(f"UPDATE plans SET {sets}, updated_at = ? WHERE id = ?",
                           (*changes.values(), _now(), plan.id))
        _event(store, plan, None, "plan", "план изменён: " + ", ".join(sorted(changes)), by)
        store.conn.commit()
    return get_plan(store, key)


def _resolve_refs(store: "Store", plan: Plan, refs: list) -> list[int]:
    out = []
    for ref in refs or []:
        _, st = get_step(store, str(ref), plan)
        out.append(st.id)
    return list(dict.fromkeys(out))


def _waits(items: list) -> list[dict]:
    out = []
    for w in items or []:
        if isinstance(w, str):
            w = {"kind": "external", "text": w}
        kind = str(w.get("kind") or "external")
        if kind not in WAIT_KINDS:
            raise PlanError(f"ожидание: вид «{kind}» — бывает {', '.join(WAIT_KINDS)} "
                            "(другие шаги — через after)")
        if not str(w.get("text") or "").strip():
            raise PlanError("ожидание: нужен text — чего именно ждём")
        out.append({"kind": kind, "text": str(w["text"]).strip(), "resolved": bool(w.get("resolved"))})
    return out


def _check_executor(executor: str) -> str:
    executor = (executor or "main").strip()
    if executor not in EXECUTORS_FIXED and not re.match(r"^agent:\S+$", executor):
        raise PlanError("исполнитель: main | owner | agent:<имя>")
    return executor


def add_step(store: "Store", plan_key: str, title: str, *, detail: str = "", key: str = "",
             parent: str = "", after: Optional[list] = None, waits: Optional[list] = None,
             together_with: Optional[list] = None, executor: str = "main", model: str = "", est: str = "",
             needs: Optional[list] = None, by: str = "") -> Step:
    plan = get_plan(store, plan_key)
    if plan.status != "active":
        raise PlanError(f"{plan.key} {plan.status} — шаги добавляют в активный план")
    if not title.strip():
        raise PlanError("у шага нужен заголовок")
    parent_id = get_step(store, parent, plan)[1].id if parent else None
    if not key:
        nums = [int(r[0]) for r in store.conn.execute("SELECT key FROM plan_steps WHERE plan_id = ?", (plan.id,))
                if str(r[0]).isdigit()]
        key = str(max(nums, default=0) + 1)
    key = key.strip()
    if "/" in key or not key:
        raise PlanError("ключ шага — без «/», например 4а")
    if store.conn.execute("SELECT 1 FROM plan_steps WHERE plan_id = ? AND key = ?", (plan.id, key)).fetchone():
        raise PlanError(f"шаг {plan.key}/{key} уже есть")
    after_ids = _resolve_refs(store, plan, after or [])
    position = (store.conn.execute("SELECT MAX(position) FROM plan_steps WHERE plan_id = ?",
                                   (plan.id,)).fetchone()[0] or 0) + 1
    cur = store.conn.execute(
        "INSERT INTO plan_steps (plan_id, key, title, detail, parent_id, position, after, waits, together_with,"
        " executor, model, est, needs, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (plan.id, key, title.strip(), detail.strip(), parent_id, position, _j(after_ids), _j(_waits(waits)),
         _j(_resolve_refs(store, plan, together_with or [])), _check_executor(executor), model, est,
         _j(_check_needs_list(needs or [])), _now()))
    step = _step_by_id(store, cur.lastrowid)
    path = _cycle(store, step.id, after_ids)
    if path:
        store.conn.rollback()
        raise PlanError("зависимость замыкает круг: " + " → ".join(
            step_ref(store, _step_by_id(store, i), plan) if _step_by_id(store, i) else key for i in path))
    _event(store, plan, step, "step", f"шаг {key} добавлен: {step.title}", by)
    _touch(store, plan)
    store.conn.commit()
    return step


@dataclass
class StepResult:
    plan: Plan
    step: Step
    freed: list = field(default_factory=list)        # шаги, у которых снялось последнее ожидание
    hanging: list = field(default_factory=list)      # зависели от отменённого — решить вручную
    notes: list = field(default_factory=list)


def update_step(store: "Store", ref: str, *, status: Optional[str] = None, evidence: Optional[list] = None,
                links: Optional[dict] = None, leftovers: Optional[list] = None, where: Optional[str] = None,
                add_after: Optional[list] = None, remove_after: Optional[list] = None,
                add_waits: Optional[list] = None, resolve_waits: Optional[list] = None,
                together_with: Optional[list] = None, executor: Optional[str] = None,
                title: Optional[str] = None, detail: Optional[str] = None, est: Optional[str] = None,
                spent: Optional[str] = None, model: Optional[str] = None, needs: Optional[list] = None,
                force: bool = False, by: str = "") -> StepResult:
    """Правка шага с проверкой правил 1–3. Ошибка правила — PlanError с причиной."""
    plan, step = get_step(store, ref)
    res = StepResult(plan, step)
    was_blocked = {s.id: bool(blockers(store, s, plan)) for s in _dependents(store, step)}
    log = []

    if title is not None and title.strip():
        step.title = title.strip()
    if detail is not None:
        step.detail = detail.strip()
    if est is not None:
        step.est = est
    if spent is not None:
        step.spent = spent
    if model is not None:
        step.model = model
    if executor is not None:
        step.executor = _check_executor(executor)
    if needs is not None:
        step.needs = _check_needs_list(needs)
    if where is not None:
        if where not in WHERE:
            raise PlanError(f"where: {', '.join(WHERE)}")
        if where != step.where:
            log.append(f"где: {step.where} → {where}")
        step.where = where
    if links:
        bad = set(links) - set(LINK_KEYS)
        if bad:
            raise PlanError(f"links: {', '.join(sorted(bad))} — бывает {', '.join(LINK_KEYS)}")
        links = dict(links)
        if links.get("job_id"):
            links["jobs"] = list(links.get("jobs") or []) + [links.pop("job_id")]
        for jid in links.get("jobs") or []:
            if store.get_job(int(jid)) is None:
                raise PlanError(f"links.jobs: работы #{jid} нет")
        if links.get("jobs"):
            links["jobs"] = [int(j) for j in links["jobs"]]
            log.append("работы: " + ", ".join(f"#{j}" for j in links["jobs"] if j not in step.links.get("jobs", [])))
        for k, v in links.items():
            if k in ("jobs", "commits", "migrations", "files"):
                step.links[k] = list(dict.fromkeys(list(step.links.get(k, [])) + list(v or [])))
            else:
                step.links[k] = v
    if leftovers is not None:
        step.leftovers = [str(x).strip() for x in leftovers if str(x).strip()]
    if evidence:
        for ev in evidence:
            checked = check_evidence(store, ev)
            step.evidence.append({**checked, "at": _now(), "by": by})
            log.append(f"доказательство {checked['kind']}: {checked['text']}")
    if remove_after:
        drop = set(_resolve_refs(store, plan, remove_after))
        step.after = [a for a in step.after if a not in drop]
        log.append("сняты зависимости: " + ", ".join(map(str, remove_after)))
    if add_after:
        new = [a for a in _resolve_refs(store, plan, add_after) if a not in step.after and a != step.id]
        path = _cycle(store, step.id, new)
        if path:
            raise PlanError("зависимость замыкает круг: " + " → ".join(
                step_ref(store, _step_by_id(store, i), plan) for i in path))
        step.after += new
        if new:
            log.append("ждёт: " + ", ".join(map(str, add_after)))
    if together_with is not None:
        step.together_with = _resolve_refs(store, plan, together_with)
    if add_waits:
        step.waits += _waits(add_waits)
        log.append("ожидания: " + "; ".join(w["text"] for w in _waits(add_waits)))
    if resolve_waits:
        for i in resolve_waits:
            if not 0 <= int(i) < len(step.waits):
                raise PlanError(f"ожидания №{i} нет (их {len(step.waits)}, счёт с 0)")
            step.waits[int(i)]["resolved"] = True
            log.append(f"снято ожидание: {step.waits[int(i)]['text']}")

    if status is not None and status != step.status:
        if status not in STEP_STATUSES:
            raise PlanError(f"статус шага: {', '.join(STEP_STATUSES)} (blocked и waiting_owner jwu ставит сам)")
        _check_transition(store, plan, step, status, force)
        log.append(f"статус: {step.status} → {status}")
        if status == "doing" and not step.started_at:
            step.started_at = _now()
        step.finished_at = _now() if status == "done" else ("" if status != "dropped" else _now())
        step.status = status

    store.conn.execute(
        "UPDATE plan_steps SET title = ?, detail = ?, status = ?, after = ?, waits = ?, together_with = ?,"
        " executor = ?, model = ?, est = ?, spent = ?, needs = ?, evidence = ?, links = ?, where_ = ?,"
        " leftovers = ?, started_at = ?, finished_at = ? WHERE id = ?",
        (step.title, step.detail, step.status, _j(step.after), _j(step.waits), _j(step.together_with),
         step.executor, step.model, step.est, step.spent, _j(step.needs), _j(step.evidence), _j(step.links),
         step.where, _j(step.leftovers), step.started_at, step.finished_at, step.id))
    for line in log:
        _event(store, plan, step, "step", line, by)
    _touch(store, plan)
    store.conn.commit()

    res.step = _step_by_id(store, step.id)
    for dep in _dependents(store, step):
        if was_blocked.get(dep.id) and not blockers(store, dep, plan) and dep.status == "todo":
            res.freed.append(step_ref(store, dep, plan))
        if step.status == "dropped" and dep.status in OPEN:
            res.hanging.append(step_ref(store, dep, plan))
    return res


def _check_transition(store: "Store", plan: Plan, step: Step, status: str, force: bool) -> None:
    if status == "doing":
        waiting = blockers(store, step, plan)
        if waiting and not force:
            raise PlanError(f"шаг {step.key} ещё ждёт: " + "; ".join(waiting))
    if status == "done":
        if not step.evidence:
            raise PlanError(f"шаг {step.key}: «готово» только с доказательством — evidence "
                            f"({', '.join(EVIDENCE_KINDS)}) со ссылкой на прогон, коммит, файл")
        missing = _needs_met(list(dict.fromkeys(plan.needs + step.needs)), step.evidence)
        if missing:
            raise PlanError(f"шаг {step.key}: для «готово» не хватает доказательств: {', '.join(missing)}")
        if step.leftovers:
            raise PlanError(f"шаг {step.key}: остаток не разобран ({'; '.join(step.leftovers)}) — сделай каждый "
                            "пункт шагом (jwu_plan_step_add) или вопросом и очисти leftovers")
        open_children = [c.key for c in _children(store, step) if c.status not in ("done", "dropped")]
        if open_children:
            raise PlanError(f"шаг {step.key}: открыты подшаги {', '.join(open_children)}")
        waiting = blockers(store, step, plan)
        if waiting and not force:
            raise PlanError(f"шаг {step.key} ещё ждёт: " + "; ".join(waiting))
    if step.status == "done" and status != "done":
        built = [step_ref(store, d, plan) for d in _dependents(store, step) if d.status in ("doing", "review", "done")]
        if built and not force:
            raise PlanError(f"на шаге {step.key} уже строят: {', '.join(built)} — откат только с force=True, "
                            "и проверь их")


def link_job(store: "Store", ref: str, job_id: int, by: str = "") -> Step:
    """Привязать работу к шагу (одна работа — к одному шагу, у шага работ может быть несколько)."""
    return update_step(store, ref, links={"jobs": [job_id]}, by=by).step


def steps_for_job(store: "Store", job_id: int) -> list[dict]:
    """Шаги планов воркспейса, к которым привязана работа: [{step, title, status, plan}]."""
    ensure_schema(store.conn)
    out = []
    for p in list_plans(store):
        for s in steps(store, p):
            if int(job_id) in (s.links.get("jobs") or []):
                out.append({"step": f"{p.key}/{s.key}", "title": s.title, "status": effective(store, s, p),
                            "plan": p.title})
    return out


# --------------------------------------------------------------------------- #
# Что брать, что идёт, сводки
# --------------------------------------------------------------------------- #


def _leaf(store: "Store", step: Step) -> bool:
    return not any(c.status in OPEN for c in _children(store, step))


def next_steps(store: "Store", plan_key: Optional[str] = None, slots: int = 3) -> list[dict]:
    """Что можно брать сейчас: открыто (todo), ничего не ждёт, не владельцу, без открытых подшагов,
    файлы не пересекаются с идущими шагами. Есть план в фокусе — без plan_key только из него."""
    plans = [get_plan(store, plan_key)] if plan_key else list_plans(store, "active")
    if not plan_key and any(p.focus for p in plans):
        plans = [p for p in plans if p.focus]
    busy_files: set[str] = set()
    for p in list_plans(store, "active"):
        for s in steps(store, p):
            if s.status in ("doing", "review"):
                busy_files |= set(s.links.get("files") or [])
    out = []
    for p in plans:
        for s in steps(store, p):
            if s.status != "todo" or s.executor == "owner" or blockers(store, s, p) or not _leaf(store, s):
                continue
            files = set(s.links.get("files") or [])
            if files & busy_files:
                continue
            out.append({"step": f"{p.key}/{s.key}", "title": s.title, "executor": s.executor, "model": s.model,
                        "est": s.est, "files": sorted(files)})
            busy_files |= files
            if len(out) >= max(1, slots):
                return out
    return out


def _counts(store: "Store", plan: Plan, items: list[Step]) -> tuple[int, int]:
    live = [s for s in items if s.status != "dropped"]
    return sum(1 for s in live if s.status == "done"), len(live)


def now(store: "Store", plan_key: Optional[str] = None) -> dict:
    """«Чо там»: по каждому плану — идёт, ждёт владельца, стоит и почему, следующее."""
    plans = [get_plan(store, plan_key)] if plan_key else list_plans(store, "active")
    out = []
    for p in plans:
        items = steps(store, p)
        done, total = _counts(store, p, items)
        row = {"plan": p.key, "title": p.title, "focus": p.focus, "done": done, "total": total,
               "doing": [], "owner": [], "blocked": [], "next": next_steps(store, p.key, slots=5)}
        for s in items:
            state = effective(store, s, p)
            ref = s.key
            if s.status in ("doing", "review"):
                row["doing"].append({"step": ref, "title": s.title, "status": s.status, "executor": s.executor})
            if state == "waiting_owner" or (s.executor == "owner" and s.status in OPEN):
                row["owner"].append({"step": ref, "title": s.title, "why": blockers(store, s, p) or ["делает владелец"]})
            elif state == "blocked":
                row["blocked"].append({"step": ref, "title": s.title, "why": blockers(store, s, p)})
        out.append(row)
    return {"plans": out, "markdown": render_now(out)}


def render_now(rows: list[dict]) -> str:
    if not rows:
        return "Активных планов нет."
    L = []
    for r in rows:
        star = " ⭐ в фокусе" if r["focus"] else ""
        L.append(f"### {r['plan']} · {r['title']} — {r['done']}/{r['total']}{star}")
        if r["owner"]:
            L.append("🙋 **Ждёт тебя:** " + "; ".join(f"{x['step']} {x['title']} ({', '.join(x['why'])})"
                                                     for x in r["owner"]))
        if r["doing"]:
            L.append("🔄 **Идёт:** " + "; ".join(f"{x['step']} {x['title']}"
                                                + (" — на проверке" if x["status"] == "review" else "")
                                                for x in r["doing"]))
        if r["blocked"]:
            L.append("⛔ **Стоит:** " + "; ".join(f"{x['step']} ← {', '.join(x['why'])}" for x in r["blocked"]))
        if r["next"]:
            L.append("➡️ **Можно брать:** " + "; ".join(f"{x['step'].split('/', 1)[1]} {x['title']}" for x in r["next"]))
        if not (r["owner"] or r["doing"] or r["blocked"] or r["next"]):
            L.append("Открытых шагов нет." if r["done"] == r["total"] else "Всё остальное отменено.")
        L.append("")
    return "\n".join(L).rstrip()


def step_dict(store: "Store", plan: Plan, s: Step) -> dict:
    return {"step": f"{plan.key}/{s.key}", "key": s.key, "title": s.title, "detail": s.detail,
            "status": s.status, "state": effective(store, s, plan), "waiting_for": blockers(store, s, plan),
            "parent": step_ref(store, _step_by_id(store, s.parent_id), plan) if s.parent_id else "",
            "after": [step_ref(store, _step_by_id(store, a), plan) for a in s.after if _step_by_id(store, a)],
            "together_with": [step_ref(store, _step_by_id(store, a), plan) for a in s.together_with
                              if _step_by_id(store, a)],
            "waits": s.waits, "executor": s.executor, "model": s.model, "est": s.est, "spent": s.spent,
            "needs": list(dict.fromkeys(plan.needs + s.needs)), "evidence": s.evidence, "links": s.links,
            "where": s.where, "leftovers": s.leftovers, "started_at": s.started_at, "finished_at": s.finished_at}


def view(store: "Store", plan_key: str) -> dict:
    """План целиком: шаги деревом, правила, последние события; markdown — готовая сводка."""
    plan = get_plan(store, plan_key)
    items = steps(store, plan)
    done, total = _counts(store, plan, items)
    by_parent: dict = {}
    for s in items:
        by_parent.setdefault(s.parent_id, []).append(s)
    L = [f"## {plan.key} · {plan.title} — {done}/{total} ({plan.status})"]
    if plan.goal:
        L.append(f"Цель: {plan.goal}")
    if plan.rules:
        L.append("Правила: " + "; ".join(plan.rules))
    if plan.needs:
        L.append("Для «готово» нужно: " + ", ".join(plan.needs))
    L.append("")

    def walk(parent, depth):
        for s in by_parent.get(parent, []):
            state = effective(store, s, plan)
            bits = []
            if s.executor != "main":
                bits.append(s.executor)
            if s.where != "none":
                bits.append(f"где: {s.where}")
            if s.evidence:
                bits.append("✓ " + ", ".join(dict.fromkeys(e["kind"] for e in s.evidence)))
            why = blockers(store, s, plan) if state in DERIVED else []
            if why:
                bits.append("ждёт: " + "; ".join(why))
            extra = f" — {' · '.join(bits)}" if bits else ""
            L.append(f"{'  ' * depth}- {ICON.get(state, '•')} **{s.key}** {s.title}{extra}")
            walk(s.id, depth + 1)

    walk(None, 0)
    if not items:
        L.append("Шагов пока нет.")
    recent = events(store, plan, limit=8)
    if recent:
        L += ["", "Последнее:"]
        L += [f"- {e['ts'][:16].replace('T', ' ')} {e['text']}" for e in recent]
    return {**plan.to_dict(), "steps": [step_dict(store, plan, s) for s in items],
            "events": recent, "markdown": "\n".join(L)}
