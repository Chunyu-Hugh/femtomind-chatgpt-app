"""Run the matrix: arms x conditions x gateway features x N repeats.

    python -m evaluation.matrix.runner --n 3 --arms nomcp mcp --conditions A B C --features ab [--dry-run] [--resume] [--only RUN ...]

For each run, in order: box.sh prepare (farm directory, local box with the
condition's materials); with MCP, a gateway at the pinned commit for the
features (t: tools only, a: +A, ab: +A+B) with the condition's catalog
variant and deny list, and a preflight (hidden tools absent, workspace set,
chain up); box.sh run (claude in the sandbox, measured); in parallel the probe
watches for the job and cancels it once it has started fitting or failed;
then classify, fetch (artefacts, L1 grade, leak scan), gateway stop (audit
log archived), and RUN.summary.json. The matrix manifest records the prompts'
hashes, the catalog references, the gateway commits, the FitPack copies and
claude's version. Runs are sequential: the account, the farm quota and the
gateway's recorded workspace are shared.

Nothing here touches the pilot (~/bench_nomcp, bench_nomcp/, its farm
directories or jobs); box.sh refuses to start while the pilot runs.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

from evaluation.common import paths
from evaluation.l1 import expected
from evaluation.matrix import classify, fetch, gateway, probe

HERE = os.path.dirname(os.path.abspath(__file__))
BOX = os.path.join(HERE, "box.sh")
RESULTS = os.path.join(paths.EVAL_ROOT, "results")
# gateway commits by feature set (B5 in the plan): the ablation is by commit, not by switch
GATEWAY_COMMITS = {"t": os.environ.get("EVAL_GW_COMMIT_T", "184246e"), "a": os.environ.get("EVAL_GW_COMMIT_A", "0aef921"),
                   "ab": os.environ.get("EVAL_GW_COMMIT_AB", "HEAD")}
VARIANT = {"A": "full", "B": "B", "C": "full"}   # A hides the catalog tools; B serves the derived catalog
PORT = int(os.environ.get("EVAL_GW_PORT", "8766"))


def run_name(arm, condition, features, repeat):
    return "%s-%s-%s-r%02d" % (arm, condition, "x" if arm == "nomcp" else features, repeat)


def plan(n, arms, conditions, features):
    runs = []
    for repeat in range(1, n + 1):
        for condition in conditions:
            for arm in arms:
                for f in (features if arm == "mcp" else ["x"]):
                    name = run_name(arm, condition, f, repeat)
                    if name not in runs:
                        runs.append(name)
    return runs


def manifest(args, runs):
    with open(os.path.join(paths.APP_ROOT, "evaluation", "prompts", "MANIFEST.json"), encoding="utf-8") as fh:
        prompts = json.load(fh)
    store = expected.catalog_store()
    refs = {t: expected.compiled(t, store)[1] for t in expected.TARGETS}
    try:
        claude = subprocess.check_output(["claude", "--version"], text=True, timeout=30).strip()
    except (OSError, subprocess.SubprocessError):
        claude = None
    commits = {}
    for f in args.features:
        try:
            commits[f] = subprocess.check_output(["git", "-C", paths.APP_ROOT, "rev-parse", GATEWAY_COMMITS[f]], text=True).strip()
        except subprocess.CalledProcessError:
            commits[f] = GATEWAY_COMMITS[f]
    return {"schema": "femtomind.matrix_manifest.v1", "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "n": args.n, "arms": args.arms, "conditions": args.conditions, "features": args.features, "runs": runs,
            "model": os.environ.get("EVAL_MODEL", "claude-opus-5-5"), "timeout": os.environ.get("EVAL_TIMEOUT", "3h"),
            "probe_minutes": args.probe_minutes, "prompts": prompts,
            "gateway_confirm_wait_seconds": int(os.environ.get("EVAL_GW_CONFIRM_WAIT", "0")),
            "note": "the gateway's review delay before a confirmation (20 s in production) is set as recorded: "
                    "the prompt makes the assistant the only actor", "catalog": refs, "gateway_commits": commits,
            "fitpack_main": os.environ.get("EVAL_FITPACK_MAIN", "/w/ceph24/JAM/chunyu/bench_nomcp/fitpack"),
            "fitpack_ai": os.environ.get("EVAL_FITPACK_AI", "/w/ceph24/JAM/chunyu/bench_mcp/fitpack"),
            "claude_version": claude, "app_head": subprocess.check_output(["git", "-C", paths.APP_ROOT, "rev-parse", "HEAD"], text=True).strip()}


def box(*argv, check=True):
    r = subprocess.run([BOX, *argv], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError("box.sh %s failed (%d): %s" % (" ".join(argv), r.returncode, (r.stderr or r.stdout)[-2000:]))
    return r


def one(run, args):
    meta = probe.parse_run(run)
    arm, condition, features = meta["arm"], meta["condition"], meta["features"]
    out_dir = os.path.join(RESULTS, run)
    os.makedirs(out_dir, exist_ok=True)
    summary = {"schema": "femtomind.run_summary.v1", "run": run, **meta, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    log = open(os.path.join(out_dir, "runner.log"), "a", encoding="utf-8")

    def say(msg):
        line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()
    say("prepare " + run)
    summary["prepare"] = box("prepare", run).stdout.strip()[-800:]
    gw = None
    if arm == "mcp":
        say("gateway start (%s, variant %s)" % (GATEWAY_COMMITS[features], VARIANT[condition]))
        gw = gateway.start("%s_%s" % (features, VARIANT[condition]), GATEWAY_COMMITS[features], VARIANT[condition], condition, PORT, out_dir)
        summary["gateway"] = {k: gw.get(k) for k in ("label", "sha", "variant", "server_side_deny", "capabilities", "tools")}
        pre = gateway.preflight(PORT, condition, "/w/ceph24/JAM/chunyu/bench_eval/mcp/" + run)
        summary["preflight"] = pre
        if not pre["ok"]:
            say("preflight failed: " + json.dumps({k: pre[k] for k in ("visible_denied", "check_hpc_connection")}))
            gateway.stop(PORT, out_dir)
            summary["aborted"] = "preflight"
            _write(out_dir, run, summary)
            return summary
    result = {}

    def watch():
        result["probe"] = probe.probe(run, args.probe_minutes, args.probe_interval, not args.no_cancel)
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    say("run (claude, timeout %s)" % os.environ.get("EVAL_TIMEOUT", "3h"))
    r = box("run", run, check=False)
    summary["box_rc"] = r.returncode
    summary["box_tail"] = (r.stdout + r.stderr)[-3000:]
    transcript = os.path.join(RESULTS, run + ".jsonl")
    started = _read(os.path.join(RESULTS, run + ".started_at"))
    ended = _read(os.path.join(RESULTS, run + ".ended_at"))
    rc = _read(os.path.join(RESULTS, run + ".rc"))
    say("classify")
    if os.path.isfile(transcript):
        summary["classify"] = classify.classify(transcript, arm, started, ended, int(rc) if rc and rc.isdigit() else None)
        with open(os.path.join(out_dir, "classify.json"), "w", encoding="utf-8") as fh:
            json.dump(summary["classify"], fh, indent=1)
    say("waiting for the probe (up to %.0f min)" % args.probe_minutes)
    watcher.join(timeout=args.probe_minutes * 60 + 120)
    summary["probe"] = result.get("probe") or {"verdict": "probe_did_not_finish"}
    with open(os.path.join(out_dir, "probe.json"), "w", encoding="utf-8") as fh:
        json.dump(summary["probe"], fh, indent=1, default=str)
    say("fetch, grade, leak scan")
    try:
        f = fetch.fetch(run, transcript, out_dir)
        summary["fetch"] = {k: f.get(k) for k in ("inputs", "requested_target", "campaign_folder", "job_script", "dump")}
        summary["grade"] = f.get("grade")
        summary["leak"] = f.get("leak")
    except Exception as exc:  # noqa: BLE001 - the run is still recorded
        summary["fetch"] = {"error": "%s: %s" % (type(exc).__name__, exc)}
    if gw:
        summary["gateway_stop"] = gateway.stop(PORT, out_dir)
    summary["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write(out_dir, run, summary)
    say("done: %s" % json.dumps({"accepted": (summary.get("classify") or {}).get("n_successful_executions"),
                                  "probe": summary["probe"].get("verdict"), "grade": summary.get("grade"),
                                  "leak": (summary.get("leak") or {}).get("verdict")}, default=str))
    log.close()
    return summary


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _write(out_dir, run, summary):
    for path in (os.path.join(out_dir, "summary.json"), os.path.join(RESULTS, run + ".summary.json")):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=1, sort_keys=True, default=str)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evaluation.matrix.runner")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--arms", nargs="+", default=["nomcp", "mcp"], choices=["nomcp", "mcp"])
    ap.add_argument("--conditions", nargs="+", default=["A", "B", "C"], choices=["A", "B", "C"])
    ap.add_argument("--features", nargs="+", default=["ab"], choices=list(GATEWAY_COMMITS))
    ap.add_argument("--probe-minutes", type=float, default=90)
    ap.add_argument("--probe-interval", type=float, default=60)
    ap.add_argument("--no-cancel", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resume", action="store_true", help="skip runs that have a summary")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args(argv)
    runs = plan(args.n, args.arms, args.conditions, args.features)
    if args.only:
        runs = [r for r in runs if r in args.only] or list(args.only)
    os.makedirs(RESULTS, exist_ok=True)
    m = manifest(args, runs)
    if args.dry_run:
        print(json.dumps(m, indent=1, default=str))
        return
    with open(os.path.join(RESULTS, "matrix_manifest_%s.json" % time.strftime("%Y%m%d_%H%M%S")), "w", encoding="utf-8") as fh:
        json.dump(m, fh, indent=1, default=str)
    for run in runs:
        if args.resume and os.path.isfile(os.path.join(RESULTS, run + ".summary.json")):
            print("skip", run, "(summary exists)")
            continue
        try:
            one(run, args)
        except Exception as exc:  # noqa: BLE001 - go on with the next run, the failure is on record
            print("FAILED", run, type(exc).__name__, exc, file=sys.stderr)
            with open(os.path.join(RESULTS, run + ".failed.json"), "w", encoding="utf-8") as fh:
                json.dump({"run": run, "error": "%s: %s" % (type(exc).__name__, exc)}, fh)
    from evaluation.matrix import aggregate
    agg = aggregate.aggregate(aggregate.load(RESULTS))
    with open(os.path.join(RESULTS, "aggregate.json"), "w", encoding="utf-8") as fh:
        json.dump(agg, fh, indent=1, sort_keys=True, default=str)
    with open(os.path.join(RESULTS, "aggregate.md"), "w", encoding="utf-8") as fh:
        fh.write(aggregate.markdown(agg))
    print(aggregate.markdown(agg))


if __name__ == "__main__":
    main()
