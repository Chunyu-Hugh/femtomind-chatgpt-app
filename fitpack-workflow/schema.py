"""
schema.py — single source of truth for the FemtoMind / FitPack SQLite schema.

Both `fitpack-workflow/server.py` (MCP server process) and
`open-webui/backend/open_webui/routers/hpc.py` (FastAPI process) write to the
same `hpc_tasks.db` and used to keep two hand-copied DDL blocks in sync. They
now both import `apply_schema(conn)` from this module instead.

`apply_schema` is idempotent:
  - `CREATE TABLE IF NOT EXISTS` for the canonical shape.
  - `ALTER TABLE … ADD COLUMN` for columns added in later releases.

Either process may be the first to call it; the second call is a no-op.

Adding a new table or column? Touch this file only.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable


# Canonical CREATE TABLE statements — match the original shape used at the
# time each table was introduced. Idempotent column additions go in
# `_PER_TABLE_MIGRATIONS` below so older databases pick them up too.
_CREATE_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS chain_state (
        chat_id TEXT PRIMARY KEY,
        current_step INTEGER DEFAULT 0,
        active_batch_id TEXT DEFAULT '',
        active_observable TEXT DEFAULT '',
        history TEXT DEFAULT '[]',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        chat_id TEXT,
        batch_id TEXT,
        file_id TEXT,
        path TEXT UNIQUE,
        status TEXT DEFAULT 'created',
        is_running INTEGER DEFAULT 0,
        slurm_job_id TEXT,
        parameters_file TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        submitted_at TIMESTAMP,
        started_at TIMESTAMP,
        completed_at TIMESTAMP,
        updated_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_hpc_config (
        user_id TEXT PRIMARY KEY,
        hpc_username TEXT NOT NULL,
        remote_base TEXT DEFAULT '',
        configured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT,
        task_path TEXT UNIQUE,
        slurm_job_id TEXT,
        hpc_username TEXT DEFAULT '',
        observable TEXT DEFAULT '',
        n_datasets INTEGER DEFAULT 0,
        n_observables INTEGER DEFAULT 0,
        step_index INTEGER DEFAULT 0,
        elapsed_min REAL,
        total_iterations INTEGER,
        final_chi2 REAL,
        final_npts INTEGER,
        final_chi2_npts REAL,
        peak_memory_mb REAL,
        cpus INTEGER DEFAULT 4,
        proposed_cpus INTEGER,
        proposed_time_min INTEGER,
        proposed_mem_mb INTEGER,
        estimation_source TEXT DEFAULT '',
        node_name TEXT DEFAULT '',
        recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS saved_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        task_path TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        chi2 REAL NOT NULL,
        chi2_npts REAL,
        final_iteration INTEGER,
        parameters_snapshot TEXT NOT NULL,
        user_note TEXT,
        is_best INTEGER DEFAULT 0,
        tags TEXT,
        saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS batch_events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id    TEXT NOT NULL,
        batch_id   TEXT NOT NULL,
        reaction   TEXT,
        status     TEXT NOT NULL,
        payload    TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        processed  INTEGER DEFAULT 0,
        UNIQUE(batch_id, status)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fit_campaigns (
        campaign_id      TEXT PRIMARY KEY,
        campaign_name    TEXT,
        chat_id          TEXT,
        config           TEXT,
        current_step     INTEGER DEFAULT 0,
        total_steps      INTEGER,
        current_batch_id TEXT,
        status           TEXT DEFAULT 'running',
        auto_advance     INTEGER DEFAULT 0,
        chi2_ok          REAL DEFAULT 2.0,
        chi2_warn        REAL DEFAULT 3.0,
        good_fraction    REAL DEFAULT 0.3,
        created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
]


_CREATE_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_saved_batch_id ON saved_results(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_saved_chat_id ON saved_results(chat_id)",
    "CREATE INDEX IF NOT EXISTS idx_saved_is_best ON saved_results(is_best)",
    "CREATE INDEX IF NOT EXISTS idx_batch_events_chat_pending ON batch_events(chat_id, processed)",
    "CREATE INDEX IF NOT EXISTS idx_fit_campaigns_chat ON fit_campaigns(chat_id)",
]


# table → list of (column_name, DDL fragment for ADD COLUMN). Applied only if
# the column is missing. Order within a list matters for predictability.
_PER_TABLE_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "tasks": [
        ("status",          "TEXT DEFAULT 'created'"),
        ("submitted_at",    "TIMESTAMP"),
        ("started_at",      "TIMESTAMP"),
        ("completed_at",    "TIMESTAMP"),
        ("updated_at",      "TIMESTAMP"),
        ("step_index",      "INTEGER DEFAULT 0"),
        ("parent_batch_id", "TEXT DEFAULT ''"),
        ("hpc_username",    "TEXT DEFAULT ''"),
        # Canonical human-readable batch label authored by DatasetSelector at
        # submit time — `YYYY-MM-DD_HHmm_<job_number>`. Persisted here so the
        # frontend, the LLM, and the DB all share one display name (replaces
        # the legacy frontend-side `deriveBatchLabel` reconstruction).
        ("batch_label",     "TEXT DEFAULT ''"),
        # The fit's observable, recorded at generation. Without it every
        # job_history row landed with observable='' and the resource
        # estimator's history branch could never match a past run, so it fell
        # back to a static per-observable table forever.
        ("observable",      "TEXT DEFAULT ''"),
    ],
    "job_history": [
        # What the estimator ASKED for, kept beside what the job actually used.
        # Only the actuals were ever stored, so "did the proposal fit?" was not
        # a question the database could answer.
        ("proposed_cpus",     "INTEGER"),
        ("proposed_time_min", "INTEGER"),
        ("proposed_mem_mb",   "INTEGER"),
        ("estimation_source", "TEXT DEFAULT ''"),
    ],
    "chain_state": [
        ("staged_config",   "TEXT DEFAULT '{}'"),
    ],
}


# Tables to migrate from legacy names. Applied before the CREATE TABLE pass so
# data carries over instead of an empty table being created next to it.
_LEGACY_TABLE_RENAMES: list[tuple[str, str]] = [
    # T1-A · 2026-04-25: split the two "chain" concepts. The implicit per-chat
    # chain stays in `chain_state`; the explicit named multi-step structure
    # used to share the word "chain" via `fit_chains` and is now `fit_campaigns`.
    ("fit_chains", "fit_campaigns"),
]


# Per-table legacy column renames, applied after the table-rename pass and
# before the canonical CREATE/ALTER pass. Values: list of (old_name, new_name).
# SQLite ≥ 3.25 supports `ALTER TABLE ... RENAME COLUMN`.
_LEGACY_COLUMN_RENAMES: dict[str, list[tuple[str, str]]] = {
    "fit_campaigns": [
        ("chain_id",   "campaign_id"),
        ("chain_name", "campaign_name"),
    ],
}


def _table_exists(cur: sqlite3.Cursor, name: str) -> bool:
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None


def _existing_columns(cur: sqlite3.Cursor, table: str) -> set[str]:
    cur.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def apply_schema(conn: sqlite3.Connection) -> None:
    """Bring `conn`'s schema up to the canonical shape. Safe to call repeatedly.

    Caller owns the connection; this function does not commit or close it,
    leaving transaction control to the caller. Both current callers commit
    immediately after invocation.
    """
    cur = conn.cursor()

    # 1. Legacy renames first — preserves data when an old DB is upgraded.
    for old_name, new_name in _LEGACY_TABLE_RENAMES:
        if _table_exists(cur, old_name) and not _table_exists(cur, new_name):
            cur.execute(f"ALTER TABLE {old_name} RENAME TO {new_name}")

    # 2. Legacy column renames within already-renamed tables (SQLite ≥ 3.25).
    for table, renames in _LEGACY_COLUMN_RENAMES.items():
        if not _table_exists(cur, table):
            continue
        existing = _existing_columns(cur, table)
        for old_col, new_col in renames:
            if old_col in existing and new_col not in existing:
                cur.execute(f"ALTER TABLE {table} RENAME COLUMN {old_col} TO {new_col}")
                existing.discard(old_col)
                existing.add(new_col)

    # 3. Canonical CREATE TABLE pass.
    for stmt in _CREATE_STATEMENTS:
        cur.execute(stmt)

    # 3. Indexes.
    for stmt in _CREATE_INDEXES:
        cur.execute(stmt)

    # 4. Idempotent ADD COLUMN for older DBs.
    for table, additions in _PER_TABLE_MIGRATIONS.items():
        existing = _existing_columns(cur, table)
        for col, typedef in additions:
            if col not in existing:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
                existing.add(col)

    conn.commit()


def schema_tables() -> Iterable[str]:
    """Return the names of tables this module manages — useful for tests."""
    return (
        "chain_state",
        "tasks",
        "user_hpc_config",
        "job_history",
        "saved_results",
        "batch_events",
        "fit_campaigns",
    )
