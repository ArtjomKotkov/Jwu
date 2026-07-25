"""SQLite-память: снапшоты задач/PR, дельты между синками, заметки Claude.

Один файл БД (по умолчанию ~/.local/share/jwu/state.db). Каждый ``sync``
создаёт запись в ``sync_runs`` и кладёт снапшот по каждой задаче/PR. Дельты считаются
сравнением последнего снапшота сущности с предыдущим (по предыдущему синку, где она встречалась).

Все локальные данные принадлежат воркспейсу (``workspace_id``): работы, заметки, анализы,
снапшоты, накопленные изменения и прогоны синка. ``Store`` открывается в скоупе одного
воркспейса и сам подставляет фильтр во все запросы — снаружи API методов от этого не зависит.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    Analysis, Delta, Issue, Job, JobPRLink, JobRecord, LocalFeature, Note, PR,
    Workspace, WorkspacePath,
)

# Префикс ключей локальных фич должен читаться как ключ Jira: HOMEJWU-1, FEAT-3.
_FEATURE_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")

# Воркспейс, в который уезжают данные, накопленные до появления воркспейсов.
DEFAULT_WORKSPACE_SLUG = "work"
DEFAULT_WORKSPACE_NAME = "Работа"

# Таблицы, скоупнутые по воркспейсу (у каждой есть колонка workspace_id).
WORKSPACE_SCOPED_TABLES = (
    "sync_runs", "issue_snapshots", "pr_snapshots",
    "notes", "analyses", "jobs", "pending_changes",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    slug              TEXT NOT NULL UNIQUE,
    name              TEXT NOT NULL DEFAULT '',
    jira_enabled      INTEGER NOT NULL DEFAULT 0,
    bitbucket_enabled INTEGER NOT NULL DEFAULT 0,
    archived          INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_paths (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    path         TEXT NOT NULL UNIQUE,
    label        TEXT NOT NULL DEFAULT '',
    added_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ws_paths_ws ON workspace_paths(workspace_id);
CREATE TABLE IF NOT EXISTS workspace_path_tags (
    path_id INTEGER NOT NULL,
    tag     TEXT NOT NULL,
    PRIMARY KEY (path_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_path_tags_tag ON workspace_path_tags(tag);
CREATE TABLE IF NOT EXISTS workspace_settings (
    workspace_id INTEGER NOT NULL,
    key          TEXT NOT NULL,
    value        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (workspace_id, key)
);
CREATE TABLE IF NOT EXISTS workspace_secrets (
    workspace_id INTEGER NOT NULL,
    slot         TEXT NOT NULL,
    value        TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (workspace_id, slot)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    views        TEXT NOT NULL,
    counts       TEXT NOT NULL DEFAULT '{}',
    workspace_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS issue_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_run_id  INTEGER NOT NULL,
    key          TEXT NOT NULL,
    signature    TEXT NOT NULL,
    fields       TEXT NOT NULL,
    views        TEXT NOT NULL DEFAULT '[]',
    fetched_at   TEXT NOT NULL,
    workspace_id INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_issue_snap_key ON issue_snapshots(key, sync_run_id);
CREATE TABLE IF NOT EXISTS pr_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_run_id  INTEGER NOT NULL,
    pr_id        INTEGER NOT NULL,
    project      TEXT NOT NULL DEFAULT '',
    repo         TEXT NOT NULL DEFAULT '',
    conflicted   INTEGER,
    fields       TEXT NOT NULL,
    signature    TEXT NOT NULL DEFAULT '{}',
    views        TEXT NOT NULL DEFAULT '[]',
    fetched_at   TEXT NOT NULL,
    workspace_id INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pr_snap_id ON pr_snapshots(pr_id, sync_run_id);
CREATE TABLE IF NOT EXISTS notes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT NOT NULL,
    author       TEXT NOT NULL DEFAULT 'claude',
    text         TEXT NOT NULL,
    ts           TEXT NOT NULL,
    workspace_id INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notes_key ON notes(key);
CREATE TABLE IF NOT EXISTS analyses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL,
    workspace_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key     TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    workspace_id INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_task ON jobs(task_key);
CREATE TABLE IF NOT EXISTS local_features (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    key          TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'open',
    priority     TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_local_features_key ON local_features(workspace_id, key);
CREATE INDEX IF NOT EXISTS idx_local_features_status ON local_features(workspace_id, status);
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
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL,
    key          TEXT NOT NULL,
    kind         TEXT NOT NULL,
    summary      TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT '',
    section      TEXT NOT NULL DEFAULT '',
    ts           TEXT NOT NULL,
    workspace_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Индексы по workspace_id живут ОТДЕЛЬНО от SCHEMA: на старой БД колонки ещё нет в момент
# executescript(SCHEMA), и CREATE INDEX по ней упал бы. Создаются в шаге миграции, после ALTER.
WORKSPACE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_issue_snap_ws ON issue_snapshots(workspace_id, key, sync_run_id);
CREATE INDEX IF NOT EXISTS idx_pr_snap_ws    ON pr_snapshots(workspace_id, pr_id, sync_run_id);
CREATE INDEX IF NOT EXISTS idx_sync_runs_ws  ON sync_runs(workspace_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_ws       ON jobs(workspace_id, task_key);
CREATE INDEX IF NOT EXISTS idx_notes_ws      ON notes(workspace_id, key);
CREATE INDEX IF NOT EXISTS idx_analyses_ws   ON analyses(workspace_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_pending_ws    ON pending_changes(workspace_id);
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
        # достоверны ли pr_ids/branches (см. Issue.dev_ok)
        "dev_ok": issue.dev_ok,
    }


def _pr_signature(pr: PR) -> dict:
    return {
        "comment_count": pr.comment_count,
        "latest_commit": pr.latest_commit,
        "conflicted": pr.conflicted,
        "reviewers": {r.name: r.approved for r in pr.reviewers},
    }


# --------------------------------------------------------------------------- #
# Миграции схемы
# --------------------------------------------------------------------------- #

# Версия схемы, до которой доводится любая открываемая БД. Хранится в meta['schema_version'].
SCHEMA_VERSION = 5


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """ALTER TABLE … ADD COLUMN, если колонки ещё нет (шаг идемпотентен)."""
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _insert_workspace(
    conn: sqlite3.Connection, slug: str, *, name: str = "",
    jira: bool = False, bitbucket: bool = False,
) -> int:
    """Создать воркспейс (или вернуть id существующего с таким slug)."""
    row = conn.execute("SELECT id FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    if row:
        return int(row["id"])
    ts = _now()
    cur = conn.execute(
        "INSERT INTO workspaces (slug, name, jira_enabled, bitbucket_enabled, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (slug, name or slug, int(jira), int(bitbucket), ts, ts),
    )
    return int(cur.lastrowid)


def _m002_workspaces(conn: sqlite3.Connection) -> None:
    """v1 → v2: воркспейсы. Всё накопленное уезжает в воркспейс «Работа».

    Уже существующие данные заведомо рабочие (Jira + Bitbucket были единственным
    режимом jwu), поэтому мигрированному воркспейсу обе интеграции включаются.
    """
    for table in WORKSPACE_SCOPED_TABLES:
        _add_column(conn, table, "workspace_id", "INTEGER NOT NULL DEFAULT 0")
    conn.executescript(WORKSPACE_INDEXES)

    wid = _insert_workspace(
        conn, DEFAULT_WORKSPACE_SLUG, name=DEFAULT_WORKSPACE_NAME, jira=True, bitbucket=True
    )
    for table in WORKSPACE_SCOPED_TABLES:
        conn.execute(f"UPDATE {table} SET workspace_id = ? WHERE workspace_id = 0", (wid,))

    # Пер-воркспейсные ключи meta переезжают под префикс воркспейса; ui.theme — глобальный.
    for key in ("identity", "pr_task_aliases"):
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            continue
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"w{wid}:{key}", row["value"]),
        )
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))


def _m003_features(conn: sqlite3.Connection) -> None:
    """v2 → v3: локальный трекер фич и работы без задачи Jira.

    ``jobs.task_key`` намеренно остаётся NOT NULL (пустая строка = «задачи нет») —
    так не нужно пересобирать таблицу; якорем может быть локальная фича.
    """
    _add_column(conn, "jobs", "feature_id", "INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_feature ON jobs(feature_id)")


def _m004_secrets(conn: sqlite3.Connection) -> None:
    """v3 → v4: секреты воркспейсов живут в БД (таблица создаётся в SCHEMA).

    Сам перенос из keyring делает ``workspaces.migrate_legacy_config`` — он требует
    конфига, а миграции схемы намеренно ничего о нём не знают.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS workspace_secrets ("
        " workspace_id INTEGER NOT NULL, slot TEXT NOT NULL, value TEXT NOT NULL,"
        " updated_at TEXT NOT NULL, PRIMARY KEY (workspace_id, slot))"
    )


def _m005_path_tags(conn: sqlite3.Connection) -> None:
    """v4 → v5: теги папок воркспейса («legacy-бэкенд», «новая-версия» и т.п.).

    Отдельной таблицей, а не списком в колонке: по тегу ищут, а поиск по подстроке
    в JSON — это то, за что потом стыдно.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS workspace_path_tags ("
        " path_id INTEGER NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (path_id, tag))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_path_tags_tag ON workspace_path_tags(tag)")


_MIGRATIONS: list[tuple[int, object]] = [
    (2, _m002_workspaces),
    (3, _m003_features),
    (4, _m004_secrets),
    (5, _m005_path_tags),
]


class Store:
    def __init__(self, path: str | Path, workspace_id: int | None = None) -> None:
        self.path = str(path)
        self.migration_notes: list[str] = []
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._restrict_permissions()
        self._migrate()
        self.conn.commit()
        self.workspace_id = workspace_id or self._default_workspace_id()

    def _restrict_permissions(self) -> None:
        """0600 на файлы БД: в ней лежат секреты воркспейсов в открытом виде.

        Не критично, если не вышло (Windows, сетевая ФС, чужой владелец) — молча пропускаем.
        """
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.path}{suffix}")
            try:
                if path.exists():
                    path.chmod(0o600)
            except OSError:
                pass

    def use_workspace(self, workspace_id: int) -> None:
        """Переключить скоуп уже открытого соединения (смена воркспейса в TUI)."""
        self.workspace_id = workspace_id

    # --- миграции ------------------------------------------------------- #

    def _migrate(self) -> None:
        """Довести схему до ``SCHEMA_VERSION``, сняв копию БД перед структурной миграцией."""
        self._legacy_column_migrations()
        current = int(self.get_meta("schema_version") or 0) or 1
        if current > SCHEMA_VERSION:
            print(
                f"⚠ БД версии схемы {current} новее, чем понимает эта версия jwu "
                f"({SCHEMA_VERSION}) — обнови jwu, иначе возможны странности.",
                file=sys.stderr,
            )
            return
        pending = [(v, step) for v, step in _MIGRATIONS if v > current]
        if not pending:
            return
        self._backup_before_migration(pending[-1][0])
        for version, step in pending:
            step(self.conn)  # type: ignore[operator]
            self.conn.commit()
            self.set_meta("schema_version", str(version))

    def _legacy_column_migrations(self) -> None:
        """Доезд старых БД (до появления версионирования): недостающие колонки."""
        for table in ("issue_snapshots", "pr_snapshots"):
            if "views" not in _columns(self.conn, table):
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN views TEXT NOT NULL DEFAULT '[]'"
                )
        if "signature" not in _columns(self.conn, "pr_snapshots"):
            self.conn.execute(
                "ALTER TABLE pr_snapshots ADD COLUMN signature TEXT NOT NULL DEFAULT '{}'"
            )
        if "section" not in _columns(self.conn, "pending_changes"):
            self.conn.execute(
                "ALTER TABLE pending_changes ADD COLUMN section TEXT NOT NULL DEFAULT ''"
            )

    def _has_any_data(self) -> bool:
        for table in ("sync_runs", "jobs", "notes", "analyses"):
            row = self.conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            if row:
                return True
        return False

    def _backup_before_migration(self, to_version: int) -> None:
        """Копия БД перед структурной миграцией — на случай, если что-то пойдёт не так.

        Пустую (только что созданную) базу не бэкапим. Ошибку бэкапа не считаем фатальной,
        но громко сообщаем: миграция всё равно пойдёт, а пользователь должен знать.
        """
        if not self._has_any_data():
            return
        from . import maintenance  # локально: maintenance тянет config, а тот — не Store

        try:
            dest = maintenance.backup_before_migration(Path(self.path), to_version=to_version)
        except Exception as exc:  # noqa: BLE001 — бэкап не должен блокировать работу
            note = f"⚠ не удалось сделать бэкап БД перед миграцией схемы: {exc}"
        else:
            note = f"бэкап БД перед миграцией схемы: {dest.name}" if dest else ""
        if note:
            self.migration_notes.append(note)
            print(note, file=sys.stderr)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- воркспейсы ----------------------------------------------------- #

    def _default_workspace_id(self) -> int:
        """Воркспейс по умолчанию для Store без явного скоупа: «Работа» либо первый."""
        row = self.conn.execute(
            "SELECT id FROM workspaces WHERE slug = ?", (DEFAULT_WORKSPACE_SLUG,)
        ).fetchone()
        if row:
            return int(row["id"])
        row = self.conn.execute("SELECT id FROM workspaces ORDER BY id LIMIT 1").fetchone()
        if row:
            return int(row["id"])
        wid = _insert_workspace(
            self.conn, DEFAULT_WORKSPACE_SLUG, name=DEFAULT_WORKSPACE_NAME,
            jira=True, bitbucket=True,
        )
        self.conn.commit()
        return wid

    @staticmethod
    def _workspace_from_row(row) -> Workspace:
        return Workspace(
            id=row["id"], slug=row["slug"], name=row["name"],
            jira_enabled=bool(row["jira_enabled"]),
            bitbucket_enabled=bool(row["bitbucket_enabled"]),
            archived=bool(row["archived"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def _fill_paths(self, ws: Workspace) -> Workspace:
        ws.paths = self.workspace_paths(ws.id)
        return ws

    def create_workspace(
        self, slug: str, *, name: str = "", jira_enabled: bool = False,
        bitbucket_enabled: bool = False,
    ) -> Workspace:
        wid = _insert_workspace(
            self.conn, slug, name=name, jira=jira_enabled, bitbucket=bitbucket_enabled
        )
        self.conn.commit()
        ws = self.get_workspace(wid)
        assert ws is not None
        return ws

    def get_workspace(self, workspace_id: int) -> Workspace | None:
        row = self.conn.execute(
            "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()
        return self._fill_paths(self._workspace_from_row(row)) if row else None

    def get_workspace_by_slug(self, slug: str) -> Workspace | None:
        row = self.conn.execute("SELECT * FROM workspaces WHERE slug = ?", (slug,)).fetchone()
        return self._fill_paths(self._workspace_from_row(row)) if row else None

    def list_workspaces(self, *, include_archived: bool = False) -> list[Workspace]:
        sql = "SELECT * FROM workspaces"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY id"
        return [
            self._fill_paths(self._workspace_from_row(r)) for r in self.conn.execute(sql)
        ]

    def update_workspace(self, workspace_id: int, **fields) -> None:
        """Точечно обновить поля воркспейса (name/slug/jira_enabled/bitbucket_enabled/archived)."""
        allowed = {"slug", "name", "jira_enabled", "bitbucket_enabled", "archived"}
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"Неизвестное поле воркспейса: {key}")
            sets.append(f"{key} = ?")
            params.append(int(value) if isinstance(value, bool) else value)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([_now(), workspace_id])
        self.conn.execute(f"UPDATE workspaces SET {', '.join(sets)} WHERE id = ?", params)
        self.conn.commit()

    def delete_workspace(self, workspace_id: int, *, keep_data: bool = False) -> None:
        """Удалить воркспейс; без keep_data — вместе со всеми его локальными данными."""
        if not keep_data:
            job_ids = [
                r["id"] for r in self.conn.execute(
                    "SELECT id FROM jobs WHERE workspace_id = ?", (workspace_id,)
                )
            ]
            for jid in job_ids:
                self.conn.execute("DELETE FROM job_records WHERE job_id = ?", (jid,))
                self.conn.execute("DELETE FROM job_prs WHERE job_id = ?", (jid,))
            for table in WORKSPACE_SCOPED_TABLES:
                self.conn.execute(f"DELETE FROM {table} WHERE workspace_id = ?", (workspace_id,))
        self.conn.execute(
            "DELETE FROM workspace_path_tags WHERE path_id IN"
            " (SELECT id FROM workspace_paths WHERE workspace_id = ?)", (workspace_id,))
        self.conn.execute("DELETE FROM workspace_paths WHERE workspace_id = ?", (workspace_id,))
        self.conn.execute("DELETE FROM workspace_settings WHERE workspace_id = ?", (workspace_id,))
        self.conn.execute("DELETE FROM workspace_secrets WHERE workspace_id = ?", (workspace_id,))
        self.conn.execute(
            "DELETE FROM meta WHERE key LIKE ?", (f"w{workspace_id}:%",)
        )
        self.conn.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
        self.conn.commit()

    # --- папки воркспейса ------------------------------------------------ #

    def add_workspace_path(self, workspace_id: int, path: str, label: str = "",
                           tags: list[str] | None = None) -> WorkspacePath:
        ts = _now()
        cur = self.conn.execute(
            "INSERT INTO workspace_paths (workspace_id, path, label, added_at) VALUES (?, ?, ?, ?)",
            (workspace_id, path, label, ts),
        )
        path_id = int(cur.lastrowid)
        self.conn.commit()
        applied = self.add_path_tags(path_id, tags or [])
        return WorkspacePath(id=path_id, workspace_id=workspace_id, path=path,
                             label=label, added_at=ts, tags=applied)

    def _path_tags(self, path_ids: list[int]) -> dict[int, list[str]]:
        """Теги пачкой: одна выборка на все папки, чтобы не дёргать БД в цикле."""
        if not path_ids:
            return {}
        marks = ",".join("?" * len(path_ids))
        rows = self.conn.execute(
            f"SELECT path_id, tag FROM workspace_path_tags WHERE path_id IN ({marks})"
            " ORDER BY tag",
            path_ids,
        ).fetchall()
        out: dict[int, list[str]] = {}
        for row in rows:
            out.setdefault(row["path_id"], []).append(row["tag"])
        return out

    def set_path_tags(self, path_id: int, tags: list[str]) -> list[str]:
        """Заменить набор тегов папки целиком."""
        self.conn.execute("DELETE FROM workspace_path_tags WHERE path_id = ?", (path_id,))
        clean = sorted({t for t in (t.strip() for t in tags) if t})
        self.conn.executemany(
            "INSERT OR IGNORE INTO workspace_path_tags (path_id, tag) VALUES (?, ?)",
            [(path_id, t) for t in clean],
        )
        self.conn.commit()
        return clean

    def add_path_tags(self, path_id: int, tags: list[str]) -> list[str]:
        clean = {t for t in (t.strip() for t in tags) if t}
        self.conn.executemany(
            "INSERT OR IGNORE INTO workspace_path_tags (path_id, tag) VALUES (?, ?)",
            [(path_id, t) for t in sorted(clean)],
        )
        self.conn.commit()
        return self._path_tags([path_id]).get(path_id, [])

    def remove_path_tags(self, path_id: int, tags: list[str]) -> list[str]:
        clean = {t for t in (t.strip() for t in tags) if t}
        self.conn.executemany(
            "DELETE FROM workspace_path_tags WHERE path_id = ? AND tag = ?",
            [(path_id, t) for t in sorted(clean)],
        )
        self.conn.commit()
        return self._path_tags([path_id]).get(path_id, [])

    def all_tags(self, workspace_id: int) -> dict[str, int]:
        """Теги воркспейса со счётчиком папок — чтобы было видно, что вообще заведено."""
        rows = self.conn.execute(
            "SELECT t.tag AS tag, COUNT(*) AS n FROM workspace_path_tags t"
            " JOIN workspace_paths p ON p.id = t.path_id"
            " WHERE p.workspace_id = ? GROUP BY t.tag ORDER BY t.tag",
            (workspace_id,),
        ).fetchall()
        return {r["tag"]: r["n"] for r in rows}

    def remove_workspace_path(self, path: str, workspace_id: int | None = None) -> bool:
        find, params = "SELECT id FROM workspace_paths WHERE path = ?", [path]
        if workspace_id is not None:
            find += " AND workspace_id = ?"
            params.append(workspace_id)
        row = self.conn.execute(find, params).fetchone()
        if row is None:
            return False
        self.conn.execute("DELETE FROM workspace_path_tags WHERE path_id = ?", (row["id"],))
        self.conn.execute("DELETE FROM workspace_paths WHERE id = ?", (row["id"],))
        self.conn.commit()
        return True

    def workspace_paths(self, workspace_id: int, *, tag: str | None = None) -> list[WorkspacePath]:
        """Папки воркспейса вместе с тегами; с ``tag`` — только помеченные им."""
        sql = ("SELECT p.id, p.workspace_id, p.path, p.label, p.added_at FROM workspace_paths p")
        params: list = []
        if tag:
            sql += " JOIN workspace_path_tags t ON t.path_id = p.id AND t.tag = ?"
            params.append(tag.strip())
        sql += " WHERE p.workspace_id = ? ORDER BY p.path"
        params.append(workspace_id)
        rows = self.conn.execute(sql, params).fetchall()
        tags = self._path_tags([r["id"] for r in rows])
        return [WorkspacePath(id=r["id"], workspace_id=r["workspace_id"], path=r["path"],
                              label=r["label"], added_at=r["added_at"],
                              tags=tags.get(r["id"], [])) for r in rows]

    def all_workspace_paths(self) -> list[WorkspacePath]:
        """Все зарегистрированные папки (для резолва воркспейса по текущей директории)."""
        rows = self.conn.execute(
            "SELECT id, workspace_id, path, label, added_at FROM workspace_paths"
        ).fetchall()
        tags = self._path_tags([r["id"] for r in rows])
        return [WorkspacePath(id=r["id"], workspace_id=r["workspace_id"], path=r["path"],
                              label=r["label"], added_at=r["added_at"],
                              tags=tags.get(r["id"], [])) for r in rows]

    # --- настройки воркспейса (плоский KV: 'jira.base_url' → значение) ---- #

    def workspace_settings(self, workspace_id: int) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT key, value FROM workspace_settings WHERE workspace_id = ?", (workspace_id,)
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_workspace_settings(self, workspace_id: int, values: dict[str, str]) -> None:
        self.conn.executemany(
            "INSERT INTO workspace_settings (workspace_id, key, value) VALUES (?, ?, ?)"
            " ON CONFLICT(workspace_id, key) DO UPDATE SET value = excluded.value",
            [(workspace_id, k, v) for k, v in values.items()],
        )
        self.conn.commit()

    # --- секреты воркспейса (плайнтекст в БД; см. chmod 600 в __init__) --- #

    def get_workspace_secret(self, workspace_id: int, slot: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM workspace_secrets WHERE workspace_id = ? AND slot = ?",
            (workspace_id, slot),
        ).fetchone()
        return (row["value"] or None) if row else None

    def set_workspace_secret(self, workspace_id: int, slot: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO workspace_secrets (workspace_id, slot, value, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(workspace_id, slot) DO UPDATE SET"
            " value = excluded.value, updated_at = excluded.updated_at",
            (workspace_id, slot, value, _now()),
        )
        self.conn.commit()

    def delete_workspace_secret(self, workspace_id: int, slot: str) -> None:
        self.conn.execute(
            "DELETE FROM workspace_secrets WHERE workspace_id = ? AND slot = ?",
            (workspace_id, slot),
        )
        self.conn.commit()

    def workspace_secrets(self, workspace_id: int) -> dict[str, str]:
        """Все секреты воркспейса. Вызывающий обязан не печатать их без спроса."""
        rows = self.conn.execute(
            "SELECT slot, value FROM workspace_secrets WHERE workspace_id = ?", (workspace_id,)
        ).fetchall()
        return {r["slot"]: r["value"] for r in rows}

    # --- запись синка --------------------------------------------------- #

    def start_sync_run(self, views: list[str]) -> int:
        cur = self.conn.execute(
            "INSERT INTO sync_runs (started_at, views, workspace_id) VALUES (?, ?, ?)",
            (_now(), json.dumps(views), self.workspace_id),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_sync_run(self, run_id: int, counts: dict) -> None:
        self.conn.execute(
            "UPDATE sync_runs SET counts = ? WHERE id = ? AND workspace_id = ?",
            (json.dumps(counts), run_id, self.workspace_id),
        )
        self.conn.commit()

    def save_issue_snapshot(
        self, run_id: int, issue: Issue, views: list[str] | None = None
    ) -> None:
        self.conn.execute(
            "INSERT INTO issue_snapshots"
            " (sync_run_id, key, signature, fields, views, fetched_at, workspace_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                issue.key,
                json.dumps(_issue_signature(issue)),
                issue.model_dump_json(),
                json.dumps(sorted(views or [])),
                _now(),
                self.workspace_id,
            ),
        )
        self.conn.commit()

    def save_pr_snapshot(self, run_id: int, pr: PR, views: list[str] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO pr_snapshots (sync_run_id, pr_id, project, repo, conflicted, fields,"
            " signature, views, fetched_at, workspace_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                self.workspace_id,
            ),
        )
        self.conn.commit()

    # --- чтение --------------------------------------------------------- #

    def latest_run_id(self) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(id) AS m FROM sync_runs WHERE workspace_id = ?", (self.workspace_id,)
        ).fetchone()
        return row["m"] if row and row["m"] is not None else None

    def last_sync_at(self, token: str | None = None) -> str | None:
        """Время последнего синка; с token — последнего синка секции (значение в views)."""
        if token is None:
            row = self.conn.execute(
                "SELECT started_at FROM sync_runs WHERE workspace_id = ? ORDER BY id DESC LIMIT 1",
                (self.workspace_id,),
            ).fetchone()
            return row["started_at"] if row else None
        for row in self.conn.execute(
            "SELECT started_at, views FROM sync_runs WHERE workspace_id = ? ORDER BY id DESC",
            (self.workspace_id,),
        ):
            if token in json.loads(row["views"]):
                return row["started_at"]
        return None

    def _membership_run(
        self, token: str, count_key: str, before: int | None = None
    ) -> int | None:
        """Последний прогон, надёжно синкавший вкладку ``token``.

        Надёжный = ``token`` есть в ``views`` прогона И ``count_key`` есть в его
        ``counts`` (фетч не упал — при ошибке ключ в counts не пишется, см.
        ``_sync_tasks``/``_sync_prs``). Это отделяет «вкладка реально опустела»
        от «синк вкладки упал»: на сбое мы откатываемся к прошлому надёжному
        прогону и не затираем таб.

        Фолбэк: если ни одного прогона с заполненным counts нет (напр. в юнит-тестах
        без ``finish_sync_run``), берём последний прогон, где ``token`` есть в views.
        ``before`` — рассматривать только прогоны строго раньше указанного id.
        """
        fallback: int | None = None
        for row in self.conn.execute(
            "SELECT id, views, counts FROM sync_runs WHERE workspace_id = ? ORDER BY id DESC",
            (self.workspace_id,),
        ):
            if before is not None and row["id"] >= before:
                continue
            if token not in json.loads(row["views"]):
                continue
            if fallback is None:
                fallback = row["id"]
            if count_key in json.loads(row["counts"] or "{}"):
                return row["id"]
        return fallback

    def _issue_members_in_run(self, run_id: int, view: str) -> set[str]:
        """Ключи задач, попавшие во вкладку ``view`` в конкретном прогоне."""
        return {
            r["key"]
            for r in self.conn.execute(
                "SELECT key, views FROM issue_snapshots WHERE sync_run_id = ? AND workspace_id = ?",
                (run_id, self.workspace_id),
            )
            if view in json.loads(r["views"])
        }

    def _pr_members_in_run(self, run_id: int, view: str) -> set[int]:
        """id PR, попавшие во вкладку ``view`` в конкретном прогоне."""
        return {
            r["pr_id"]
            for r in self.conn.execute(
                "SELECT pr_id, views FROM pr_snapshots WHERE sync_run_id = ? AND workspace_id = ?",
                (run_id, self.workspace_id),
            )
            if view in json.loads(r["views"])
        }

    def latest_issues(self, view: str | None = None) -> list[Issue]:
        """Свежайший снапшот по каждой задаче (опц. фильтр по вью), updated DESC.

        Поля берём из последнего снапшота *по ключу* (свежайшие данные), но для
        вкладок mine/mentions состав фильтруем по членству в последнем надёжном
        синке этой вкладки — иначе закрытые/переназначенные задачи, переставшие
        приходить из Jira, висели бы в списке вечно (их снапшот не обновляется).
        """
        live: set[str] | None = None
        if view in ("mine", "mentions"):
            run_id = self._membership_run(view, f"tasks:{view}")
            if run_id is not None:
                live = self._issue_members_in_run(run_id, view)
        rows = self.conn.execute(
            "SELECT key, fields, views, MAX(sync_run_id) FROM issue_snapshots"
            " WHERE workspace_id = ? GROUP BY key",
            (self.workspace_id,),
        ).fetchall()
        issues: list[Issue] = []
        for row in rows:
            if live is not None:
                if row["key"] not in live:
                    continue
            elif view is not None and view not in json.loads(row["views"]):
                continue
            issues.append(Issue.model_validate_json(row["fields"]))
        issues.sort(key=lambda i: i.updated, reverse=True)
        return issues

    def latest_prs(self, view: str | None = None) -> list[PR]:
        """Свежайший снапшот по каждому PR (опц. фильтр по вью: mine|review).

        Состав mine/review фильтруется по членству в последнем надёжном синке
        вкладки — смерженные/отклонённые PR, переставшие приходить из Bitbucket,
        пропадают из списка (а не висят по устаревшему снапшоту).
        """
        live: set[int] | None = None
        if view in ("mine", "review"):
            run_id = self._membership_run(f"prs:{view}", f"prs:{view}")
            if run_id is not None:
                live = self._pr_members_in_run(run_id, view)
        rows = self.conn.execute(
            "SELECT pr_id, fields, views, MAX(sync_run_id) FROM pr_snapshots"
            " WHERE workspace_id = ? GROUP BY pr_id",
            (self.workspace_id,),
        ).fetchall()
        prs: list[PR] = []
        for row in rows:
            if live is not None:
                if row["pr_id"] not in live:
                    continue
            elif view is not None and view not in json.loads(row["views"]):
                continue
            prs.append(PR.model_validate_json(row["fields"]))
        prs.sort(key=lambda p: p.updated, reverse=True)
        return prs

    def snapshotted_issue_keys(self, run_id: int) -> set[str]:
        """Ключи задач, уже снапшотнутые в этом прогоне (чтобы не плодить дубли)."""
        rows = self.conn.execute(
            "SELECT DISTINCT key FROM issue_snapshots WHERE sync_run_id = ? AND workspace_id = ?",
            (run_id, self.workspace_id),
        ).fetchall()
        return {r["key"] for r in rows}

    def _prev_issue_signature(self, key: str, before_run: int) -> dict | None:
        row = self.conn.execute(
            "SELECT signature FROM issue_snapshots WHERE key = ? AND sync_run_id < ?"
            " AND workspace_id = ? ORDER BY sync_run_id DESC LIMIT 1",
            (key, before_run, self.workspace_id),
        ).fetchone()
        return json.loads(row["signature"]) if row else None

    def _prev_reliable_pr_ids(self, key: str, before_run: int) -> list | None:
        """pr_ids из последнего снапшота с достоверной dev-панелью (dev_ok != False).

        Пустой pr-список из-за сбоя dev-status (dev_ok=False) как базу сравнения не
        берём — иначе вернувшиеся на следующем синке PR выглядят «новыми». Старые
        строки без поля dev_ok считаем достоверными (COALESCE → 1)."""
        row = self.conn.execute(
            "SELECT signature FROM issue_snapshots WHERE key = ? AND sync_run_id < ?"
            " AND workspace_id = ?"
            " AND COALESCE(json_extract(signature, '$.dev_ok'), 1) = 1"
            " ORDER BY sync_run_id DESC LIMIT 1",
            (key, before_run, self.workspace_id),
        ).fetchone()
        if not row:
            return None
        return json.loads(row["signature"]).get("pr_ids", [])

    def _prev_pr_signature(self, pr_id: int, before_run: int) -> dict | None:
        row = self.conn.execute(
            "SELECT signature FROM pr_snapshots WHERE pr_id = ? AND sync_run_id < ?"
            " AND workspace_id = ? ORDER BY sync_run_id DESC LIMIT 1",
            (pr_id, before_run, self.workspace_id),
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
            "SELECT key, signature, fields FROM issue_snapshots"
            " WHERE sync_run_id = ? AND workspace_id = ?",
            (run_id, self.workspace_id),
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
            # new_pr — только если dev-панель текущего снапшота достоверна, и
            # сравниваем с последним ДОСТОВЕРНЫМ снапшотом (сбойные пустые пропускаем),
            # иначе вернувшиеся после сбоя dev-status PR выглядели бы «новыми».
            if cur.get("dev_ok", True):
                base_pr_ids = self._prev_reliable_pr_ids(key, run_id)
                if base_pr_ids is not None:
                    new_prs = set(cur.get("pr_ids", [])) - set(base_pr_ids)
                    if new_prs:
                        deltas.append(Delta(
                            key=key, kind="new_pr", summary=summary,
                            detail=", ".join(map(str, sorted(new_prs))),
                        ))

        # PR: новые комменты/коммиты, апрувы, конфликт
        pr_rows = self.conn.execute(
            "SELECT pr_id, project, repo, signature, fields FROM pr_snapshots"
            " WHERE sync_run_id = ? AND workspace_id = ?",
            (run_id, self.workspace_id),
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

        deltas.extend(self._gone_deltas(run_id))
        return deltas

    def _last_issue_summary(self, key: str) -> str:
        row = self.conn.execute(
            "SELECT fields FROM issue_snapshots WHERE key = ? AND workspace_id = ?"
            " ORDER BY sync_run_id DESC LIMIT 1",
            (key, self.workspace_id),
        ).fetchone()
        return json.loads(row["fields"]).get("summary", "") if row else ""

    def _last_pr_ref(self, pr_id: int) -> tuple[str, str]:
        row = self.conn.execute(
            "SELECT project, repo, fields FROM pr_snapshots WHERE pr_id = ? AND workspace_id = ?"
            " ORDER BY sync_run_id DESC LIMIT 1",
            (pr_id, self.workspace_id),
        ).fetchone()
        if not row:
            return "", f"#{pr_id}"
        title = json.loads(row["fields"]).get("title", "")
        return title, f"{row['project']}/{row['repo']}#{pr_id}"

    def _gone_deltas(self, run_id: int) -> list[Delta]:
        """Дельты об исчезновении: задача ушла из выборки / PR смержен-отклонён.

        Сравниваем состав вкладки в текущем прогоне с прошлым надёжным прогоном
        той же вкладки. Шумим только по вкладкам, которые в этом синке надёжно
        обновились (есть в counts) — иначе сбой фетча выглядел бы как «всё ушло».
        Если сущность всё ещё видна в другой вкладке — не считаем её исчезнувшей.
        """
        run = self.conn.execute(
            "SELECT views, counts FROM sync_runs WHERE id = ? AND workspace_id = ?",
            (run_id, self.workspace_id),
        ).fetchone()
        if run is None:
            return []
        cur_views = set(json.loads(run["views"]))
        cur_counts = json.loads(run["counts"] or "{}")
        out: list[Delta] = []

        live_issues: set[str] = set()
        for v in ("mine", "mentions"):
            r = self._membership_run(v, f"tasks:{v}")
            if r is not None:
                live_issues |= self._issue_members_in_run(r, v)
        for v in ("mine", "mentions"):
            if v not in cur_views or f"tasks:{v}" not in cur_counts:
                continue
            prev_run = self._membership_run(v, f"tasks:{v}", before=run_id)
            if prev_run is None:
                continue
            gone = self._issue_members_in_run(prev_run, v) - self._issue_members_in_run(run_id, v)
            for key in sorted(gone - live_issues):
                out.append(Delta(
                    key=key, kind="gone", summary=self._last_issue_summary(key), section=v,
                    detail="ушла из выборки (закрыта / сменила статус / переназначена)",
                ))

        live_prs: set[int] = set()
        for v in ("mine", "review"):
            r = self._membership_run(f"prs:{v}", f"prs:{v}")
            if r is not None:
                live_prs |= self._pr_members_in_run(r, v)
        for v, section in (("mine", "prs_mine"), ("review", "prs_review")):
            token = f"prs:{v}"
            if token not in cur_views or token not in cur_counts:
                continue
            prev_run = self._membership_run(token, token, before=run_id)
            if prev_run is None:
                continue
            gone = self._pr_members_in_run(prev_run, v) - self._pr_members_in_run(run_id, v)
            for pid in sorted(gone - live_prs):
                title, pr_key = self._last_pr_ref(pid)
                out.append(Delta(
                    key=pr_key, kind="pr_gone", summary=title, section=section,
                    detail="пропал из списка (вероятно смержен / отклонён)",
                ))
        return out

    # --- накопленные изменения (копятся, пока не закрыты явно) ----------- #

    def add_pending_changes(self, run_id: int, deltas: list[Delta]) -> None:
        """Дописать дельты синка в накопитель (показываются, пока не очистят)."""
        ts = _now()
        self.conn.executemany(
            "INSERT INTO pending_changes"
            " (run_id, key, kind, summary, detail, section, ts, workspace_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(run_id, d.key, d.kind, d.summary, d.detail, d.section, ts, self.workspace_id)
             for d in deltas],
        )
        self.conn.commit()

    def pending_changes(self) -> list[Delta]:
        rows = self.conn.execute(
            "SELECT key, kind, summary, detail, section FROM pending_changes"
            " WHERE workspace_id = ? ORDER BY id",
            (self.workspace_id,),
        ).fetchall()
        return [
            Delta(key=r["key"], kind=r["kind"], summary=r["summary"], detail=r["detail"],
                  section=r["section"])
            for r in rows
        ]

    def clear_pending_changes(self, pairs: list[tuple[str, str]] | None = None) -> None:
        """Очистить накопленные изменения: все (pairs=None) или только по (key, kind)."""
        if pairs is None:
            self.conn.execute(
                "DELETE FROM pending_changes WHERE workspace_id = ?", (self.workspace_id,)
            )
        elif pairs:
            self.conn.executemany(
                "DELETE FROM pending_changes WHERE key = ? AND kind = ? AND workspace_id = ?",
                [(key, kind, self.workspace_id) for key, kind in pairs],
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

    def workspace_meta_key(self, key: str) -> str:
        """Ключ meta в пространстве текущего воркспейса (``identity`` → ``w3:identity``)."""
        return f"w{self.workspace_id}:{key}"

    def get_workspace_meta(self, key: str) -> str | None:
        return self.get_meta(self.workspace_meta_key(key))

    def set_workspace_meta(self, key: str, value: str) -> None:
        self.set_meta(self.workspace_meta_key(key), value)

    # --- заметки -------------------------------------------------------- #

    def add_note(self, key: str, text: str, author: str = "claude") -> Note:
        ts = _now()
        self.conn.execute(
            "INSERT INTO notes (key, author, text, ts, workspace_id) VALUES (?, ?, ?, ?, ?)",
            (key, author, text, ts, self.workspace_id),
        )
        self.conn.commit()
        return Note(key=key, author=author, text=text, ts=ts)

    def get_notes(self, key: str) -> list[Note]:
        rows = self.conn.execute(
            "SELECT key, author, text, ts FROM notes WHERE key = ? AND workspace_id = ?"
            " ORDER BY ts",
            (key, self.workspace_id),
        ).fetchall()
        return [
            Note(key=r["key"], author=r["author"], text=r["text"], ts=r["ts"])
            for r in rows
        ]

    # --- анализы (дневные планы) ---------------------------------------- #

    def save_analysis(self, content: str, title: str = "") -> Analysis:
        ts = _now()
        cur = self.conn.execute(
            "INSERT INTO analyses (created_at, title, content, workspace_id) VALUES (?, ?, ?, ?)",
            (ts, title, content, self.workspace_id),
        )
        self.conn.commit()
        return Analysis(id=int(cur.lastrowid), created_at=ts, title=title, content=content)

    def list_analyses(self, limit: int = 50) -> list[Analysis]:
        """Список без полного content (только метаданные), новые сверху."""
        rows = self.conn.execute(
            "SELECT id, created_at, title FROM analyses WHERE workspace_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (self.workspace_id, limit),
        ).fetchall()
        return [
            Analysis(id=r["id"], created_at=r["created_at"], title=r["title"])
            for r in rows
        ]

    def get_analysis(self, analysis_id: int | None = None) -> Analysis | None:
        """Анализ по id; без id — последний."""
        if analysis_id is None:
            row = self.conn.execute(
                "SELECT id, created_at, title, content FROM analyses WHERE workspace_id = ?"
                " ORDER BY id DESC LIMIT 1",
                (self.workspace_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT id, created_at, title, content FROM analyses"
                " WHERE id = ? AND workspace_id = ?",
                (analysis_id, self.workspace_id),
            ).fetchone()
        if not row:
            return None
        return Analysis(id=row["id"], created_at=row["created_at"],
                        title=row["title"], content=row["content"])

    # --- локальные фичи (мини-трекер воркспейса) ------------------------- #

    def feature_prefix(self) -> str:
        """Префикс ключей фич: настройка воркспейса либо производная от его slug.

        Формат обязан подходить под ту же регулярку, что и ключи Jira
        (``[A-Z][A-Z0-9]+-\\d+``) — иначе перестанет работать вытаскивание ключа из
        имени ветки в скиллах.
        """
        settings = self.workspace_settings(self.workspace_id)
        explicit = (settings.get("features.prefix") or "").strip().upper()
        if _FEATURE_PREFIX_RE.match(explicit):
            return explicit
        row = self.conn.execute(
            "SELECT slug FROM workspaces WHERE id = ?", (self.workspace_id,)
        ).fetchone()
        cleaned = re.sub(r"[^A-Z0-9]", "", (row["slug"] if row else "").upper())[:8]
        if _FEATURE_PREFIX_RE.match(cleaned):
            return cleaned
        cleaned = f"F{cleaned}"[:8]
        return cleaned if _FEATURE_PREFIX_RE.match(cleaned) else "FEAT"

    def _next_feature_key(self) -> str:
        """Следующий ключ фичи. Счётчик монотонный: номера удалённых фич не переиспользуются.

        Иначе ветка ``HOME-2`` от удалённой фичи начала бы указывать на другую фичу.
        Счётчик хранится в настройках воркспейса, но подстраховывается максимумом по
        существующим ключам — на случай, если настройку потеряли.
        """
        prefix = self.feature_prefix()
        settings = self.workspace_settings(self.workspace_id)
        seq = int(settings.get("features.seq") or 0)
        for row in self.conn.execute(
            "SELECT key FROM local_features WHERE workspace_id = ?", (self.workspace_id,)
        ):
            head, _, tail = (row["key"] or "").rpartition("-")
            if head == prefix and tail.isdigit():
                seq = max(seq, int(tail))
        seq += 1
        self.set_workspace_settings(self.workspace_id, {"features.seq": str(seq)})
        return f"{prefix}-{seq}"

    @staticmethod
    def _feature_from_row(row) -> LocalFeature:
        return LocalFeature(
            id=row["id"], workspace_id=row["workspace_id"], key=row["key"], title=row["title"],
            status=row["status"], priority=row["priority"], description=row["description"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def create_feature(self, title: str, *, description: str = "",
                       priority: str = "") -> LocalFeature:
        ts = _now()
        key = self._next_feature_key()
        cur = self.conn.execute(
            "INSERT INTO local_features"
            " (workspace_id, key, title, status, priority, description, created_at, updated_at)"
            " VALUES (?, ?, ?, 'open', ?, ?, ?, ?)",
            (self.workspace_id, key, title, priority, description, ts, ts),
        )
        self.conn.commit()
        return LocalFeature(id=int(cur.lastrowid), workspace_id=self.workspace_id, key=key,
                            title=title, status="open", priority=priority,
                            description=description, created_at=ts, updated_at=ts)

    def get_feature(self, ref: int | str) -> LocalFeature | None:
        """Фича по id либо по ключу (регистр ключа не важен)."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            row = self.conn.execute(
                "SELECT * FROM local_features WHERE id = ? AND workspace_id = ?",
                (int(ref), self.workspace_id),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM local_features WHERE UPPER(key) = ? AND workspace_id = ?",
                (str(ref).upper(), self.workspace_id),
            ).fetchone()
        return self._feature_from_row(row) if row else None

    def list_features(self, *, status: str | None = None) -> list[LocalFeature]:
        sql = "SELECT * FROM local_features WHERE workspace_id = ?"
        params: list = [self.workspace_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id DESC"
        return [self._feature_from_row(r) for r in self.conn.execute(sql, params)]

    def update_feature(self, feature_id: int, **fields) -> None:
        allowed = {"title", "status", "priority", "description"}
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"Неизвестное поле фичи: {key}")
            if value is None:
                continue
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([_now(), feature_id, self.workspace_id])
        self.conn.execute(
            f"UPDATE local_features SET {', '.join(sets)} WHERE id = ? AND workspace_id = ?",
            params,
        )
        self.conn.commit()

    def delete_feature(self, feature_id: int) -> None:
        """Удалить фичу; привязанные работы остаются, но теряют якорь."""
        self.conn.execute(
            "UPDATE jobs SET feature_id = NULL WHERE feature_id = ? AND workspace_id = ?",
            (feature_id, self.workspace_id),
        )
        self.conn.execute(
            "DELETE FROM local_features WHERE id = ? AND workspace_id = ?",
            (feature_id, self.workspace_id),
        )
        self.conn.commit()

    # --- работы (jobs) -------------------------------------------------- #

    def create_job(self, task_key: str, title: str = "",
                   feature_id: int | None = None) -> Job:
        ts = _now()
        cur = self.conn.execute(
            "INSERT INTO jobs (task_key, title, status, created_at, updated_at, workspace_id,"
            " feature_id) VALUES (?, ?, 'active', ?, ?, ?, ?)",
            (task_key, title, ts, ts, self.workspace_id, feature_id),
        )
        self.conn.commit()
        feature = self.get_feature(feature_id) if feature_id else None
        return Job(id=int(cur.lastrowid), task_key=task_key, title=title,
                   status="active", created_at=ts, updated_at=ts,
                   feature_id=feature_id, feature_key=feature.key if feature else "")

    def _touch_job(self, job_id: int) -> None:
        """Обновить updated_at. БЕЗ commit — вызывающий коммитит сам."""
        self.conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (_now(), job_id))

    def _require_own_job(self, job_id: int) -> None:
        """Не дать писать в работу чужого воркспейса (id работ сквозные по всей БД)."""
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE id = ? AND workspace_id = ?", (job_id, self.workspace_id)
        ).fetchone()
        if not row:
            slug = self.job_workspace_slug(job_id)
            hint = f" (она в воркспейсе «{slug}»)" if slug else ""
            raise ValueError(f"Работа #{job_id} не найдена в текущем воркспейсе{hint}.")

    def add_job_record(self, job_id: int, text: str, kind: str = "note",
                       status: str | None = None) -> JobRecord:
        self._require_own_job(job_id)
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
        self._require_own_job(job_id)
        self.conn.execute(
            "INSERT OR IGNORE INTO job_prs (job_id, pr_id, project, repo) VALUES (?, ?, ?, ?)",
            (job_id, pr_id, project, repo),
        )
        self._touch_job(job_id)
        self.conn.commit()

    def set_job_status(self, job_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
            (status, _now(), job_id, self.workspace_id),
        )
        self.conn.commit()

    def delete_job(self, job_id: int) -> None:
        """Удалить работу вместе с записями и связями с PR (в пределах воркспейса)."""
        row = self.conn.execute(
            "SELECT id FROM jobs WHERE id = ? AND workspace_id = ?", (job_id, self.workspace_id)
        ).fetchone()
        if not row:
            return
        self.conn.execute("DELETE FROM job_records WHERE job_id = ?", (job_id,))
        self.conn.execute("DELETE FROM job_prs WHERE job_id = ?", (job_id,))
        self.conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        self.conn.commit()

    def job_workspace_slug(self, job_id: int) -> str | None:
        """В каком воркспейсе живёт работа (для подсказки «работа #N в воркспейсе X»)."""
        row = self.conn.execute(
            "SELECT w.slug AS slug FROM jobs j JOIN workspaces w ON w.id = j.workspace_id"
            " WHERE j.id = ?",
            (job_id,),
        ).fetchone()
        return row["slug"] if row else None

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
        keys = row.keys()
        job = Job(id=row["id"], task_key=row["task_key"], title=row["title"],
                  status=row["status"], created_at=row["created_at"], updated_at=row["updated_at"],
                  feature_id=row["feature_id"] if "feature_id" in keys else None,
                  feature_key=(row["feature_key"] or "") if "feature_key" in keys else "")
        job.prs = self._job_prs(job.id)
        if with_records:
            job.records = self._job_records(job.id)
        return job

    _JOB_COLUMNS = (
        "j.id, j.task_key, j.title, j.status, j.created_at, j.updated_at, j.feature_id,"
        " f.key AS feature_key"
    )
    _JOB_FEATURE_JOIN = " LEFT JOIN local_features f ON f.id = j.feature_id"

    def get_job(self, job_id: int) -> Job | None:
        row = self.conn.execute(
            f"SELECT {self._JOB_COLUMNS} FROM jobs j{self._JOB_FEATURE_JOIN}"
            " WHERE j.id = ? AND j.workspace_id = ?",
            (job_id, self.workspace_id),
        ).fetchone()
        return self._job_from_row(row, with_records=True) if row else None

    def list_jobs(self, *, task_key: str | None = None, pr_id: int | None = None,
                  status: str | None = None, project: str | None = None,
                  repo: str | None = None, feature_id: int | None = None) -> list[Job]:
        sql = f"SELECT DISTINCT {self._JOB_COLUMNS} FROM jobs j{self._JOB_FEATURE_JOIN}"
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
        conds: list[str] = ["j.workspace_id = ?"]
        params.append(self.workspace_id)
        if task_key is not None:
            conds.append("j.task_key = ?"); params.append(task_key)
        if feature_id is not None:
            conds.append("j.feature_id = ?"); params.append(feature_id)
        if status is not None:
            conds.append("j.status = ?"); params.append(status)
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
