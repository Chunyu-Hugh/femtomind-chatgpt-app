#!/usr/bin/env python3
"""
Database migration script to add saved_results table for storing best parameters.
"""
import sqlite3
import sys
from pathlib import Path

DB_PATH = "/home/temp-user/fitpack_legacy_copy/fitpack-workflow/hpc_tasks.db"

def migrate():
    """Add saved_results table for storing best task results."""
    print(f"Starting saved_results table migration for {DB_PATH}")
    
    if not Path(DB_PATH).exists():
        print(f"Error: Database not found at {DB_PATH}")
        sys.exit(1)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # Check if table already exists
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='saved_results'
        """)
        
        if cursor.fetchone():
            print("Table 'saved_results' already exists, skipping creation")
        else:
            print("Creating 'saved_results' table...")
            cursor.execute("""
                CREATE TABLE saved_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    task_path TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    
                    -- Result metrics
                    chi2 REAL NOT NULL,
                    chi2_npts REAL,
                    final_iteration INTEGER,
                    
                    -- Parameter snapshot (JSON format)
                    parameters_snapshot TEXT NOT NULL,
                    
                    -- Metadata
                    user_note TEXT,
                    is_best INTEGER DEFAULT 0,
                    tags TEXT,
                    
                    saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    
                    -- Indexes for faster queries
                    FOREIGN KEY (task_path) REFERENCES tasks(path) ON DELETE CASCADE
                )
            """)
            
            # Create indexes
            cursor.execute("""
                CREATE INDEX idx_saved_batch_id ON saved_results(batch_id)
            """)
            cursor.execute("""
                CREATE INDEX idx_saved_chat_id ON saved_results(chat_id)
            """)
            cursor.execute("""
                CREATE INDEX idx_saved_is_best ON saved_results(is_best)
            """)
            
            print("✓ Table 'saved_results' created successfully")
        
        conn.commit()
        print("✓ Migration completed successfully!")
        
        # Show table schema
        cursor.execute("PRAGMA table_info(saved_results)")
        print("\nTable schema:")
        for col in cursor.fetchall():
            print(f"  {col[1]}: {col[2]}")
        
    except sqlite3.Error as e:
        print(f"Migration error: {e}")
        conn.rollback()
        sys.exit(1)
    finally:
        conn.close()

if __name__ == "__main__":
    migrate()
