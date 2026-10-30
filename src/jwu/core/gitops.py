"""Git-операции jwu под политикой (:mod:`gitpolicy`): то, что раньше модель делала голым git.

Каждая пишущая операция сначала спрашивает политику. Отказ — структурированный ответ
(:class:`Blocked`), git не запускается вовсе. Перед тем, что может что-то стереть (sync,
discard, history, undo), снимается backup-ссылка ``refs/jwu/backup/<время>-<операция>`` —
всё откатывается :func:`undo`. Каждое действие — в журнал политики и, если папка — рабочая
копия работы, в журнал этой работы.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from . import gitpolicy as gp

if TYPE_CHECKING:
    from .store import Store

BACKUP_PREFIX = "refs/jwu/backup/"
MAX_OUT = 60_000

Runner = Callable[[list[str]], tuple[int, str]]
Notify = Callable[["Store", dict], None]


class GitOpError(RuntimeError):
    """Git не справился (не политика): нет репозитория, конфликт, сеть."""


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        return 127, f"{cmd[0]}: не найден"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(cmd)}: таймаут"
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


@dataclass
class Ctx:
    store: "Store"
    path: str
    root: str
    remote: str
    branch: str
    head: str
    where: str
    policy: gp.Policy
    job_id: Optional[int] = None
    runner: Runner = _run
    notify: Optional[Notify] = None

    def git(self, *args: str, check: bool = True) -> str:
        code, out = self.runner(["git", "-C", self.root, *args])
        if check and code != 0:
            raise GitOpError(f"git {' '.join(args[:3])}: {out[-600:]}")
        return out

    def ok(self, *args: str) -> bool:
        return self.runner(["git", "-C", self.root, *args])[0] == 0

    def refresh(self) -> None:
        self.branch = self.git("rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
        self.head = self.git("rev-parse", "HEAD", check=False).strip()


@dataclass
class Blocked(Exception):
    """Политика не пустила. ``to_dict`` — ответ агенту: что, почему и что делать."""

    op: str
    level: str
    rule: str
    reason: str
    ask_user: str
    retry: str = ""
    grant_id: Optional[int] = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {"ok": False, "blocked": self.level, "op": self.op, "rule": self.rule, "reason": self.reason,
               "ask_user": self.ask_user, "retry": self.retry}
        if self.grant_id:
            out["grant_id"] = self.grant_id
        return {**out, **self.extra}


def _job_of(store: "Store", root: str) -> Optional[int]:
    try:
        rows = store.conn.execute(
            "SELECT job_id, ref FROM job_resources WHERE kind = 'worktree' AND removed_at = ''").fetchall()
    except Exception:  # noqa: BLE001
        return None
    real = str(Path(root).resolve())
    return next((int(r["job_id"]) for r in rows if str(Path(r["ref"]).resolve()) == real), None)


def context(store: "Store", path: str, *, runner: Runner = _run, notify: Optional[Notify] = None) -> Ctx:
    if not path or not Path(path).exists():
        raise GitOpError(f"Папки {path or '(пусто)'} нет")
    info = gp.repo_of(path, runner)
    if not info:
        raise GitOpError(f"{path}: не git-репозиторий")
    policy = gp.ensure(store)
    if info["remote"] and not policy.repo(info["remote"]):
        # незнакомый репозиторий — завести с наследованием всего: дальше его можно уточнять
        policy.repos.append(gp.Repo(remote=info["remote"], name=info["remote"].split("/")[-1],
                                    target=info.get("target", "")))
        policy = gp.save(store, policy)
    job_id = _job_of(store, info["root"])
    return Ctx(store=store, path=path, root=info["root"], remote=info["remote"], branch=info["branch"],
               head=info["head"], where="job" if job_id else gp.where_of(store, info["root"]), policy=policy,
               job_id=job_id, runner=runner, notify=notify)


def _record(ctx: Ctx, text: str) -> None:
    if ctx.job_id:
        try:
            ctx.store.add_job_record(ctx.job_id, f"git: {text}", "note")
        except Exception:  # noqa: BLE001 — журнал работы не повод ронять операцию
            pass


def gate(ctx: Ctx, op: str, *, branch: str = "", user_approved: bool = False, detail: str = "") -> gp.Decision:
    """Решение политики + журнал. Не пустили — :class:`Blocked` (git не трогаем)."""
    branch = branch or ctx.branch
    d = gp.decide(ctx.policy, op, remote=ctx.remote, branch=branch, where=ctx.where)
    what = f"{gp.OP_TITLES[op]} в {ctx.remote or ctx.root}:{branch}"

    def block(reason: str, ask: str, retry: str = "", grant_id: Optional[int] = None) -> Blocked:
        gp.audit(ctx.store, op=op, decision=f"blocked:{d.level}", remote=ctx.remote, branch=branch, rule=d.rule,
                 detail=detail, job_id=ctx.job_id)
        _record(ctx, f"⛔ политика: {what} — {reason}")
        return Blocked(op=op, level=d.level, rule=d.rule, reason=reason, ask_user=ask, retry=retry, grant_id=grant_id)

    if d.level == "deny":
        raise block(f"{what} запрещено политикой воркспейса ({d.rule})",
                    "Сделать это через jwu нельзя. Скажи пользователю, что политика запрещает, и спроси, как "
                    "быть: другая ветка или он сделает сам. Менять политику ты не можешь.")
    if d.level == "ask" and not user_approved:
        raise block(f"{what} — только с «да» пользователя ({d.rule})",
                    f"Покажи пользователю, что именно будет сделано ({detail or what}), и спроси меню «да / нет».",
                    "после «да» повтори вызов с user_approved=True")
    if d.level == "grant":
        if gp.consume_grant(ctx.store, remote=ctx.remote, branch=branch, op=op, head=ctx.head, policy=ctx.policy):
            gp.audit(ctx.store, op=op, decision="allow:grant", remote=ctx.remote, branch=branch, rule=d.rule,
                     detail=detail, job_id=ctx.job_id)
            return d
        req = gp.request_grant(ctx.store, remote=ctx.remote, branch=branch, op=op, head=ctx.head,
                               detail=detail or what, job_id=ctx.job_id)
        if req.get("new") and ctx.notify is not None:
            try:
                ctx.notify(ctx.store, req)
            except Exception:  # noqa: BLE001 — нет бота: остаётся CLI
                pass
        via = []
        if "telegram" in ctx.policy.grant_via:
            via.append("кнопка в Telegram-боте (запрос отправлен)")
        if "cli" in ctx.policy.grant_via:
            via.append(f"в своём терминале: `! jwu git grant {req['id']}`")
        raise block(f"{what} — нужен допуск пользователя, «да» в чате не хватает ({d.rule})",
                    "Нужен допуск: " + " или ".join(via) + ". Сам выдать допуск ты не можешь и не пытайся.",
                    "после допуска повтори тот же вызов", grant_id=int(req["id"]))
    gp.audit(ctx.store, op=op, decision="allow" if d.level == "allow" else "allow:approved", remote=ctx.remote,
             branch=branch, rule=d.rule, detail=detail, job_id=ctx.job_id)
    return d


# --------------------------------------------------------------------------- #
# Backup-ссылки
# --------------------------------------------------------------------------- #


def backup(ctx: Ctx, op: str) -> str:
    """Снять состояние: HEAD и (если есть) несохранённое — ``git stash create`` не трогает дерево."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    ref = f"{BACKUP_PREFIX}{stamp}-{op}"
    n = 1
    while ctx.ok("rev-parse", "--verify", "--quiet", ref):
        n += 1
        ref = f"{BACKUP_PREFIX}{stamp}-{op}-{n}"
    target = ctx.git("stash", "create", f"jwu backup before {op}", check=False).strip() or ctx.head
    if target:
        ctx.git("update-ref", ref, target)
    _prune_backups(ctx)
    return ref if target else ""


def _prune_backups(ctx: Ctx) -> None:
    edge = datetime.now(timezone.utc) - timedelta(days=ctx.policy.backup_days)
    for line in ctx.git("for-each-ref", "--format=%(refname)", BACKUP_PREFIX, check=False).splitlines():
        m = re.search(r"/(\d{8}-\d{6})-", line)
        if m and datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc) < edge:
            ctx.git("update-ref", "-d", line.strip(), check=False)


def backups(ctx: Ctx) -> list[dict]:
    out = ctx.git("for-each-ref", "--sort=-refname", "--format=%(refname) %(objectname:short) %(subject)",
                  BACKUP_PREFIX, check=False)
    rows = []
    for line in out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2:
            rows.append({"ref": parts[0], "sha": parts[1], "subject": parts[2] if len(parts) > 2 else ""})
    return rows


# --------------------------------------------------------------------------- #
# Чтение
# --------------------------------------------------------------------------- #


def _clip(text: str, limit: int = MAX_OUT) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… (обрезано, всего {len(text)} символов)"


def status(ctx: Ctx) -> dict:
    porcelain = ctx.git("status", "--porcelain=v1", "--branch", check=False).splitlines()
    head_line = porcelain[0] if porcelain and porcelain[0].startswith("##") else ""
    ahead = re.search(r"ahead (\d+)", head_line)
    behind = re.search(r"behind (\d+)", head_line)
    files = [ln for ln in porcelain if not ln.startswith("##")]
    conflicts = [ln[3:] for ln in files if ln[:2] in ("UU", "AA", "DD", "AU", "UA", "UD", "DU")]
    state = ""
    gitdir = ctx.git("rev-parse", "--git-dir", check=False).strip()
    gd = Path(gitdir if Path(gitdir).is_absolute() else Path(ctx.root) / gitdir)
    if (gd / "rebase-merge").exists() or (gd / "rebase-apply").exists():
        state = "rebase"
    elif (gd / "MERGE_HEAD").exists():
        state = "merge"
    return {"ok": True, "repo": ctx.remote, "root": ctx.root, "where": ctx.where, "job_id": ctx.job_id,
            "branch": ctx.branch, "head": ctx.head[:12], "ahead": int(ahead.group(1)) if ahead else 0,
            "behind": int(behind.group(1)) if behind else 0, "files": files, "conflicts": conflicts,
            "in_progress": state, "target": gp.target_branch(ctx.policy, ctx.remote),
            "guard": gp.guard_on(ctx.policy, ctx.remote)}


def diff(ctx: Ctx, *, staged: bool = False, ref: str = "", paths: Optional[list[str]] = None,
         stat: bool = False) -> dict:
    args = ["diff"] + (["--cached"] if staged else []) + (["--stat"] if stat else []) + ([ref] if ref else [])
    if paths:
        args += ["--", *paths]
    return {"ok": True, "diff": _clip(ctx.git(*args))}


def log(ctx: Ctx, *, n: int = 20, ref: str = "") -> dict:
    out = ctx.git("log", f"-{max(1, min(n, 200))}", "--format=%h\t%an\t%ad\t%s", "--date=short", *([ref] if ref else []))
    return {"ok": True, "commits": [dict(zip(("sha", "author", "date", "subject"), ln.split("\t", 3)))
                                    for ln in out.splitlines() if ln.strip()]}


# --------------------------------------------------------------------------- #
# Запись
# --------------------------------------------------------------------------- #


def _need_branch(ctx: Ctx) -> None:
    if not ctx.branch or ctx.branch == "HEAD":
        raise GitOpError(f"{ctx.root}: не на ветке (detached HEAD)")


def commit(ctx: Ctx, message: str, *, files: Optional[list[str]] = None, user_approved: bool = False) -> dict:
    _need_branch(ctx)
    if not (message or "").strip():
        raise GitOpError("Пустое сообщение коммита — его пишет jwu-commit-message")
    changed = ctx.git("status", "--porcelain", "--", *(files or []), check=False).splitlines()
    if not changed:
        return {"ok": True, "committed": False, "reason": "нечего коммитить"}
    gate(ctx, "commit", user_approved=user_approved, detail=f"{len(changed)} файлов: {message.splitlines()[0][:80]}")
    if files:
        ctx.git("add", "--", *files)
    else:
        ctx.git("add", "-A")
    staged = ctx.git("diff", "--cached", "--name-status").splitlines()
    ctx.git("commit", "-q", "-m", message)
    ctx.refresh()
    _record(ctx, f"коммит {ctx.head[:8]} «{message.splitlines()[0][:80]}» ({len(staged)} файлов)")
    return {"ok": True, "committed": True, "sha": ctx.head[:12], "branch": ctx.branch, "files": staged}


def switch(ctx: Ctx, branch: str, *, create: bool = False, base: str = "", user_approved: bool = False) -> dict:
    if not branch or branch.startswith("-"):
        raise GitOpError("Нужно имя ветки")
    gate(ctx, "switch", branch=branch, user_approved=user_approved,
         detail=f"{'создать ' if create else ''}{branch}{' от ' + base if base else ''}")
    if create:
        ctx.git("switch", "-c", branch, *([base] if base else []))
    else:
        ctx.git("switch", branch)
    ctx.refresh()
    _record(ctx, f"ветка {branch}{' (новая)' if create else ''}")
    return {"ok": True, "branch": ctx.branch, "head": ctx.head[:12]}


def sync(ctx: Ctx, *, target: str = "", mode: str = "", user_approved: bool = False) -> dict:
    """Подтянуть целевую ветку в текущую (merge|rebase из политики). Конфликт — не откатываем сами."""
    _need_branch(ctx)
    target = target or gp.target_branch(ctx.policy, ctx.remote)
    if not target:
        raise GitOpError("Не знаю целевую ветку: укажи target или задай её репозиторию (jwu git repo set --target)")
    mode = mode or gp.sync_mode(ctx.policy, ctx.remote)
    if mode not in gp.SYNC_MODES:
        raise GitOpError(f"mode: {', '.join(gp.SYNC_MODES)}")
    if ctx.git("status", "--porcelain", check=False).strip():
        raise GitOpError("Есть несохранённое — сначала коммит (jwu_git_commit) или stash")
    gate(ctx, "sync", user_approved=user_approved, detail=f"{mode} origin/{target} → {ctx.branch}")
    ctx.git("fetch", "origin", target)
    ref = backup(ctx, "sync")
    code, out = ctx.runner(["git", "-C", ctx.root, *(["merge", "--no-edit", f"origin/{target}"] if mode == "merge"
                                                      else ["rebase", f"origin/{target}"])])
    ctx.refresh()
    if code != 0:
        conflicts = [ln[3:] for ln in ctx.git("status", "--porcelain", check=False).splitlines()
                     if ln[:2] in ("UU", "AA", "DD", "AU", "UA", "UD", "DU")]
        _record(ctx, f"{mode} origin/{target}: конфликты в {len(conflicts)} файлах (backup {ref})")
        return {"ok": False, "conflicts": conflicts, "mode": mode, "target": target, "backup": ref,
                "next": "реши конфликты и jwu_git_sync_continue, либо jwu_git_sync_abort", "output": out[-1500:]}
    _record(ctx, f"{mode} origin/{target} → {ctx.head[:8]} (backup {ref})")
    return {"ok": True, "mode": mode, "target": target, "head": ctx.head[:12], "backup": ref}


def sync_continue(ctx: Ctx) -> dict:
    st = status(ctx)
    if not st["in_progress"]:
        raise GitOpError("Ни merge, ни rebase не идут")
    if st["conflicts"]:
        return {"ok": False, "conflicts": st["conflicts"], "next": "сначала реши конфликты"}
    ctx.git("add", "-A")
    if st["in_progress"] == "rebase":
        code, out = ctx.runner(["git", "-C", ctx.root, "-c", "core.editor=true", "rebase", "--continue"])
    else:
        code, out = ctx.runner(["git", "-C", ctx.root, "commit", "--no-edit", "-q"])
    ctx.refresh()
    if code != 0:
        return {"ok": False, "output": out[-1500:], "conflicts": status(ctx)["conflicts"]}
    _record(ctx, f"{st['in_progress']} завершён → {ctx.head[:8]}")
    return {"ok": True, "head": ctx.head[:12]}


def sync_abort(ctx: Ctx) -> dict:
    st = status(ctx)
    if not st["in_progress"]:
        raise GitOpError("Ни merge, ни rebase не идут")
    ctx.git(st["in_progress"], "--abort")
    ctx.refresh()
    _record(ctx, f"{st['in_progress']} отменён")
    return {"ok": True, "head": ctx.head[:12]}


def stash(ctx: Ctx, action: str = "push", *, message: str = "", user_approved: bool = False) -> dict:
    if action == "list":
        return {"ok": True, "stashes": ctx.git("stash", "list", check=False).splitlines()}
    if action not in ("push", "pop", "apply"):
        raise GitOpError("action: push | pop | apply | list")
    gate(ctx, "stash", user_approved=user_approved, detail=f"stash {action}")
    out = ctx.git("stash", action, *(["-u", "-m", message] if action == "push" and message else
                                     (["-u"] if action == "push" else [])))
    _record(ctx, f"stash {action}")
    return {"ok": True, "output": out[-800:]}


def discard(ctx: Ctx, *, files: Optional[list[str]] = None, untracked: bool = False,
            user_approved: bool = False) -> dict:
    dirty = ctx.git("status", "--porcelain", check=False).splitlines()
    if not dirty:
        return {"ok": True, "discarded": False, "reason": "нечего выкидывать"}
    gate(ctx, "discard", user_approved=user_approved,
         detail=f"{len(files) if files else len(dirty)} файлов{' + неотслеживаемые' if untracked else ''}")
    ref = backup(ctx, "discard")
    if files:
        ctx.git("restore", "--staged", "--worktree", "--", *files)
    else:
        ctx.git("reset", "-q", "--hard")
    if untracked:
        ctx.git("clean", "-fd", *(["--", *files] if files else []))
    _record(ctx, f"выкинул несохранённое (backup {ref})")
    return {"ok": True, "discarded": True, "backup": ref,
            "note": "неотслеживаемые файлы в backup не попадают" if untracked else ""}


def _pushed(ctx: Ctx, since: str) -> int:
    """Сколько коммитов из since..HEAD уже есть на origin (переписывать их — force)."""
    total = ctx.git("rev-list", "--count", f"{since}..HEAD").strip()
    local = ctx.git("rev-list", "--count", f"{since}..HEAD", "--not", "--remotes=origin").strip()
    return int(total or 0) - int(local or 0)


def amend(ctx: Ctx, message: str = "", *, user_approved: bool = False) -> dict:
    _need_branch(ctx)
    has_parent = ctx.ok("rev-parse", "--verify", "--quiet", "HEAD~1")
    if (_pushed(ctx, "HEAD~1") if has_parent else int(not ctx.git("branch", "-r", "--contains", "HEAD",
                                                                  check=False).strip() == "")):
        raise GitOpError("Последний коммит уже на origin — его amend потребует force-пуша; сделай новый коммит")
    gate(ctx, "history", user_approved=user_approved, detail="amend последнего коммита")
    ref = backup(ctx, "amend")
    ctx.git("add", "-A")
    ctx.git("commit", "-q", "--amend", *(["-m", message] if message else ["--no-edit"]))
    ctx.refresh()
    _record(ctx, f"amend → {ctx.head[:8]} (backup {ref})")
    return {"ok": True, "sha": ctx.head[:12], "backup": ref}


def reset_to(ctx: Ctx, ref: str, *, keep_changes: bool = True, user_approved: bool = False) -> dict:
    """Откатить ветку на ref (незапушенное): keep_changes — правки остаются в дереве (--soft/--mixed)."""
    _need_branch(ctx)
    if not ctx.ok("rev-parse", "--verify", "--quiet", ref):
        raise GitOpError(f"Нет такого коммита: {ref}")
    if not ctx.ok("merge-base", "--is-ancestor", ref, "HEAD"):
        raise GitOpError(f"{ref} не предок HEAD — это не откат, а переход на другую историю")
    if _pushed(ctx, ref):
        raise GitOpError("Часть этих коммитов уже на origin — откат потребует force-пуша; сделай revert")
    gate(ctx, "history", user_approved=user_approved, detail=f"reset на {ref}{'' if keep_changes else ' (--hard)'}")
    bref = backup(ctx, "reset")
    ctx.git("reset", "-q", "--mixed" if keep_changes else "--hard", ref)
    ctx.refresh()
    _record(ctx, f"reset на {ref} (backup {bref})")
    return {"ok": True, "head": ctx.head[:12], "backup": bref}


def push(ctx: Ctx, *, force: bool = False, dry_run: bool = False, user_approved: bool = False,
         expected_branch: str = "") -> dict:
    """Пуш текущей ветки в одноимённую на origin. force — только --force-with-lease."""
    _need_branch(ctx)
    branch = ctx.branch
    if expected_branch and branch != expected_branch:
        raise GitOpError(f"Копия на ветке {branch}, а ожидалась {expected_branch}; переключать молча не буду")
    if ctx.git("status", "--porcelain", check=False).strip():
        raise GitOpError("Есть незакоммиченное — сначала коммит")
    code, out = ctx.runner(["git", "-C", ctx.root, "fetch", "origin", branch])
    remote_exists = code == 0
    if not remote_exists and "couldn't find remote ref" not in out.lower():
        raise GitOpError(f"git fetch origin {branch}: {out[-300:]}")
    diverged = remote_exists and not ctx.ok("merge-base", "--is-ancestor", f"origin/{branch}", "HEAD")
    if diverged and not force:
        raise GitOpError(f"origin/{branch} ушла вперёд или разошлась с копией — сначала jwu_git_sync "
                         f"(или force-пуш, если переписывали свою ветку)")
    rng = [f"origin/{branch}..HEAD"] if remote_exists else ["HEAD", "--not", "--remotes=origin"]
    commits = [ln for ln in ctx.git("log", "--format=%h %s", *rng, check=False).splitlines() if ln.strip()]
    result = {"ok": True, "repo": ctx.remote, "path": ctx.root, "branch": branch, "new_branch": not remote_exists,
              "commits": commits, "force": bool(force and diverged), "pushed": False}
    if not commits and not (force and diverged):
        return {**result, "reason": "нечего пушить"}
    if dry_run:
        return result
    op = "force_push" if force and diverged else "push"
    gate(ctx, op, user_approved=user_approved,
         detail=f"{len(commits)} коммитов → origin/{branch}: " + "; ".join(c.split(' ', 1)[-1] for c in commits[:3]))
    cmd = ["push", "-u", "origin", f"HEAD:refs/heads/{branch}"]
    if op == "force_push":
        cmd.insert(1, f"--force-with-lease=refs/heads/{branch}")
    ctx.git(*cmd)
    _record(ctx, f"{'force-' if op == 'force_push' else ''}пуш {branch}: {len(commits)} коммитов")
    return {**result, "pushed": True}


def delete_branch(ctx: Ctx, branch: str, *, remote: bool = False, user_approved: bool = False) -> dict:
    if branch == ctx.branch:
        raise GitOpError("Нельзя удалить текущую ветку — сначала переключись")
    gate(ctx, "delete_branch", branch=branch, user_approved=user_approved,
         detail=f"удалить {branch}{' и на origin' if remote else ''}")
    ref = f"{BACKUP_PREFIX}{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-delete-{branch.replace('/', '_')}"
    if ctx.ok("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
        ctx.git("update-ref", ref, f"refs/heads/{branch}")
        ctx.git("branch", "-D", branch)
    if remote:
        ctx.git("push", "origin", "--delete", branch)
    _record(ctx, f"удалил ветку {branch}{' (и на origin)' if remote else ''}")
    return {"ok": True, "deleted": branch, "backup": ref}


def tag(ctx: Ctx, name: str, *, message: str = "", push_tag: bool = False, user_approved: bool = False) -> dict:
    if not name or name.startswith("-"):
        raise GitOpError("Нужно имя тега")
    gate(ctx, "tag", user_approved=user_approved, detail=f"тег {name}{' + пуш' if push_tag else ''}")
    ctx.git("tag", *(["-a", name, "-m", message] if message else [name]))
    if push_tag:
        ctx.git("push", "origin", f"refs/tags/{name}")
    _record(ctx, f"тег {name}{' (запушен)' if push_tag else ''}")
    return {"ok": True, "tag": name}


def undo(ctx: Ctx, ref: str = "", *, user_approved: bool = False) -> dict:
    """Вернуть ветку и дерево к backup-ссылке. Без ref — список. Всегда с «да» пользователя."""
    items = backups(ctx)
    if not ref:
        return {"ok": True, "backups": items}
    if not ref.startswith(BACKUP_PREFIX):
        ref = BACKUP_PREFIX + ref
    if not any(b["ref"] == ref for b in items):
        raise GitOpError(f"Нет такой backup-ссылки: {ref}")
    if not user_approved:
        raise Blocked(op="undo", level="ask", rule="откат всегда с «да»", reason="откат меняет ветку и дерево",
                      ask_user=f"Покажи пользователю, к чему откатываемся ({ref}), и спроси «да / нет».",
                      retry="после «да» повтори с user_approved=True")
    before = backup(ctx, "undo")
    is_stash = "jwu backup before" in ctx.git("log", "-1", "--format=%s", ref, check=False)
    base = f"{ref}^1" if is_stash else ref
    ctx.git("reset", "-q", "--hard", base)
    if is_stash:
        ctx.git("stash", "apply", ref, check=False)
    ctx.refresh()
    _record(ctx, f"откат к {ref} (прежнее состояние — {before})")
    return {"ok": True, "head": ctx.head[:12], "restored": ref, "backup": before}
