#!/bin/bash
# Queue state of this run's job and the end of its log.
# Over ssh: status.sh <run>
set -euo pipefail
source "$(dirname "$0")/common.sh"
run_dir "$1"
f=$ROOT/state/$1.jobid
[ -e "$f" ] || { echo "No job submitted yet."; exit 0; }
job=$(cat "$f")
squeue -h -j "$job" -o 'queue: %i %T elapsed %M %R' 2>/dev/null || true
sacct -n -X -j "$job" -o JobID,State,Elapsed,ExitCode 2>/dev/null | sed 's/^/sacct: /' || true
log=$LOGS/${LOGTAG}_$1_$job.out
if [ -f "$log" ]; then
  echo "--- last 40 lines of the job log"
  tail -n 40 "$log"
fi
