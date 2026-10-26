"""Collect what a run produced on the farm, grade it, scan it.

    python -m evaluation.matrix.fetch --run RUN --transcript RUN.jsonl --out DIR

Without MCP the artefact is the agent's job script and the FitPack inputs it
runs: the launcher the sandbox wrote (state/RUN.launcher.sh) names the
script; the inputs are the .py files its python lines name, in order, that
exist in the run directory (a generator may have written them at job time);
if that finds nothing, every top-level and inputs/*.py file is taken in
name order. With MCP it is the campaign the gateway materialized: the
registry names the folder; stages/pdf_stage/input.py, the resolved manifest,
the lock, the card and the Slurm script are copied.

The inputs are loaded on the farm, inside the container, by the L1 dumper
(evaluation/l1/remote_dump.py) with the run's own FitPack on the path, and
graded against the catalog (evaluation/l1/grade.py); the transcript is
scanned for leaks (evaluation/conditions/leak_scan.py). DIR receives
artefacts/*, dump.json.gz, grade.json and leak.json.
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import shlex

from evaluation.common import ssh
from evaluation.conditions import leak_scan
from evaluation.l1 import cli, grade
from evaluation.matrix.probe import ROOT, parse_run

MCP_FILES = ("stages/pdf_stage/input.py", "stages/pdf_stage/manifest.resolved.yaml", "campaign.lock.json",
             "campaign.request.json", "campaign.yaml", "materialize.json")


def _cat(path):
    rc, out, _ = ssh.run("cat %s 2>/dev/null" % shlex.quote(path), timeout=120)
    return out if rc == 0 else None


def _exists(paths):
    if not paths:
        return []
    cmd = "; ".join("test -f %s && echo %s" % (shlex.quote(p), shlex.quote(p)) for p in paths)
    rc, out, _ = ssh.run(cmd)
    return [ln for ln in out.splitlines() if ln.strip()]


def script_candidates(script, run_dir):
    """The input files a job script runs, as glob patterns in order: python
    lines' .py arguments after the program, with the script's own VAR=value
    assignments expanded, loop variables widened to '*', and relative names
    resolved against the directory the script cd'ed into (else the run dir)."""
    assigns = {"WD": run_dir, "WORK": run_dir, "RUN": run_dir, "PWD": run_dir}
    for m in re.finditer(r"^\s*(?:export\s+)?([A-Za-z_]\w*)=([^\s;]+)", script, re.MULTILINE):
        assigns.setdefault(m.group(1), m.group(2))

    def expand(tok, rounds=4):
        for _ in range(rounds):
            new = re.sub(r"\$\{?([A-Za-z_]\w*)\}?", lambda m: assigns.get(m.group(1), "*"), tok)
            if new == tok:
                break
            tok = new
        return tok.strip("\"'")
    cwd = run_dir
    out = []
    for line in script.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        m = re.match(r"cd\s+(\S+)", text)
        if m:
            target = expand(m.group(1))
            cwd = target if target.startswith("/") else cwd + "/" + target
            continue
        if re.search(r"\bpython[0-9.]*\b", text):
            toks = re.findall(r"[\w./${}\"'-]+\.py\b", text)
            for tok in toks[1:]:
                tok = expand(tok)
                if re.search(r"/fitpack/", tok):
                    continue
                out.append(tok if tok.startswith("/") else cwd + "/" + tok.lstrip("./"))
    seen, ordered = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def nomcp_inputs(run):
    """(job_script_path, script_text, [input paths in order], how)"""
    run_dir = "%s/nomcp/%s" % (ROOT, run)
    launcher = _cat("%s/state/%s.launcher.sh" % (ROOT, run)) or ""
    m = re.search(r"\sbash\s+(\S+\.sh)\s*$", launcher.strip().splitlines()[-1] if launcher.strip() else "")
    script_path = m.group(1) if m else None
    script = _cat(script_path) if script_path else None
    present, how = [], "job script python lines (variables, cd and loops expanded)"
    if script:
        patterns = script_candidates(script, run_dir)
        if patterns:
            cmd = "; ".join("ls -1 %s 2>/dev/null" % p for p in patterns)   # globs expand on the host
            rc, out, _ = ssh.run(cmd)
            for ln in out.splitlines():
                ln = ln.strip()
                if ln and ln not in present:
                    present.append(ln)
            # a loop's glob lists its files in name order, which is the chain's order
    if not present:
        rc, out, _ = ssh.run("find %s -maxdepth 3 -name '*.py' -not -path '*/fitpack/*' -not -path '*/scratch/*' 2>/dev/null | sort"
                             % shlex.quote(run_dir))
        present = [ln.strip() for ln in out.splitlines() if ln.strip()]
        how = "all *.py under the run directory (depth 3, not fitpack/ or scratch/) by name"
    return script_path, script, present, how


def mcp_folder(run):
    ws = "%s/mcp/%s" % (ROOT, run)
    text = _cat(ws + "/campaigns/collections.json")
    if not text:
        return ws, None, {}
    try:
        reg = json.loads(text)
    except ValueError:
        return ws, None, {"error": "registry unreadable"}
    folders = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("folder", "campaign_folder") and isinstance(v, str):
                    folders.append(os.path.basename(v))
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(reg)
    return ws, (folders[-1] if folders else None), {"folders": folders}


def fetch(run, transcript, out_dir):
    meta = parse_run(run)
    arm, condition = meta["arm"], meta["condition"]
    os.makedirs(os.path.join(out_dir, "artefacts"), exist_ok=True)
    report = {"schema": "femtomind.fetch.v1", "run": run, **meta, "artefacts": {}, "inputs": []}
    inputs, fitpack_root, target = [], None, "JAM24.final"
    if arm == "nomcp":
        script_path, script, inputs, how = nomcp_inputs(run)
        report.update(job_script=script_path, inputs_found_by=how)
        if script:
            _save(out_dir, report, "job_script.sh", script)
        fitpack_root = "%s/nomcp/%s/fitpack" % (ROOT, run)
    else:
        ws, folder, info = mcp_folder(run)
        report.update(workspace=ws, campaign_folder=folder, registry=info)
        fitpack_root = ws
        if folder:
            for rel in MCP_FILES:
                text = _cat("%s/campaigns/%s/%s" % (ws, folder, rel))
                if text is not None:
                    _save(out_dir, report, rel.replace("/", "__"), text)
                    if rel == "campaign.request.json":
                        m = re.search(r'"analysis_id":\s*"(JAM24\.\w+)"', text)
                        if m:
                            target = m.group(1)
            rc, out, _ = ssh.run("ls %s/campaigns/%s/stages/pdf_stage/slurm/*.sh 2>/dev/null" % (shlex.quote(ws), shlex.quote(folder)))
            for p in out.splitlines():
                if p.strip():
                    text = _cat(p.strip())
                    if text is not None:
                        _save(out_dir, report, "slurm__" + os.path.basename(p.strip()), text)
            inputs = _exists(["%s/campaigns/%s/stages/pdf_stage/input.py" % (ws, folder)])
    report["inputs"] = inputs
    report["requested_target"] = target
    if inputs:
        program = cli.program({"inputs": inputs, "fitpack_root": fitpack_root, "cwd": os.path.dirname(inputs[-1])})
        out = ssh.python(program, container=True, fitpack_root=fitpack_root, timeout=900)
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        dump = json.loads(lines[-1])
        with gzip.open(os.path.join(out_dir, "dump.json.gz"), "wt", encoding="utf-8") as fh:
            json.dump(dump, fh)
        report["dump"] = {"inputs": [{k: r.get(k) for k in ("path", "sha256", "loaded", "error")} for r in dump.get("inputs", [])],
                          "error": dump.get("error"), "coercions": len(dump.get("coercions") or [])}
        try:
            g = grade.grade(dump, condition, target)
            with open(os.path.join(out_dir, "grade.json"), "w", encoding="utf-8") as fh:
                json.dump(g, fh, indent=1, sort_keys=True, default=str)
            report["grade"] = g["verdict"]
        except ValueError as exc:
            report["grade"] = {"error": str(exc)}
    else:
        report["grade"] = {"error": "no input to grade"}
    if transcript and os.path.isfile(transcript):
        leak = leak_scan.scan(transcript, arm, condition)
        with open(os.path.join(out_dir, "leak.json"), "w", encoding="utf-8") as fh:
            json.dump(leak, fh, indent=1)
        report["leak"] = {"verdict": leak["verdict"], "counts": leak["counts"]}
    with open(os.path.join(out_dir, "fetch.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, sort_keys=True, default=str)
    return report


def _save(out_dir, report, name, text):
    path = os.path.join(out_dir, "artefacts", name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    report["artefacts"][name] = {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "bytes": len(text.encode("utf-8"))}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evaluation.matrix.fetch")
    ap.add_argument("--run", required=True)
    ap.add_argument("--transcript")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    print(json.dumps(fetch(args.run, args.transcript, args.out), indent=1, default=str))


if __name__ == "__main__":
    main()
