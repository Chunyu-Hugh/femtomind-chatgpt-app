#!/usr/bin/env bash
# The evaluation matrix's sandbox and launcher: the pilot's bench_nomcp/
# run_stage1.sh, with the evaluation's own roots (nothing of the pilot's is
# touched or shared), the knowledge conditions, and one run name scheme
#   <arm>-<condition>-<features>-r<NN>      e.g. nomcp-A-x-r01, mcp-B-ab-r02
# (arm nomcp|mcp; condition A|B|C; features x for no-MCP, t|a|ab for the
# gateway commit used: tools only, +A, +A+B).
#
#   box.sh install                 sandbox scripts to the farm and ~/bench_eval/bin, read-only
#   box.sh prepare RUN             farm directory (FitPack copy or gateway workspace) and local box
#   box.sh attack RUN              try to get out of the no-MCP sandbox; every line must say "blocked"
#   box.sh calibrate ARM           one-line run: sign-in works, expected tools, gateway connects (mcp)
#   box.sh run RUN                 the measured run; transcript in ~/bench_eval/results/RUN.jsonl
#   box.sh summary FILE            tokens, cost and time of a finished run
#
# Environment: FEMTOMIND_IFARM_SOCK (default ~/.ssh/controlmasters/ifarm-eval),
# FEMTOMIND_EVAL_ROOT (~/bench_eval), EVAL_MODEL (claude-opus-5-5), EVAL_TIMEOUT
# (3h), EVAL_GW_PORT (8766; the gateway must be running, see gateway.py),
# EVAL_FITPACK_MAIN / EVAL_FITPACK_AI (the pristine clones copied per run).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
APP=$(cd "$HERE/../.." && pwd)
ROOT=/w/ceph24/JAM/chunyu/bench_eval
AREA=/w/ceph24/JAM/chunyu
EVAL=${FEMTOMIND_EVAL_ROOT:-$HOME/bench_eval}
LBIN=$EVAL/bin
LWORK=$EVAL/work
RESULTS=$EVAL/results
CFG=$EVAL/claude-config                    # a config of its own: no CLAUDE.md, memory, plugins or hooks
SOCK=${FEMTOMIND_IFARM_SOCK:-$HOME/.ssh/controlmasters/ifarm-eval}
MODEL=${EVAL_MODEL:-claude-opus-5-5}
TIMEOUT=${EVAL_TIMEOUT:-3h}
NODE=$HOME/.local/node
BRIDGE=$HOME/.claude/mcp-bridge/mcp-http-bridge.mjs
MCP_URL=http://127.0.0.1:${EVAL_GW_PORT:-8766}/mcp
GW_TOKEN_FILE=$EVAL/gw_token
G=mcp__jam-gateway
FITPACK_MAIN=${EVAL_FITPACK_MAIN:-/w/ceph24/JAM/chunyu/bench_nomcp/fitpack}   # read only, copied per run
FITPACK_AI=${EVAL_FITPACK_AI:-/w/ceph24/JAM/chunyu/bench_mcp/fitpack}

# Disabled on both sides: tools that add tokens or reach outside the box, and
# reads of the sign-in or of process environments (the gateway token).
COMMON_DENY=(WebSearch WebFetch Agent "Read(/$CFG/**)" "Read(//proc/**)"
  Artifact ArtifactComments ArtifactData CronCreate CronDelete CronList DesignSync
  EnterWorktree ExitWorktree ListAgents Monitor NotebookEdit PushNotification RemoteTrigger
  ReportFindings ScheduleWakeup SendMessage Skill TaskStop Workflow)
OPTS=(--model "$MODEL"
      --strict-mcp-config --mcp-config '{"mcpServers":{}}'
      --allowedTools "Bash($LBIN/farm *)" "Bash($LBIN/farm-submit *)" "Bash($LBIN/farm-status)" Read Write Edit
      --disallowedTools "${COMMON_DENY[@]}"
      --output-format stream-json --verbose)

# The gateway tools a condition hides, from evaluation/conditions/deny_lists.py,
# also disallowed on the client side.
mcp_opts() {
  local condition=$1 denied
  denied=$(cd "$APP" && FEMTOMIND_APP_ROOT=$APP python3 -B -c "
from evaluation.conditions import deny_lists; print(' '.join('${G}__' + t for t in deny_lists.denied('$condition')))")
  local cfg
  cfg=$(jq -nc --arg node "$NODE/bin/node" --arg bridge "$BRIDGE" --arg url "$MCP_URL" \
    '{mcpServers: {"jam-gateway": {command: $node, args: [$bridge, $url]}}}')
  OPTS_MCP=(--model "$MODEL"
      --strict-mcp-config --mcp-config "$cfg"
      --allowedTools "$G" Read Write Edit
      --disallowedTools Bash "${COMMON_DENY[@]}" $denied
      --output-format stream-json --verbose)
}

farm_raw() { ssh -S "$SOCK" -o BatchMode=yes ifarm-tunnel "bash -lc '$1'" 2> >(grep -v syscshrc >&2); }

parse_run() {   # sets ARM CONDITION FEATURES
  [[ "$1" =~ ^(nomcp|mcp)-([ABC])-([a-z0-9]+)-r[0-9]{2}$ ]] || { echo "bad run name: $1 (arm-condition-features-rNN)" >&2; exit 2; }
  ARM=${BASH_REMATCH[1]}; CONDITION=${BASH_REMATCH[2]}; FEATURES=${BASH_REMATCH[3]}
}

# Run a command under bubblewrap with working directory $1: the system is
# read-only, the home directory is empty except for what is bound back in
# (BOX_SIDE picks the no-MCP or MCP extras). BOX_TIMEOUT, if set, limits it.
in_box() {
  local cwd=$1; shift
  local extra
  if [ "${BOX_SIDE:-nomcp}" = mcp ]; then
    extra=(--ro-bind "$NODE" "$NODE" --ro-bind "$BRIDGE" "$BRIDGE")
  else
    extra=(--ro-bind "$HOME/.ssh/config" "$HOME/.ssh/config"
           --bind "$HOME/.ssh/controlmasters" "$HOME/.ssh/controlmasters"
           --ro-bind "$LBIN" "$LBIN")
  fi
  ${BOX_TIMEOUT:+timeout "$BOX_TIMEOUT"} bwrap --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp \
    --tmpfs "$HOME" \
    --ro-bind "$HOME/.local/bin" "$HOME/.local/bin" \
    --ro-bind "$HOME/.local/share/claude" "$HOME/.local/share/claude" \
    "${extra[@]}" \
    --bind "$CFG" "$CFG" \
    --bind "$HOME/.claude/.credentials.json" "$CFG/.credentials.json" \
    --bind "$cwd" "$cwd" --chdir "$cwd" \
    --unshare-pid --die-with-parent \
    --setenv CLAUDE_CONFIG_DIR "$CFG" --setenv ENABLE_CLAUDEAI_MCP_SERVERS false \
    --setenv DISABLE_AUTOUPDATER 1 \
    "$@"
}

# The local gateway's token into the environment (inherited by the bridge;
# never on a command line). The gateway must be running.
gateway_token() {
  MCP_BEARER_TOKEN=$(cat "$GW_TOKEN_FILE" 2>/dev/null) || { echo "No $GW_TOKEN_FILE (gateway.py start writes it)." >&2; exit 1; }
  export MCP_BEARER_TOKEN
  curl -s -o /dev/null --max-time 10 -X POST "$MCP_URL" || { echo "The local gateway is not running at $MCP_URL." >&2; exit 1; }
}

# A run can end more than once: the agent answers, then a background command
# it started finishes and it carries on. Cost, model time and modelUsage are
# cumulative in the last result; turns add up; wall time comes from the
# session transcript's first and last timestamps.
summary() {
  local sid session
  sid=$(jq -r 'select(.type=="system" and .subtype=="init") | .session_id' "$1" | head -1)
  session=$(ls "$CFG"/projects/*/"$sid".jsonl 2>/dev/null | head -1)
  if [ -n "${2:-}" ]; then
    echo "wall time:   $2 s, this part alone"
  elif [ -n "$session" ]; then
    jq -rs '[.[] | .timestamp // empty | sub("\\.[0-9]+Z$"; "Z") | fromdate] |
      "wall time:   \(max - min) s (\((max - min) / 60 * 10 | floor / 10) min), start to end"' "$session"
  fi
  jq -rs '[.[] | select(.type=="result")] as $r | $r[-1] as $last |
    "endings:     \($r | length) (\($r | map(.subtype) | join(", ")))",
    "cost (USD):  \($last.total_cost_usd)",
    "model time:  \($last.duration_api_ms / 1000) s",
    "turns:       \($r | map(.num_turns) | add)",
    ($last.modelUsage | to_entries[] |
      "tokens \(.key): input \(.value.inputTokens), cache write \(.value.cacheCreationInputTokens), cache read \(.value.cacheReadInputTokens), output \(.value.outputTokens) (thinking \(.value.thinkingTokens)), $\(.value.costUSD)")' "$1"
  echo "tool calls:"
  jq -r 'select(.type=="assistant") | .message.content[]? | select(.type=="tool_use") | .name' "$1" \
    | sort | uniq -c | sed 's/^/  /'
  echo "final answer:"
  jq -r 'select(.type=="result") | .result // empty' "$1" | sed 's/^/  /'
}

init_line() {
  jq -r 'select(.type=="system" and .subtype=="init") |
    "model:       \(.model)", "mcp servers: \(.mcp_servers | tostring)",
    "tools:       \(.tools | length): \(.tools | join(" "))"' "$1"
}

guard_pilot() {   # the pilot shares the account and the farm: never run both
  if pgrep -f "bench_nomcp/run_stage1.sh" >/dev/null 2>&1; then
    echo "The pilot's run_stage1.sh is running; the matrix waits for it." >&2; exit 1
  fi
}

CMD=${1:-}
case "$CMD" in
  install)
    farm_raw "mkdir -p $ROOT/bin $ROOT/state $ROOT/nomcp $ROOT/mcp && chmod -R u+w $ROOT/bin"
    for f in common exec submit status; do
      ssh -S "$SOCK" -o BatchMode=yes ifarm-tunnel "bash -lc 'cat > $ROOT/bin/$f.sh'" \
        < "$HERE/remote/$f.sh" 2> >(grep -v syscshrc >&2)
    done
    farm_raw "chmod 555 $ROOT/bin/*.sh $ROOT/bin && ls -l $ROOT/bin"
    mkdir -p "$LBIN" "$CFG" "$RESULTS" "$LWORK"
    chmod u+w "$LBIN"
    install -m 555 "$HERE/local/farm" "$HERE/local/farm-submit" "$HERE/local/farm-status" "$LBIN/"
    rm -f "$LBIN/SOCK"; echo "$SOCK" > "$LBIN/SOCK"; chmod 444 "$LBIN/SOCK"
    chmod 555 "$LBIN"
    touch "$CFG/.credentials.json"         # mount point for the sign-in, bound in by in_box
    ls -l "$LBIN"
    ;;
  prepare)
    run=${2:?run}; parse_run "$run"
    # idempotent: a prepared farm directory is kept (a run is measured once; see run)
    if [ "$ARM" = nomcp ]; then
      farm_raw "if test -e $ROOT/nomcp/$run; then echo farm: $ROOT/nomcp/$run already prepared; else mkdir -p $ROOT/nomcp/$run && cp -a $FITPACK_MAIN $ROOT/nomcp/$run/fitpack && echo farm: $ROOT/nomcp/$run ready; fi"
      chmod u+w "$LBIN"; rm -f "$LBIN/RUN"; echo "$run" > "$LBIN/RUN"; chmod 444 "$LBIN/RUN"; chmod 555 "$LBIN"
    else
      farm_raw "test -d $FITPACK_AI/fitlib && if test -e $ROOT/mcp/$run; then echo farm: $ROOT/mcp/$run already prepared; else cp -a $FITPACK_AI $ROOT/mcp/$run && echo farm: $ROOT/mcp/$run ready; fi"
    fi
    mkdir -p "$LWORK/$run" "$RESULTS"
    (cd "$APP" && FEMTOMIND_APP_ROOT=$APP python3 -B -m evaluation.conditions.materials \
       --arm "$ARM" --condition "$CONDITION" --run "$run" --out "$LWORK/$run" > "$RESULTS/$run.materials.json")
    # the prompt's farm paths are the evaluation's, not the pilot's
    sed -i "s|/home/temp-user/bench_nomcp/bin|$LBIN|g; s|/w/ceph24/JAM/chunyu/bench_nomcp/|$ROOT/nomcp/|g; s|/w/ceph24/JAM/chunyu/bench_mcp/|$ROOT/mcp/|g" "$LWORK/$run/prompt.txt"
    cp "$LWORK/$run/prompt.txt" "$RESULTS/$run.prompt.txt"
    echo "local: $LWORK/$run ready; prompt: $RESULTS/$run.prompt.txt"
    ;;
  attack)
    run=${2:?run}; parse_run "$run"
    bad=0
    blocked() {
      local what=$1; shift
      if "$@" >/dev/null 2>&1; then echo "OPEN     $what"; bad=1; else echo "blocked  $what"; fi
    }
    F=$LBIN/farm
    blocked "farm: write the shared grids"         "$F" "touch $(printf %q /w/ceph24/JAM/share/fitpack_grids/.eval_probe)"
    blocked "farm: write the LHAPDF tables"        "$F" 'touch /w/ceph24/JAM/apps/lhapdf_tables/more/more1/.eval_probe'
    blocked "farm: write another chunyu folder"    "$F" 'touch /w/ceph24/JAM/chunyu/.eval_probe'
    blocked "farm: read the original JAM24 input"  "$F" 'cat /w/ceph24/JAM/treyand/pdf_plotting/strange_analysis/combined4/2-0/input.py'
    blocked "farm: read the catalog on the farm"   "$F" 'ls /w/ceph24/JAM/treyand/femtomind-chatgpt-app/analyses'
    blocked "farm: read the pilot's runs"          "$F" 'ls /w/ceph24/JAM/chunyu/bench_nomcp'
    blocked "farm: see other analyses in grids"    "$F" 'ls /w/ceph24/JAM/share/fitpack_grids/results'
    blocked "farm: see the pristine FitPack clone" "$F" "ls $FITPACK_MAIN"
    blocked "farm: see the real home"              "$F" 'ls /home/chunyu/.ssh'
    blocked "farm: run sbatch directly"            "$F" 'sbatch --version'
    "$F" "echo x'; touch /home/chunyu/.eval_inject; '" >/dev/null 2>&1 || true
    blocked "farm: quoting injection on the host"  farm_raw 'test -e /home/chunyu/.eval_inject'
    blocked "submit: a script outside the run"     "$LBIN/farm-submit" /w/ceph24/JAM/share/fitpack_grids/README.md
    blocked "local: see the gateway repository"    in_box "$LWORK/$run" ls "$APP"
    blocked "local: see Claude memory"             in_box "$LWORK/$run" ls "$HOME/.claude/projects"
    blocked "local: see the pilot's work"          in_box "$LWORK/$run" ls "$HOME/bench_nomcp"
    blocked "local: change the entry commands"     in_box "$LWORK/$run" touch "$LBIN/farm"
    blocked "local: write outside the work dir"    in_box "$LWORK/$run" touch "$EVAL/results/x"
    if "$F" 'touch .eval_probe && rm .eval_probe' >/dev/null 2>&1; then echo "works    farm: write the run directory"
    else echo "BROKEN   farm: write the run directory"; bad=1; fi
    [ "$bad" = 0 ] && echo "Sandbox OK." || { echo "Sandbox NOT OK."; exit 1; }
    ;;
  calibrate)
    arm=${2:?nomcp|mcp}
    mkdir -p "$LWORK/calibrate_$arm" "$RESULTS"
    f=$RESULTS/calibrate_${arm}_$(date +%Y%m%d_%H%M%S).jsonl
    if [ "$arm" = mcp ]; then
      gateway_token; mcp_opts C
      BOX_SIDE=mcp in_box "$LWORK/calibrate_$arm" claude -p "Reply with just OK." "${OPTS_MCP[@]}" > "$f"
    else
      in_box "$LWORK/calibrate_$arm" claude -p "Reply with just OK." "${OPTS[@]}" > "$f"
    fi
    init_line "$f"; summary "$f"
    ;;
  run)
    run=${2:?run}; parse_run "$run"; guard_pilot
    [ -f "$RESULTS/$run.prompt.txt" ] || { echo "Run $0 prepare $run first." >&2; exit 1; }
    ssh -S "$SOCK" -O check ifarm-tunnel >/dev/null 2>&1 || { echo "No ControlMaster at $SOCK." >&2; exit 1; }
    f=$RESULTS/$run.jsonl
    [ -e "$f" ] && { echo "$f exists; a run is measured once." >&2; exit 1; }
    date -u +%Y-%m-%dT%H:%M:%SZ > "$RESULTS/$run.started_at"
    echo "Running $run (model $MODEL, timeout $TIMEOUT); transcript $f"
    rc=0
    if [ "$ARM" = mcp ]; then
      gateway_token; mcp_opts "$CONDITION"
      BOX_SIDE=mcp BOX_TIMEOUT=$TIMEOUT in_box "$LWORK/$run" \
        claude -p "$(cat "$RESULTS/$run.prompt.txt")" "${OPTS_MCP[@]}" < /dev/null > "$f" || rc=$?
    else
      BOX_TIMEOUT=$TIMEOUT in_box "$LWORK/$run" claude -p "$(cat "$RESULTS/$run.prompt.txt")" "${OPTS[@]}" < /dev/null > "$f" || rc=$?
    fi
    date -u +%Y-%m-%dT%H:%M:%SZ > "$RESULTS/$run.ended_at"
    echo "$rc" > "$RESULTS/$run.rc"
    [ "$rc" = 124 ] && echo "TIMED OUT after $TIMEOUT (counts as not finished)."
    summary "$f"
    ;;
  summary)
    summary "${2:?usage: $0 summary FILE}"
    ;;
  *)
    sed -n '2,24p' "$0"
    exit 2
    ;;
esac
