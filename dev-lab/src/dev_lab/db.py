"""SQLite store for durable runtime data — run history now, chat logs later.

A tiny migration runner keyed on ``PRAGMA user_version`` applies ordered
migrations on connect, so the schema can evolve safely across releases.

Migration rules:
- Append a new ``(version, sql)`` entry; versions are consecutive and ascending.
- NEVER edit, reorder, or renumber a migration that has shipped — write a new one.
- ``migrate()`` applies only entries newer than the DB's current ``user_version``.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

# Ordered, append-only schema migrations.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      TEXT,
            instruction TEXT NOT NULL,
            repo        TEXT,
            branch      TEXT,
            base_sha    TEXT,
            commit_sha  TEXT,
            status      TEXT NOT NULL,            -- 'done' | 'failed'
            committed   INTEGER NOT NULL DEFAULT 0,
            cost_usd    REAL,
            error       TEXT,
            created_at  REAL NOT NULL
        );
        """,
    ),
    (
        2,
        """
        CREATE TABLE projects (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL UNIQUE,
            path            TEXT NOT NULL,
            remote_url      TEXT,
            branch          TEXT,
            last_session_id TEXT,
            created_at      REAL NOT NULL
        );
        CREATE TABLE messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES projects(id),
            role        TEXT NOT NULL,              -- 'user' | 'assistant'
            content     TEXT NOT NULL,
            created_at  REAL NOT NULL
        );
        CREATE INDEX idx_messages_project ON messages(project_id, id);
        """,
    ),
]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply migrations newer than the DB's ``user_version``; return the new version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in sorted(MIGRATIONS):
        if version > current:
            conn.executescript(sql)
            # PRAGMA can't be parametrized; version is a trusted int from MIGRATIONS.
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
            current = version
    return current


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating parent dirs) and migrate a SQLite database."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    migrate(conn)
    return conn


def record_run(
    conn: sqlite3.Connection,
    *,
    job_id: str | None,
    instruction: str,
    repo: str | None,
    status: str,
    branch: str | None = None,
    base_sha: str | None = None,
    commit_sha: str | None = None,
    committed: bool = False,
    cost_usd: float | None = None,
    error: str | None = None,
) -> int:
    """Append one run record; return its row id."""
    cur = conn.execute(
        """
        INSERT INTO runs (job_id, instruction, repo, branch, base_sha, commit_sha,
                          status, committed, cost_usd, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            instruction,
            repo,
            branch,
            base_sha,
            commit_sha,
            status,
            int(committed),
            cost_usd,
            error,
            time.time(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


# --- Projects -------------------------------------------------------------

def create_project(
    conn: sqlite3.Connection, *, name: str, path: str, remote_url: str | None = None
) -> int:
    cur = conn.execute(
        "INSERT INTO projects (name, path, remote_url, created_at) VALUES (?, ?, ?, ?)",
        (name, path, remote_url, time.time()),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_project(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()


def get_project_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM projects ORDER BY name"))


def update_project(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    branch: str | None = None,
    last_session_id: str | None = None,
) -> None:
    sets, params = [], []
    if branch is not None:
        sets.append("branch = ?")
        params.append(branch)
    if last_session_id is not None:
        sets.append("last_session_id = ?")
        params.append(last_session_id)
    if not sets:
        return
    params.append(project_id)
    conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()


# --- Messages -------------------------------------------------------------

def record_message(conn: sqlite3.Connection, *, project_id: int, role: str, content: str) -> int:
    cur = conn.execute(
        "INSERT INTO messages (project_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (project_id, role, content, time.time()),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_messages(
    conn: sqlite3.Connection, project_id: int, *, limit: int = 500
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM messages WHERE project_id = ? ORDER BY id LIMIT ?",
            (project_id, limit),
        )
    )
