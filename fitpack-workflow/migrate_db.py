#!/usr/bin/env python3
"""
Database migration script to add status tracking fields to tasks table.
"""
import sqlite3
import sys
from pathlib import Path

DB_PATH = "/home/temp-user/fitpack_legacy_copy/fitpack-workflow/hpc_tasks.db"

def migrate():
    """Add status and timestamp fields to tasks table."""
    print(f"Starting database migration for {DB_PATH}")
    
    if not Path(DB_PATH).exists():
        print(f"Error: Database not found at {DB_PATH}")
        sys.exit(1)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # Check current schema
        cursor.execute("PRAGMA table_info(tasks)")
        columns = {col[1] for col in cursor.fetchall()}
        print(f"Current columns: {columns}")
        
        # Add new columns if they don't exist
        new_columns = [
            ("status", "TEXT DEFAULT 'created'"),
            ("submitted_at", "TIMESTAMP"),
            ("started_at", "TIMESTAMP"),
            ("completed_at", "TIMESTAMP"),
        ]
        
        for col_name, col_def in new_columns:
            if col_name not in columns:
                print(f"Adding column: {col_name}")
                cursor.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_def}")
            else:
                print(f"Column {col_name} already exists, skipping")
        
        # Migrate existing data: convert is_running to status
        if "status" not in columns or True:  # Always update to ensure consistency
            print("Migrating is_running values to status...")
            # Tasks with slurm_job_id but is_running=0 are likely submitted
            cursor.execute("""
                UPDATE tasks 
                SET status = CASE 
                    WHEN is_running = 1 THEN 'running'
                    WHEN slurm_job_id IS NOT NULL THEN 'submitted'
                    ELSE 'created'
                END
                WHERE status IS NULL OR status = 'created'
            """)
            affected = cursor.rowcount
            print(f"Updated {affected} task statuses")
        
        conn.commit()
        print("✓ Migration completed successfully!")
        
        # Show summary
        cursor.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")
        print("\nCurrent task status distribution:")
        for status, count in cursor.fetchall():
            print(f"  {status}: {count}")
        
    except sqlite3.OperationalError as e:
        print(f"Migration error: {e}")
        conn.rollback()
        sys.exit(1)
    finally:
        conn.close()

if __name__ == "__main__":
    migrate()
