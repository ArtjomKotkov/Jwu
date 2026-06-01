"""SQLite-память: снапшоты задач/PR, дельты между синками, заметки Claude.

Один файл БД (по умолчанию ~/.local/share/jwu/state.db). Каждый ``sync``
создаёт запись в ``sync_runs`` и кладёт снапшот по каждой задаче/PR. Дельты считаются
сравнением последнего снапшота сущности с предыдущим (по предыдущему синку, где она встречалась).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Analysis, Delta, Issue, Job, JobPRLink, JobRecord, Note, PR

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    views       TEXT NOT NULL,
    counts      TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS issue_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_run_id INTEGER NOT NULL,
    key         TEXT NOT NULL,
    signature   TEXT NOT NULL,
    fields      TEXT NOT NULL,
    views       TEXT NOT NULL DEFAULT '[]',
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_issue_snap_key ON issue_snapshots(key, sync_run_id);
CREATE TABLE IF NOT EXISTS pr_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_run_id INTEGER NOT NULL,
    pr_id       INTEGER NOT NULL,
    project     TEXT NOT NULL DEFAULT '',
    repo        TEXT NOT NULL DEFAULT '',
    conflicted  INTEGER,
    fields      TEXT NOT NULL,
    signature   TEXT NOT NULL DEFAULT '{}',
    views       TEXT NOT NULL DEFAULT '[]',
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pr_snap_id ON pr_snapshots(pr_id, sync_run_id);
CREATE TABLE IF NOT EXISTS notes (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    key    TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT 'claude',
    text   TEXT NOT NULL,
    ts     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_key ON notes(key);
CREATE TABLE IF NOT EXISTS analyses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    content    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key    TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_task ON jobs(task_key);
CREATE TABLE IF NOT EXISTS job_records (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    kind   TEXT NOT NULL DEFAULT 'note',
    text   TEXT NOT NULL,
    status TEXT,
    ts     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_records_job ON job_records(job_id);
CREATE TABLE IF NOT EXISTS job_prs (
    job_id  INTEGER NOT NULL,
    pr_id   INTEGER NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    repo    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (job_id, pr_id, project, repo)
);
CREATE TABLE IF NOT EXISTS pending_changes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER NOT NULL,
    key     TEXT NOT NULL,
    kind    TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    detail  TEXT NOT NULL DEFAULT '',
    ts      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _issue_signature(issue: Issue) -> dict:
    return {
        "status": issue.status,
        "resolution": issue.resolution,
        "comment_ids": [c.id for c in issue.comments],
        "pr_ids": [pr.id for pr in issue.pull_requests],
        "branches": [b.name for b in issue.branches],
    }


def _pr_signature(pr: PR) -> dict:
    return {
        "comment_count": pr.comment_count,
        "latest_commit": pr.latest_commit,
        "conflicted": pr.conflicted,
        "reviewers": {r.name: r.approved for r in pr.reviewers},
    }


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Доезд старых БД: добавить недостающие колонки."""
        for table in ("issue_snapshots", "pr_snapshots"):
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if "views" not in cols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN views TEXT NOT NULL DEFAULT '[]'"
                )
        pr_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(pr_snapshots)")}
        if "signature" not in pr_cols:
            self.conn.execute(
                "ALTER TABLE pr_snapshots ADD COLUMN signature TEXT NOT NULL DEFAULT '{}'"
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- запись синка --------------------------------------------------- #

    def start_sync_run(self, views: list[str]) -> int:
        cur = self.conn.execute(
            "INSERT INTO sync_runs (started_at, views) VALUES (?, ?)",
            (_now(), json.dumps(views)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_sync_run(self, run_id: int, counts: dict) -> None:
        self.conn.execute(
            "UPDATE sync_runs SET counts = ? WHERE id = ?",
            (json.dumps(counts), run_id),
        )
        self.conn.commit()

    def save_issue_snapshot(
        self, run_id: int, issue: Issue, views: list[str] | None = None
    ) -> None:
        self.conn.execute(
            "INSERT INTO issue_snapshots (sync_run_id, key, signature, fields, views, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                issue.key,
                json.dumps(_issue_signature(issue)),
                issue.model_dump_json(),
                json.dumps(sorted(views or [])),
                _now(),
            ),
        )
        self.conn.commit()

    def save_pr_snapshot(self, run_id: int, pr: PR, views: list[str] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO pr_snapshots (sync_run_id, pr_id, project, repo, conflicted, fields, signature, views, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                pr.id,
                pr.project,
                pr.repository,
                None if pr.conflicted is None else int(pr.conflicted),
                pr.model_dump_json(),
                json.dumps(_pr_signature(pr)),
                json.dumps(sorted(views or [])),
                _now(),
            ),
        )
        self.conn.commit()

    # --- чтение --------------------------------------------------------- #

    def latest_run_id(self) -> int | None:
        row = self.conn.execute("SELECT MAX(id) AS m FROM sync_runs").fetchone()
        return row["m"] if row and row["m"] is not None else None

    def last_sync_at(self, token: str | None = None) -> str | None:
        """Время последнего синка; с token — последнего синка секции (значение в views)."""
        if token is None:
            row = self.conn.execute(
                "SELECT started_at FROM sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row["started_at"] if row else None
        for row in self.conn.execute(
            "SELECT started_at, views FROM sync_runs ORDER BY id DESC"
        ):
            if token in json.loads(row["views"]):
                return row["started_at"]
        return None

    def latest_issues(self, view: str | None = None) -> list[Issue]:
        """Свежайший снапшот по каждой задаче (опц. фильтр по вью), updated DESC.

        Берём именно последний снапшот *по ключу*, а не по последнему run —
        чтобы посекционный синк не «терял» вкладки, синканные в других прогонах.
        """
        rows = self.conn.execute(
            "SELECT fields, views, MAX(sync_run_id) FROM issue_snapshots GROUP BY key"
        ).fetchall()
        issues: list[Issue] = []
        for row in rows:
            if view is not None and view not in json.loads(row["views"]):
                continue
            issues.append(Issue.model_validate_json(row["fields"]))
        issues.sort(key=lambda i: i.updated, reverse=True)
        return issues

    def latest_prs(self, view: str | None = None) -> list[PR]:
        """Свежайший снапшот по каждому PR (опц. фильтр по вью: mine|review)."""
        rows = self.conn.execute(
            "SELECT fields, views, MAX(sync_run_id) FROM pr_snapshots GROUP BY pr_id"
        ).fetchall()
        prs: list[PR] = []
        for row in rows:
            if view is not None and view not in json.loads(row["views"]):
                continue
            prs.append(PR.model_validate_json(row["fields"]))
        prs.sort(key=lambda p: p.updated, reverse=True)
        return prs

    def snapshotted_issue_keys(self, run_id: int) -> set[str]:
        """Ключи задач, уже снапшотнутые в этом прогоне (чтобы не плодить дубли)."""
        rows = self.conn.execute(
            "SELECT DISTINCT key FROM issue_snapshots WHERE sync_run_id = ?", (run_id,)
        ).fetchall()
        return {r["key"] for r in rows}

    def _prev_issue_signature(self, key: str, before_run: int) -> dict | None:
        row = self.conn.execute(
            "SELECT signature FROM issue_snapshots WHERE key = ? AND sync_run_id < ?"
            " ORDER BY sync_run_id DESC LIMIT 1",
            (key, before_run),
        ).fetchone()
        return json.loads(row["signature"]) if row else None

    def _prev_pr_signature(self, pr_id: int, before_run: int) -> dict | None:
        row = self.conn.execute(
            "SELECT signature FROM pr_snapshots WHERE pr_id = ? AND sync_run_id < ?"
            " ORDER BY sync_run_id DESC LIMIT 1",
            (pr_id, before_run),
        ).fetchone()
        if not row:
            return None
        sig = json.loads(row["signature"])
        return sig or None  # пустая '{}' от старых строк = «не видели по-настоящему»

    # --- дельты --------------------------------------------------------- #

    def compute_changes(self, run_id: int | None = None) -> list[Delta]:
        """Сравнить снапшоты последнего синка с предыдущими и вернуть дельты."""
        run_id = run_id or self.latest_run_id()
        if run_id is None:
            return []
        deltas: list[Delta] = []

        # задачи
        rows = self.conn.execute(
            "SELECT key, signature, fields FROM issue_snapshots WHERE sync_run_id = ?",
            (run_id,),
        ).fetchall()
        for row in rows:
            key = row["key"]
            cur = json.loads(row["signature"])
            prev = self._prev_issue_signature(key, run_id)
            summary = json.loads(row["fields"]).get("summary", "")
            if prev is None:
                deltas.append(Delta(key=key, kind="new_issue", summary=summary))
                continue
            if cur.get("status") != prev.get("status"):
                deltas.append(Delta(
                    key=key, kind="status_change", summary=summary,
                    detail=f"{prev.get('status')} → {cur.get('status')}",
                ))
            if not prev.get("resolution") and cur.get("resolution"):
                deltas.append(Delta(
                    key=key, kind="resolved", summary=summary,
                    detail=cur.get("resolution", ""),
                ))
            new_comments = set(cur.get("comment_ids", [])) - set(prev.get("comment_ids", []))
            if new_comments:
                deltas.append(Delta(
                    key=key, kind="new_comment", summary=summary,
                    detail=f"+{len(new_comments)} комм.",
                ))
            new_prs = set(cur.get("pr_ids", [])) - set(prev.get("pr_ids", []))
            if new_prs:
                deltas.append(Delta(
                    key=key, kind="new_pr", summary=summary,
                    detail=", ".join(map(str, sorted(new_prs))),
                ))

        # PR: новые комменты/коммиты, апрувы, конфликт
        pr_rows = self.conn.execute(
            "SELECT pr_id, project, repo, signature, fields FROM pr_snapshots WHERE sync_run_id = ?",
            (run_id,),
        ).fetchall()
        for row in pr_rows:
            cur = json.loads(row["signature"])
            prev = self._prev_pr_signature(row["pr_id"], run_id)
            if prev is None:
                continue  # первый раз видим PR — не шумим
            pr_key = f"{row['project']}/{row['repo']}#{row['pr_id']}"
            title = json.loads(row["fields"]).get("title", "")

            added = (cur.get("comment_count") or 0) - (prev.get("comment_count") or 0)
            if added > 0:
                deltas.append(Delta(
                    key=pr_key, kind="new_pr_comment", summary=title,
                    detail=f"+{added} комм.",
                ))
            if cur.get("latest_commit") and cur.get("latest_commit") != prev.get("latest_commit"):
                deltas.append(Delta(
                    key=pr_key, kind="new_pr_commit", summary=title, detail="новый коммит",
                ))
            prev_rev = prev.get("reviewers", {}) or {}
            for name, approved in (cur.get("reviewers", {}) or {}).items():
                if approved and not prev_rev.get(name, False):
                    deltas.append(Delta(
                        key=pr_key, kind="reviewer_approved", summary=title,
                        detail=f"{name} проапрувил",
                    ))
            if cur.get("conflicted") and not prev.get("conflicted"):
                deltas.append(Delta(
                    key=pr_key, kind="new_conflict", summary=title,
                    detail="появился merge-конфликт",
                ))
        return deltas

    # --- накопленные изменения (копятся, пока не закрыты явно) ----------- #

    def add_pending_changes(self, run_id: int, deltas: list[Delta]) -> None:
        """Дописать дельты синка в накопитель (показываются, пока не очистят)."""
        ts = _now()
        self.conn.executemany(
            "INSERT INTO pending_changes (run_id, key, kind, summary, detail, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(run_id, d.key, d.kind, d.summary, d.detail, ts) for d in deltas],
        )
        self.conn.commit()

    def pending_changes(self) -> list[Delta]:
        rows = self.conn.execute(
            "SELECT key, kind, summary, detail FROM pending_changes ORDER BY id"
        ).fetchall()
        return [
            Delta(key=r["key"], kind=r["kind"], summary=r["summary"], detail=r["detail"])
            for r in rows
        ]

    def clear_pending_changes(self, pairs: list[tuple[str, str]] | None = None) -> None:
        """Очистить накопленные изменения: все (pairs=None) или только по (key, kind)."""
        if pairs is None:
            self.conn.execute("DELETE FROM pending_changes")
        elif pairs:
            self.conn.executemany(
                "DELETE FROM pending_changes WHERE key = ? AND kind = ?", pairs
            )
        self.conn.commit()

    # --- произвольные метаданные (key-value) ---------------------------- #

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    # --- заметки -------------------------------------------------------- #

    def add_note(self, key: str, text: str, author: str = "claude") -> Note:
        ts = _now()
        self.conn.execute(
            "INSERT INTO notes (key, author, text, ts) VALUES (?, ?, ?, ?)",
            (key, author, text, ts),
        )
        self.conn.commit()
        return Note(key=key, author=author, text=text, ts=ts)

    def get_notes(self, key: str) -> list[Note]:
        rows = self.conn.execute(
            "SELECT key, author, text, ts FROM notes WHERE key = ? ORDER BY ts",
            (key,),
        ).fetchall()
        return [
            Note(key=r["key"], author=r["author"], text=r["text"], ts=r["ts"])
            for r in rows
        ]

    # --- анализы (дневные планы) ---------------------------------------- #

    def save_analysis(self, content: str, title: str = "") -> Analysis:
        ts = _now()
        cur = self.conn.execute(
            "INSERT INTO analyses (created_at, title, content) VALUES (?, ?, ?)",
            (ts, title, content),
        )
        self.conn.commit()
        return Analysis(id=int(cur.lastrowid), created_at=ts, title=title, content=content)

    def list_analyses(self, limit: int = 50) -> list[Analysis]:
        """Список без полного content (только метаданные), новые сверху."""
        rows = self.conn.execute(
            "SELECT id, created_at, title FROM analyses ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            Analysis(id=r["id"], created_at=r["created_at"], title=r["title"])
            for r in rows
        ]

    def get_analysis(self, analysis_id: int | None = None) -> Analysis | None:
        """Анализ по id; без id — последний."""
        if analysis_id is None:
            row = self.conn.execute(
                "SELECT id, created_at, title, content FROM analyses ORDER BY id DESC LIMIT 1"
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT id, created_at, title, content FROM analyses WHERE id = ?",
                (analysis_id,),
            ).fetchone()
        if not row:
            return None
        return Analysis(id=row["id"], created_at=row["created_at"],
                        title=row["title"], content=row["content"])

    # --- работы (jobs) -------------------------------------------------- #

    def create_job(self, task_key: str, title: str = "") -> Job:
        ts = _now()
        cur = self.conn.execute(
            "INSERT INTO jobs (task_key, title, status, created_at, updated_at)"
            " VALUES (?, ?, 'active', ?, ?)",
            (task_key, title, ts, ts),
        )
        self.conn.commit()
        return Job(id=int(cur.lastrowid), task_key=task_key, title=title,
                   status="active", created_at=ts, updated_at=ts)

    def _touch_job(self, job_id: int) -> None:
        """Обновить updated_at. БЕЗ commit — вызывающий коммитит сам."""
        self.conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (_now(), job_id))

    def add_job_record(self, job_id: int, text: str, kind: str = "note",
                       status: str | None = None) -> JobRecord:
        ts = _now()
        cur = self.conn.execute(
            "INSERT INTO job_records (job_id, kind, text, status, ts) VALUES (?, ?, ?, ?, ?)",
            (job_id, kind, text, status, ts),
        )
        self._touch_job(job_id)
        self.conn.commit()
        return JobRecord(id=int(cur.lastrowid), job_id=job_id, kind=kind,
                         text=text, status=status, ts=ts)

    def link_job_pr(self, job_id: int, pr_id: int, project: str = "", repo: str = "") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO job_prs (job_id, pr_id, project, repo) VALUES (?, ?, ?, ?)",
            (job_id, pr_id, project, repo),
        )
        self._touch_job(job_id)
        self.conn.commit()

    def set_job_status(self, job_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), job_id),
        )
        self.conn.commit()

    def delete_job(self, job_id: int) -> None:
        """Удалить работу вместе с записями и связями с PR."""
        self.conn.execute("DELETE FROM job_records WHERE job_id = ?", (job_id,))
        self.conn.execute("DELETE FROM job_prs WHERE job_id = ?", (job_id,))
        self.conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        self.conn.commit()

    def _job_records(self, job_id: int) -> list[JobRecord]:
        rows = self.conn.execute(
            "SELECT id, job_id, kind, text, status, ts FROM job_records WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
        return [JobRecord(id=r["id"], job_id=r["job_id"], kind=r["kind"], text=r["text"],
                          status=r["status"], ts=r["ts"]) for r in rows]

    def _job_prs(self, job_id: int) -> list[JobPRLink]:
        rows = self.conn.execute(
            "SELECT pr_id, project, repo FROM job_prs WHERE job_id = ? ORDER BY pr_id",
            (job_id,),
        ).fetchall()
        return [JobPRLink(pr_id=r["pr_id"], project=r["project"], repo=r["repo"]) for r in rows]

    def _job_from_row(self, row, *, with_records: bool) -> Job:
        job = Job(id=row["id"], task_key=row["task_key"], title=row["title"],
                  status=row["status"], created_at=row["created_at"], updated_at=row["updated_at"])
        job.prs = self._job_prs(job.id)
        if with_records:
            job.records = self._job_records(job.id)
        return job

    def get_job(self, job_id: int) -> Job | None:
        row = self.conn.execute(
            "SELECT id, task_key, title, status, created_at, updated_at FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        return self._job_from_row(row, with_records=True) if row else None

    def list_jobs(self, *, task_key: str | None = None, pr_id: int | None = None,
                  status: str | None = None, project: str | None = None,
                  repo: str | None = None) -> list[Job]:
        sql = ("SELECT DISTINCT j.id, j.task_key, j.title, j.status, j.created_at, j.updated_at"
               " FROM jobs j")
        params: list = []
        if pr_id is not None:
            join = " JOIN job_prs p ON p.job_id = j.id AND p.pr_id = ?"
            params.append(pr_id)
            if project:
                join += " AND p.project = ?"
                params.append(project)
            if repo:
                join += " AND p.repo = ?"
                params.append(repo)
            sql += join
        conds: list[str] = []
        if task_key is not None:
            conds.append("j.task_key = ?"); params.append(task_key)
        if status is not None:
            conds.append("j.status = ?"); params.append(status)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY j.updated_at DESC, j.id DESC"
        rows = self.conn.execute(sql, params).fetchall()
        # records грузим тоже: список работ невелик (CLI/дашборд), а потребители
        # показывают число записей (jwu jobs) — без них колонка «Записей» врала бы 0.
        return [self._job_from_row(r, with_records=True) for r in rows]

    def jobs_for_task(self, task_key: str) -> list[Job]:
        return self.list_jobs(task_key=task_key)

    def jobs_for_pr(self, pr_id: int, project: str = "", repo: str = "") -> list[Job]:
        return self.list_jobs(pr_id=pr_id, project=project or None, repo=repo or None)
