"""Голос пользователя: профиль стиля и корпус его реальных внешних текстов.

Внешние тексты (комменты в PR и Jira, ответы клиенту в SDESK, тексты задач, 4test,
коммиты) пишет один агент голоса, а не каждый скилл по-своему. Агент в пакете —
безличный дефолт (``voice-writer-sample``); персональное живёт только на машине
пользователя, в каталоге данных jwu по воркспейсу:

- ``profile.md`` — правила стиля, регистры, удачные и неудачные примеры, журнал правок
  «было → стало». Пользователь правит его руками; скиллы дописывают журнал только с его
  согласия (``add_feedback``).
- ``corpus.jsonl`` — реальные тексты пользователя по каналам: его комменты в PR, описания
  его PR, комменты в Jira/SDESK, коммит-месседжи. Из них агент берёт few-shot примеры.

Чат с ассистентом в корпус НЕ попадает никогда: там пользователь пишет телеграфно и с
опечатками, и это не его внешний стиль.

Какой агент пишет — настройка контура ``voice.agent`` (свой агент кладётся в
``~/.claude/agents`` или ``.claude/agents`` проекта). Ничего наружу этот модуль не пишет:
только читает внешние системы и пишет локальные файлы.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .config import data_dir

if TYPE_CHECKING:
    from .service import Service
    from .store import Store

DEFAULT_AGENT = "voice-writer-sample"
AGENT_SETTING = "voice.agent"

CHANNELS = (
    "pr_comment", "pr_reply", "pr_review_body", "pr_task", "pr_description",
    "jira_comment", "sdesk_client", "task", "4test", "commit", "message",
)
AUDIENCES = ("colleague", "qa", "analyst", "client")

# Если своих текстов в канале мало, примеры берутся из соседнего по жанру.
_CHANNEL_FALLBACK = {
    "pr_reply": ("pr_comment",),
    "pr_comment": ("pr_reply",),
    "pr_review_body": ("pr_comment", "pr_reply"),
    "pr_task": ("pr_comment",),
    "pr_description": ("task", "commit"),
    "jira_comment": ("pr_reply", "pr_comment"),
    "sdesk_client": ("jira_comment",),
    "task": ("jira_comment", "pr_description"),
    "4test": ("jira_comment", "task"),
    "commit": ("pr_description",),
    "message": ("pr_reply", "jira_comment"),
}

FEEDBACK_HEADER = "## Журнал правок"

PROFILE_TEMPLATE = """# Профиль голоса

Этот файл читает агент голоса перед тем, как написать текст от твоего имени.
Правь его руками: правила ниже важнее примеров из корпуса. Журнал правок в конце
дописывают скиллы jwu — только с твоего согласия.

## Общие правила

- (как ты пишешь: длина, порядок мыслей, чего избегать)

## Регистры

- Коллеге-разработчику: (на «ты» / на «вы», насколько прямо)
- QA и аналитику:
- Клиенту (SDESK): (на «вы», без внутренней кухни)

## По каналам

### pr_comment / pr_reply

### jira_comment

### sdesk_client

### commit

## Принято (хорошие примеры)

## Отклонено (и почему)

{feedback_header}
""".replace("{feedback_header}", FEEDBACK_HEADER)


# --------------------------------------------------------------------------- #
# Файлы
# --------------------------------------------------------------------------- #


def voice_dir(slug: str) -> Path:
    return data_dir() / "voice" / slug


def profile_path(slug: str) -> Path:
    return voice_dir(slug) / "profile.md"


def corpus_path(slug: str) -> Path:
    return voice_dir(slug) / "corpus.jsonl"


def ensure_profile(slug: str) -> Path:
    """Профиль контура; нет — создаётся из безличного шаблона."""
    path = profile_path(slug)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PROFILE_TEMPLATE, encoding="utf-8")
    return path


def read_profile(slug: str) -> str:
    return ensure_profile(slug).read_text(encoding="utf-8")


def agent_name(store: "Store", workspace_id: int) -> str:
    return (store.workspace_settings(workspace_id).get(AGENT_SETTING) or "").strip() or DEFAULT_AGENT


def set_agent(store: "Store", workspace_id: int, name: str) -> str:
    name = (name or "").strip()
    if name in ("", DEFAULT_AGENT):
        store.delete_workspace_settings(workspace_id, [AGENT_SETTING])
        return DEFAULT_AGENT
    store.set_workspace_settings(workspace_id, {AGENT_SETTING: name})
    return name


# --------------------------------------------------------------------------- #
# Корпус
# --------------------------------------------------------------------------- #


@dataclass
class VoiceSample:
    id: str                  # источник:ссылка:id — по нему дедуп при повторном сборе
    channel: str
    text: str
    audience: str = "colleague"
    source: str = ""         # bitbucket | github | jira | sdesk | git
    ref: str = ""            # PROJ/repo#42, PROJ-123, sha
    created: str = ""        # ISO
    context: str = ""        # на что отвечали (цитата), если есть

    def as_dict(self) -> dict:
        return asdict(self)


def load_corpus(slug: str) -> list[VoiceSample]:
    path = corpus_path(slug)
    if not path.exists():
        return []
    known = set(VoiceSample.__dataclass_fields__)
    out: list[VoiceSample] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        out.append(VoiceSample(**{k: v for k, v in raw.items() if k in known}))
    return out


def save_corpus(slug: str, samples: Iterable[VoiceSample]) -> int:
    path = corpus_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    items = sorted({s.id: s for s in samples}.values(), key=lambda s: s.created, reverse=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(s.as_dict(), ensure_ascii=False) + "\n" for s in items),
                   encoding="utf-8")
    tmp.replace(path)
    return len(items)


def reset_corpus(slug: str) -> None:
    """Стереть корпус (профиль не трогаем): следующий collect соберёт его заново."""
    corpus_path(slug).unlink(missing_ok=True)


def merge_corpus(slug: str, fresh: Iterable[VoiceSample]) -> tuple[int, int]:
    """Добавить новые тексты к корпусу (по id). → (добавлено, всего)."""
    current = {s.id: s for s in load_corpus(slug)}
    added = 0
    for s in fresh:
        if s.id not in current:
            added += 1
        current[s.id] = s
    total = save_corpus(slug, current.values())
    return added, total


# --------------------------------------------------------------------------- #
# Анализ корпуса в профиле
# --------------------------------------------------------------------------- #

# Раздел профиля, который пишет скилл jwu-voice-profile: правила по каналам, выведенные
# из корпуса. Агент голоса пишет по профилю и в примеры корпуса лезет, только если канала
# нет в анализе или пользователь сказал «не похоже на меня». Границы — HTML-комментарии,
# чтобы пересборка заменяла ровно этот раздел и не трогала ручные правки.
_ANALYSIS_RE = re.compile(
    r"<!-- jwu:voice-analysis (?P<meta>[^>]*)-->\n(?P<body>.*?)<!-- /jwu:voice-analysis -->\n?", re.S)
STALE_DAYS = 90
STALE_GROWTH = 1.25


def save_analysis(slug: str, markdown: str, *, channels: Iterable[str]) -> Path:
    """Записать (или заменить) раздел «Анализ корпуса» в профиле. Локальная запись."""
    body = markdown.strip()
    if not body:
        raise ValueError("Пустой анализ")
    chans = sorted({c.strip() for c in channels if c and c.strip()})
    total = corpus_stats(slug)["total"]
    day = datetime.now(timezone.utc).date().isoformat()
    meta = f"at={day} total={total} channels={','.join(chans)} "
    block = (f"<!-- jwu:voice-analysis {meta}-->\n"
             f"## Анализ корпуса (обновлён {day}, по {total} текстам)\n\n{body}\n"
             f"<!-- /jwu:voice-analysis -->\n")
    path = ensure_profile(slug)
    text = path.read_text(encoding="utf-8")
    if _ANALYSIS_RE.search(text):
        text = _ANALYSIS_RE.sub(lambda _m: block, text, count=1)
    elif FEEDBACK_HEADER in text:
        i = text.index(FEEDBACK_HEADER)
        text = text[:i] + block + "\n" + text[i:]
    else:
        text = text.rstrip() + "\n\n" + block
    path.write_text(text, encoding="utf-8")
    return path


def analysis_info(slug: str) -> dict:
    """Есть ли анализ, когда сделан, по скольким текстам, какие каналы, не устарел ли."""
    m = _ANALYSIS_RE.search(read_profile(slug))
    if not m:
        return {"present": False, "at": "", "corpus_total": 0, "channels": [], "stale": True}
    meta = dict(kv.split("=", 1) for kv in m.group("meta").split() if "=" in kv)
    total_then = int(meta.get("total") or 0)
    total_now = corpus_stats(slug)["total"]
    at = meta.get("at", "")
    try:
        age = (datetime.now(timezone.utc).date() - datetime.fromisoformat(at).date()).days
    except ValueError:
        age = STALE_DAYS + 1
    return {"present": True, "at": at, "corpus_total": total_then,
            "channels": [c for c in meta.get("channels", "").split(",") if c],
            "stale": age > STALE_DAYS or total_now > total_then * STALE_GROWTH}


def corpus_stats(slug: str) -> dict:
    samples = load_corpus(slug)
    by_channel: dict[str, int] = {}
    for s in samples:
        by_channel[s.channel] = by_channel.get(s.channel, 0) + 1
    path = corpus_path(slug)
    updated = (datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
               if path.exists() else "")
    return {"total": len(samples), "by_channel": by_channel, "updated": updated, "path": str(path)}


def _good_length(text: str) -> bool:
    n = len(text.strip())
    return 30 <= n <= 1200


def examples(slug: str, channel: str, *, audience: str | None = None, limit: int = 8) -> list[dict]:
    """Few-shot для агента: сначала сам канал и аудитория, потом соседние каналы; свежее выше."""
    samples = [s for s in load_corpus(slug) if _good_length(s.text)]
    order = (channel, *_CHANNEL_FALLBACK.get(channel, ()))
    picked: list[VoiceSample] = []
    seen: set[str] = set()
    for ch in order:
        pool = [s for s in samples if s.channel == ch and s.id not in seen]
        # нужная аудитория — первой (клиенту и коллеге пишут по-разному), внутри — свежее;
        # сортировка устойчивая, поэтому второй проход сохраняет порядок первого
        pool.sort(key=lambda s: s.created, reverse=True)
        pool.sort(key=lambda s: audience is not None and s.audience != audience)
        for s in pool:
            if len(picked) >= limit:
                break
            picked.append(s)
            seen.add(s.id)
        if len(picked) >= limit:
            break
    return [s.as_dict() for s in picked]


# --------------------------------------------------------------------------- #
# Журнал правок (петля обучения)
# --------------------------------------------------------------------------- #


def add_feedback(slug: str, *, channel: str, verdict: str, before: str, after: str = "",
                 reason: str = "", audience: str = "") -> Path:
    """Дописать в профиль пару «было → стало» или «отклонено, почему».

    ``verdict`` — edited | rejected | accepted. Звать только после согласия пользователя.
    """
    if verdict not in ("edited", "rejected", "accepted"):
        raise ValueError("verdict: edited | rejected | accepted")
    path = ensure_profile(slug)
    text = path.read_text(encoding="utf-8")
    if FEEDBACK_HEADER not in text:
        text = text.rstrip() + f"\n\n{FEEDBACK_HEADER}\n"
    day = datetime.now(timezone.utc).date().isoformat()
    label = {"edited": "поправлено", "rejected": "отклонено", "accepted": "принято"}[verdict]
    who = f" · {audience}" if audience else ""
    block = [f"\n### {day} · {channel}{who} · {label}"]
    block.append("Было:\n" + _quote(before))
    if after:
        block.append("Стало:\n" + _quote(after))
    if reason:
        block.append(f"Почему: {reason.strip()}")
    path.write_text(text.rstrip() + "\n" + "\n\n".join(block) + "\n", encoding="utf-8")
    return path


def _quote(text: str) -> str:
    return "\n".join("> " + ln if ln.strip() else ">" for ln in text.strip().splitlines())


# --------------------------------------------------------------------------- #
# Сбор корпуса (только чтение внешних систем)
# --------------------------------------------------------------------------- #


@dataclass
class CollectReport:
    added: int = 0
    total: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def bump(self, source: str, n: int = 1) -> None:
        self.by_source[source] = self.by_source.get(source, 0) + n


def _is_me(author: str, me: set[str]) -> bool:
    return bool(author) and author.casefold() in me


def _ms_iso(ms: int) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _identity(svc: "Service") -> set[str]:
    from .service import _read_identity

    ident = _read_identity(svc.store)
    names = {ident.get("user", ""), ident.get("display_name", ""), ident.get("email", "")}
    try:
        names.add(svc._resolve_username())
    except Exception:  # noqa: BLE001 — без сети личность берём из кэша
        pass
    try:
        me = svc._myself()
        names.update({me.get("name", ""), me.get("displayName", ""), me.get("login", "")})
    except Exception:  # noqa: BLE001
        pass
    return {n.casefold() for n in names if n}


def _collect_prs(svc: "Service", me: set[str], since_ms: int, max_prs: int,
                 report: CollectReport) -> list[VoiceSample]:
    client = svc.pr_client
    if client is None:
        return []
    source = "github" if client.__class__.__name__ == "GitHubClient" else "bitbucket"
    # Bitbucket Server 6.1 не знает state=ALL: только OPEN | MERGED | DECLINED
    states = ("OPEN", "CLOSED") if source == "github" else ("OPEN", "MERGED", "DECLINED")
    prs = []
    for view in ("mine", "review"):
        for state in states:
            try:
                prs.extend((view, p) for p in client.dashboard_prs(view, state=state))
            except Exception as exc:  # noqa: BLE001 — один вью не должен ронять сбор
                report.errors.append(f"PR {view}/{state}: {exc}")
    seen: set[tuple] = set()
    out: list[VoiceSample] = []
    fresh = [(v, p) for v, p in prs if (p.updated or 0) >= since_ms]
    fresh.sort(key=lambda vp: vp[1].updated or 0, reverse=True)
    for view, pr in fresh:
        key = (pr.project, pr.repository, pr.id)
        if key in seen or len(seen) >= max_prs:
            continue
        seen.add(key)
        ref = f"{pr.project}/{pr.repository}#{pr.id}"
        if view == "mine" and _good_length(pr.description or ""):
            out.append(VoiceSample(id=f"{source}:{ref}:description", channel="pr_description",
                                   text=pr.description.strip(), source=source, ref=ref,
                                   created=_ms_iso(pr.created)))
        try:
            comments = client.pr_comments(pr.project, pr.repository, pr.id)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"{ref}: {exc}")
            continue
        parent_text = ""
        for c in comments:
            if c.depth == 0:
                parent_text = c.text
            if not _is_me(c.author, me) or not c.text.strip():
                continue
            out.append(VoiceSample(
                id=f"{source}:{ref}:{c.id}",
                channel="pr_reply" if c.depth > 0 else "pr_comment",
                text=c.text.strip(), source=source, ref=ref, created=_ms_iso(c.created),
                context=(parent_text[:400] if c.depth > 0 else ""),
            ))
    report.bump(source, len(out))
    return out


def _collect_issues(svc: "Service", me: set[str], days: int, max_issues: int,
                    report: CollectReport) -> list[VoiceSample]:
    if svc.tasks_client is None or svc.provider != "jira":
        return []
    keys: list[str] = []
    jql = (f"(assignee = currentUser() OR reporter = currentUser() OR watcher = currentUser()) "
           f"AND updated >= -{int(days)}d ORDER BY updated DESC")
    try:
        keys += [i.key for i in svc.tasks_client.search(jql, max_results=50)]
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"Jira поиск: {exc}")
    # всё, что уже есть в памяти jwu: мои задачи, упоминания, задачи PR на ревью
    keys += [i.key for i in svc.store.latest_issues(None)]
    keys += [m.task_key for m in svc.store.list_mentions(limit=500)]
    ordered: list[str] = []
    for k in keys:
        if k and k not in ordered:
            ordered.append(k)
    out: list[VoiceSample] = []
    for key in ordered[:max_issues]:
        try:
            issue = svc._client_for_key(key).issue(key, with_dev=False)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"{key}: {exc}")
            continue
        sdesk = svc._key_is_sdesk(key)
        source = "sdesk" if sdesk else "jira"
        prev = ""
        for c in issue.comments:
            if _is_me(c.author_key, me) or _is_me(c.author, me):
                if c.body.strip():
                    out.append(VoiceSample(
                        id=f"{source}:{key}:{c.id}",
                        channel="sdesk_client" if sdesk else "jira_comment",
                        audience="client" if sdesk else "colleague",
                        text=c.body.strip(), source=source, ref=key, created=c.created,
                        context=prev[:400],
                    ))
            prev = c.body or ""
    report.bump("jira", len(out))
    return out


# Трейлеры, по которым видно, что сообщение коммита писал ассистент, а не человек.
_AI_TRAILER_RE = re.compile(
    r"^(Co-Authored-By:.*\b(Claude|anthropic|Copilot|Cursor|ChatGPT|OpenAI|Codex|Gemini|Aider|Devin)\b"
    r"|Claude-Session:|Generated with \[?Claude)", re.I | re.M)


def _collect_commits(paths: list[str], days: int, report: CollectReport) -> list[VoiceSample]:
    out: list[VoiceSample] = []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    for path in paths:
        if not (Path(path) / ".git").exists():
            continue
        try:
            email = subprocess.run(["git", "-C", path, "config", "user.email"], capture_output=True,
                                   text=True, timeout=10).stdout.strip()
            if not email:
                continue
            log = subprocess.run(
                ["git", "-C", path, "log", "--all", "--no-merges", f"--author={email}",
                 f"--since={since}", "--format=%H%x1f%aI%x1f%B%x1e"],
                capture_output=True, text=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            report.errors.append(f"git {path}: {exc}")
            continue
        for entry in log.split("\x1e"):
            parts = entry.strip("\n").split("\x1f")
            if len(parts) < 3:
                continue
            sha, created, body = parts[0].strip(), parts[1].strip(), parts[2].strip()
            # коммит, написанный ассистентом (трейлер соавторства ИИ), — не голос пользователя
            if _AI_TRAILER_RE.search(body):
                report.bump("git_skipped_ai")
                continue
            body = "\n".join(ln for ln in body.splitlines()
                             if not ln.startswith(("Co-Authored-By:", "Signed-off-by:"))).strip()
            if sha and body:
                out.append(VoiceSample(id=f"git:{sha}", channel="commit", text=body, source="git",
                                       ref=f"{Path(path).name}@{sha[:10]}", created=created))
    report.bump("git", len(out))
    return out


def collect(svc: "Service", slug: str, paths: list[str], *, days: int = 180,
            max_prs: int = 80, max_issues: int = 150) -> CollectReport:
    """Собрать корпус: мои тексты из PR, Jira/SDESK и git за ``days`` дней, дописать к файлу."""
    report = CollectReport()
    me = _identity(svc)
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    fresh: list[VoiceSample] = []
    if me:
        fresh += _collect_prs(svc, me, since_ms, max_prs, report)
        fresh += _collect_issues(svc, me, days, max_issues, report)
    else:
        report.errors.append("не знаю, кто я в Jira/Bitbucket/GitHub — PR и задачи пропущены")
    fresh += _collect_commits(paths, days, report)
    report.added, report.total = merge_corpus(slug, fresh)
    return report
