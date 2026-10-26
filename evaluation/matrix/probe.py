"""Watch the one job a run submitted, say what it did, and cancel it.

    python -m evaluation.matrix.probe --run RUN [--minutes 90] [--interval 60] [--no-cancel] > probe.json

Every interval the probe reads the job's Slurm state (squeue, sacct), the
tail of its log and, with MCP, the replica's progress file, and decides:

  started_fitting     the optimizer is running (chi2 lines in the log, or the
                      progress file says optimizing);
  failed_before_fit   the job ended or crashed before that (a traceback, or
                      sacct FAILED / OUT_OF_MEMORY / TIMEOUT / CANCELLED);
  pending_timeout     still queued when the minutes ran out;
  never_submitted     no job id appeared.

Once decided (or at the deadline) the job is cancelled, since the matrix
measures the way to a running fit, not the fit. Only a job the run itself
recorded is ever cancelled: the id comes from this run's state file (no-MCP)
or its workspace's campaign registry (MCP), and sacct must show it as this
user's. Nothing else on the farm is touched.
"""
import argparse
import json
import os
import re
import shlex
import time

from evaluation.common import ssh

ROOT = "/w/ceph24/JAM/chunyu/bench_eval"
LOGS = "/farm_out/chunyu"
FIT_MARK = re.compile(r"^\s*chi2\s*=\s*[-0-9.]", re.MULTILINE)
LOAD_MARK = re.compile(r"loading \w+ data sets", re.IGNORECASE)
ENDED = ("FAILED", "OUT_OF_MEMORY", "TIMEOUT", "CANCELLED", "NODE_FAIL", "COMPLETED", "PREEMPTED", "DEADLINE")


def parse_run(run):
    m = re.fullmatch(r"(nomcp|mcp)-([ABC])-([a-z0-9]+)-r(\d{2})", run)
    if not m:
        raise ValueError("bad run name: " + run)
    return {"arm": m.group(1), "condition": m.group(2), "features": m.group(3), "repeat": int(m.group(4))}


def _digits(value):
    return re.findall(r"\b(\d{6,9})\b", value or "")


def job_of(run, arm):
    """(slurm_job_id, details) from the run's own records, or (None, details)."""
    if arm == "nomcp":
        rc, out, _ = ssh.run("cat %s/state/%s.jobid 2>/dev/null" % (ROOT, shlex.quote(run)))
        ids = _digits(out) if rc == 0 else []
        return (ids[0] if ids else None), {"source": "state/%s.jobid" % run, "log": None if not ids else
                                           "%s/eval_%s_%s.out" % (LOGS, run, ids[0])}
    ws = "%s/mcp/%s" % (ROOT, run)
    rc, out, _ = ssh.run("cat %s/campaigns/collections.json 2>/dev/null" % shlex.quote(ws))
    if rc != 0 or not out.strip():
        return None, {"source": ws + "/campaigns/collections.json", "present": False}
    try:
        reg = json.loads(out)
    except ValueError:
        return None, {"source": ws + "/campaigns/collections.json", "present": True, "error": "unreadable"}
    found = []

    def walk(o, folder=None):
        if isinstance(o, dict):
            folder = o.get("folder") or o.get("campaign_folder") or folder
            for k, v in o.items():
                if k in ("slurm_job_id", "array_job_id", "job_id", "job", "jobs") and isinstance(v, (str, int)):
                    for d in _digits(str(v)):
                        found.append((d, folder))
                else:
                    walk(v, folder)
        elif isinstance(o, list):
            for v in o:
                walk(v, folder)
    walk(reg)
    if not found:
        return None, {"source": ws + "/campaigns/collections.json", "present": True, "jobs": 0}
    job, folder = found[-1]
    folder_name = os.path.basename(str(folder or ""))
    return job, {"source": ws + "/campaigns/collections.json", "present": True, "jobs": len(found), "folder": folder_name,
                 "log_glob": "%s/jam_%s_%s_*.out" % (LOGS, folder_name, job),
                 "progress": "%s/campaigns/%s/stages/pdf_stage/checkpoints/replica_1/current_progress.json" % (ws, folder_name)}


def look(job, details):
    """One observation of the job."""
    q = shlex.quote
    cmd = ("squeue -h -j %s -o '%%T|%%M|%%R' 2>/dev/null; echo '--sacct'; sacct -n -X -j %s -o State,Elapsed,ExitCode,User,JobName%%40 2>/dev/null"
           % (q(job), q(job)))
    if details.get("log"):
        cmd += "; echo '--log'; tail -n 60 %s 2>/dev/null" % q(details["log"])
    elif details.get("log_glob"):
        cmd += "; echo '--log'; for f in %s; do [ -f \"$f\" ] && tail -n 60 \"$f\"; done 2>/dev/null" % details["log_glob"]
    if details.get("progress"):
        cmd += "; echo '--progress'; cat %s 2>/dev/null" % q(details["progress"])
    rc, out, err = ssh.run(cmd, timeout=120)
    parts = re.split(r"^--(sacct|log|progress)$", out, flags=re.MULTILINE)
    obs = {"squeue": parts[0].strip(), "sacct": "", "log_tail": "", "progress": None, "ssh_rc": rc}
    for i in range(1, len(parts) - 1, 2):
        obs[parts[i] if parts[i] != "log" else "log_tail"] = parts[i + 1].strip()
    if obs.get("progress"):
        try:
            obs["progress"] = json.loads(obs["progress"])
        except ValueError:
            obs["progress"] = {"raw": obs["progress"][:300]}
    sacct = obs["sacct"].split()
    obs["state"] = (obs["squeue"].split("|")[0] if obs["squeue"] else (sacct[0] if sacct else "UNKNOWN")).split("+")[0]
    obs["user"] = sacct[3] if len(sacct) > 3 else None
    obs["job_name"] = sacct[4] if len(sacct) > 4 else None
    return obs


def verdict_of(obs):
    log = obs.get("log_tail") or ""
    prog = obs.get("progress") if isinstance(obs.get("progress"), dict) else {}
    if FIT_MARK.search(log) or prog.get("phase") in ("optimizing", "fitting") or prog.get("evaluations"):
        return "started_fitting"
    if "Traceback" in log or "Error" in log.split("\n")[-1:][0][:40]:
        return "failed_before_fit"
    if obs["state"] in ENDED:
        return "failed_before_fit"
    return None


def cancel(job, obs, me):
    if obs.get("user") and obs["user"] != me:
        return {"cancelled": False, "reason": "job belongs to %s" % obs["user"]}
    rc, out, err = ssh.run("scancel %s && echo cancelled" % shlex.quote(job))
    return {"cancelled": rc == 0 and "cancelled" in out, "output": (out + err)[-300:]}


def probe(run, minutes=90, interval=60, do_cancel=True, me="chunyu"):
    meta = parse_run(run)
    t0 = time.time()
    deadline = t0 + minutes * 60
    report = {"schema": "femtomind.probe.v1", "run": run, **meta, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "observations": [], "verdict": None, "job": None}
    job, details = None, {}
    while time.time() < deadline and not job:
        job, details = job_of(run, meta["arm"])
        if not job:
            time.sleep(min(interval, max(5, deadline - time.time())))
    report["job"], report["job_details"] = job, details
    if not job:
        report["verdict"] = "never_submitted"
        report["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return report
    verdict = None
    first_fit = None
    while True:
        obs = look(job, details)
        obs["t"] = round(time.time() - t0)
        verdict = verdict_of(obs)
        if verdict == "started_fitting" and first_fit is None:
            first_fit = obs["t"]
        report["observations"].append({k: v for k, v in obs.items() if k != "log_tail"} | {"log_tail": obs["log_tail"][-1500:]})
        if verdict or time.time() >= deadline:
            break
        time.sleep(interval)
    if not verdict:
        verdict = "pending_timeout" if obs["state"] in ("PENDING", "UNKNOWN", "") else "running_no_fit_at_deadline"
    report["verdict"] = verdict
    report["seconds_to_verdict"] = round(time.time() - t0)
    report["seconds_to_first_fit_mark"] = first_fit
    report["final_state"] = obs["state"]
    if do_cancel and obs["state"] not in ENDED:
        report["cancel"] = cancel(job, obs, me)
    report["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evaluation.matrix.probe")
    ap.add_argument("--run", required=True)
    ap.add_argument("--minutes", type=float, default=90)
    ap.add_argument("--interval", type=float, default=60)
    ap.add_argument("--no-cancel", action="store_true")
    ap.add_argument("--user", default=os.environ.get("EVAL_FARM_USER", "chunyu"))
    args = ap.parse_args(argv)
    print(json.dumps(probe(args.run, args.minutes, args.interval, not args.no_cancel, args.user), indent=1, default=str))


if __name__ == "__main__":
    main()
