"""Доска «что делать» (/jwu-todo): задачи, требующие действия, и раздача их по сессиям.

Только из кеша jwu — снапшоты последнего синка, без сети. Группы:

- ``simple`` — мой PR с конфликтом и/или красной сборкой и без замечаний людей;
- ``fixes`` — правки: needs work, открытые задачи в PR, задача вернулась с QA (в том числе
  с конфликтом и сборкой — они уходят сюда, а не в простое);
- ``todo`` — несделанные задачи на мне: не начата / в работе, без моего открытого PR;
- ``review`` — чужие PR, которые ждут моего ревью.

«Взято» = по задаче/PR есть активная работа (кем и когда — из её записей). ``take``
заводит работы атомарно: две сессии не возьмут одно и то же. Номера привязаны к тому
списку, который показали этой сессии (``todo:board:<сессия>`` в meta), — между запусками
синк мог обновиться, а «1» должна остаться той задачей, что ты видел.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable, Optional

from .dates import fmt_ago

if TYPE_CHECKING:
    from .models import PR, Issue, Job
    from .store import Store

GROUPS = ("duty", "simple", "fixes", "reply", "move", "todo", "review", "close")
GROUP_TITLES = {
    "duty": "🛟 Дежурство",
    "reply": "📣 Ответить",
    "move": "➡️ Двигать дальше",
    "close": "🧹 Закрыть работу",
    "simple": "🔧 Конфликты и сборки",
    "fixes": "✍️ Правки по ревью и QA",
    "todo": "📋 Несделанные задачи",
    "review": "👀 Ждут моего ревью",
}
TODO_STATUSES_SETTING = "todo.statuses.todo"
QA_STATUSES_SETTING = "todo.statuses.qa_failed"
DEFAULT_TODO_STATUSES = ("open", "to do", "selected for development", "in progress", "dev at stand")
DEFAULT_QA_STATUSES = ("defect", "need update", "qa failed", "reopened", "failed")
# Приоритет Jira → вес (меньше — важнее) и значок. Неизвестный — в конец.
PRIORITY_ORDER = {"blocker": 0, "highest": 0, "critical": 1, "high": 1, "major": 2, "medium": 2,
                  "minor": 3, "low": 3, "trivial": 4, "lowest": 4}
PRIORITY_ICON = {0: "🔴", 1: "🟠", 2: "🟡", 3: "🟢", 4: "⚪"}
# Статус внутри группы «несделанные»: что уже в работе — выше того, что не начато.
IN_WORK_STATUSES = ("in progress", "dev at stand")
STALE_MINUTES = 30
STALE_JOB_DAYS = 3      # работа висит дольше — на доске подсказка «давно: закрыть или продолжить?»
SESSION_MARK = "Взято сессией"
KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


@dataclass
class Item:
    n: int
    group: str
    key: str                        # ключ задачи или PR-ссылка PROJ/repo#42
    title: str
    status: str = ""
    problems: list[str] = field(default_factory=list)
    pr: Optional[dict] = None       # {"id", "project", "repo", "title"}
    issue_key: str = ""
    taken: Optional[dict] = None    # {"job_id", "since", "ago", "session"}
    priority: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Board:
    items: list[Item]
    synced_at: str = ""
    synced_ago: str = ""
    stale: bool = False
    session: str = ""
    syncing_since: str = ""   # синк идёт прямо сейчас — с какого времени
    sessions: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"items": [i.to_dict() for i in self.items], "synced_at": self.synced_at,
                "synced_ago": self.synced_ago, "stale": self.stale, "session": self.session,
                "syncing_since": self.syncing_since,
                "sessions": self.sessions}


def _statuses(store: "Store", workspace_id: int, key: str, default: Iterable[str]) -> set[str]:
    raw = (store.workspace_settings(workspace_id).get(key) or "").strip()
    values = [s for s in raw.split(",")] if raw else list(default)
    return {s.strip().casefold() for s in values if s.strip()}


def _pr_ref(pr: "PR") -> str:
    return f"{pr.project}/{pr.repository}#{pr.id}"


def _issue_key(pr: "PR") -> str:
    found = KEY_RE.search(pr.title or "") or KEY_RE.search(pr.source_branch or "")
    return found.group(1) if found else ""


def _my_status(pr: "PR", login: str) -> str:
    from .reviewq import my_status

    return my_status(pr, login)


def _taken_index(store: "Store") -> tuple[dict[str, "Job"], dict[tuple, "Job"]]:
    """Активные работы по ключу задачи и по PR (project, repo, id)."""
    by_key: dict[str, "Job"] = {}
    by_pr: dict[tuple, "Job"] = {}
    for job in store.list_jobs(status="active"):
        if job.task_key:
            by_key.setdefault(job.task_key, job)
        for link in job.prs:
            by_pr.setdefault((link.project, link.repo, link.pr_id), job)
    return by_key, by_pr


def _taken(job: Optional["Job"]) -> Optional[dict]:
    if job is None:
        return None
    session = ""
    for rec in job.records:
        if rec.text.startswith(SESSION_MARK):
            m = re.match(rf"{SESSION_MARK}\s*«([^»]*)»", rec.text)
            session = m.group(1) if m else ""
    return {"job_id": job.id, "since": job.created_at, "ago": fmt_ago(job.created_at),
            "session": session, "title": job.title}


def build(store: "Store", workspace_id: int, login: str, *, session: str = "",
          now: Optional[datetime] = None) -> Board:
    """Собрать доску из кеша и запомнить её номера за сессией."""
    from .daemon import LAST_PASS_META

    todo_st = _statuses(store, workspace_id, TODO_STATUSES_SETTING, DEFAULT_TODO_STATUSES)
    qa_st = _statuses(store, workspace_id, QA_STATUSES_SETTING, DEFAULT_QA_STATUSES)
    issues = {i.key: i for i in store.latest_issues("mine")}
    by_key, by_pr = _taken_index(store)
    items: list[Item] = []
    covered: set[str] = set()

    for pr in store.latest_prs("mine"):
        if pr.state and pr.state.upper() != "OPEN":
            continue
        key = _issue_key(pr)
        issue = issues.get(key)
        problems: list[str] = []
        review = []
        needs_work = [r.display_name or r.name for r in pr.reviewers if (r.status or "").upper() == "NEEDS_WORK"]
        if needs_work:
            review.append(f"needs work: {', '.join(needs_work)}")
        if pr.tasks_open:
            review.append(f"открыто задач в PR: {pr.tasks_open}")
        qa = bool(issue and (issue.status or "").casefold() in qa_st)
        if qa:
            review.append(f"QA: {issue.status}")
        tech = []
        if pr.conflicted:
            tech.append("конфликт")
        if pr.build_state == "FAILED":
            tech.append("сборка красная")
        if not review and not tech:
            continue
        group = "fixes" if review else "simple"
        problems = review + tech
        job = by_pr.get((pr.project, pr.repository, pr.id)) or (by_key.get(key) if key else None)
        items.append(Item(n=0, group=group, key=_pr_ref(pr), title=pr.title, status=issue.status if issue else "",
                          priority=issue.priority if issue else "",
                          problems=problems, issue_key=key,
                          pr={"id": pr.id, "project": pr.project, "repo": pr.repository, "title": pr.title},
                          taken=_taken(job)))
        if key:
            covered.add(key)

    open_pr_keys = {_issue_key(pr) for pr in store.latest_prs("mine")
                    if not pr.state or pr.state.upper() == "OPEN"}
    # одна задача — несколько моих открытых PR: правки в разных ветках, пометить на доске
    prs_by_key: dict[str, list[str]] = {}
    for pr in store.latest_prs("mine"):
        if (not pr.state or pr.state.upper() == "OPEN") and _issue_key(pr):
            prs_by_key.setdefault(_issue_key(pr), []).append(f"{pr.repository}#{pr.id}")
    for item in items:
        others = [ref for ref in prs_by_key.get(item.issue_key, [])
                  if not item.pr or ref != f"{item.pr['repo']}#{item.pr['id']}"]
        if others:
            item.problems.append(f"⚠️ ещё PR по задаче: {', '.join(others)}")
    for key, issue in issues.items():
        status = (issue.status or "").casefold()
        if key in covered:
            continue
        if status in qa_st:
            items.append(Item(n=0, group="fixes", key=key, title=issue.summary, status=issue.status,
                              priority=issue.priority, problems=[f"QA: {issue.status}"], issue_key=key,
                              taken=_taken(by_key.get(key))))
        elif status in todo_st and key not in open_pr_keys:
            items.append(Item(n=0, group="todo", key=key, title=issue.summary, status=issue.status,
                              priority=issue.priority, problems=[], issue_key=key, taken=_taken(by_key.get(key))))

    for pr in store.latest_prs("review"):
        if pr.state and pr.state.upper() != "OPEN":
            continue
        if _my_status(pr, login) in ("APPROVED", "NEEDS_WORK"):
            continue
        items.append(Item(n=0, group="review", key=_pr_ref(pr), title=pr.title,
                          problems=[f"автор {pr.author}"] if pr.author else [], issue_key=_issue_key(pr),
                          pr={"id": pr.id, "project": pr.project, "repo": pr.repository, "title": pr.title},
                          taken=_taken(by_pr.get((pr.project, pr.repository, pr.id)))))

    # Дежурство: вопросы «Д<N>»; работа у вопроса появляется, только когда его взяли
    from .duty import questions as duty_questions

    for q in duty_questions(store):
        taken = None
        if q.get("job_id"):
            job = store.get_job(int(q["job_id"]))
            info = _taken(job) if job else None
            taken = info if info and info["session"] else None
        source = " · ".join(x for x in (q.get("author", ""), q.get("when", ""), q.get("where", "")) if x)
        items.append(Item(n=0, group="duty", key=q["id"], title=q["summary"],
                          problems=[source] if source else [], taken=taken))

    listed = {i.key for i in items}
    _extra_groups(store, workspace_id, login, items, listed, issues, qa_st)

    # Группы — в фиксированном порядке; внутри: свободное выше взятого, в работе выше не начатого,
    # дальше от важного приоритета к неважному.
    items.sort(key=lambda i: (GROUPS.index(i.group), i.taken is not None,
                              0 if (i.status or "").casefold() in IN_WORK_STATUSES else 1,
                              _prio(i.priority), i.key))
    for n, item in enumerate(items, 1):
        item.n = n
    store.set_workspace_meta(f"todo:board:{session or '-'}",
                             json.dumps([{"n": i.n, "key": i.key, "group": i.group} for i in items]))
    last = store.get_meta(LAST_PASS_META) or ""
    stale = True
    if last:
        try:
            dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            stale = ((now or datetime.now(timezone.utc)) - dt).total_seconds() > STALE_MINUTES * 60
        except ValueError:
            pass
    from .daemon import pass_in_progress

    running = pass_in_progress(store)
    return Board(items=items, synced_at=last, synced_ago=fmt_ago(last) if last else "ещё не было",
                 stale=stale, session=session, syncing_since=running or "",
                 sessions=sessions(store))


DONE_STATUSES = ("done", "closed", "resolved", "закрыта", "решена")


def _extra_groups(store: "Store", workspace_id: int, login: str, items: list, listed: set,
                  issues: dict, qa_st: set) -> None:
    """То, что раньше давал дневной анализ: двигать дальше, ответить, закрыть работу."""
    from .duty import QUESTION_PREFIX
    from .store import is_testing_status

    by_key, by_pr = _taken_index(store)
    mine_prs = [pr for pr in store.latest_prs("mine") if not pr.state or pr.state.upper() == "OPEN"]

    # ➡️ апрувы собраны, блокеров нет — задачу перевести дальше / мержить
    for pr in mine_prs:
        ref = _pr_ref(pr)
        if ref in listed or not pr.reviewers or not all(r.approved for r in pr.reviewers):
            continue
        if pr.conflicted or pr.build_state == "FAILED" or pr.tasks_open:
            continue
        key = _issue_key(pr)
        issue = issues.get(key)
        status = issue.status if issue else ""
        nxt = ("задача уже на тестах — мержить по готовности" if is_testing_status(status)
               else "апрувы собраны — перевести задачу в тестирование")
        items.append(Item(n=0, group="move", key=ref, title=pr.title, status=status,
                          priority=issue.priority if issue else "", issue_key=key,
                          problems=[f"апрувы {len(pr.reviewers)}/{len(pr.reviewers)}", nxt],
                          pr={"id": pr.id, "project": pr.project, "repo": pr.repository, "title": pr.title},
                          taken=_taken(by_pr.get((pr.project, pr.repository, pr.id)) or by_key.get(key))))
        listed.add(ref)

    # 📣 непрочитанные упоминания — от меня чего-то ждут
    seen_keys: dict[str, list] = {}
    for m in store.unseen_mentions():
        seen_keys.setdefault(m.task_key, []).append(m)
    for task_key, ms in seen_keys.items():
        last = ms[-1]
        text = " ".join((last.text or "").split())
        items.append(Item(n=0, group="reply", key=f"{task_key} ↩", title=last.summary or task_key,
                          issue_key=task_key,
                          problems=[f"{last.author}: {text[:70]}" + (f" (+{len(ms) - 1})" if len(ms) > 1 else "")],
                          taken=_taken(by_key.get(task_key))))

    # 🧹 работа открыта, а задача закрыта или ушла от меня
    for job in store.list_jobs(status="active"):
        if not job.task_key or job.title.startswith(QUESTION_PREFIX):
            continue
        issue = issues.get(job.task_key)
        if issue is None:
            reason = "задача ушла из моих (переназначена или закрыта)"
        elif (issue.status or "").casefold() in DONE_STATUSES or issue.resolution:
            reason = f"задача в статусе {issue.status}"
        else:
            continue
        items.append(Item(n=0, group="close", key=f"#{job.id}", title=job.title, issue_key=job.task_key,
                          problems=[reason]))


# --------------------------------------------------------------------------- #
# Имя сессии
# --------------------------------------------------------------------------- #

_ADJ = ("Shiny", "Brave", "Quiet", "Swift", "Lucky", "Sunny", "Calm", "Bold", "Clever", "Happy",
        "Mighty", "Silent", "Rapid", "Gentle", "Wild", "Bright", "Cosmic", "Frosty", "Golden", "Witty")
_NOUN = ("Gold", "Fox", "Otter", "Falcon", "Maple", "Comet", "Pebble", "Tiger", "Harbor", "Lotus",
         "Raven", "Cedar", "Badger", "Meteor", "Willow", "Panda", "Orbit", "Lynx", "Coral", "Ember")


def random_session_name(taken: Iterable[str] = ()) -> str:
    """Имя сессии вроде «Shiny-Gold»: не спрашиваем — придумываем, чтобы не мешать."""
    import random

    used = set(taken)
    for _ in range(50):
        name = f"{random.choice(_ADJ)}-{random.choice(_NOUN)}"
        if name not in used:
            return name
    return f"{random.choice(_ADJ)}-{random.choice(_NOUN)}-{random.randint(10, 99)}"


def active_session_names(store: "Store") -> set[str]:
    names = set()
    for job in store.list_jobs(status="active"):
        info = _taken(job)
        if info and info["session"]:
            names.add(info["session"])
    return names


# --------------------------------------------------------------------------- #
# Живые сессии
# --------------------------------------------------------------------------- #

SESSION_META = "session:"       # session:<имя> → {"pid", "cwd", "seen"} — общий meta БД
SESSION_ALIVE_MIN = 10


def heartbeat(store: "Store", name: str, *, pid: int, cwd: str) -> None:
    store.set_meta(SESSION_META + name, json.dumps(
        {"pid": pid, "cwd": cwd, "seen": datetime.now(timezone.utc).isoformat(timespec="seconds")}))


def _pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def sessions(store: "Store") -> dict[str, dict]:
    """Сессии, которые отмечались: жива ли (процесс есть и отмечалась ≤ 10 мин назад)."""
    out: dict[str, dict] = {}
    for key, raw in store.meta_items(SESSION_META).items():
        try:
            info = json.loads(raw)
        except ValueError:
            continue
        seen = info.get("seen", "")
        age_min = _days_since(seen) * 1440 if seen else 1e9
        info["alive"] = age_min <= SESSION_ALIVE_MIN and _pid_alive(info.get("pid", 0))
        info["seen_ago"] = fmt_ago(seen) if seen else "давно"
        out[key[len(SESSION_META):]] = info
    return out


def release(store: "Store", session: str) -> list[int]:
    """Освободить работы молчащей сессии: в паузу с записью — их задачи снова свободны."""
    released = []
    for job in store.list_jobs(status="active"):
        info = _taken(job)
        if info and info["session"] == session:
            store.set_job_status(job.id, "paused")
            store.add_job_record(job.id, f"Освобождено: сессия «{session}» не отвечает — задача снова свободна", kind="note")
            released.append(job.id)
    store.delete_meta(SESSION_META + session)
    return released


# --------------------------------------------------------------------------- #
# Обновить кеш
# --------------------------------------------------------------------------- #

REFRESH_TIMEOUT = 600


def refresh(store: "Store", *, timeout: float = REFRESH_TIMEOUT, sleep=None) -> dict:
    """Свежие данные для доски: синк делает демон — один на все сессии.

    Демон запущен — будим его и ждём конца прохода: пять сессий не пойдут в Jira и Bitbucket
    разом. Проход **тихий**: данные обновятся, а уведомления отложатся и уйдут со следующим
    обычным синком. Демона нет — синкаем этот контур сами, тоже тихо.
    """
    import time

    from . import daemon

    sleep = sleep or time.sleep
    before = store.get_meta(daemon.LAST_PASS_META) or ""
    # синк уже идёт (его запустила другая сессия или расписание) — не будим, просто ждём его
    running = daemon.pass_in_progress(store)
    pid = daemon.SingleInstance().holder_pid() if running else daemon.kick(store, silent=True)
    if pid is not None:
        waited = 0.0
        while waited < timeout:
            sleep(2)
            waited += 2
            if (store.get_meta(daemon.LAST_PASS_META) or "") != before:
                return {"ok": True, "how": "daemon" if not running else "daemon (уже шёл)",
                        "waited_s": int(waited)}
        return {"ok": False, "how": "daemon", "waited_s": int(waited),
                "error": "демон не закончил проход за отведённое время — показываю, что есть"}
    ws = store.get_workspace(store.workspace_id)
    svc = daemon._default_factory(ws)
    svc.defer_notifications = True
    try:
        svc.sync()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "how": "sync", "error": str(exc)}
    finally:
        svc.close()
    store.set_meta(daemon.LAST_PASS_META, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return {"ok": True, "how": "sync"}


# --------------------------------------------------------------------------- #
# Взять в работу
# --------------------------------------------------------------------------- #


def _goal(item: Item) -> str:
    pr = f" PR #{item.pr['id']}" if item.pr else ""
    if item.group == "simple":
        what = " и ".join(p for p in item.problems if p in ("конфликт", "сборка красная"))
        return f"{what.capitalize()}{pr}".replace("Сборка красная", "Красная сборка").replace("сборка красная", "красная сборка")
    if item.group == "fixes":
        return f"Правки по ревью{pr}" if any(not p.startswith("QA:") for p in item.problems if p not in ("конфликт", "сборка красная")) \
            else f"Правки по возврату QA{pr}"
    if item.group == "review":
        return f"Ревью{pr}"
    if item.group == "move":
        return f"Довести{pr}: апрувы собраны"
    if item.group == "reply":
        return "Ответить на упоминание"
    return "Сделать задачу"


def take(store: "Store", workspace_id: int, login: str, *, numbers: Iterable[int] = (),
         group: str = "", session: str = "") -> dict:
    """Взять пункты в работу этой сессией: по номерам из её последней доски или всю группу.

    Атомарно: под BEGIN IMMEDIATE проверяем, что по задаче/PR нет активной работы, и
    заводим её. Уже взятое другой сессией — в ``skipped`` с тем, кем и когда.
    """
    board = build(store, workspace_id, login, session=session)
    shown = json.loads(store.get_workspace_meta(f"todo:board:{session or '-'}") or "[]")
    wanted_keys: list[str] = []
    numbers = [int(n) for n in numbers]
    if numbers:
        by_n = {row["n"]: row["key"] for row in shown}
        wanted_keys = [by_n[n] for n in numbers if n in by_n]
        unknown = [n for n in numbers if n not in by_n]
    else:
        unknown = []
    if group:
        wanted_keys += [i.key for i in board.items if i.group == group and i.taken is None]
    items = {i.key: i for i in board.items}
    taken: list[dict] = []
    skipped: list[dict] = [{"n": n, "reason": "нет такого номера в показанном списке"} for n in unknown]
    # Вопросы дежурства берутся отдельно: работа у них заводится только сейчас
    from .duty import question_take

    for key in [k for k in dict.fromkeys(wanted_keys) if items.get(k) and items[k].group == "duty"]:
        item = items[key]
        if item.taken:
            skipped.append({"key": key, "n": item.n, "reason": f"уже взято сессией «{item.taken['session']}», {item.taken['ago']}"})
            continue
        q = question_take(store, key, session=session)
        taken.append({"n": item.n, "key": key, "group": "duty", "job_id": q["job_id"],
                      "goal": item.title, "issue_key": "", "pr": None})
    wanted_keys = [k for k in wanted_keys if not (items.get(k) and items[k].group == "duty")]
    conn = store.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        by_key, by_pr = _taken_index(store)
        claimed: set[str] = set()
        for key in dict.fromkeys(wanted_keys):
            item = items.get(key)
            if item is None:
                skipped.append({"key": key, "reason": "больше не требует действия (синк обновился)"})
                continue
            if item.group == "close":
                skipped.append({"key": key, "n": item.n, "reason": f"это закрытие работы {key}: «закрой работу {key}» (jwu-close-job)"})
                continue
            job = (by_pr.get((item.pr["project"], item.pr["repo"], item.pr["id"])) if item.pr else None) \
                or (by_key.get(item.issue_key) if item.issue_key else None)
            if item.issue_key and item.issue_key in claimed:
                skipped.append({"key": key, "n": item.n, "reason": "та же задача уже взята этим запуском"})
                continue
            if job is not None:
                info = _taken(job)
                skipped.append({"key": key, "n": item.n, "reason": "уже взято"
                                + (f" сессией «{info['session']}»" if info["session"] else "")
                                + f", {info['ago']}", "job_id": job.id})
                continue
            cur = conn.execute(
                "INSERT INTO jobs (task_key, title, status, created_at, updated_at, workspace_id)"
                " VALUES (?, ?, 'active', ?, ?, ?)",
                (item.issue_key, _goal(item), _iso(), _iso(), store.workspace_id))
            job_id = int(cur.lastrowid)
            if item.pr:
                conn.execute("INSERT OR IGNORE INTO job_prs (job_id, pr_id, project, repo) VALUES (?, ?, ?, ?)",
                             (job_id, item.pr["id"], item.pr["project"], item.pr["repo"]))
            if item.group != "review" and item.issue_key:
                # все мои открытые PR по этой задаче — в работу: правки в нескольких ветках
                for pr in store.latest_prs("mine"):
                    if (not pr.state or pr.state.upper() == "OPEN") and _issue_key(pr) == item.issue_key:
                        conn.execute("INSERT OR IGNORE INTO job_prs (job_id, pr_id, project, repo)"
                                     " VALUES (?, ?, ?, ?)", (job_id, pr.id, pr.project, pr.repository))
            conn.execute(
                "INSERT INTO job_records (job_id, kind, text, ts) VALUES (?, 'note', ?, ?)",
                (job_id, f"{SESSION_MARK} «{session or 'без имени'}»: {item.key} — {', '.join(item.problems)}", _iso()))
            if item.issue_key:
                claimed.add(item.issue_key)
            taken.append({"n": item.n, "key": key, "group": item.group, "job_id": job_id,
                          "goal": _goal(item), "issue_key": item.issue_key, "pr": item.pr})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"taken": taken, "skipped": skipped}


def _prio(priority: str) -> int:
    return PRIORITY_ORDER.get((priority or "").casefold(), len(PRIORITY_ICON))


def _days_since(ts: str) -> float:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Вывод
# --------------------------------------------------------------------------- #


def _cell(text: str, limit: int = 0) -> str:
    text = " ".join((text or "").split()).replace("|", "\\|")
    if limit and len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text or "—"


def _taken_cell(item: Item) -> str:
    if not item.taken:
        return "свободно"
    who = f"«{item.taken['session']}»" if item.taken["session"] else f"работа #{item.taken['job_id']}"
    cell = f"⏳ {who}, {item.taken['ago']}"
    state = _SESSIONS_SEEN.get(item.taken["session"]) if item.taken["session"] else None
    if state is not None and not state.get("alive"):
        cell += f" 💤 сессия молчит ({state.get('seen_ago', '')})"
    elif _days_since(item.taken["since"]) >= STALE_JOB_DAYS:
        cell += " ⚠️ давно"
    return _cell(cell)


_SESSIONS_SEEN: dict = {}


def _prio_cell(item: Item) -> str:
    if not item.priority:
        return "—"
    return f"{PRIORITY_ICON.get(_prio(item.priority), '⚪')} {_cell(item.priority)}"


# Колонки по группе: у «несделанных» проблем нет — там важны статус и приоритет.
_COLUMNS = {
    "duty":   ("№", "Вопрос", "Откуда", "Взято"),
    "reply":  ("№", "Задача", "Заголовок", "Кто и что", "Взято"),
    "move":   ("№", "PR", "Задача", "Заголовок", "Статус", "Что дальше", "Взято"),
    "close":  ("№", "Работа", "Задача", "Почему"),
    "simple": ("№", "PR", "Задача", "Заголовок", "Приоритет", "Что не так", "Взято"),
    "fixes":  ("№", "PR", "Задача", "Заголовок", "Приоритет", "Статус", "Что не так", "Взято"),
    "todo":   ("№", "Задача", "Заголовок", "Приоритет", "Статус", "Взято"),
    "review": ("№", "PR", "Задача", "Заголовок", "Автор", "Взято"),
}


def _title(i: Item) -> str:
    """Заголовок без ключа в начале (у PR он часто начинается с «KEY: …»)."""
    title = i.title or ""
    if i.issue_key and title.startswith(i.issue_key):
        title = title[len(i.issue_key):].lstrip(" :—-")
    return _cell(title, 60)


def _row(group: str, i: Item) -> list[str]:
    key_md = f"**{_cell(i.issue_key)}**" if i.issue_key else "—"
    pr_md = f"{i.pr['repo']}#{i.pr['id']}" if i.pr else "—"
    if group == "reply":
        return [str(i.n), key_md, _cell(i.title, 50), _cell(", ".join(i.problems), 80), _taken_cell(i)]
    if group == "move":
        return [str(i.n), pr_md, key_md, _title(i), _cell(i.status), _cell(i.problems[-1]), _taken_cell(i)]
    if group == "close":
        return [str(i.n), f"**{i.key}**", key_md, _cell(", ".join(i.problems))]
    if group == "duty":
        return [str(i.n), f"**{i.key}** {_cell(i.title, 70)}", _cell(", ".join(i.problems), 50), _taken_cell(i)]
    pr = f"{i.pr['repo']}#{i.pr['id']}" if i.pr else "—"
    key = f"**{_cell(i.issue_key)}**" if i.issue_key else "—"
    problems = _cell(", ".join(p for p in i.problems if not p.startswith("автор ")))
    if group == "simple":
        return [str(i.n), pr, key, _title(i), _prio_cell(i), problems, _taken_cell(i)]
    if group == "fixes":
        return [str(i.n), pr, key, _title(i), _prio_cell(i), _cell(i.status), problems, _taken_cell(i)]
    if group == "todo":
        return [str(i.n), key, _title(i), _prio_cell(i), _cell(i.status), _taken_cell(i)]
    author = next((p[len("автор "):] for p in i.problems if p.startswith("автор ")), "")
    return [str(i.n), pr, key, _title(i), _cell(author), _taken_cell(i)]


def render_md(board: Board, *, disk: str = "") -> str:
    """Готовая сводка таблицами — агент показывает её как есть, ничего не перерисовывая."""
    head = ([f"**Сессия:** {board.session}"] if board.session else []) + [f"**Синк:** {board.synced_ago}"]
    if board.syncing_since:
        head.append(f"⏳ идёт обновление (начато {fmt_ago(board.syncing_since)}) — можно подождать")
    elif board.stale:
        head.append("⚠️ данные старые")
    if disk:
        head.append(disk.replace("Диск:", "**Диск:**", 1))
    _SESSIONS_SEEN.clear()
    _SESSIONS_SEEN.update(board.sessions)
    lines = [" · ".join(head)]
    # В строке «Сессии» — только живые: молчащие видны там, где важны, — в ячейке «взято»
    # у их работ (💤), и тогда же подсказка, как эти работы освободить.
    alive = [name for name, info in board.sessions.items() if info.get("alive")]
    if alive:
        lines.append("**Сессии:** " + " · ".join(
            f"🟢 {name}" + (" (эта)" if name == board.session else "") for name in alive))
    if any(i.taken and i.taken.get("session") and not board.sessions.get(i.taken["session"], {}).get("alive", True)
           for i in board.items):
        lines.append("Работы молчащей сессии (💤) можно освободить: «освободи <имя>».")
    lines.append("")
    free = sum(1 for i in board.items if not i.taken)
    lines.append(f"Всего {len(board.items)}, свободно {free}. Взять: «делай 1 и 3» или «бери все конфликты и падения».")
    for group in GROUPS:
        part = [i for i in board.items if i.group == group]
        if not part:
            continue
        cols = _COLUMNS[group]
        lines += ["", f"### {GROUP_TITLES[group]} · {len(part)}", "",
                  "| " + " | ".join(cols) + " |",
                  "|" + "|".join(":-:" if c == "№" else "---" for c in cols) + "|"]
        lines += ["| " + " | ".join(_row(group, i)) + " |" for i in part]
    if not board.items:
        lines += ["", "Делать нечего: конфликтов, красных сборок, правок и несделанных задач нет."]
    return "\n".join(lines)
