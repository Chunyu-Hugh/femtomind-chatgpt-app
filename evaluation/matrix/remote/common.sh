# Shared by the farm side of the evaluation sandbox (sourced, not run). A copy
# of the pilot's bench_nomcp/remote/common.sh with the evaluation's own root
# and run names; inside the sandbox only the run directory is writable, the
# shared grids and LHAPDF tables are read-only, nothing else of the host is
# visible: no home, no other JAM folders, no Slurm or SSH credentials.
ROOT=/w/ceph24/JAM/chunyu/bench_eval
IMAGE=/u/scigroup/scicomp/jupyterhub/theory/theory-notebook.sif
GRIDS=/w/ceph24/JAM/share/fitpack_grids
LHAPDF=/w/ceph24/JAM/apps/lhapdf_tables/more/more1
LOGS=/farm_out/$USER                 # job logs; outside the sandbox
LOGTAG=eval                          # $LOGS/eval_<run>_<job>.out

run_dir() {
  [[ "$1" =~ ^nomcp-[ABC]-[a-z0-9]+-r[0-9]{2}$ ]] || { echo "Bad run name: $1" >&2; exit 2; }
  RUN=$ROOT/nomcp/$1
  [ -d "$RUN" ] || { echo "No run directory $RUN" >&2; exit 2; }
  SANDBOX=(--containall --bind "$RUN:$RUN" --bind "$LHAPDF:$LHAPDF:ro" --pwd "$RUN")
  # Only the grid folders themselves: the shared folder also holds other
  # analyses' inputs (results/), which the agent must not see.
  local g
  for g in dy dypion jet pjet pp_hadron wasym wc wzrv zrap; do
    SANDBOX+=(--bind "$GRIDS/$g:$GRIDS/$g:ro")
  done
}
