#!/usr/bin/env python3
"""Daily snapshots of the two databases this deployment cannot be rebuilt from.

hpc_tasks.db holds the fits and their history; auth.db holds who is allowed to
run them. Losing the latter means re-issuing every collaborator's access code,
so it is backed up on the same schedule.

Uses sqlite3's online backup API rather than copying the files, so a snapshot
taken while a fit is being submitted is still internally consistent.
"""
import pathlib
import sqlite3
import time

SOURCES = [
    pathlib.Path.home() / "femtomind/fitpack-workflow/hpc_tasks.db",
    pathlib.Path.home() / "femtomind/femtomind-chatgpt-app/auth.db",
]
DEST_DIR = pathlib.Path.home() / "femtomind/backups"
KEEP = 7

DEST_DIR.mkdir(parents=True, exist_ok=True)
stamp = time.strftime("%Y%m%d-%H%M%S")

for src_path in SOURCES:
    if not src_path.exists():
        print(f"skip {src_path.name}: not present")
        continue
    dest = DEST_DIR / f"{src_path.stem}-{stamp}.db"
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()

    snaps = sorted(DEST_DIR.glob(f"{src_path.stem}-*.db"))
    for old in snaps[:-KEEP]:
        old.unlink()
    print(f"{dest.name}  ({dest.stat().st_size} bytes)  kept={len(snaps[-KEEP:])}")
