#!/bin/bash
# Submit the agent's job script, once per run. The job runs inside the same
# sandbox as exec.sh; only whitelisted #SBATCH options are passed on, the log
# goes to /farm_out (the site's Slurm filter allows no other log path; it is
# read with status.sh), and there is no job array.
# Over ssh: submit.sh <run>, with the base64-encoded script path on stdin.
# BENCH_DRYRUN=1 prints the launcher instead of submitting it.
set -euo pipefail
source "$(dirname "$0")/common.sh"
run_dir "$1"
path=$(base64 -d)
case "$path" in /*) ;; *) path=$RUN/$path ;; esac
script=$(realpath -e -- "$path" 2>/dev/null) || { echo "No such file: $path" >&2; exit 1; }
case "$script" in "$RUN"/*) ;; *) echo "Refused: the script must be inside $RUN." >&2; exit 1 ;; esac
[ -f "$script" ] || { echo "Not a file: $script" >&2; exit 1; }

state=$ROOT/state
mkdir -p "$state"
if [ -e "$state/$1.jobid" ]; then
  echo "Refused: this run already submitted job $(cat "$state/$1.jobid"); only one job is allowed." >&2
  exit 1
fi

allowed=$(grep -E '^#SBATCH[[:space:]]+(--(time|mem|cpus-per-task|partition|job-name)=[^[:space:]]+|-[tcpJ][[:space:]]+[^[:space:]]+)[[:space:]]*$' "$script" || true)
ignored=$(grep -E '^#SBATCH' "$script" | grep -vxF -f <(printf '%s\n' "$allowed") || true)
launcher=$state/$1.launcher.sh
{
  echo '#!/bin/bash'
  [ -n "$allowed" ] && printf '%s\n' "$allowed"
  echo "#SBATCH --output=$LOGS/${LOGTAG}_$1_%j.out"
  echo "#SBATCH --error=$LOGS/${LOGTAG}_$1_%j.out"
  echo "#SBATCH --ntasks=1"
  printf 'exec apptainer exec %s %q bash %q\n' "$(printf '%q ' "${SANDBOX[@]}")" "$IMAGE" "$script"
} > "$launcher"
if [ -n "$ignored" ]; then
  echo "Ignored (not allowed in the sandbox):"
  printf '%s\n' "$ignored" | sed 's/^/  /'
fi
if [ "${BENCH_DRYRUN:-}" = 1 ]; then cat "$launcher"; exit 0; fi
job=$(sbatch --parsable "$launcher")
echo "$job" > "$state/$1.jobid"
date -u +%Y-%m-%dT%H:%M:%SZ > "$state/$1.submitted_at"
echo "Submitted job $job; farm-status shows its state and log."
