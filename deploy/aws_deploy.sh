#!/usr/bin/env bash
# FemtoMind MCP app -> AWS production deploy (manual rsync, used until CI is repaired).
#
# Usage:
#   AWS_KEY=~/.ssh/femtomind_aws.pem ./aws_deploy.sh            # real deploy
#   AWS_KEY=~/.ssh/femtomind_aws.pem DRY_RUN=1 ./aws_deploy.sh  # show what would sync
#
# Deploys: fitpack-workflow/ (incl. path_policy.py, validation_registry.py),
#          femtomind_app.py, agent_brief.md, docs/.
# Restarts: femtomind-mcpo, femtomind-app (systemd on the AWS host).
#
# Host layout (Amazon Linux, user ec2-user — NOT ubuntu): the app lives in
# /home/ec2-user/femtomind/femtomind-chatgpt-app/ and fitpack-workflow is a
# SIBLING at /home/ec2-user/femtomind/fitpack-workflow/ (mcpo points at the
# sibling path). The rsync targets below mirror the CI workflow.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$HERE")"                       # femtomind-chatgpt-app/

AWS_KEY="${AWS_KEY:-${HOME:-/root}/.ssh/femtomind_aws.pem}"
HOST="${AWS_HOST:-ec2-user@femtomind.duckdns.org}"
APP_TARGET_DIR="${AWS_APP_TARGET_DIR:-/home/ec2-user/femtomind/femtomind-chatgpt-app}"
FW_TARGET_DIR="${AWS_FW_TARGET_DIR:-/home/ec2-user/femtomind/fitpack-workflow}"
DRY_RUN="${DRY_RUN:-0}"

RSYNC_OPTS=(-az --exclude '__pycache__/' --exclude '*.pyc' --exclude '.venv/' --exclude 'agents/')
SSH_OPTS=(-i "$AWS_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=15)
if [ "$DRY_RUN" = "1" ]; then
  RSYNC_OPTS+=(--dry-run --itemize-changes)
fi

echo "== deploying to $HOST (fitpack-workflow -> $FW_TARGET_DIR, app -> $APP_TARGET_DIR) =="
rsync "${RSYNC_OPTS[@]}" -e "ssh ${SSH_OPTS[*]}" \
  "$APP_DIR/fitpack-workflow/" "$HOST:$FW_TARGET_DIR/"
rsync "${RSYNC_OPTS[@]}" -e "ssh ${SSH_OPTS[*]}" \
  "$APP_DIR/femtomind_app.py" "$APP_DIR/agent_brief.md" "$HOST:$APP_TARGET_DIR/"
rsync "${RSYNC_OPTS[@]}" -e "ssh ${SSH_OPTS[*]}" \
  "$APP_DIR/docs/" "$HOST:$APP_TARGET_DIR/docs/"

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
