#!/usr/bin/env python3
"""One-shot migration: add batch_summaries table.

Safe to run multiple times (uses CREATE TABLE IF NOT EXISTS).
The application also ensures the schema at runtime via
chi2_bootstrap_false.ensure_schema(), so this script is only for
pre-deploy DB prep.

Usage:
    python3 migrate_chi2_bf.py
    DB_PATH=/custom/path.db python3 migrate_chi2_bf.py
"""
import os
import sqlite3
import sys
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).parent / "hpc_tasks.db"))

SQL = """
CREATE TABLE IF NOT EXISTS batch_summaries (
    batch_id         TEXT PRIMARY KEY,
    hpc_username     TEXT NOT NULL,
    mode             TEXT NOT NULL DEFAULT 'bootstrap_false',
    status           TEXT NOT NULL DEFAULT 'computing',
    convergence_gate REAL DEFAULT 3.0,
    n_total          INTEGER,
    n_survivors      INTEGER,
    started_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    computed_at      TIMESTAMP,
    compute_seconds  REAL,
    remote_output    TEXT,
    remote_status    TEXT,
    result_json      TEXT,
    error_message    TEXT
);
CREATE INDEX IF NOT EXISTS idx_batch_summaries_status
    ON batch_summaries(status);
"""


def main() -> int:
    print(f"[migrate_chi2_bf] using DB at {DB_PATH}")
    con = sqlite3.connect(DB_PATH)
    try:
        con.executescript(SQL)
        con.commit()
        count = con.execute("SELECT COUNT(*) FROM batch_summaries").fetchone()[0]
        print(f"[migrate_chi2_bf] OK. batch_summaries rows: {count}")
    except Exception as e:
        print(f"[migrate_chi2_bf] FAILED: {e}", file=sys.stderr)
        return 1
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
