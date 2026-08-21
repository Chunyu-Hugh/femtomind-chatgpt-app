#!/usr/bin/env python3
"""
Test script for result management functionality.
"""
import sys
sys.path.insert(0, '/home/temp-user/fitpack_legacy_copy/fitpack-workflow')

from result_manager import (
    save_task_result, get_saved_results, get_batch_best_result,
    restore_parameters_from_saved, delete_saved_result
)
import sqlite3

DB_PATH = "/home/temp-user/fitpack_legacy_copy/fitpack-workflow/hpc_tasks.db"

def main():
    print("=" * 60)
    print("Testing HPC Result Management")
    print("=" * 60)
    
    # 1. Check saved_results table exists
    print("\n1. Checking saved_results table...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT name FROM sqlite_master 
        WHERE type='table' AND name='saved_results'
    """)
    
    if cursor.fetchone():
        print("   ✓ Table 'saved_results' exists")
        
        # Get column info
        cursor.execute("PRAGMA table_info(saved_results)")
        columns = [col[1] for col in cursor.fetchall()]
        print(f"   Columns: {', '.join(columns)}")
    else:
        print("   ✗ Table 'saved_results' not found!")
        conn.close()
        return
    
    # 2. Get current tasks that could be saved
    print("\n2. Finding tasks with results...")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT path, batch_id, status, slurm_job_id 
        FROM tasks 
        WHERE status IN ('running', 'completed')
        LIMIT 5
    """)
    tasks = cursor.fetchall()
    
    if not tasks:
        print("   No tasks found")
        conn.close()
        return
    
    for task in tasks:
        print(f"   - {task['path']}")
        print(f"     Batch: {task['batch_id']}, Status: {task['status']}")
    
    # 3. Try to save a result
    print("\n3. Testing save_task_result...")
    test_task_path = tasks[0]['path']
    test_batch_id = tasks[0]['batch_id']
    
    result = save_task_result(
        task_path=test_task_path,
        user_note="Test save from automated script",
        mark_as_best=True,
        tags=["test", "automated"]
    )
    
    if "error" in result:
        print(f"   ✗ Error: {result['error']}")
    else:
        print(f"   ✓ Saved successfully!")
        print(f"     ID: {result['saved_id']}")
        print(f"     Action: {result['action']}")
        print(f"     χ²/npts: {result.get('chi2_npts', 'N/A')}")
        print(f"     Is Best: {result['is_best']}")
    
    # 4. List saved results
    print("\n4. Testing get_saved_results...")
    saved_results = get_saved_results()
    
    if saved_results:
        print(f"   Found {len(saved_results)} saved result(s)")
        for r in saved_results:
            print(f"   - ID: {r['id']}")
            print(f"     Batch: {r['batch_id']}")
            print(f"     χ²/npts: {r['chi2_npts']:.6f}")
            print(f"     Best: {'⭐' if r['is_best'] else '  '}")
            print(f"     Note: {r['user_note']}")
            print(f"     Tags: {', '.join(r['tags'])}")
            print(f"     Saved: {r['saved_at']}")
    else:
        print("   No saved results found")
    
    # 5. Test batch best
    if saved_results and test_batch_id:
        print(f"\n5. Testing get_batch_best_result for batch {test_batch_id}...")
        best_result = get_batch_best_result(test_batch_id)
        
        if "error" in best_result:
            print(f"   ✗ Error: {best_result['error']}")
        else:
            best = best_result['best_result']
            print(f"   ✓ Best result found!")
            print(f"     ID: {best['id']}")
            print(f"     χ²/npts: {best['chi2_npts']:.6f}")
            print(f"     Task: {best['task_path']}")
    
    # 6. Test parameter restoration (dry run - don't actually restore)
    if saved_results:
        print(f"\n6. Testing restore_parameters_from_saved (info only)...")
        first_saved = saved_results[0]
        print(f"   Would restore from saved result #{first_saved['id']}")
        print(f"   χ²/npts: {first_saved['chi2_npts']:.6f}")
        print(f"   (Skipping actual restore to preserve current config)")
    
    # 7. Show current state
    print("\n7. Current saved_results table state:")
    cursor.execute("SELECT COUNT(*) as count FROM saved_results")
    count = cursor.fetchone()[0]
    print(f"   Total saved results: {count}")
    
    cursor.execute("SELECT COUNT(*) as count FROM saved_results WHERE is_best = 1")
    best_count = cursor.fetchone()[0]
    print(f"   Marked as best: {best_count}")
    
    conn.close()
    
    print("\n" + "=" * 60)
    print("Test completed!")
    print("=" * 60)

if __name__ == "__main__":
    main()
