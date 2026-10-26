"""Command line for the L1 grader.

    python -m evaluation.l1.cli dump --local INPUT [INPUT ...] [--fitpack-root DIR] [--cwd DIR] > dump.json
    python -m evaluation.l1.cli program --request request.json > program.py   (to ship to the farm)
    python -m evaluation.l1.cli grade --dump dump.json [--condition A|B|C] [--target JAM24.final] > grade.json
    python -m evaluation.l1.cli expected [--target JAM24.final] [--kind literal|reproduction] > conf.json

`dump --local` executes the candidate on this machine in a fresh, isolated
interpreter (python3 -I); a candidate input is a program an agent wrote, so run
it only where that is acceptable (the farm's container, or a scratch copy).
"""
import argparse
import json
import os
import subprocess
import sys

from evaluation.l1 import expected, grade

HERE = os.path.dirname(os.path.abspath(__file__))


def program(request):
    """The remote_dump.py source with the request filled in."""
    with open(os.path.join(HERE, "remote_dump.py"), encoding="utf-8") as fh:
        src = fh.read()
    return src.replace("__REQUEST__  # noqa: E999 - filled in before the program is sent",
                   "json.loads(%s)" % json.dumps(json.dumps(request)))


def dump_local(inputs, fitpack_root=None, cwd=None, timeout=600):
    request = {"inputs": [os.path.abspath(p) for p in inputs], "fitpack_root": fitpack_root, "cwd": cwd}
    proc = subprocess.run([sys.executable, "-B", "-I", "-"], input=program(request), capture_output=True,
                          text=True, timeout=timeout, cwd=cwd or None)
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    if proc.returncode != 0 or not lines:
        raise RuntimeError("dumper failed: " + (proc.stderr or proc.stdout)[-1500:])
    return json.loads(lines[-1])


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evaluation.l1.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump")
    d.add_argument("--local", nargs="+", required=True, metavar="INPUT")
    d.add_argument("--fitpack-root")
    d.add_argument("--cwd")
    p = sub.add_parser("program")
    p.add_argument("--request", required=True)
    g = sub.add_parser("grade")
    g.add_argument("--dump", required=True)
    g.add_argument("--condition", default="A")
    g.add_argument("--target", default="JAM24.final")
    e = sub.add_parser("expected")
    e.add_argument("--target", default="JAM24.final")
    e.add_argument("--kind", default="literal", choices=["literal", "reproduction"])
    args = ap.parse_args(argv)
    if args.cmd == "dump":
        print(json.dumps(dump_local(args.local, args.fitpack_root, args.cwd), default=str))
    elif args.cmd == "program":
        with open(args.request, encoding="utf-8") as fh:
            print(program(json.load(fh)))
    elif args.cmd == "grade":
        with open(args.dump, encoding="utf-8") as fh:
            dump = json.load(fh)
        print(json.dumps(grade.grade(dump, args.condition, args.target), default=str, indent=1))
    elif args.cmd == "expected":
        print(json.dumps(expected.expected_confs(args.target)[args.kind], default=str, indent=1))


if __name__ == "__main__":
    main()
