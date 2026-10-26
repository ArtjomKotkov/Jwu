"""Рабочее место работы: свои git-копии, свой стенд, уборка и контроль свободного места.

Работа, которая правит код, получает **рабочую копию** (git worktree) на каждый
репозиторий — в общей папке воркспейса (настройка ``jobs.dir``), с номером работы в имени:
``<папка>/backend-job442-PROJ-1160``. Чистый анализ копию не заводит.

Всё, что работа создаёт, запоминается в ``job_resources``: копии, docker-ресурсы, каталоги
кеша. Docker-ресурсы стенда дополнительно помечаются ``jwu.job=<id>`` (а compose-проект
называется ``jwu-job<id>``) — по метке уборка найдёт и то, что забыли записать, и не тронет
чужое. Перед закрытием работы ``cleanup`` сносит стенд, контейнеры, вольюмы, сети, образы,
кеш и копии. Копию с незакоммиченным или незапушенным — не трогает без ``force``.

Место на диске проверяется перед созданием копии и подъёмом стенда (``check_space``):
docker и агент виснут на полном диске. Порог — настройка ``disk.min_free_gb`` (20 ГБ).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from .models import Job
    from .store import Store

JOBS_DIR_SETTING = "jobs.dir"
DISK_MIN_SETTING = "disk.min_free_gb"
DEFAULT_MIN_FREE_GB = 20.0
LABEL = "jwu.job"
RESOURCE_KINDS = ("worktree", "container", "volume", "network", "image", "compose", "path")

Runner = Callable[[list[str]], tuple[int, str]]


class JobSpaceError(RuntimeError):
    pass


def _run(cmd: list[str], cwd: Optional[str] = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        return 127, f"{cmd[0]}: не найден"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(cmd)}: таймаут"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def label(job_id: int) -> str:
    return f"{LABEL}={job_id}"


def compose_project(job_id: int) -> str:
    return f"jwu-job{job_id}"


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #


def jobs_dir(store: "Store", workspace_id: int) -> Optional[Path]:
    raw = (store.workspace_settings(workspace_id).get(JOBS_DIR_SETTING) or "").strip()
    return Path(raw).expanduser() if raw else None


def set_jobs_dir(store: "Store", workspace_id: int, path: str) -> Path:
    target = Path(path).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    store.set_workspace_settings(workspace_id, {JOBS_DIR_SETTING: str(target)})
    return target


def min_free_gb(store: "Store", workspace_id: int) -> float:
    raw = (store.workspace_settings(workspace_id).get(DISK_MIN_SETTING) or "").strip()
    try:
        return max(0.0, float(raw.replace(",", "."))) if raw else DEFAULT_MIN_FREE_GB
    except ValueError:
        return DEFAULT_MIN_FREE_GB


def set_min_free_gb(store: "Store", workspace_id: int, value: float) -> float:
    store.set_workspace_settings(workspace_id, {DISK_MIN_SETTING: str(max(0.0, float(value)))})
    return min_free_gb(store, workspace_id)


# --------------------------------------------------------------------------- #
# Место на диске
# --------------------------------------------------------------------------- #


@dataclass
class DiskReport:
    path: str
    free_gb: float
    total_gb: float
    min_free_gb: float
    docker: dict = field(default_factory=dict)    # {"images": "12GB (5GB reclaimable)", …}
    leftovers: list[dict] = field(default_factory=list)   # неубранное у закрытых работ

    @property
    def ok(self) -> bool:
        return self.free_gb >= self.min_free_gb

    def to_dict(self) -> dict:
        return {**asdict(self), "ok": self.ok}

    def summary(self) -> str:
        mark = "ок" if self.ok else "МАЛО"
        return f"свободно {self.free_gb:.1f} из {self.total_gb:.0f} ГБ (порог {self.min_free_gb:.0f}) — {mark}"


def _existing(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def docker_usage(runner: Runner = _run) -> dict:
    """``docker system df``: сколько занимают образы, контейнеры, вольюмы, кеш сборки."""
    code, out = runner(["docker", "system", "df", "--format", "{{json .}}"])
    if code != 0:
        return {}
    usage: dict[str, str] = {}
    for line in out.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        usage[str(row.get("Type", "?"))] = f"{row.get('Size', '?')} (можно освободить {row.get('Reclaimable', '?')})"
    return usage


def check_space(store: "Store", workspace_id: int, path: Optional[str] = None, *,
                with_docker: bool = True, runner: Runner = _run) -> DiskReport:
    """Свободное место там, где будут копии (или ``path``), плюс docker и неубранное."""
    target = Path(path).expanduser() if path else (jobs_dir(store, workspace_id) or Path.home())
    usage = shutil.disk_usage(_existing(target))
    leftovers = [r for r in store.all_open_job_resources() if r["job_status"] in ("done", "cancelled")]
    return DiskReport(path=str(target), free_gb=usage.free / 1024 ** 3, total_gb=usage.total / 1024 ** 3,
                      min_free_gb=min_free_gb(store, workspace_id),
                      docker=docker_usage(runner) if with_docker else {}, leftovers=leftovers)


def require_space(store: "Store", workspace_id: int, path: Optional[str] = None, *,
                  runner: Runner = _run) -> DiskReport:
    report = check_space(store, workspace_id, path, runner=runner)
    if not report.ok:
        hint = ""
        if report.leftovers:
            jobs = sorted({r["job_id"] for r in report.leftovers})
            hint = f" Не убрано у закрытых работ: {', '.join(f'#{j}' for j in jobs)} — jwu job cleanup <id>."
        raise JobSpaceError(f"Мало места: {report.summary()}.{hint} Освободи место, потом продолжай.")
    return report


# --------------------------------------------------------------------------- #
# Рабочие копии
# --------------------------------------------------------------------------- #


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")


def worktree_name(repo: Path, job: "Job") -> str:
    anchor = job.task_key or job.feature_key
    return _slug(f"{repo.name}-job{job.id}" + (f"-{anchor}" if anchor else ""))


def _repo_root(path: str, runner: Runner) -> Path:
    code, out = runner(["git", "-C", str(Path(path).expanduser()), "rev-parse", "--show-toplevel"])
    if code != 0:
        raise JobSpaceError(f"не git-репозиторий: {path}")
    return Path(out.splitlines()[-1].strip())


def add_worktree(store: "Store", job: "Job", repo: str, *, branch: str = "", base: str = "",
                 runner: Runner = _run) -> dict:
    """Рабочая копия репозитория для работы. Уже есть — вернуть её.

    ``branch`` — ветка копии: есть — берётся она, нет — создаётся от ``base``
    (по умолчанию HEAD основного репозитория). Пусто — ``job<id>-<якорь>``.
    """
    root_dir = jobs_dir(store, store.workspace_id)
    if root_dir is None:
        raise JobSpaceError("Папка для рабочих копий не задана: jwu workspace jobs-dir <путь>")
    repo_root = _repo_root(repo, runner)
    for res in store.job_resources(job.id):
        if res["kind"] == "worktree" and res["meta"].get("repo") == str(repo_root) and Path(res["ref"]).exists():
            return {**res, "created": False}
    require_space(store, store.workspace_id, str(root_dir), runner=runner)
    path = root_dir / worktree_name(repo_root, job)
    branch = branch or _slug(f"job{job.id}" + (f"-{job.task_key or job.feature_key}" if (job.task_key or job.feature_key) else ""))
    exists = runner(["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])[0] == 0
    if exists:
        cmd = ["git", "-C", str(repo_root), "worktree", "add", str(path), branch]
    else:
        remote = runner(["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet",
                         f"refs/remotes/origin/{branch}"])[0] == 0
        if remote:
            cmd = ["git", "-C", str(repo_root), "worktree", "add", "--track", "-b", branch, str(path), f"origin/{branch}"]
        else:
            cmd = ["git", "-C", str(repo_root), "worktree", "add", "-b", branch, str(path)] + ([base] if base else [])
    code, out = runner(cmd)
    if code != 0:
        raise JobSpaceError(f"git worktree add не вышел: {out[-400:]}")
    rid = store.add_job_resource(job.id, "worktree", str(path), {"repo": str(repo_root), "branch": branch})
    return {"id": rid, "kind": "worktree", "ref": str(path),
            "meta": {"repo": str(repo_root), "branch": branch}, "created": True}


def worktree_state(path: str, runner: Runner = _run) -> dict:
    """Что потеряется при удалении копии: незакоммиченное и незапушенные коммиты."""
    if not Path(path).exists():
        return {"exists": False, "dirty": [], "unpushed": 0}
    code, out = runner(["git", "-C", path, "status", "--porcelain"])
    dirty = [line for line in out.splitlines() if line.strip()] if code == 0 else []
    code, out = runner(["git", "-C", path, "rev-list", "--count", "HEAD", "--not", "--remotes"])
    unpushed = int(out.strip() or 0) if code == 0 and out.strip().isdigit() else 0
    return {"exists": True, "dirty": dirty, "unpushed": unpushed}


# --------------------------------------------------------------------------- #
# Уборка
# --------------------------------------------------------------------------- #


@dataclass
class CleanupReport:
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)      # не тронуто и почему
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def blocked(self) -> bool:
        return bool(self.kept)

    def to_dict(self) -> dict:
        return {**asdict(self), "blocked": self.blocked}


def _docker_ids(kind: str, job_id: int, runner: Runner) -> list[str]:
    filters = [label(job_id)]
    if kind in ("container", "volume", "network", "image"):
        filters.append(f"com.docker.compose.project={compose_project(job_id)}")
    ids: list[str] = []
    for flt in filters:
        cmd = {"container": ["docker", "ps", "-aq"], "volume": ["docker", "volume", "ls", "-q"],
               "network": ["docker", "network", "ls", "-q"], "image": ["docker", "images", "-q"]}[kind]
        code, out = runner(cmd + ["--filter", f"label={flt}"])
        if code == 0:
            ids += [x.strip() for x in out.splitlines() if x.strip()]
    return list(dict.fromkeys(ids))


_DOCKER_RM = {"container": ["docker", "rm", "-f"], "volume": ["docker", "volume", "rm", "-f"],
              "network": ["docker", "network", "rm"], "image": ["docker", "rmi", "-f"]}


def cleanup(store: "Store", job_id: int, *, dry_run: bool = False, force: bool = False,
            runner: Runner = _run) -> CleanupReport:
    """Снести всё, что создала работа: стенд, контейнеры, вольюмы, сети, образы, кеш, копии.

    Порядок: контейнеры → сети → вольюмы → образы (иначе занятое не удалится), потом
    каталоги и рабочие копии. Копию с незакоммиченным/незапушенным не трогает без ``force``
    — она попадает в ``kept`` с причиной. ``dry_run`` — только показать, что будет удалено.
    """
    report = CleanupReport(dry_run=dry_run)
    resources = store.job_resources(job_id)
    docker_ok = runner(["docker", "info", "--format", "{{.ServerVersion}}"])[0] == 0

    def done(res_id: Optional[int], text: str) -> None:
        report.removed.append(text)
        if res_id is not None and not dry_run:
            store.mark_job_resource_removed(res_id)

    for res in [r for r in resources if r["kind"] == "compose"]:
        if not docker_ok:
            report.errors.append(f"compose {res['ref']}: docker недоступен")
            continue
        cmd = ["docker", "compose", "-p", res["ref"], "down", "-v", "--remove-orphans", "--rmi", "local"]
        if dry_run:
            done(None, f"compose {res['ref']}: down -v")
            continue
        code, out = runner(cmd)
        if code == 0:
            done(res["id"], f"compose {res['ref']}: остановлен и удалён")
        else:
            report.errors.append(f"compose {res['ref']}: {out[-200:]}")

    if docker_ok:
        for kind in ("container", "network", "volume", "image"):
            ids = set(_docker_ids(kind, job_id, runner))
            named = {r["ref"]: r for r in resources if r["kind"] == kind}
            for ref in list(ids) + [n for n in named if n not in ids]:
                if dry_run:
                    done(None, f"{kind} {ref}")
                    continue
                code, out = runner(_DOCKER_RM[kind] + [ref])
                gone = code == 0 or "No such" in out or "not found" in out.lower()
                if gone:
                    done(named[ref]["id"] if ref in named else None, f"{kind} {ref}")
                else:
                    report.errors.append(f"{kind} {ref}: {out[-200:]}")
    elif any(r["kind"] in ("container", "volume", "network", "image") for r in resources):
        report.errors.append("docker недоступен — docker-ресурсы не убраны")

    for res in [r for r in resources if r["kind"] == "path"]:
        path = Path(res["ref"])
        if dry_run:
            done(None, f"каталог {path}")
            continue
        try:
            if path.exists():
                shutil.rmtree(path)
            done(res["id"], f"каталог {path}")
        except OSError as exc:
            report.errors.append(f"каталог {path}: {exc}")

    for res in [r for r in resources if r["kind"] == "worktree"]:
        path, repo = res["ref"], res["meta"].get("repo", "")
        state = worktree_state(path, runner)
        if not state["exists"]:
            done(res["id"], f"копия {path} (её уже нет)")
            continue
        reasons = []
        if state["dirty"]:
            reasons.append(f"незакоммичено файлов: {len(state['dirty'])}")
        if state["unpushed"]:
            reasons.append(f"незапушенных коммитов: {state['unpushed']}")
        if reasons and not force:
            report.kept.append(f"копия {path}: {', '.join(reasons)}")
            continue
        if dry_run:
            done(None, f"копия {path}" + (f" (с потерей: {', '.join(reasons)})" if reasons else ""))
            continue
        cmd = ["git", "-C", repo or path, "worktree", "remove"] + (["--force"] if force else []) + [path]
        code, out = runner(cmd)
        if code == 0:
            runner(["git", "-C", repo or path, "worktree", "prune"])
            done(res["id"], f"копия {path}")
        else:
            report.errors.append(f"копия {path}: {out[-200:]}")
    return report
