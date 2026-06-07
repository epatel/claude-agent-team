# sqlite-runtime-data

Decision: durable runtime data (run history, chat logs, …) lives in SQLite, behind a migration runner.

## Decision

Any persistent runtime data the lab accumulates — run/job history, and later the
chat-message log and similar records — is stored in a **SQLite database** with an
explicit **migration path**, not ad-hoc files or formats that can't evolve.

## Why

- One embedded, queryable store with no server — a good fit for the Pi.
- A migration runner lets the schema change across releases without losing data
  or hand-editing tables — the safety precaution requested up front.

## How

- `dev_lab/db.py`: `connect(path)` opens (WAL mode), runs migrations, returns the
  connection; `record_run(...)` appends a row; `migrate(conn)` applies pending
  migrations.
- Migrations are an **append-only** ordered `MIGRATIONS = [(version, sql), …]`
  list, applied when `version > PRAGMA user_version`, which then advances.
- **Never edit, reorder, or renumber a shipped migration** — add a new one.
- The supervisor records each job's outcome (status, branch, commit, cost,
  error) via `record_run`; the DB path is `dev-lab serve --db <path>`.

## Boundary

The job **queue** stays a filesystem work-state (`pending/running/done/failed`
with atomic renames) — that's the right tool for lock-free, crash-safe claiming.
SQLite is for durable **records/logs/history**, not the ephemeral work queue.

## Revisit if

- Concurrency or query needs outgrow SQLite (unlikely for a single-host lab) →
  consider a client/server database.
