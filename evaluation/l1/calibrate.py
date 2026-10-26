"""Calibrate the L1 grader against the things whose grade is known.

    FEMTOMIND_IFARM_SOCK=~/.ssh/controlmasters/ifarm-eval \\
    python -m evaluation.l1.calibrate --out evaluation/l1/calibration/<date> [--offline]

Six checks, each a grade report plus a verdict on what it had to show:

  1 official   the official JAM24 input (read on the farm; its sha256 is
               compared with the one the catalog recorded, and any drift
               since the catalog read is reported, not hidden) against the
               literal conf of JAM24.final: Tier 0 empty means the catalog is
               the official input, key for key.
  2 bundle     the conf FitPack saved into the fit's parameter bundle
               (replicas_cut['conf']) against the same literal conf: the
               top-level keys only the saved conf has are what FitPack adds
               at run time, and become RUNTIME_KEYS.
  3 overlay    literal vs reproduction conf of JAM24.final: exactly the 650
               author-clarification paths (offline).
  4 compiler   the compiler's rendered input, loaded as FitPack would load it,
               against the reproduction conf: Tier 0 empty (offline).
  5 pilot-mcp  the pilot's MCP-side campaign input (run3) against the literal
               conf of the target it was built from: the differences are the
               MCP side's by-construction differences (ftol, jamai keys ...).
  6 pilot-nomcp the pilot's no-MCP seven-step chain (run3), the last step
               graded, against JAM24.final: a real agent artefact end to end.

Everything is written under --out: calibration.json (all verdicts), one
grade.<check>.json per check, dumps/<check>.json.gz, and summary.md.
"""
import argparse
import gzip
import json
import os
import sys
import tempfile
import time

from evaluation.common import ssh
from evaluation.common.paths import APP_ROOT
from evaluation.l1 import canonicalize, cli, expected, grade
from jamai import compiled_input

OFFICIAL_INPUT = "/w/ceph24/JAM/treyand/pdf_plotting/strange_analysis/combined4/2-0/input.py"
OFFICIAL_FITPACK = "/w/ceph24/JAM/treyand/fitpack_legacy_mcp"
PILOT_MCP_INPUT = "/w/ceph24/JAM/chunyu/bench_mcp/run3/campaigns/jam24_run3_20261024_231619/stages/pdf_stage/input.py"
PILOT_MCP_FITPACK = "/w/ceph24/JAM/chunyu/bench_mcp/run3"   # the campaign ran with FITPACK=<workspace>
PILOT_NOMCP_ROOT = "/w/ceph24/JAM/chunyu/bench_nomcp/run3"
PILOT_NOMCP_CHAIN = [PILOT_NOMCP_ROOT + "/inputs/step%02d.py" % i for i in range(1, 8)]


def _catalog_official_record():
    """What the catalog recorded about the official input when it was read."""
    with open(os.path.join(APP_ROOT, "analyses", "JAM24", "catalog", "analysis_config.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    found = []

    def walk(o):
        if isinstance(o, dict):
            if str(o.get("path", "")).endswith("2-0/input.py") and "sha256" in o:
                found.append({k: o[k] for k in ("path", "sha256", "size_bytes", "mtime_ns", "read_utc") if k in o})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(doc)
    return found[0] if found else {}


def _bundle_path():
    with open(os.path.join(APP_ROOT, "analyses", "JAM24", "results", "reference_manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)["server_directory"] + "/original_parameter_bundle.npy.gz"


def _remote_dump(request, fitpack_root):
    program = cli.program(request)
    out = ssh.python(program, container=True, fitpack_root=fitpack_root, timeout=900)
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


def _remote_stat(path):
    rc, out, err = ssh.run("stat -c '%%s %%Y' %s && sha256sum %s" % (ssh.shlex.quote(path), ssh.shlex.quote(path)))
    if rc:
        return {"readable": False, "error": err[-500:]}
    size_mtime, sha = out.strip().splitlines()[:2]
    size, mtime = size_mtime.split()
    return {"readable": True, "bytes": int(size), "mtime_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(mtime))),
            "sha256": sha.split()[0]}


def _save(out_dir, name, dump, report):
    os.makedirs(os.path.join(out_dir, "dumps"), exist_ok=True)
    with gzip.open(os.path.join(out_dir, "dumps", name + ".json.gz"), "wt", encoding="utf-8") as fh:
        json.dump(dump, fh)
    with open(os.path.join(out_dir, "grade.%s.json" % name), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, sort_keys=True, default=str)


def _paths(diffs):
    return sorted({d["path"] for d in diffs})


def check_official(targets, out_dir):
    record = _catalog_official_record()
    stat = _remote_stat(OFFICIAL_INPUT)
    result = {"path": OFFICIAL_INPUT, "catalog_record": record, "now": stat}
    if not stat.get("readable"):
        result["verdict"] = "unreadable: fell back to the bundle conf (check 2)"
        return result
    result["drift_since_catalog_read"] = stat["sha256"] != record.get("sha256")
    dump = _remote_dump({"inputs": [OFFICIAL_INPUT], "fitpack_root": OFFICIAL_FITPACK}, OFFICIAL_FITPACK)
    report = grade.grade(dump, "A", "JAM24.final", targets)
    _save(out_dir, "official", dump, report)
    t0 = report["tier0"]
    result.update(loaded=dump["inputs"][0].get("loaded"), coercions=len(dump.get("coercions") or []),
                  tier0_vs_literal_final=t0["JAM24.final"]["n"], tier0_vs_literal_current=t0["JAM24.current"]["n"],
                  tier1_vs_literal_final=report["tier1"]["JAM24.final"]["n"],
                  tier2_final=report["tier2"]["JAM24.final"]["fraction"],
                  overlay_paths_present=report["reproduction_overlay_view"]["matched"],
                  free_parameters=report["verdict"]["free_parameters"],
                  differences_vs_literal_final=_paths(t0["JAM24.final"]["differences"])[:100],
                  differences_vs_literal_current=_paths(t0["JAM24.current"]["differences"])[:100])
    if result["tier0_vs_literal_final"] == 0:
        result["verdict"] = "catalog == official input (Tier 0 empty)"
    elif result["drift_since_catalog_read"]:
        result["verdict"] = ("official input changed after the catalog read (%s -> %s); the Tier 0 differences are "
                             "that drift" % (record.get("sha256", "?")[:8], stat["sha256"][:8]))
    else:
        result["verdict"] = "MISMATCH: same file as the catalog read, yet Tier 0 differs"
    return result


def check_bundle(targets, out_dir):
    path = _bundle_path()
    result = {"path": path}
    dump = _remote_dump({"bundle": path, "fitpack_root": OFFICIAL_FITPACK}, OFFICIAL_FITPACK)
    if not dump["inputs"][0].get("loaded"):
        result.update(verdict="bundle not loadable", error=dump["inputs"][0].get("error"))
        return result
    candidate = grade.candidate_conf(dump)
    literal = targets["JAM24.final"]["literal"]
    extra_top = sorted(k for k in candidate if k not in literal)
    missing_top = sorted(k for k in literal if k not in candidate)
    # strip the runtime keys and look at what remains
    rest = {k: v for k, v in candidate.items() if k not in extra_top}
    diffs = expected.differences(literal, rest)
    report = grade.grade(dump, "A", "JAM24.final", targets)
    _save(out_dir, "bundle", dump, report)
    result.update(loaded=True, bundle_keys=dump["inputs"][0].get("keys"), coercions=dump.get("coercions"),
                  runtime_keys=extra_top, missing_top_level_keys=missing_top,
                  differences_after_runtime_keys_removed=diffs[:100],
                  n_differences_after_runtime_keys_removed=len(diffs),
                  overlay_paths_present=report["reproduction_overlay_view"]["matched"])
    if not diffs:
        result["verdict"] = "saved conf == literal catalog conf + %d runtime keys" % len(extra_top)
    else:
        result["verdict"] = ("saved conf differs from the literal catalog conf at %d paths besides the %d runtime keys"
                             % (len(diffs), len(extra_top)))
    with open(os.path.join(out_dir, "runtime_keys.json"), "w", encoding="utf-8") as fh:
        json.dump({"runtime_keys": extra_top, "source": path, "bundle_sha256": dump["inputs"][0].get("sha256"),
                   "note": "top-level keys of replicas_cut['conf'] absent from the official input; FitPack adds them at run time"},
                  fh, indent=1)
    return result


def check_overlay(targets):
    t = targets["JAM24.final"]
    diffs = expected.differences(t["literal"], t["reproduction"])
    paths = _paths(diffs)
    by_top = {}
    for p in paths:
        by_top[p.split("/")[1]] = by_top.get(p.split("/")[1], 0) + 1
    return {"n_paths": len(paths), "by_top_level_key": by_top,
            "verdict": "exactly 650 overlay paths" if len(paths) == 650 else "UNEXPECTED overlay count %d" % len(paths)}


def check_compiler(targets, out_dir):
    store = expected.catalog_store()
    ir, refs = expected.compiled("JAM24.final", store)
    text = compiled_input.render_compiled_input(store, refs["compiled_ref"])["input_text"]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "compiled_final.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        dump = cli.dump_local([path])
    report = grade.grade(dump, "C", "JAM24.final", targets)
    _save(out_dir, "compiler", dump, report)
    n = report["tier0"]["JAM24.final"]["n"]
    return {"compiled_ref": refs["compiled_ref"], "rendered_sha256": dump["inputs"][0]["sha256"],
            "tier0_vs_reproduction_final": n, "overlay_paths_present": report["reproduction_overlay_view"]["matched"],
            "verdict": "compiler output == reproduction conf" if n == 0 else "MISMATCH at %d paths" % n}


def check_pilot_mcp(targets, out_dir):
    stat = _remote_stat(PILOT_MCP_INPUT)
    result = {"path": PILOT_MCP_INPUT, "now": stat}
    if not stat.get("readable"):
        result["verdict"] = "pilot MCP input not readable"
        return result
    dump = _remote_dump({"inputs": [PILOT_MCP_INPUT], "fitpack_root": PILOT_MCP_FITPACK}, PILOT_MCP_FITPACK)
    report = grade.grade(dump, "B", "JAM24.current", targets)
    _save(out_dir, "pilot_mcp", dump, report)
    result.update(loaded=dump["inputs"][0].get("loaded"), error=dump["inputs"][0].get("error"),
                  coercions=dump.get("coercions"), step_projection=dump.get("step_projection"))
    if not result["loaded"]:
        result["verdict"] = "pilot MCP input did not load"
        return result
    for name in ("JAM24.current", "JAM24.final"):
        result[name] = {"tier0": report["tier0"][name]["n"], "tier1": report["tier1"][name]["n"],
                        "tier2": report["tier2"][name]["fraction"],
                        "tier1_paths": _paths(report["tier1"][name]["differences"])[:120]}
    result["free_parameters"] = report["verdict"]["free_parameters"]
    result["verdict"] = ("MCP-side by-construction differences vs JAM24.current: %d Tier 1 paths; free parameters %s"
                         % (result["JAM24.current"]["tier1"], report["verdict"]["free_parameters"]))
    return result


def check_pilot_nomcp(targets, out_dir):
    present = [p for p in PILOT_NOMCP_CHAIN if ssh.readable(p)]
    result = {"chain": PILOT_NOMCP_CHAIN, "present": len(present)}
    if not present:
        result["verdict"] = "pilot no-MCP chain not readable"
        return result
    fitpack = PILOT_NOMCP_ROOT + "/fitpack"
    dump = _remote_dump({"inputs": present, "fitpack_root": fitpack}, fitpack)
    report = grade.grade(dump, "A", "JAM24.final", targets)
    _save(out_dir, "pilot_nomcp", dump, report)
    result.update(loaded=[r.get("loaded") for r in dump["inputs"]], graded=report["candidate"]["graded"],
                  step_projection=dump.get("step_projection"), coercions=len(dump.get("coercions") or []))
    if dump.get("error"):
        result.update(verdict="last step did not load", error=dump["error"])
        return result
    t2 = report["tier2"]["JAM24.final"]
    result.update(tier0=report["tier0"]["JAM24.final"]["n"], tier1=report["tier1"]["JAM24.final"]["n"],
                  tier2=t2["fraction"], free_parameters=report["verdict"]["free_parameters"],
                  datasets={k: {"expected": v["expected"], "found": v["found"]} for k, v in t2["datasets"].items()},
                  settings_mismatch=[k for k, v in t2["settings"].items() if not v["match"]],
                  verdict="graded the 7-step chain's last step: Tier 2 %.3f, free parameters %s"
                          % (t2["fraction"] or 0, report["verdict"]["free_parameters"]))
    return result


def summary_md(cal):
    lines = ["# L1 grader calibration (%s)" % cal["run_at"], ""]
    for name, r in cal["checks"].items():
        lines.append("## %s" % name)
        lines.append("")
        lines.append("**Verdict:** " + str(r.get("verdict")))
        lines.append("")
        for k in ("drift_since_catalog_read", "tier0_vs_literal_final", "tier0_vs_literal_current", "runtime_keys",
                  "n_differences_after_runtime_keys_removed", "n_paths", "tier0_vs_reproduction_final",
                  "free_parameters", "settings_mismatch", "step_projection"):
            if k in r:
                lines.append("- %s: `%s`" % (k, json.dumps(r[k], default=str)[:600]))
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evaluation.l1.calibrate")
    ap.add_argument("--out", required=True)
    ap.add_argument("--offline", action="store_true", help="only the checks that need no farm access (3, 4)")
    ap.add_argument("--only", nargs="*", default=None, choices=["official", "bundle", "overlay", "compiler", "pilot_mcp", "pilot_nomcp"])
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    targets = expected.all_targets()
    cal = {"schema": "femtomind.l1_calibration.v1", "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "targets": {k: v["refs"] for k, v in targets.items()},
           "runtime_keys_in_effect": sorted(canonicalize.RUNTIME_KEYS), "checks": {}}
    remote_checks = {"official": check_official, "bundle": check_bundle, "pilot_mcp": check_pilot_mcp,
                     "pilot_nomcp": check_pilot_nomcp}
    offline_checks = {"overlay": lambda t, o: check_overlay(t), "compiler": check_compiler}
    wanted = args.only or (list(offline_checks) if args.offline else ["official", "bundle", "overlay", "compiler",
                                                                       "pilot_mcp", "pilot_nomcp"])
    if any(w in remote_checks for w in wanted):
        cal["ssh"] = {"socket": ssh.SOCK, "host": ssh.HOST, "alive": ssh.alive()}
        if not cal["ssh"]["alive"]:
            sys.exit("no ControlMaster at %s; open one with ifarm_controlmaster.sh or set FEMTOMIND_IFARM_SOCK" % ssh.SOCK)
    for name in wanted:
        print("..", name, file=sys.stderr, flush=True)
        fn = remote_checks.get(name) or offline_checks[name]
        try:
            cal["checks"][name] = fn(targets, args.out)
        except Exception as exc:  # noqa: BLE001 - one failed check must not hide the others
            cal["checks"][name] = {"verdict": "FAILED: %s: %s" % (type(exc).__name__, exc)}
        print("   ", cal["checks"][name].get("verdict"), file=sys.stderr, flush=True)
    with open(os.path.join(args.out, "calibration.json"), "w", encoding="utf-8") as fh:
        json.dump(cal, fh, indent=1, sort_keys=True, default=str)
    with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(summary_md(cal))
    print(summary_md(cal))


if __name__ == "__main__":
    main()
