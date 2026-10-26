#!/bin/bash
# Run one command from the agent inside the sandbox.
# Over ssh: exec.sh <run>, with the base64-encoded command on stdin (so the
# farm's tcsh login shell never parses it).
set -euo pipefail
source "$(dirname "$0")/common.sh"
run_dir "$1"
cmd=$(base64 -d)
exec apptainer exec "${SANDBOX[@]}" "$IMAGE" bash -c "$cmd"
