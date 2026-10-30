"""Хук Claude Code ``PreToolUse`` на Bash: пишущий git — только через ``jwu_git_*``.

Deny-маски Claude Code (``Bash(git push:*)``) обходятся ``git -C x push``, ``cd x && git push``,
``env … git push``, ``sh -c "git push"``. Поэтому команда разбирается целиком: цепочки
(``;``, ``&&``, ``||``, ``|``, ``&``), подстановки ``$(…)`` и обратные кавычки, ``sh/bash -c``,
``eval``, префиксы (``env``, ``VAR=…``, ``command``, ``sudo``, ``nohup``, ``time``), глобальные
флаги git (``-C``, ``-c``, ``--git-dir``…).

Что режется:

- пишущий git (всё, кроме чтения из :data:`READ_SUBCOMMANDS`) — в папках воркспейсов, где
  ``guard`` политики включён; вне воркспейсов jwu хук git не трогает;
- везде — то, что меняет саму политику или выдаёт допуск (``jwu git policy edit/init/import``,
  ``jwu git grant``, ``jwu git repo/rule …``), ``jwu job push`` (в обход вопроса MCP) и
  ``sqlite3`` по базе jwu: политику, которую агент может переписать, незачем и заводить.

Это страховка от случайности и самодеятельности агента, не песочница: окончательный замок —
права веток на сервере.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

HOOK_COMMAND = "jwu-git-guard"
HOOK_MATCHER = "Bash"

# Подкоманды git, которые ничего не пишут (или пишут только служебное — fetch обновляет
# origin/*, но не трогает ветки и дерево). Для части — только «читающие» формы, см. _READ_FORMS.
READ_SUBCOMMANDS = {
    "status", "diff", "log", "show", "blame", "annotate", "rev-parse", "rev-list", "ls-files", "ls-tree",
    "ls-remote", "cat-file", "describe", "shortlog", "grep", "fetch", "merge-base", "name-rev",
    "for-each-ref", "show-ref", "help", "version", "var", "check-ignore", "check-attr", "count-objects",
    "whatchanged", "range-diff", "cherry", "difftool", "show-branch", "verify-commit", "verify-tag",
    "fsck", "clone", "init", "archive", "--version", "--help",
}
_BRANCH_READ_FLAGS = {"-a", "-r", "--all", "--remotes", "-v", "-vv", "--verbose", "--list", "-l",
                      "--show-current", "--no-color", "--color", "--column", "--no-column", "-i", "--ignore-case"}
_BRANCH_READ_PREFIX = ("--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort",
                       "--format", "--color=", "--abbrev")


def _branch_read(a: list[str]) -> bool:
    listing = any(x in ("--list", "-l") for x in a)
    for x in a:
        if x.startswith("-"):
            if x not in _BRANCH_READ_FLAGS and not x.startswith(_BRANCH_READ_PREFIX):
                return False
        elif not listing:
            return False          # git branch NAME — создание ветки
    return True


# «Читающие» формы подкоманд, которые умеют и писать.
_READ_FORMS = {
    "branch": _branch_read,
    "remote": lambda a: not a or a[0] in ("-v", "--verbose", "show", "get-url"),
    "config": lambda a: any(x in ("--get", "--get-all", "--get-regexp", "--list", "-l", "--get-urlmatch")
                            for x in a) or len([x for x in a if not x.startswith("-")]) == 1,
    "stash": lambda a: bool(a) and a[0] in ("list", "show"),
    "tag": lambda a: not a or a[0] in ("-l", "--list", "-v", "--verify") or a[0].startswith(
        ("--contains", "--points-at", "--sort", "--format", "--list", "--merged", "--no-merged")),
    "worktree": lambda a: bool(a) and a[0] == "list",
    "reflog": lambda a: not a or a[0] == "show",
    "notes": lambda a: not a or a[0] in ("list", "show"),
    "submodule": lambda a: bool(a) and a[0] in ("status", "summary"),
    "symbolic-ref": lambda a: len([x for x in a if not x.startswith("-")]) <= 1,
    "sparse-checkout": lambda a: bool(a) and a[0] == "list",
    "bisect": lambda a: bool(a) and a[0] in ("log", "visualize", "view"),
    "lfs": lambda a: bool(a) and a[0] in ("ls-files", "status", "env", "version"),
}
# Глобальные флаги git со значением отдельным аргументом.
_GIT_OPT_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix",
                       "--config-env", "--list-cmds"}
_PREFIX_CMDS = {"env", "command", "builtin", "exec", "nohup", "time", "sudo", "nice", "ionice", "stdbuf",
                "timeout", "caffeinate", "xargs", "doas"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_JWU_READ_GIT = {("policy", "show"), ("policy", "check"), ("policy", "export"), ("log",), ("repo", "list"),
                 ("grants",)}


@dataclass
class Verdict:
    allow: bool
    reason: str = ""


# --------------------------------------------------------------------------- #
# Разбор команды
# --------------------------------------------------------------------------- #


def _substitutions(cmd: str) -> list[str]:
    """Содержимое $(…) и `…` — тоже команды."""
    out = []
    depth, start, i = 0, -1, 0
    while i < len(cmd):
        if cmd.startswith("$(", i):
            if depth == 0:
                start = i + 2
            depth += 1
            i += 2
            continue
        if cmd[i] == ")" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(cmd[start:i])
        i += 1
    out += re.findall(r"`([^`]*)`", cmd)
    return out


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredocs(cmd: str) -> tuple[str, list[str]]:
    """Тело heredoc — данные, а не команды (``cat > f <<'EOF'`` с кодом внутри). Исключения:
    тело читает shell (``bash <<EOF``) — это команды; разделитель без кавычек — ``$(…)`` в
    теле выполнится, его проверяем. → (команда без тел, что проверить дополнительно)."""
    lines = cmd.split("\n")
    out: list[str] = []
    extra: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in _HEREDOC.finditer(line):
            quoted, delim = bool(m.group(1)), m.group(2)
            strip_tabs = line[m.start():m.start() + 3] == "<<-"
            body = []
            while i < len(lines) and (lines[i].lstrip("\t") if strip_tabs else lines[i]) != delim:
                body.append(lines[i])
                i += 1
            i += 1   # строка с разделителем
            head = line[:m.start()].split("|")[-1].split(";")[-1].split("&&")[-1].strip()
            consumer = os.path.basename((head.split() or [""])[0])
            text = "\n".join(body)
            if consumer in _SHELLS or consumer == "eval":
                extra.append(text)
            elif not quoted:
                extra += _substitutions(text)
    return "\n".join(out), extra


def segments(cmd: str) -> list[list[str]]:
    """Команда → список простых команд (argv). Вложенные sh -c / eval / $(…) — раскрыты."""
    result: list[list[str]] = []
    if "<<" in cmd:
        cmd, extra = _strip_heredocs(cmd)
        for sub in extra:
            result += segments(sub)
    for sub in _substitutions(cmd):
        result += segments(sub)
    try:
        lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        # кавычки не закрыты — разбираем грубо по словам, чтобы git всё равно увидеть
        tokens = cmd.replace(";", " ; ").replace("&&", " ; ").replace("||", " ; ").replace("|", " ; ").split()
    cur: list[str] = []
    for tok in tokens:
        if tok and all(c in ";&|()<>" for c in tok):
            if cur:
                result.append(cur)
            cur = []
            continue
        cur.append(tok)
    if cur:
        result.append(cur)
    expanded: list[list[str]] = []
    for argv in result:
        expanded += _expand(argv)
    return expanded


def _expand(argv: list[str]) -> list[list[str]]:
    argv = _strip_prefix(argv)
    if not argv:
        return []
    name = os.path.basename(argv[0])
    if name in _SHELLS:
        for i, a in enumerate(argv[1:], 1):
            if a == "-c" or (a.startswith("-") and not a.startswith("--") and "c" in a[1:]):
                return segments(argv[i + 1]) if i + 1 < len(argv) else []
        return [argv]
    if name == "eval":
        return segments(" ".join(argv[1:]))
    return [argv]


def _strip_prefix(argv: list[str]) -> list[str]:
    i = 0
    while i < len(argv):
        a = argv[i]
        if _ASSIGN.match(a):
            i += 1
            continue
        base = os.path.basename(a)
        if base in _PREFIX_CMDS:
            i += 1
            # флаги префикса (env -i, timeout 10, sudo -u x …) — до первого «словa-команды»
            while i < len(argv) and (argv[i].startswith("-") or _ASSIGN.match(argv[i])
                                     or (base == "timeout" and re.fullmatch(r"[\d.]+[smhd]?", argv[i]))
                                     or (base in ("sudo", "doas") and i > 0 and argv[i - 1] in ("-u", "-g"))):
                i += 1
            continue
        break
    return argv[i:]


def parse_git(argv: list[str], cwd: str) -> Optional[dict]:
    """argv git → {dir, sub, args}; не git — None."""
    if not argv or os.path.basename(argv[0]) != "git":
        return None
    d = cwd
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        a = argv[i]
        if a == "-C" and i + 1 < len(argv):
            d = os.path.join(d, os.path.expanduser(argv[i + 1]))
            i += 2
            continue
        if a.startswith("--git-dir=") or a.startswith("--work-tree="):
            d = os.path.join(d, os.path.expanduser(a.split("=", 1)[1]))
            if a.startswith("--git-dir="):
                d = os.path.dirname(d.rstrip("/")) if d.rstrip("/").endswith(".git") else d
            i += 1
            continue
        if a in _GIT_OPT_WITH_VALUE and i + 1 < len(argv):
            if a in ("--git-dir", "--work-tree"):
                d = os.path.join(d, os.path.expanduser(argv[i + 1]))
            i += 2
            continue
        if a in ("--version", "--help"):
            return {"dir": d, "sub": a, "args": []}
        i += 1
    if i >= len(argv):
        return {"dir": d, "sub": "", "args": []}
    return {"dir": d, "sub": argv[i], "args": argv[i + 1:]}


# Пишут только файлы и индекс, как обычная правка, — ни коммитов, ни веток, ни истории:
# например, перенос диффа копии субагента в основную папку (`git apply --index patch`).
FILE_ONLY_SUBCOMMANDS = {"apply"}


def is_read(sub: str, args: list[str]) -> bool:
    """Пропустить ли подкоманду: чтение или правка только файлов (``apply``)."""
    if sub in ("", *READ_SUBCOMMANDS, *FILE_ONLY_SUBCOMMANDS):
        return True
    form = _READ_FORMS.get(sub)
    try:
        return bool(form and form(args))
    except Exception:  # noqa: BLE001
        return False


# Подсказка, чем заменить пишущую подкоманду.
_INSTEAD = {
    "commit": "jwu_git_commit", "add": "jwu_git_commit (files=…)", "push": "jwu_git_push / jwu_job_push",
    "checkout": "jwu_git_switch (ветка) или jwu_git_discard (файлы)", "switch": "jwu_git_switch",
    "merge": "jwu_git_sync", "rebase": "jwu_git_sync (mode=rebase) / jwu_git_history",
    "pull": "jwu_git_sync", "reset": "jwu_git_history (reset) или jwu_git_discard", "restore": "jwu_git_discard",
    "clean": "jwu_git_discard (untracked=True)", "stash": "jwu_git_stash", "branch": "jwu_git_switch / "
    "jwu_git_branch_delete", "tag": "jwu_git_tag", "cherry-pick": "попроси пользователя",
    "revert": "попроси пользователя", "am": "попроси пользователя",
    "worktree": "jwu_git_worktree_remove(path=<копия>) — убрать копию с бэкапом и её веткой; "
                "path=<корень репо> — prune. Копии субагентов harness создаёт сам",
}


def _jwu_verdict(argv: list[str]) -> Optional[Verdict]:
    if not argv or os.path.basename(argv[0]) not in ("jwu", "jwu-mcp"):
        return None
    words = [a for a in argv[1:] if not a.startswith("-")]
    # глобальные флаги вида -W slug: значение — тоже слово, убираем его
    if len(argv) > 2 and argv[1] in ("-W", "--workspace"):
        words = [a for a in argv[3:] if not a.startswith("-")]
    if words[:1] == ["git"]:
        rest = tuple(words[1:3])
        if any(rest[:len(r)] == r for r in _JWU_READ_GIT):
            return Verdict(True)
        return Verdict(False, "Политику git и допуски меняет только пользователь в своём терминале "
                              f"(`jwu git {' '.join(words[1:3])}` из сессии агента запрещён). Спроси пользователя.")
    if words[:2] == ["job", "push"]:
        return Verdict(False, "Пуш работы — через MCP jwu_job_push (под политикой и с вопросом), не через CLI.")
    if words[:2] == ["workspace", "setting"] and len(words) > 2 and words[2].startswith(("git.", "push.")):
        return Verdict(False, "Настройки git воркспейса меняет только пользователь.")
    return Verdict(True)


def _db_touch(argv: list[str], db_paths: list[str]) -> bool:
    if not argv or os.path.basename(argv[0]) not in ("sqlite3", "litecli", "sqlite-utils"):
        return False
    joined = " ".join(argv[1:])
    return any(p and (p in joined or os.path.basename(p) in joined) for p in db_paths)


# --------------------------------------------------------------------------- #
# Решение
# --------------------------------------------------------------------------- #


def _guarded(store, path: str) -> tuple[bool, str]:
    """(guard включён для папки, репозиторий). Вне воркспейсов jwu — не наше дело."""
    from . import gitpolicy as gp
    from .workspaces import workspace_for_path

    real = path if os.path.isdir(path) else os.path.dirname(path)
    found = workspace_for_path(store, real)
    if found is None:
        return False, ""
    ws, _ = found
    store.use_workspace(ws.id)
    policy = gp.load(store, ws.id)
    info = gp.repo_of(real) or {}
    return gp.guard_on(policy, info.get("remote", "")), info.get("remote", "")


def check(command: str, cwd: str, store=None, db_paths: Optional[list[str]] = None) -> Verdict:
    """Решение по одной Bash-команде."""
    db_paths = db_paths or []
    for argv in segments(command):
        v = _jwu_verdict(argv)
        if v is not None and not v.allow:
            return v
        if _db_touch(argv, db_paths):
            return Verdict(False, "Прямой доступ к базе jwu из сессии агента запрещён — только через jwu/MCP.")
        g = parse_git(argv, cwd)
        if g is None or is_read(g["sub"], g["args"]):
            continue
        if store is None:
            continue
        on, remote = _guarded(store, g["dir"])
        if not on:
            continue
        instead = _INSTEAD.get(g["sub"], "подходящий jwu_git_*, или попроси пользователя")
        return Verdict(False, f"git {g['sub']} напрямую запрещён политикой jwu"
                              f"{' для ' + remote if remote else ''}: пишущий git — только через MCP jwu "
                              f"(здесь — {instead}). Чтение (status/diff/log/show…) — можно. "
                              f"Политику смотри jwu_git_policy; менять её может только пользователь.")
    return Verdict(True)


def _respond(v: Verdict) -> None:
    if v.allow:
        return
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": v.reason}}, ensure_ascii=False))


def main() -> None:
    """Точка входа хука: JSON события на stdin, решение — JSON на stdout (deny) или тишина."""
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    if (event.get("tool_name") or "") != "Bash":
        return
    command = str((event.get("tool_input") or {}).get("command") or "")
    if not command.strip():
        return
    cwd = str(event.get("cwd") or os.getcwd())
    lowered = command.lower()
    if "git" not in lowered and "jwu" not in lowered and "sqlite" not in lowered:
        return   # быстрый путь: ничего нашего, базу даже не открываем
    try:
        from .config import db_path
        from .store import Store

        path = str(db_path())
        store = Store(path)
        try:
            v = check(command, cwd, store, db_paths=[path])
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 — сломанный хук не должен молча пропускать git
        v = check(command, cwd, None)
        if v.allow and any(parse_git(a, cwd) and not is_read(parse_git(a, cwd)["sub"], parse_git(a, cwd)["args"])
                           for a in segments(command)):
            v = Verdict(False, f"jwu git-guard не смог проверить политику ({exc}) — пишущий git только через "
                               f"jwu_git_*, или попроси пользователя.")
    _respond(v)


# --------------------------------------------------------------------------- #
# Установка в settings.json Claude Code
# --------------------------------------------------------------------------- #


def hook_command() -> str:
    import shutil

    exe = shutil.which(HOOK_COMMAND)
    return exe or HOOK_COMMAND


def _is_ours(cmd: str) -> bool:
    return Path(str(cmd).split()[0] if cmd else "").name == HOOK_COMMAND


def install(path: Optional[Path] = None, command: Optional[str] = None) -> str:
    from .statusline import _read_settings, _write_settings, settings_path

    path = path or settings_path()
    data = _read_settings(path)
    hooks = data.setdefault("hooks", {})
    entries = hooks.setdefault("PreToolUse", [])
    for e in entries:
        e["hooks"] = [h for h in e.get("hooks", []) if not _is_ours(h.get("command", ""))]
    entries[:] = [e for e in entries if e.get("hooks")]
    entries.append({"matcher": HOOK_MATCHER, "hooks": [{"type": "command", "command": command or hook_command(),
                                                       "timeout": 10}]})
    _write_settings(path, data)
    return f"хук git-guard → {path}"


def uninstall(path: Optional[Path] = None) -> str:
    from .statusline import _read_settings, _write_settings, settings_path

    path = path or settings_path()
    data = _read_settings(path)
    entries = (data.get("hooks") or {}).get("PreToolUse") or []
    for e in entries:
        e["hooks"] = [h for h in e.get("hooks", []) if not _is_ours(h.get("command", ""))]
    entries[:] = [e for e in entries if e.get("hooks")]
    if not entries and "hooks" in data:
        data["hooks"].pop("PreToolUse", None)
        if not data["hooks"]:
            data.pop("hooks")
    _write_settings(path, data)
    return "хук git-guard убран"


def installed(path: Optional[Path] = None) -> bool:
    from .statusline import _read_settings, settings_path

    data = _read_settings(path or settings_path())
    return any(_is_ours(h.get("command", "")) for e in (data.get("hooks") or {}).get("PreToolUse") or []
               for h in e.get("hooks", []))
