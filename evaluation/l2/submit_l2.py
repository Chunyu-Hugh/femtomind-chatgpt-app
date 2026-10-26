"""Run the L2 recompute on the farm: one replica now, or all 791 as an array.

    python -m evaluation.l2.submit_l2 smoke --replica 0            # one replica, in the container on the login node
    python -m evaluation.l2.submit_l2 array --per-task 20 [--time 03:00:00 --mem 8G]   # sbatch --array over all replicas
    python -m evaluation.l2.submit_l2 fetch                        # copy the chunk JSONs home

Outputs go to /w/ceph24/JAM/chunyu/bench_eval/l2/<stamp>/ on the farm and
~/bench_eval/l2/<stamp>/ at home. The worker is analyses/JAM24/results/
l2_recompute.py, shipped as text; the archive is the server directory named
in analyses/JAM24/results/reference_manifest.json; FitPack is the gateway's
ai-branch clone (EVAL_FITPACK_AI).
"""
import argparse
import json
import os
import shlex
import time

from evaluation.common import paths, ssh

ROOT = "/w/ceph24/JAM/chunyu/bench_eval/l2"
WORKER = os.path.join(paths.APP_ROOT, "analyses", "JAM24", "results", "l2_recompute.py")
FITPACK = os.environ.get("EVAL_FITPACK_AI", "/w/ceph24/JAM/chunyu/bench_mcp/fitpack")
LHAPDF = "/w/ceph24/JAM/apps/lhapdf_tables/more/more1"


def archive_dir():
    with open(os.path.join(paths.APP_ROOT, "analyses", "JAM24", "results", "reference_manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)["server_directory"]


def stage(stamp):
    """Copy the worker to the farm; returns its remote path."""
    remote_dir = "%s/%s" % (ROOT, stamp)
    ssh.check("mkdir -p %s/out && cat > %s/l2_recompute.py" % (remote_dir, remote_dir), stdin=open(WORKER, encoding="utf-8").read())
    return remote_dir


def smoke(replica):
    stamp = time.strftime("%Y%m%d_%H%M%S")
    remote_dir = stage(stamp)
    cmd = ("cd %s && %s %s/l2_recompute.py --archive %s --fitpack %s --out %s/out --first %d --last %d"
           % (remote_dir, ssh.container_python(FITPACK, env={"LHAPDF_DATA_PATH": LHAPDF}, read_only=False).replace(" -I -", " ").rstrip(),
              remote_dir, archive_dir(), FITPACK, remote_dir, replica, replica))
    # the worker is a file, not stdin: drop the trailing '-' of container_python
    cmd = cmd.replace("python3 -B  ", "python3 -B ")
    rc, out, err = ssh.run(cmd, timeout=3600)
    return {"stamp": stamp, "remote_dir": remote_dir, "rc": rc, "stdout": out[-2000:], "stderr": err[-3000:]}


def array(per_task, time_limit, mem, replicas=791):
    stamp = time.strftime("%Y%m%d_%H%M%S")
    remote_dir = stage(stamp)
    ntasks = (replicas + per_task - 1) // per_task
    script = """#!/bin/bash
#SBATCH --job-name=eval_l2
#SBATCH --partition=production
#SBATCH --time=%s
#SBATCH --mem=%s
#SBATCH --cpus-per-task=1
#SBATCH --array=0-%d
#SBATCH --output=/farm_out/%%u/eval_l2_%%A_%%a.out
set -euo pipefail
FIRST=$((SLURM_ARRAY_TASK_ID * %d)); LAST=$((FIRST + %d - 1))
cd %s
exec apptainer exec --no-home --cleanenv --containall --network none --bind /w/ceph24:/w/ceph24 \\
  --env OPENBLAS_NUM_THREADS=1 --env OMP_NUM_THREADS=1 --env PYTHONPATH=%s --env FITPACK=%s --env LHAPDF_DATA_PATH=%s \\
  %s python3 -B %s/l2_recompute.py --archive %s --fitpack %s --out %s/out --first $FIRST --last $LAST
""" % (time_limit, mem, ntasks - 1, per_task, per_task, remote_dir, FITPACK, FITPACK, LHAPDF, ssh.IMAGE, remote_dir, archive_dir(), FITPACK, remote_dir)
    ssh.check("cat > %s/array.sh" % remote_dir, stdin=script)
    rc, out, err = ssh.run("cd %s && sbatch --parsable array.sh" % remote_dir, timeout=120)
    return {"stamp": stamp, "remote_dir": remote_dir, "tasks": ntasks, "rc": rc, "job": out.strip(), "stderr": err[-500:]}


def fetch(stamp):
    remote_dir = "%s/%s/out" % (ROOT, stamp)
    local = paths.eval_dir("l2", stamp)
    rc, out, _ = ssh.run("ls %s/*.json 2>/dev/null" % remote_dir)
    files = [ln.strip() for ln in out.splitlines() if ln.strip()]
    for f in files:
        with open(os.path.join(local, os.path.basename(f)), "w", encoding="utf-8") as fh:
            fh.write(ssh.read_text(f, timeout=600))
    return {"stamp": stamp, "files": len(files), "local": local}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evaluation.l2.submit_l2")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("smoke"); s.add_argument("--replica", type=int, default=0)
    a = sub.add_parser("array"); a.add_argument("--per-task", type=int, default=20); a.add_argument("--time", default="03:00:00"); a.add_argument("--mem", default="8G")
    f = sub.add_parser("fetch"); f.add_argument("--stamp", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "smoke":
        print(json.dumps(smoke(args.replica), indent=1))
    elif args.cmd == "array":
        print(json.dumps(array(args.per_task, args.time, args.mem), indent=1))
    else:
        print(json.dumps(fetch(args.stamp), indent=1))


if __name__ == "__main__":
    main()
