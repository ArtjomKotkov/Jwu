"""Память отдельно от кэша: экспорт/импорт того, что ценно, и синк через git.

В ``state.db`` живут две разные вещи. Снапшоты задач и PR — кэш: тяжёлый (сотни мегабайт),
восстановимый одним синком. Работы, их записи, заметки, правила, фичи, воркспейсы с папками
и тегами — память: мало, и заново не соберёшь. Здесь память выгружается в каталог обычных
JSON-файлов (по воркспейсу на подкаталог) и загружается обратно с дедупликацией по
естественным ключам, так что повторный импорт ничего не задваивает.

Секреты и снапшоты сюда не попадают никогда: каталог рассчитан на приватный git-репозиторий,
и ``sync`` умеет коммитить его туда и забирать чужие изменения перед экспортом. Это НЕ
рабочие репозитории проектов — свой, отдельный; следов jwu в проектных репозиториях не будет.

Естественные ключи (по ним же и мержим): воркспейс — slug; папка — путь; правило — (kind,
title, tag); фича — key; заметка — (key, ts, text); работа — (created_at, task_key, title);
запись работы — (ts, kind, text); связь работы с PR — (project, repo, pr_id).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import workspaces as ws_mod
from .config import data_dir

if TYPE_CHECKING:
    from .store import Store

FORMAT_VERSION = 1
MEMORY_REPO_META = "memory:repo"   # где лежит git-каталог памяти (meta БД)
LAST_SYNC_META = "memory:last_sync"


class MemoryError(RuntimeError):
    pass


def default_dir() -> Path:
    return data_dir() / "memory"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Экспорт
# --------------------------------------------------------------------------- #


@dataclass
class ExportReport:
    dest: str
    workspaces: int = 0
    rules: int = 0
    features: int = 0
    jobs: int = 0
    notes: int = 0

    def summary(self) -> str:
        return (f"воркспейсов {self.workspaces}, правил {self.rules}, фич {self.features}, "
                f"работ {self.jobs}, заметок {self.notes} → {self.dest}")


# Настройки воркспейса, которые НЕ выгружаем: секретов там нет, но «где чей токен лежал»
# и служебные счётчики — не память, а локальное устройство этой машины.
_SKIP_SETTING_PREFIXES = ("features.seq",)


def _all_notes(store: "Store") -> list[dict]:
    rows = store.conn.execute(
        "SELECT key, author, text, ts FROM notes WHERE workspace_id = ? ORDER BY ts, id",
        (store.workspace_id,),
    ).fetchall()
    return [{"key": r["key"], "author": r["author"], "text": r["text"], "ts": r["ts"]} for r in rows]


def export_memory(store: "Store", dest: Path | None = None) -> ExportReport:
    """Выгрузить память всех воркспейсов в каталог JSON-файлов."""
    dest = Path(dest or default_dir())
    dest.mkdir(parents=True, exist_ok=True)
    report = ExportReport(dest=str(dest))
    index: list[dict] = []
    active = store.get_meta(ws_mod.ACTIVE_META_KEY) or ""
    for ws in store.list_workspaces(include_archived=True):
        store.use_workspace(ws.id)
        settings = {k: v for k, v in store.workspace_settings(ws.id).items()
                    if not k.startswith(_SKIP_SETTING_PREFIXES)}
        index.append({
            "slug": ws.slug, "name": ws.name, "provider": ws.provider,
            "bitbucket_enabled": ws.bitbucket_enabled, "archived": ws.archived,
            "created_at": ws.created_at,
            "paths": [{"path": p.path, "label": p.label, "tags": sorted(p.tags)} for p in ws.paths],
            "settings": settings,
        })
        wdir = dest / ws.slug
        rules = [r.model_dump(exclude={"id", "workspace_id"}) for r in store.list_rules()]
        features = [f.model_dump(exclude={"id", "workspace_id"}) for f in store.list_features()]
        jobs = []
        for job in store.list_jobs():
            jobs.append({
                "task_key": job.task_key, "title": job.title, "status": job.status,
                "created_at": job.created_at, "updated_at": job.updated_at,
                "feature_key": job.feature_key,
                "prs": [p.model_dump() for p in job.prs],
                "records": [r.model_dump(exclude={"id", "job_id"}) for r in job.records],
            })
        notes = _all_notes(store)
        _write_json(wdir / "rules.json", rules)
        _write_json(wdir / "features.json", features)
        _write_json(wdir / "jobs.json", jobs)
        _write_json(wdir / "notes.json", notes)
        report.workspaces += 1
        report.rules += len(rules)
        report.features += len(features)
        report.jobs += len(jobs)
        report.notes += len(notes)
    _write_json(dest / "workspaces.json", {
        "format": FORMAT_VERSION, "exported_at": _now(), "active": active, "workspaces": index,
    })
    return report


# --------------------------------------------------------------------------- #
# Импорт (слияние по естественным ключам)
# --------------------------------------------------------------------------- #


@dataclass
class ImportReport:
    source: str
    dry_run: bool = False
    added: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)

    def bump(self, bucket: dict[str, int], kind: str) -> None:
        bucket[kind] = bucket.get(kind, 0) + 1

    def summary(self) -> str:
        def fmt(b: dict[str, int]) -> str:
            return ", ".join(f"{k} {v}" for k, v in sorted(b.items())) or "—"
        head = "сухой прогон: " if self.dry_run else ""
        return f"{head}добавлено: {fmt(self.added)}; обновлено: {fmt(self.updated)}; без изменений: {fmt(self.skipped)}"


def _import_workspace_meta(store: "Store", meta: dict, report: ImportReport) -> "int":
    """Воркспейс по slug: создать либо обновить название/провайдер/папки/теги/настройки."""
    ws = store.get_workspace_by_slug(meta["slug"])
    if ws is None:
        if not report.dry_run:
            ws = ws_mod.create(
                store, meta["slug"], name=meta.get("name", ""),
                provider=meta.get("provider", "local"),
                bitbucket=bool(meta.get("bitbucket_enabled")),
            )
        report.bump(report.added, "workspaces")
        if report.dry_run:
            return -1
    else:
        changed = (ws.name != meta.get("name", ws.name) or ws.provider != meta.get("provider", ws.provider)
                   or ws.bitbucket_enabled != bool(meta.get("bitbucket_enabled", ws.bitbucket_enabled)))
        if changed and not report.dry_run:
            store.conn.execute(
                "UPDATE workspaces SET name = ?, provider = ?, bitbucket_enabled = ? WHERE id = ?",
                (meta.get("name", ws.name), meta.get("provider", ws.provider),
                 int(bool(meta.get("bitbucket_enabled", ws.bitbucket_enabled))), ws.id),
            )
            store.conn.commit()
        report.bump(report.updated if changed else report.skipped, "workspaces")
    known = {p.path: p for p in store.workspace_paths(ws.id)}
    for p in meta.get("paths", []) or []:
        path = ws_mod.normalize_path(p["path"])
        if path in known:
            existing = known[path]
            new_tags = sorted(set(p.get("tags") or []) | set(existing.tags))
            if new_tags != sorted(existing.tags):
                if not report.dry_run:
                    store.set_path_tags(existing.id, new_tags)
                report.bump(report.updated, "paths")
            else:
                report.bump(report.skipped, "paths")
        else:
            if not report.dry_run:
                store.add_workspace_path(ws.id, path, p.get("label", ""), p.get("tags") or [])
            report.bump(report.added, "paths")
    settings = meta.get("settings") or {}
    current = store.workspace_settings(ws.id)
    fresh = {k: v for k, v in settings.items() if current.get(k) != v}
    if fresh:
        if not report.dry_run:
            store.set_workspace_settings(ws.id, fresh)
        report.bump(report.updated, "settings")
    return ws.id


def _import_rules(store: "Store", rules: list[dict], report: ImportReport) -> None:
    existing = {(r.kind, r.title, r.tag): r for r in store.list_rules()}
    for raw in rules:
        key = (raw.get("kind", "info"), raw.get("title", ""), raw.get("tag", "") or "")
        cur = existing.get(key)
        if cur is None:
            if not report.dry_run:
                rule = store.add_rule(raw.get("title", ""), text=raw.get("text", ""),
                                      kind=raw.get("kind", "info"), tag=raw.get("tag", "") or "")
                _stamp(store, "workspace_rules", rule.id, raw)
            report.bump(report.added, "rules")
        elif (raw.get("text", "") != cur.text) and (raw.get("updated_at", "") > (cur.updated_at or "")):
            if not report.dry_run:
                store.update_rule(cur.id, text=raw.get("text", ""))
            report.bump(report.updated, "rules")
        else:
            report.bump(report.skipped, "rules")


def _stamp(store: "Store", table: str, row_id: int, raw: dict) -> None:
    """Вернуть импортированной строке исходные метки времени (иначе всё «сегодняшнее»)."""
    created = raw.get("created_at") or raw.get("ts")
    updated = raw.get("updated_at") or created
    cols = {r["name"] for r in store.conn.execute(f"PRAGMA table_info({table})")}
    sets, params = [], []
    if created and "created_at" in cols:
        sets.append("created_at = ?"); params.append(created)
    if updated and "updated_at" in cols:
        sets.append("updated_at = ?"); params.append(updated)
    if sets:
        params.append(row_id)
        store.conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?", params)
        store.conn.commit()


def _import_features(store: "Store", features: list[dict], report: ImportReport) -> None:
    existing = {f.key: f for f in store.list_features()}
    for raw in features:
        key = raw.get("key", "")
        cur = existing.get(key)
        if cur is None:
            if not report.dry_run:
                ts = raw.get("created_at") or _now()
                store.conn.execute(
                    "INSERT INTO local_features (workspace_id, key, title, status, priority,"
                    " description, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (store.workspace_id, key, raw.get("title", ""), raw.get("status", "open"),
                     raw.get("priority", ""), raw.get("description", ""), ts,
                     raw.get("updated_at") or ts),
                )
                store.conn.commit()
                _bump_feature_seq(store, key)
            report.bump(report.added, "features")
        elif raw.get("updated_at", "") > (cur.updated_at or "") and (
            raw.get("status") != cur.status or raw.get("title") != cur.title
            or raw.get("description") != cur.description or raw.get("priority") != cur.priority
        ):
            if not report.dry_run:
                store.update_feature(cur.id, title=raw.get("title"), status=raw.get("status"),
                                     priority=raw.get("priority"), description=raw.get("description"))
            report.bump(report.updated, "features")
        else:
            report.bump(report.skipped, "features")


def _bump_feature_seq(store: "Store", key: str) -> None:
    """Счётчик ключей фич не должен отстать от импортированных ключей."""
    try:
        number = int(key.rsplit("-", 1)[-1])
    except ValueError:
        return
    settings = store.workspace_settings(store.workspace_id)
    seq = int(settings.get("features.seq") or 0)
    if number > seq:
        store.set_workspace_settings(store.workspace_id, {"features.seq": str(number)})


def _import_notes(store: "Store", notes: list[dict], report: ImportReport) -> None:
    existing = {(n["key"], n["ts"], n["text"]) for n in _all_notes(store)}
    for raw in notes:
        key = (raw.get("key", ""), raw.get("ts", ""), raw.get("text", ""))
        if key in existing:
            report.bump(report.skipped, "notes")
            continue
        if not report.dry_run:
            store.conn.execute(
                "INSERT INTO notes (key, author, text, ts, workspace_id) VALUES (?, ?, ?, ?, ?)",
                (raw.get("key", ""), raw.get("author", "claude"), raw.get("text", ""),
                 raw.get("ts") or _now(), store.workspace_id),
            )
            store.conn.commit()
        report.bump(report.added, "notes")


def _import_jobs(store: "Store", jobs: list[dict], report: ImportReport) -> None:
    existing = {(j.created_at, j.task_key, j.title): j for j in store.list_jobs()}
    features = {f.key: f for f in store.list_features()}
    for raw in jobs:
        key = (raw.get("created_at", ""), raw.get("task_key", ""), raw.get("title", ""))
        cur = existing.get(key)
        if cur is None:
            if report.dry_run:
                report.bump(report.added, "jobs")
                continue
            feature = features.get(raw.get("feature_key") or "")
            job = store.create_job(raw.get("task_key", ""), raw.get("title", ""),
                                   feature_id=feature.id if feature else None)
            if raw.get("status") and raw["status"] != "active":
                store.set_job_status(job.id, raw["status"])
            for link in raw.get("prs", []) or []:
                store.link_job_pr(job.id, int(link.get("pr_id", 0)), link.get("project", ""), link.get("repo", ""))
            for rec in raw.get("records", []) or []:
                r = store.add_job_record(job.id, rec.get("text", ""), kind=rec.get("kind", "note"),
                                         status=rec.get("status"), branch=rec.get("branch", ""),
                                         commit=rec.get("commit", ""))
                if rec.get("ts"):
                    store.conn.execute("UPDATE job_records SET ts = ? WHERE id = ?", (rec["ts"], r.id))
            store.conn.execute(
                "UPDATE jobs SET created_at = ?, updated_at = ? WHERE id = ?",
                (raw.get("created_at") or job.created_at, raw.get("updated_at") or job.updated_at, job.id),
            )
            store.conn.commit()
            report.bump(report.added, "jobs")
            continue
        # работа есть: досыпать недостающие записи и связи, подтянуть статус, если он свежее
        changed = False
        known_recs = {(r.ts, r.kind, r.text) for r in cur.records}
        for rec in raw.get("records", []) or []:
            if (rec.get("ts", ""), rec.get("kind", "note"), rec.get("text", "")) in known_recs:
                continue
            changed = True
            if not report.dry_run:
                r = store.add_job_record(cur.id, rec.get("text", ""), kind=rec.get("kind", "note"),
                                         status=rec.get("status"), branch=rec.get("branch", ""),
                                         commit=rec.get("commit", ""))
                if rec.get("ts"):
                    store.conn.execute("UPDATE job_records SET ts = ? WHERE id = ?", (rec["ts"], r.id))
                    store.conn.commit()
        known_prs = {(p.project, p.repo, p.pr_id) for p in cur.prs}
        for link in raw.get("prs", []) or []:
            if (link.get("project", ""), link.get("repo", ""), int(link.get("pr_id", 0))) in known_prs:
                continue
            changed = True
            if not report.dry_run:
                store.link_job_pr(cur.id, int(link.get("pr_id", 0)), link.get("project", ""), link.get("repo", ""))
        if raw.get("status") and raw["status"] != cur.status and raw.get("updated_at", "") > (cur.updated_at or ""):
            changed = True
            if not report.dry_run:
                store.set_job_status(cur.id, raw["status"])
        report.bump(report.updated if changed else report.skipped, "jobs")


def import_memory(store: "Store", src: Path | None = None, *, dry_run: bool = False) -> ImportReport:
    """Слить память из каталога в БД. Ничего не удаляет; ``dry_run`` — только посчитать."""
    src = Path(src or default_dir())
    index_file = src / "workspaces.json"
    if not index_file.exists():
        raise MemoryError(f"В {src} нет workspaces.json — это не каталог памяти jwu")
    index = _read_json(index_file, {})
    if int(index.get("format", 0) or 0) > FORMAT_VERSION:
        raise MemoryError("Каталог памяти записан более новой версией jwu — обнови jwu")
    report = ImportReport(source=str(src), dry_run=dry_run)
    for meta in index.get("workspaces", []) or []:
        wid = _import_workspace_meta(store, meta, report)
        if wid < 0:
            continue  # сухой прогон нового воркспейса: считать его содержимое некуда
        store.use_workspace(wid)
        wdir = src / meta["slug"]
        _import_rules(store, _read_json(wdir / "rules.json", []), report)
        _import_features(store, _read_json(wdir / "features.json", []), report)
        _import_notes(store, _read_json(wdir / "notes.json", []), report)
        _import_jobs(store, _read_json(wdir / "jobs.json", []), report)
    return report


# --------------------------------------------------------------------------- #
# Синк через git
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return 127, "git не найден"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def sync_memory(store: "Store", repo: Path | None = None, *, push: bool = True,
                message: str | None = None) -> dict:
    """pull → import → export → commit → push. Каталог должен быть git-репозиторием.

    Порядок важен: сначала забираем чужие изменения и вливаем их в БД, потом выгружаем
    уже объединённую память и коммитим. Так две машины сходятся к одному состоянию без
    ручного разбора конфликтов JSON.
    """
    repo = Path(repo or store.get_meta(MEMORY_REPO_META) or default_dir())
    if not (repo / ".git").exists():
        raise MemoryError(
            f"{repo} — не git-репозиторий. Создай его (git init / git clone приватного репо) "
            f"и укажи: jwu memory sync --repo {repo}"
        )
    result: dict = {"repo": str(repo), "pulled": False, "committed": False, "pushed": False}
    code, out = _git(repo, "remote")
    has_remote = code == 0 and bool(out.strip())
    if has_remote:
        code, out = _git(repo, "pull", "--rebase", "--quiet")
        if code != 0:
            raise MemoryError(f"git pull не удался: {out}")
        result["pulled"] = True
    if (repo / "workspaces.json").exists():
        result["import"] = import_memory(store, repo).summary()
    result["export"] = export_memory(store, repo).summary()
    _git(repo, "add", "-A")
    code, out = _git(repo, "status", "--porcelain")
    if out.strip():
        msg = message or f"jwu memory {_now()}"
        code, out = _git(repo, "commit", "-q", "-m", msg)
        if code != 0:
            raise MemoryError(f"git commit не удался: {out}")
        result["committed"] = True
    if push and has_remote and result["committed"]:
        code, out = _git(repo, "push", "--quiet")
        if code != 0:
            raise MemoryError(f"git push не удался: {out}")
        result["pushed"] = True
    store.set_meta(MEMORY_REPO_META, str(repo))
    store.set_meta(LAST_SYNC_META, _now())
    return result
