#!/usr/bin/env bash
# FemtoMind MCP app -> generic Linux host deploy (rsync + systemd restart).
# Same layout as the AWS deploy, but the host/user/paths are parameters so
# the app can live on any lab/university/JLab server.
#
# Usage:
#   DEPLOY_HOST=you@server.lab.edu DEPLOY_USER=you ./generic_deploy.sh          # real deploy
#   DEPLOY_HOST=you@server.lab.edu DEPLOY_USER=you DRY_RUN=1 ./generic_deploy.sh
#
# Assumes the host already has (see MIGRATION.md):
#   - python venv at /home/<user>/femtomind/.venv
#   - repo cloned at /home/<user>/femtomind/femtomind-chatgpt-app
#   - fitpack-workflow/ + hpc-monitor/ as SIBLINGS under /home/<user>/femtomind/
#   - systemd units femtomind-mcpo / femtomind-app enabled
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$HERE")"                       # femtomind-chatgpt-app/

HOST="${DEPLOY_HOST:-}"
USER_ON_HOST="${DEPLOY_USER:-}"
SSH_KEY="${DEPLOY_KEY:-}"
FEMTO_BASE="${DEPLOY_BASE:-/home/${USER_ON_HOST}/femtomind}"
APP_TARGET_DIR="${DEPLOY_APP_DIR:-${FEMTO_BASE}/femtomind-chatgpt-app}"
FW_TARGET_DIR="${DEPLOY_FW_DIR:-${FEMTO_BASE}/fitpack-workflow}"
HP_TARGET_DIR="${DEPLOY_HP_DIR:-${FEMTO_BASE}/hpc-monitor}"
DRY_RUN="${DRY_RUN:-0}"

if [ -z "$HOST" ] || [ -z "$USER_ON_HOST" ]; then
  echo "usage: DEPLOY_HOST=you@host DEPLOY_USER=you ./generic_deploy.sh" >&2
  exit 2
fi

RSYNC_OPTS=(-rlDz --exclude '.git' --exclude '.github' --exclude '__pycache__/' \
  --exclude '*.pyc' --exclude '.venv/' --exclude 'deploy/' --exclude 'docs/' \
  --exclude 'DEPLOYMENT.md' --exclude 'RECOVERY.md' \
  --exclude 'fitpack-workflow/' --exclude 'hpc-monitor/')
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
[ -n "$SSH_KEY" ] && SSH_OPTS+=(-i "$SSH_KEY")
if [ "$DRY_RUN" = "1" ]; then
  RSYNC_OPTS+=(--dry-run --itemize-changes)
fi

echo "== deploying to $HOST =="
echo "== app     -> $APP_TARGET_DIR =="
rsync "${RSYNC_OPTS[@]}" -e "ssh ${SSH_OPTS[*]}" "$APP_DIR/" "$HOST:$APP_TARGET_DIR/"

# The backends are SIBLINGS on the host (mcpo points at the sibling paths):
# copy them only if they exist locally in the nested layout.
for name in fitpack-workflow hpc-monitor; do
  if [ -d "$APP_DIR/$name" ]; then
    target="${FW_TARGET_DIR}"
    [ "$name" = "hpc-monitor" ] && target="${HP_TARGET_DIR}"
    echo "== $name -> $target =="
    rsync "${RSYNC_OPTS[@]}" -e "ssh ${SSH_OPTS[*]}" "$APP_DIR/$name/" "$HOST:$target/"
  fi
done

if [ "$DRY_RUN" = "1" ]; then
  echo "== dry run done (no restart) =="
  exit 0
fi

echo "== restarting services =="
ssh "${SSH_OPTS[@]}" "$HOST" \
  "sudo systemctl restart femtomind-mcpo femtomind-app && sleep 4 && sudo systemctl is-active femtomind-mcpo femtomind-app"

echo "== smoke check =="
ssh "${SSH_OPTS[@]}" "$HOST" \
  "curl -s -m 20 -X POST http://127.0.0.1:8000/fitpack/validation_query -H 'Content-Type: application/json' -d '{}'"

echo "== deploy complete =="
