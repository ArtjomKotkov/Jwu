"""Политика git воркспейса: какие пишущие операции можно, где и с чьего разрешения.

Решает код, а не инструкция в скилле: ``jwu_git_*`` спрашивает :func:`decide` перед каждой
пишущей операцией и при отказе git не запускает вовсе, а хук ``jwu hook git-guard`` не даёт
обойти это голым ``git`` из Bash.

Уровни:

- ``allow`` — сразу;
- ``ask``   — нужно «да» пользователя в меню (``user_approved=True``);
- ``grant`` — «да» в чате мало: нужен допуск, который агент выдать не может (кнопка в
  Telegram-боте или ``jwu git grant`` в терминале пользователя), одноразовый, с TTL и
  привязкой к HEAD;
- ``deny``  — никогда.

Два уровня настройки. Воркспейс (``git_policy`` + правила с ``repo_id IS NULL``) задаёт общее,
репозиторий (``git_repos`` + свои правила) уточняет. Репозиторий узнаётся по ``origin``
(``project/repo``), а не по папке: основная папка, рабочие копии работ и любой клон живут
по одной политике. Порядок решения: правила репозитория → правила воркспейса → уровень
операции у репозитория → у воркспейса. Первое совпадение выигрывает.

Пусто в базе — работают встроенные значения (:data:`DEFAULT_OPS`, :data:`DEFAULT_RULES`);
:func:`ensure` записывает их при первом обращении, перенося старый ``push.protected``.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Optional

if TYPE_CHECKING:
    from .store import Store

LEVELS = ("allow", "ask", "grant", "deny")
OPS = ("commit", "switch", "sync", "stash", "discard", "history",
       "push", "force_push", "delete_branch", "tag")
OP_TITLES = {
    "commit": "коммит", "switch": "переключить/создать ветку", "sync": "подтянуть целевую ветку",
    "stash": "stash", "discard": "выкинуть несохранённое", "history": "переписать незапушенную историю",
    "push": "пуш", "force_push": "force-пуш", "delete_branch": "удалить ветку", "tag": "тег",
}
WHERE = ("any", "job", "main")
SYNC_MODES = ("merge", "rebase")
GRANT_VIA = ("telegram", "cli")

DEFAULT_OPS = {
    "commit": "allow", "switch": "allow", "sync": "allow", "stash": "allow",
    "discard": "ask", "history": "ask", "push": "ask", "force_push": "ask",
    "delete_branch": "ask", "tag": "grant",
}
DEFAULT_SETTINGS = {"guard": True, "sync": "merge", "backup_days": 14,
                    "grant_ttl": 600, "grant_via": ["telegram", "cli"], "grant_bind": "head"}
PROD_BRANCHES = ["master", "main"]
SHARED_BRANCHES = ["develop", "release*", "stable*"]
DEFAULT_RULES = [
    {"name": "прод-ветки", "branch": PROD_BRANCHES,
     "ops": {"push": "deny", "force_push": "deny", "commit": "deny", "history": "deny", "sync": "deny"}},
    {"name": "общие ветки", "branch": SHARED_BRANCHES,
     "ops": {"push": "grant", "force_push": "deny", "commit": "ask", "history": "deny", "delete_branch": "deny"}},
    {"name": "основная папка", "where": "main",
     "ops": {"commit": "ask", "sync": "ask", "history": "deny"}},
]
LEGACY_PROTECTED_SETTING = "push.protected"
KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+)-\d+")

GIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS git_policy (
    workspace_id INTEGER PRIMARY KEY,
    guard        INTEGER NOT NULL DEFAULT 1,
    sync         TEXT NOT NULL DEFAULT 'merge',
    backup_days  INTEGER NOT NULL DEFAULT 14,
    grant_ttl    INTEGER NOT NULL DEFAULT 600,
    grant_via    TEXT NOT NULL DEFAULT 'telegram,cli',
    grant_bind   TEXT NOT NULL DEFAULT 'head',
    ops          TEXT NOT NULL DEFAULT '{}',
    updated_at   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS git_repos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    remote       TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    tag          TEXT NOT NULL DEFAULT '',
    target       TEXT NOT NULL DEFAULT '',
    guard        INTEGER,
    sync         TEXT NOT NULL DEFAULT '',
    ops          TEXT NOT NULL DEFAULT '{}',
    UNIQUE (workspace_id, remote)
);
CREATE TABLE IF NOT EXISTS git_rules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    repo_id      INTEGER,
    position     INTEGER NOT NULL DEFAULT 0,
    name         TEXT NOT NULL DEFAULT '',
    match_branch TEXT NOT NULL DEFAULT '',
    match_key    TEXT NOT NULL DEFAULT '',
    match_where  TEXT NOT NULL DEFAULT 'any',
    ops          TEXT NOT NULL DEFAULT '{}',
    enabled      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_git_rules_ws ON git_rules(workspace_id, repo_id, position);
CREATE TABLE IF NOT EXISTS git_grants (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    remote       TEXT NOT NULL,
    branch       TEXT NOT NULL,
    op           TEXT NOT NULL,
    head_sha     TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'requested',
    via          TEXT NOT NULL DEFAULT '',
    job_id       INTEGER,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL DEFAULT '',
    used_at      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_git_grants_ws ON git_grants(workspace_id, remote, branch, op, status);
CREATE TABLE IF NOT EXISTS git_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    workspace_id INTEGER NOT NULL,
    job_id       INTEGER,
    remote       TEXT NOT NULL DEFAULT '',
    branch       TEXT NOT NULL DEFAULT '',
    op           TEXT NOT NULL,
    decision     TEXT NOT NULL,
    rule         TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_git_audit_ws ON git_audit(workspace_id, ts);
"""


class PolicyError(ValueError):
    """Неверная политика (уровень, операция, маска) или неизвестный репозиторий/правило."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).isoformat(timespec="seconds")


def _csv(values: Iterable[str]) -> str:
    return ",".join(v.strip() for v in values if v and v.strip())


def _list(raw: str) -> list[str]:
    return [v.strip() for v in (raw or "").split(",") if v.strip()]


def _check_ops(ops: dict) -> dict:
    out = {}
    for op, level in (ops or {}).items():
        if op not in OPS:
            raise PolicyError(f"Нет такой операции «{op}». Есть: {', '.join(OPS)}")
        if level not in LEVELS:
            raise PolicyError(f"Нет такого уровня «{level}» у {op}. Есть: {', '.join(LEVELS)}")
        out[op] = level
    return out


def _check_masks(masks: Iterable[str], what: str) -> list[str]:
    out = []
    for m in masks or []:
        m = str(m).strip()
        if not m:
            continue
        if any(c in m for c in " \t,"):
            raise PolicyError(f"Маска {what} «{m}» с пробелом или запятой — по одной маске на элемент")
        out.append(m)
    return out


# --------------------------------------------------------------------------- #
# Модель
# --------------------------------------------------------------------------- #


@dataclass
class Rule:
    name: str = ""
    branch: list[str] = field(default_factory=list)
    key: list[str] = field(default_factory=list)
    where: str = "any"
    ops: dict = field(default_factory=dict)
    enabled: bool = True
    id: int = 0
    position: int = 0

    def validate(self) -> "Rule":
        self.branch = _check_masks(self.branch, "ветки")
        self.key = _check_masks(self.key, "ключа")
        if self.where not in WHERE:
            raise PolicyError(f"where = «{self.where}»: бывает {', '.join(WHERE)}")
        self.ops = _check_ops(self.ops)
        if not self.ops:
            raise PolicyError(f"Правило «{self.name or 'без имени'}» ничего не решает: пустой ops")
        return self

    def matches(self, *, branch: str, key: str, where: str) -> bool:
        if not self.enabled:
            return False
        if self.branch and not any(fnmatch.fnmatchcase(branch, m) for m in self.branch):
            return False
        if self.key:
            if not key:
                return False
            if not any(m == "*" or fnmatch.fnmatchcase(key, m) for m in self.key):
                return False
        return self.where == "any" or self.where == where

    def label(self) -> str:
        if self.name:
            return self.name
        parts = ([f"ветки {', '.join(self.branch)}"] if self.branch else []) \
            + ([f"ключ {', '.join(self.key)}"] if self.key else []) \
            + ([f"где {self.where}"] if self.where != "any" else [])
        return " · ".join(parts) or f"правило #{self.id}"

    def to_toml(self) -> dict:
        out: dict = {}
        if self.name:
            out["name"] = self.name
        match = {k: v for k, v in (("branch", self.branch), ("key", self.key)) if v}
        if self.where != "any":
            match["where"] = self.where
        if match:
            out["match"] = match
        out["ops"] = dict(self.ops)
        if not self.enabled:
            out["enabled"] = False
        return out

    @classmethod
    def from_toml(cls, raw: dict) -> "Rule":
        match = raw.get("match") or {}
        unknown = set(raw) - {"name", "match", "ops", "enabled"}
        if unknown:
            raise PolicyError(f"Неизвестные поля правила: {', '.join(sorted(unknown))}")
        unknown = set(match) - {"branch", "key", "where"}
        if unknown:
            raise PolicyError(f"Неизвестные поля match: {', '.join(sorted(unknown))}")

        def as_list(v):
            return [v] if isinstance(v, str) else list(v or [])

        return cls(name=str(raw.get("name", "")), branch=as_list(match.get("branch")),
                   key=as_list(match.get("key")), where=str(match.get("where", "any")),
                   ops=dict(raw.get("ops") or {}), enabled=bool(raw.get("enabled", True))).validate()


@dataclass
class Repo:
    remote: str
    name: str = ""
    tag: str = ""
    target: str = ""
    guard: Optional[bool] = None
    sync: str = ""
    ops: dict = field(default_factory=dict)
    rules: list[Rule] = field(default_factory=list)
    id: int = 0

    def validate(self) -> "Repo":
        if not self.remote or " " in self.remote:
            raise PolicyError(f"remote «{self.remote}»: нужен вид project/repo")
        if self.sync and self.sync not in SYNC_MODES:
            raise PolicyError(f"sync = «{self.sync}»: бывает {', '.join(SYNC_MODES)}")
        self.ops = _check_ops(self.ops)
        for r in self.rules:
            r.validate()
        return self


@dataclass
class Policy:
    guard: bool = True
    sync: str = "merge"
    backup_days: int = 14
    grant_ttl: int = 600
    grant_via: list[str] = field(default_factory=lambda: list(GRANT_VIA))
    grant_bind: str = "head"
    ops: dict = field(default_factory=lambda: dict(DEFAULT_OPS))
    rules: list[Rule] = field(default_factory=list)
    repos: list[Repo] = field(default_factory=list)

    def validate(self) -> "Policy":
        if self.sync not in SYNC_MODES:
            raise PolicyError(f"sync = «{self.sync}»: бывает {', '.join(SYNC_MODES)}")
        if self.grant_ttl < 30 or self.grant_ttl > 24 * 3600:
            raise PolicyError("grant_ttl — от 30 секунд до суток")
        bad = [v for v in self.grant_via if v not in GRANT_VIA]
        if bad or not self.grant_via:
            raise PolicyError(f"grant_via: {', '.join(GRANT_VIA)} (хотя бы одно)")
        if self.grant_bind not in ("head", "none"):
            raise PolicyError("grant_bind: head | none")
        self.ops = {**DEFAULT_OPS, **_check_ops(self.ops)}
        for r in self.rules:
            r.validate()
        seen = set()
        for repo in self.repos:
            repo.validate()
            if repo.remote in seen:
                raise PolicyError(f"Репозиторий {repo.remote} описан дважды")
            seen.add(repo.remote)
        return self

    def repo(self, remote: str) -> Optional[Repo]:
        return next((r for r in self.repos if r.remote == remote), None)


@dataclass
class Decision:
    op: str
    level: str
    rule: str          # что решило: «репозиторий X · правило Y» / «воркспейс · ops»
    remote: str = ""
    branch: str = ""
    where: str = "any"
    key: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Хранение
# --------------------------------------------------------------------------- #


def ensure_schema(conn) -> None:
    conn.executescript(GIT_SCHEMA)


def _legacy_shared(store: "Store", workspace_id: int) -> list[str]:
    """Старый ``push.protected`` → маски «общих» веток (прод-ветки и так под deny)."""
    raw = store.workspace_settings(workspace_id).get(LEGACY_PROTECTED_SETTING)
    if raw is None:
        return list(SHARED_BRANCHES)
    masks = [m for m in _list(raw) if m not in PROD_BRANCHES]
    return masks


def default_policy(store: Optional["Store"] = None, workspace_id: int = 0) -> Policy:
    rules = [Rule(name=r["name"], branch=list(r.get("branch", [])), where=r.get("where", "any"),
                  ops=dict(r["ops"])) for r in DEFAULT_RULES]
    if store is not None:
        shared = _legacy_shared(store, workspace_id)
        rules = [r for r in rules if r.name != "общие ветки" or shared]
        for r in rules:
            if r.name == "общие ветки":
                r.branch = shared
    return Policy(rules=rules).validate()


def has_policy(store: "Store", workspace_id: int) -> bool:
    ensure_schema(store.conn)
    return store.conn.execute("SELECT 1 FROM git_policy WHERE workspace_id = ?",
                              (workspace_id,)).fetchone() is not None


def ensure(store: "Store", workspace_id: Optional[int] = None) -> Policy:
    """Политика воркспейса; нет в базе — записать встроенную (с переносом push.protected)."""
    ws = workspace_id or store.workspace_id
    if not has_policy(store, ws):
        save(store, default_policy(store, ws), ws)
    return load(store, ws)


def _rule_from_row(r) -> Rule:
    return Rule(id=r["id"], position=r["position"], name=r["name"], branch=_list(r["match_branch"]),
                key=_list(r["match_key"]), where=r["match_where"] or "any",
                ops=json.loads(r["ops"] or "{}"), enabled=bool(r["enabled"]))


def load(store: "Store", workspace_id: Optional[int] = None) -> Policy:
    ws = workspace_id or store.workspace_id
    ensure_schema(store.conn)
    row = store.conn.execute("SELECT * FROM git_policy WHERE workspace_id = ?", (ws,)).fetchone()
    if row is None:
        return default_policy(store, ws)
    rules = store.conn.execute(
        "SELECT * FROM git_rules WHERE workspace_id = ? ORDER BY position, id", (ws,)).fetchall()
    repos = []
    for r in store.conn.execute("SELECT * FROM git_repos WHERE workspace_id = ? ORDER BY remote", (ws,)):
        repos.append(Repo(id=r["id"], remote=r["remote"], name=r["name"], tag=r["tag"], target=r["target"],
                          guard=None if r["guard"] is None else bool(r["guard"]), sync=r["sync"],
                          ops=json.loads(r["ops"] or "{}"),
                          rules=[_rule_from_row(x) for x in rules if x["repo_id"] == r["id"]]))
    return Policy(guard=bool(row["guard"]), sync=row["sync"], backup_days=row["backup_days"],
                  grant_ttl=row["grant_ttl"], grant_via=_list(row["grant_via"]), grant_bind=row["grant_bind"],
                  ops={**DEFAULT_OPS, **json.loads(row["ops"] or "{}")},
                  rules=[_rule_from_row(x) for x in rules if x["repo_id"] is None], repos=repos)


def save(store: "Store", policy: Policy, workspace_id: Optional[int] = None) -> Policy:
    """Записать политику целиком (воркспейс, репозитории, правила) — одной транзакцией."""
    ws = workspace_id or store.workspace_id
    policy.validate()
    ensure_schema(store.conn)
    conn = store.conn
    try:
        conn.execute("DELETE FROM git_rules WHERE workspace_id = ?", (ws,))
        conn.execute("DELETE FROM git_repos WHERE workspace_id = ?", (ws,))
        conn.execute(
            "INSERT OR REPLACE INTO git_policy (workspace_id, guard, sync, backup_days, grant_ttl, grant_via,"
            " grant_bind, ops, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ws, int(policy.guard), policy.sync, policy.backup_days, policy.grant_ttl, _csv(policy.grant_via),
             policy.grant_bind, json.dumps({k: v for k, v in policy.ops.items() if DEFAULT_OPS.get(k) != v}),
             _iso()))

        def put_rules(rules: list[Rule], repo_id: Optional[int]) -> None:
            for pos, r in enumerate(rules):
                conn.execute(
                    "INSERT INTO git_rules (workspace_id, repo_id, position, name, match_branch, match_key,"
                    " match_where, ops, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ws, repo_id, pos, r.name, _csv(r.branch), _csv(r.key), r.where, json.dumps(r.ops),
                     int(r.enabled)))

        put_rules(policy.rules, None)
        for repo in policy.repos:
            cur = conn.execute(
                "INSERT INTO git_repos (workspace_id, remote, name, tag, target, guard, sync, ops)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ws, repo.remote, repo.name, repo.tag, repo.target,
                 None if repo.guard is None else int(repo.guard), repo.sync, json.dumps(repo.ops)))
            put_rules(repo.rules, int(cur.lastrowid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return load(store, ws)


# --------------------------------------------------------------------------- #
# Репозитории: origin → project/repo
# --------------------------------------------------------------------------- #

_REMOTE_RE = re.compile(r"(?:[:/])(?:scm/)?([^/:]+)/([^/]+?)(?:\.git)?/?$")

Runner = Callable[[list[str]], tuple[int, str]]


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def remote_slug(url: str) -> str:
    """URL origin → ``project/repo`` (ssh, https, Bitbucket ``/scm/``, GitHub)."""
    m = _REMOTE_RE.search((url or "").strip())
    return f"{m.group(1)}/{m.group(2)}".lower() if m else ""


def repo_of(path: str, runner: Runner = _run) -> dict:
    """Что за репозиторий в папке: remote, корень, ветка, HEAD, ветка по умолчанию origin."""
    code, top = runner(["git", "-C", path, "rev-parse", "--show-toplevel"])
    if code != 0:
        return {}
    root = top.strip()

    def one(*args: str) -> str:
        code, out = runner(["git", "-C", root, *args])
        return out.strip().splitlines()[0].strip() if code == 0 and out.strip() else ""

    target = one("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    return {"root": root, "remote": remote_slug(one("remote", "get-url", "origin")),
            "branch": one("rev-parse", "--abbrev-ref", "HEAD"), "head": one("rev-parse", "HEAD"),
            "target": target.split("/", 1)[-1] if target else ""}


def where_of(store: "Store", root: str) -> str:
    """``job`` — рабочая копия работы jwu, иначе ``main``."""
    try:
        rows = store.conn.execute(
            "SELECT ref FROM job_resources WHERE kind = 'worktree' AND removed_at = ''").fetchall()
    except Exception:  # noqa: BLE001 — старая БД без таблицы
        return "main"
    real = str(Path(root).resolve())
    return "job" if any(str(Path(r["ref"]).resolve()) == real for r in rows) else "main"


def discover(store: "Store", workspace_id: Optional[int] = None, runner: Runner = _run) -> list[Repo]:
    """Найти репозитории по папкам воркспейса и завести недостающие (всё наследуют)."""
    ws_id = workspace_id or store.workspace_id
    policy = ensure(store, ws_id)
    ws = store.get_workspace(ws_id)
    added = []
    for p in (ws.paths if ws else []):
        info = repo_of(p.path, runner)
        if not info.get("remote") or policy.repo(info["remote"]):
            continue
        repo = Repo(remote=info["remote"], name=info["remote"].split("/")[-1],
                    tag=",".join(sorted(p.tags)), target=info.get("target", ""))
        policy.repos.append(repo)
        added.append(repo)
    if added:
        save(store, policy, ws_id)
    return added


# --------------------------------------------------------------------------- #
# Решение
# --------------------------------------------------------------------------- #


def branch_key(branch: str) -> str:
    """Префикс задачи из имени ветки: ``feature/PROJ-12-x`` → ``PROJ``."""
    m = KEY_RE.search(branch or "")
    return m.group(1) if m else ""


def decide(policy: Policy, op: str, *, remote: str = "", branch: str = "", where: str = "any") -> Decision:
    if op not in OPS:
        raise PolicyError(f"Нет такой операции «{op}». Есть: {', '.join(OPS)}")
    key = branch_key(branch)
    base = dict(op=op, remote=remote, branch=branch, where=where, key=key)
    repo = policy.repo(remote) if remote else None
    if repo is not None:
        for r in repo.rules:
            if op in r.ops and r.matches(branch=branch, key=key, where=where):
                return Decision(level=r.ops[op], rule=f"репозиторий {repo.remote} · {r.label()}", **base)
    for r in policy.rules:
        if op in r.ops and r.matches(branch=branch, key=key, where=where):
            return Decision(level=r.ops[op], rule=f"воркспейс · {r.label()}", **base)
    if repo is not None and op in repo.ops:
        return Decision(level=repo.ops[op], rule=f"репозиторий {repo.remote} · операции", **base)
    return Decision(level=policy.ops.get(op, DEFAULT_OPS[op]), rule="воркспейс · операции", **base)


def guard_on(policy: Policy, remote: str = "") -> bool:
    repo = policy.repo(remote) if remote else None
    return policy.guard if repo is None or repo.guard is None else repo.guard


def sync_mode(policy: Policy, remote: str = "") -> str:
    repo = policy.repo(remote) if remote else None
    return (repo.sync if repo and repo.sync else policy.sync)


def target_branch(policy: Policy, remote: str = "", fallback: str = "") -> str:
    repo = policy.repo(remote) if remote else None
    return (repo.target if repo and repo.target else fallback)


# --------------------------------------------------------------------------- #
# Допуски (grant)
# --------------------------------------------------------------------------- #


def request_grant(store: "Store", *, remote: str, branch: str, op: str, head: str = "",
                  detail: str = "", job_id: Optional[int] = None) -> dict:
    """Запрос допуска (агент упёрся в ``grant``). Тот же открытый запрос не плодится."""
    ensure_schema(store.conn)
    row = store.conn.execute(
        "SELECT * FROM git_grants WHERE workspace_id = ? AND remote = ? AND branch = ? AND op = ?"
        " AND head_sha = ? AND status = 'requested' ORDER BY id DESC LIMIT 1",
        (store.workspace_id, remote, branch, op, head)).fetchone()
    if row is not None:
        return {**dict(row), "new": False}
    cur = store.conn.execute(
        "INSERT INTO git_grants (workspace_id, remote, branch, op, head_sha, detail, status, job_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'requested', ?, ?)",
        (store.workspace_id, remote, branch, op, head, detail, job_id, _iso()))
    store.conn.commit()
    return {**dict(store.conn.execute("SELECT * FROM git_grants WHERE id = ?", (cur.lastrowid,)).fetchone()),
            "new": True}


def pending_grants(store: "Store") -> list[dict]:
    ensure_schema(store.conn)
    return [dict(r) for r in store.conn.execute(
        "SELECT * FROM git_grants WHERE workspace_id = ? AND status = 'requested' ORDER BY id",
        (store.workspace_id,))]


def answer_grant(store: "Store", grant_id: int, *, allow: bool, via: str,
                 policy: Optional[Policy] = None) -> dict:
    """Ответ пользователя на запрос: допуск на ``grant_ttl`` или отказ. Только бот и CLI."""
    if via not in GRANT_VIA:
        raise PolicyError(f"via: {', '.join(GRANT_VIA)}")
    policy = policy or load(store)
    if via not in policy.grant_via:
        raise PolicyError(f"Допуск через {via} выключен политикой (grant_via: {', '.join(policy.grant_via)})")
    ensure_schema(store.conn)
    row = store.conn.execute("SELECT * FROM git_grants WHERE id = ? AND workspace_id = ?",
                             (grant_id, store.workspace_id)).fetchone()
    if row is None:
        raise PolicyError(f"Запроса допуска #{grant_id} нет")
    if row["status"] != "requested":
        raise PolicyError(f"Запрос #{grant_id} уже {row['status']}")
    status = "granted" if allow else "denied"
    expires = _iso(_now() + timedelta(seconds=policy.grant_ttl)) if allow else ""
    store.conn.execute("UPDATE git_grants SET status = ?, via = ?, expires_at = ? WHERE id = ?",
                       (status, via, expires, grant_id))
    store.conn.commit()
    audit(store, op=row["op"], decision=f"grant:{status}", remote=row["remote"], branch=row["branch"],
          job_id=row["job_id"], detail=f"запрос #{grant_id} через {via}")
    return dict(store.conn.execute("SELECT * FROM git_grants WHERE id = ?", (grant_id,)).fetchone())


def grant_direct(store: "Store", *, remote: str, branch: str, op: str, head: str = "", via: str = "cli",
                 policy: Optional[Policy] = None) -> dict:
    """Допуск без запроса агента — пользователь выдал заранее (``jwu git grant``)."""
    req = request_grant(store, remote=remote, branch=branch, op=op, head=head, detail="выдан заранее")
    return answer_grant(store, int(req["id"]), allow=True, via=via, policy=policy)


def consume_grant(store: "Store", *, remote: str, branch: str, op: str, head: str = "",
                  policy: Optional[Policy] = None) -> Optional[dict]:
    """Живой допуск под операцию → отметить использованным и вернуть; нет — None."""
    policy = policy or load(store)
    ensure_schema(store.conn)
    now = _iso()
    rows = store.conn.execute(
        "SELECT * FROM git_grants WHERE workspace_id = ? AND remote = ? AND branch = ? AND op = ?"
        " AND status = 'granted' AND expires_at > ? ORDER BY id DESC",
        (store.workspace_id, remote, branch, op, now)).fetchall()
    for row in rows:
        if policy.grant_bind == "head" and row["head_sha"] and head and row["head_sha"] != head:
            continue
        if policy.grant_bind == "head" and row["head_sha"] and not head:
            continue
        store.conn.execute("UPDATE git_grants SET status = 'used', used_at = ? WHERE id = ?", (now, row["id"]))
        store.conn.commit()
        return dict(row)
    return None


# --------------------------------------------------------------------------- #
# Журнал
# --------------------------------------------------------------------------- #


def audit(store: "Store", *, op: str, decision: str, remote: str = "", branch: str = "", rule: str = "",
          detail: str = "", job_id: Optional[int] = None) -> None:
    ensure_schema(store.conn)
    store.conn.execute(
        "INSERT INTO git_audit (ts, workspace_id, job_id, remote, branch, op, decision, rule, detail)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_iso(), store.workspace_id, job_id, remote, branch, op, decision, rule, detail[:500]))
    store.conn.commit()


def audit_log(store: "Store", limit: int = 50) -> list[dict]:
    ensure_schema(store.conn)
    return [dict(r) for r in store.conn.execute(
        "SELECT * FROM git_audit WHERE workspace_id = ? ORDER BY id DESC LIMIT ?",
        (store.workspace_id, limit))]


# --------------------------------------------------------------------------- #
# TOML: формат правки и переноса
# --------------------------------------------------------------------------- #


def to_toml(policy: Policy) -> str:
    import tomli_w

    git: dict = {"guard": policy.guard, "sync": policy.sync, "backup_days": policy.backup_days,
                 "grant": {"ttl": policy.grant_ttl, "via": list(policy.grant_via), "bind": policy.grant_bind},
                 "ops": dict(policy.ops)}
    if policy.rules:
        git["rule"] = [r.to_toml() for r in policy.rules]
    if policy.repos:
        repos = []
        for repo in policy.repos:
            item: dict = {"remote": repo.remote}
            for k in ("name", "tag", "target", "sync"):
                if getattr(repo, k):
                    item[k] = getattr(repo, k)
            if repo.guard is not None:
                item["guard"] = repo.guard
            if repo.ops:
                item["ops"] = dict(repo.ops)
            if repo.rules:
                item["rule"] = [r.to_toml() for r in repo.rules]
            repos.append(item)
        git["repo"] = repos
    head = ("# Политика git воркспейса (jwu). Уровни: allow | ask | grant | deny.\n"
            f"# Операции: {', '.join(OPS)}.\n"
            "# Правила — по порядку, первое совпадение; репозиторий раньше воркспейса.\n"
            "# match: branch (маски), key (префикс задачи в ветке, «*» — любой), where = any | job | main.\n\n")
    return head + tomli_w.dumps({"git": git})


def from_toml(text: str) -> Policy:
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover
        import tomli as tomllib  # type: ignore[no-redef]

    try:
        data = tomllib.loads(text)
    except Exception as exc:  # noqa: BLE001
        raise PolicyError(f"TOML не разобрать: {exc}") from exc
    git = data.get("git")
    if not isinstance(git, dict):
        raise PolicyError("Нет секции [git]")
    unknown = set(git) - {"guard", "sync", "backup_days", "grant", "ops", "rule", "repo"}
    if unknown:
        raise PolicyError(f"Неизвестные поля [git]: {', '.join(sorted(unknown))}")
    grant = git.get("grant") or {}
    repos = []
    for raw in git.get("repo") or []:
        unknown = set(raw) - {"remote", "name", "tag", "target", "guard", "sync", "ops", "rule"}
        if unknown:
            raise PolicyError(f"Неизвестные поля репозитория: {', '.join(sorted(unknown))}")
        repos.append(Repo(remote=str(raw.get("remote", "")).lower(), name=str(raw.get("name", "")),
                          tag=str(raw.get("tag", "")), target=str(raw.get("target", "")),
                          guard=raw.get("guard"), sync=str(raw.get("sync", "")), ops=dict(raw.get("ops") or {}),
                          rules=[Rule.from_toml(r) for r in raw.get("rule") or []]))
    return Policy(guard=bool(git.get("guard", True)), sync=str(git.get("sync", "merge")),
                  backup_days=int(git.get("backup_days", 14)), grant_ttl=int(grant.get("ttl", 600)),
                  grant_via=list(grant.get("via", list(GRANT_VIA))), grant_bind=str(grant.get("bind", "head")),
                  ops=dict(git.get("ops") or {}), rules=[Rule.from_toml(r) for r in git.get("rule") or []],
                  repos=repos).validate()


# --------------------------------------------------------------------------- #
# Выгрузка в память (перенос на другую машину): политика и правила, без допусков и журнала
# --------------------------------------------------------------------------- #


def export_dict(store: "Store", workspace_id: int) -> Optional[str]:
    return to_toml(load(store, workspace_id)) if has_policy(store, workspace_id) else None


def import_text(store: "Store", workspace_id: int, text: str) -> None:
    save(store, from_toml(text), workspace_id)
