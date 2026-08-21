#!/usr/bin/env python3
"""
Test script for status synchronization functionality.
"""
import sys
sys.path.insert(0, '/home/temp-user/fitpack_legacy_copy/fitpack-workflow')

from status_sync import sync_task_statuses, get_task_status_info, query_slurm_job_status
import sqlite3

DB_PATH = "/home/temp-user/fitpack_legacy_copy/fitpack-workflow/hpc_tasks.db"

def main():
    print("=" * 60)
    print("Testing HPC Task Status Synchronization")
    print("=" * 60)
    
    # 1. Check database schema
    print("\n1. Checking database schema...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(tasks)")
    columns = [col[1] for col in cursor.fetchall()]
    print(f"   Columns: {', '.join(columns)}")
    
    required_columns = ["status", "submitted_at", "started_at", "completed_at"]
    for col in required_columns:
        if col in columns:
            print(f"   ✓ Column '{col}' exists")
        else:
            print(f"   ✗ Column '{col}' missing!")
    
    # 2. Show current tasks
    print("\n2. Current tasks in database:")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT path, status, slurm_job_id, created_at FROM tasks")
    tasks = cursor.fetchall()
    
    if not tasks:
        print("   No tasks found")
    else:
        for task in tasks:
            print(f"   - {task['path']}")
            print(f"     Status: {task['status']}")
            print(f"     Job ID: {task['slurm_job_id']}")
            print(f"     Created: {task['created_at']}")
    
    conn.close()
    
    # 3. Test status sync
    if tasks:
        print("\n3. Testing status synchronization...")
        result = sync_task_statuses()
        print(f"   Updated: {result.get('updated_count', 0)} tasks")
        print(f"   Failed: {result.get('failed_count', 0)} tasks")
        
        if result.get('tasks'):
            for task in result['tasks']:
                print(f"   - {task['path']}: {task['old_status']} → {task['new_status']}")
        
        # 4. Test individual task status
        print("\n4. Testing individual task status...")
        first_task_path = tasks[0]['path']
        status_info = get_task_status_info(first_task_path)
        
        if 'error' in status_info:
            print(f"   Error: {status_info['error']}")
        else:
            print(f"   Path: {status_info['path']}")
            print(f"   DB Status: {status_info['status']}")
            print(f"   Live Status: {status_info['live_status']}")
            print(f"   Job ID: {status_info['slurm_job_id']}")
            print(f"   Submitted: {status_info['submitted_at']}")
            print(f"   Started: {status_info['started_at']}")
            print(f"   Completed: {status_info['completed_at']}")
    
    print("\n" + "=" * 60)
    print("Test completed!")
    print("=" * 60)

if __name__ == "__main__":
    main()
