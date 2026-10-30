"""Общая уборка за всеми сессиями: что осталось, когда что-то пошло не так.

Работа закрылась, а стенд остался; сессия упала посреди работы; копию положили руками и
забыли; вложения скачались и не удалились. ``scan`` находит это по всем воркспейсам и ничего
не трогает; ``apply`` убирает выбранные группы.

Группы:

* ``closed_jobs`` — неубранное у закрытых (done/cancelled) работ: стенд, контейнеры,
  вольюмы, сети, образы, каталоги, рабочие копии (``jobspace.cleanup``);
* ``orphan_docker`` — docker-ресурсы с меткой работы или compose-проектом ``jwu-job<N>``,
  а работы N нет или она закрыта;
* ``orphan_worktrees`` — папки в каталоге работ с ``-job<N>`` в имени, у которых работы нет
  или она закрыта и копия за ней не записана; и копии субагентов Claude Code
  (``<репо>/.claude/worktrees/agent-*``), не менявшиеся дольше ``AGENT_TREE_IDLE_HOURS`` —
  их убирает ``gitops.worktree_remove`` под политикой, несохранённое уходит в бэкап;
* ``temp`` — скачанные вложения во временной папке jwu старше суток;
* ``dead_sessions`` — замолчавшие сессии: их активные работы уходят в паузу (снова свободны
  на доске), запись о сессии удаляется;
* ``dead_workers`` — воркеры оркестратора, чей процесс завершился без «готово»: запись
  убирается, их открытые работы — в паузу (продолжит новый воркер или сессия).

Открытые (активные, на паузе) работы и их ресурсы не трогаются никогда. Копия с
незакоммиченным или незапушенным попадает в ``kept`` — убрать её можно только с ``force``.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

from . import jobspace, todo

if TYPE_CHECKING:
    from .store import Store

GROUPS = ("closed_jobs", "orphan_docker", "orphan_worktrees", "temp", "dead_sessions", "dead_workers")
CLOSED = ("done", "cancelled")
TEMP_MAX_AGE_HOURS = 24
_JOB_IN_NAME = re.compile(r"-job(\d+)(?:-|$)")
_COMPOSE_JOB = re.compile(r"^jwu-job(\d+)$")


DEAD_SESSION_HOURS = 2


def _silent_hours(info: dict) -> float:
    seen = info.get("seen", "")
    return todo._days_since(seen) * 24 if seen else 1e9


def temp_root() -> Path:
    return Path(tempfile.gettempdir()) / "jwu"


def _job_status(store: "Store") -> dict[int, str]:
    """Статусы всех работ во всех воркспейсах (номера работ общие)."""
    return {int(r["id"]): r["status"] for r in store.conn.execute("SELECT id, status FROM jobs").fetchall()}


def _docker_job_ids(runner: jobspace.Runner) -> set[int]:
    """Номера работ, у которых в docker что-то есть: по метке и по compose-проекту."""
    ids: set[int] = set()
    lists = (["docker", "ps", "-a", "--format", '{{.Label "' + jobspace.LABEL + '"}}|{{.Label "com.docker.compose.project"}}'],
             ["docker", "volume", "ls", "--format", '{{.Label "' + jobspace.LABEL + '"}}|{{.Label "com.docker.compose.project"}}'],
             ["docker", "network", "ls", "--format", '{{.Label "' + jobspace.LABEL + '"}}|{{.Label "com.docker.compose.project"}}'])
    for cmd in lists:
        code, out = runner(cmd)
        if code != 0:
            continue
        for line in out.splitlines():
            mark, _, project = line.partition("|")
            if mark.strip().isdigit():
                ids.add(int(mark.strip()))
            m = _COMPOSE_JOB.match(project.strip())
            if m:
                ids.add(int(m.group(1)))
    code, out = runner(["docker", "images", "-q", "--filter", f"label={jobspace.LABEL}"])
    for image in (out.split() if code == 0 else []):
        c, label = runner(["docker", "image", "inspect", "--format",
                           '{{index .Config.Labels "' + jobspace.LABEL + '"}}', image])
        if c == 0 and label.strip().isdigit():
            ids.add(int(label.strip()))
    return ids


AGENT_TREE_IDLE_HOURS = DEAD_SESSION_HOURS


def _agent_tree_idle_hours(path: Path, now: float) -> float:
    """Сколько копия субагента не менялась: папка, её индекс и HEAD в общем .git."""
    stamps = [path.stat().st_mtime]
    marker = path / ".git"
    if marker.is_file():
        raw = marker.read_text(encoding="utf-8", errors="replace").strip()
        if raw.startswith("gitdir:"):
            gitdir = Path(raw[7:].strip())
            for name in ("index", "HEAD", "logs/HEAD"):
                try:
                    stamps.append((gitdir / name).stat().st_mtime)
                except OSError:
                    pass
    return (now - max(stamps)) / 3600


def _agent_trees(store: "Store", ws, now: float) -> list[dict]:
    """Копии субагентов Claude Code (``<репо>/.claude/worktrees/agent-*``), давно без изменений."""
    from .gitops import AGENT_TREES

    out = []
    for p in ws.paths:
        root = Path(p.path) / AGENT_TREES
        for path in sorted(root.glob("agent-*")) if root.is_dir() else []:
            if not path.is_dir():
                continue
            idle = _agent_tree_idle_hours(path, now)
            if idle >= AGENT_TREE_IDLE_HOURS:
                out.append({"path": str(path), "kind": "agent", "workspace": ws.slug, "idle_hours": int(idle),
                            "note": "несохранённое уйдёт в бэкап (патч + backup-ссылка)"})
    return out


def _registered_trees(store: "Store") -> set[str]:
    rows = store.conn.execute("SELECT ref FROM job_resources WHERE kind = 'worktree' AND removed_at = ''").fetchall()
    return {str(Path(r["ref"]).resolve()) for r in rows}


def scan(store: "Store", *, runner: jobspace.Runner = jobspace._run, now: Optional[float] = None) -> dict:
    """Что можно убрать — по группам, ничего не трогая."""
    now = now or time.time()
    status = _job_status(store)
    original_ws = store.workspace_id
    out: dict = {g: [] for g in GROUPS}
    out["kept"] = []

    try:
        # 1. закрытые работы с неубранным — по каждому воркспейсу
        for ws in store.list_workspaces(include_archived=True):
            store.use_workspace(ws.id)
            for job_id in sorted({r["job_id"] for r in store.all_open_job_resources() if r["job_status"] in CLOSED}):
                rep = jobspace.cleanup(store, job_id, dry_run=True, runner=runner)
                out["closed_jobs"].append({"job_id": job_id, "workspace": ws.slug, "items": rep.removed,
                                           "kept": rep.kept})
                out["kept"] += [{"group": "closed_jobs", "job_id": job_id, "why": k} for k in rep.kept]

            # 3. рабочие копии без живой работы
            root = jobspace.jobs_dir(store, ws.id)
            registered = _registered_trees(store)
            for path in sorted(root.iterdir()) if root and root.is_dir() else []:
                m = _JOB_IN_NAME.search(path.name)
                if not path.is_dir() or not m or str(path.resolve()) in registered:
                    continue
                jid = int(m.group(1))
                if status.get(jid) not in (None, *CLOSED):
                    continue  # работа открыта — не трогаем
                state = jobspace.worktree_state(str(path), runner)
                item = {"path": str(path), "job_id": jid, "workspace": ws.slug,
                        "job": status.get(jid, "нет такой работы"),
                        "dirty": len(state["dirty"]), "unpushed": state["unpushed"]}
                if state["dirty"] or state["unpushed"]:
                    out["kept"].append({"group": "orphan_worktrees", **item,
                                        "why": f"незакоммиченного {item['dirty']}, незапушенных коммитов {item['unpushed']}"})
                else:
                    out["orphan_worktrees"].append(item)
            # 3б. копии субагентов (isolation: worktree), брошенные без живого агента
            out["orphan_worktrees"] += _agent_trees(store, ws, now)
    finally:
        store.use_workspace(original_ws)

    # 2. docker без живой работы (закрытые с записанными ресурсами уже в группе 1)
    if runner(["docker", "info", "--format", "{{.ServerVersion}}"])[0] == 0:
        in_closed = {j["job_id"] for j in out["closed_jobs"]}
        for jid in sorted(_docker_job_ids(runner)):
            if status.get(jid) in (None, *CLOSED) and jid not in in_closed:
                rep = jobspace.cleanup(store, jid, dry_run=True, runner=runner)
                if rep.removed:
                    out["orphan_docker"].append({"job_id": jid, "job": status.get(jid, "нет такой работы"),
                                                 "items": rep.removed})

    # 4. старые скачанные вложения
    root = temp_root()
    for path in sorted(root.iterdir()) if root.is_dir() else []:
        targets = sorted(path.iterdir()) if path.name == "pr" and path.is_dir() else [path]
        for target in targets:
            age_h = (now - target.stat().st_mtime) / 3600
            if target.is_dir() and age_h >= TEMP_MAX_AGE_HOURS:
                out["temp"].append({"path": str(target), "age_hours": int(age_h)})

    # 5. замолчавшие сессии и их активные работы во всех воркспейсах
    # «Мёртвая» по процессу ещё не значит замолчавшая: процесс, записавший отметку, мог смениться.
    # Освобождаем работы только у сессий, которые молчат дольше DEAD_SESSION_HOURS.
    dead = {name: info for name, info in todo.sessions(store).items()
            if not info.get("alive") and _silent_hours(info) >= DEAD_SESSION_HOURS}
    held: dict[str, list[int]] = {name: [] for name in dead}
    try:
        for ws in store.list_workspaces(include_archived=True):
            store.use_workspace(ws.id)
            for job in store.list_jobs(status="active"):
                session = (todo._taken(job) or {}).get("session")
                if session in held:
                    held[session].append(job.id)
    finally:
        store.use_workspace(original_ws)
    for name, info in dead.items():
        out["dead_sessions"].append({"session": name, "seen_ago": info.get("seen_ago", ""), "jobs": held[name]})

    # 6. упавшие воркеры оркестратора
    from . import workers

    for w in workers.dead(store):
        out["dead_workers"].append({"session": w["name"], "jobs": w.get("jobs", []), "seen_ago": w.get("ago", "")})
    return out


def apply(store: "Store", groups: Iterable[str], *, force: bool = False,
          runner: jobspace.Runner = jobspace._run, plan: Optional[dict] = None) -> dict:
    """Убрать выбранные группы. ``force`` — убрать и копии с несохранённым (из ``kept``)."""
    groups = [g for g in groups if g in GROUPS]
    plan = plan or scan(store, runner=runner)
    done: dict = {g: [] for g in groups}
    errors: list[str] = []
    original_ws = store.workspace_id
    try:
        for job in plan["closed_jobs"] if "closed_jobs" in groups else []:
            ws = store.get_workspace_by_slug(job["workspace"])
            store.use_workspace(ws.id)
            rep = jobspace.cleanup(store, job["job_id"], force=force, runner=runner)
            done["closed_jobs"] += [f"#{job['job_id']}: {x}" for x in rep.removed]
            errors += [f"#{job['job_id']}: {e}" for e in rep.errors]
    finally:
        store.use_workspace(original_ws)

    for job in plan["orphan_docker"] if "orphan_docker" in groups else []:
        rep = jobspace.cleanup(store, job["job_id"], runner=runner)
        done["orphan_docker"] += [f"#{job['job_id']}: {x}" for x in rep.removed]
        errors += [f"#{job['job_id']}: {e}" for e in rep.errors]

    if "orphan_worktrees" in groups:
        trees = list(plan["orphan_worktrees"])
        if force:
            trees += [k for k in plan["kept"] if k.get("group") == "orphan_worktrees"]
        for item in trees:
            path = item["path"]
            if item.get("kind") == "agent":
                # через политику и с бэкапом несохранённого; «да» на уборку уже получено
                from . import gitops

                ws = store.get_workspace_by_slug(item["workspace"])
                try:
                    if ws:
                        store.use_workspace(ws.id)
                    res = gitops.worktree_remove(store, path, user_approved=True)
                except gitops.Blocked as b:
                    errors.append(f"{path}: {b.reason}")
                    continue
                except gitops.GitOpError as exc:
                    errors.append(f"{path}: {exc}")
                    continue
                finally:
                    store.use_workspace(original_ws)
                if not res.get("ok", False):
                    errors.append(f"{path}: {res.get('reason', 'не убрана')}")
                    continue
                done["orphan_worktrees"].append(path + (f" (бэкап {res['backup']['ref']})"
                                                        if res["backup"]["ref"] else ""))
                continue
            code, common = runner(["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"])
            if code == 0 and common.strip():
                main_repo = str(Path(common.strip().splitlines()[-1]).parent)
                code, out = runner(["git", "-C", main_repo, "worktree", "remove"] + (["--force"] if force else []) + [path])
                runner(["git", "-C", main_repo, "worktree", "prune"])
                if code != 0:
                    errors.append(f"{path}: {out[-200:]}")
                    continue
            elif force:
                shutil.rmtree(path, ignore_errors=True)
            else:
                errors.append(f"{path}: не git-копия — без force не удаляю")
                continue
            done["orphan_worktrees"].append(path)

    for item in plan["temp"] if "temp" in groups else []:
        shutil.rmtree(item["path"], ignore_errors=True)
        done["temp"].append(item["path"])

    try:
        for item in plan["dead_sessions"] if "dead_sessions" in groups else []:
            released: list[int] = []
            for ws in store.list_workspaces(include_archived=True):
                store.use_workspace(ws.id)
                released += todo.release(store, item["session"])
            done["dead_sessions"].append({"session": item["session"], "released_jobs": released})
    finally:
        store.use_workspace(original_ws)

    if "dead_workers" in groups:
        from . import workers

        for item in plan["dead_workers"]:
            try:
                res = workers.stop(store, item["session"], force=True)
                done["dead_workers"].append({"session": item["session"], "released_jobs": res["paused_jobs"]})
            except workers.WorkerError as exc:
                errors.append(f"{item['session']}: {exc}")

    return {"done": done, "errors": errors}
