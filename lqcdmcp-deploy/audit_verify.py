#!/usr/bin/env python3
"""Check a gateway audit log's chain.

    python3 audit_verify.py ~/.jam_gateway/audit.jsonl [--anchor]

Every line of the log is a JSON record with a running number (seq) and the
sha256 of the previous line (prev). The chain is intact when every line's
prev is the sha256 of the line before it and the numbers rise by one; a
removed, edited or reordered line breaks it, and this prints where. The
gateway restarts the chain from the last line it finds, so a log spanning
several gateway runs is one chain. A line it could not parse at start-up is
followed by an audit.recover record, and the chain restarts after it.

--anchor prints "seq:sha256" of the last line: pasted somewhere else (a
notebook, a commit message) it later shows whether the log has been cut or
rewritten since. Nothing here imports the gateway, on purpose.
"""
import hashlib
import json
import sys


def verify(path):
    """Return (records, head_sha256, problems). problems is a list of
    (line number, what is wrong); the chain is intact when it is empty."""
    problems = []
    prev = "0" * 64
    seq = 0
    head = None
    with open(path, "rb") as fh:
        for n, raw in enumerate(fh, 1):
            line = raw.rstrip(b"\n")
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                problems.append((n, "not JSON"))
                prev, seq = None, None
                continue
            if rec.get("kind") == "audit.recover":
                # The gateway found an unparseable last line and restarted the chain here.
                prev = rec.get("prev")
                seq = int(rec.get("seq", 0)) - 1
            if prev is not None and rec.get("prev") != prev:
                problems.append((n, "prev does not match the line before (a line was removed, changed or reordered)"))
            if seq is not None and rec.get("seq") != seq + 1:
                problems.append((n, f"seq {rec.get('seq')} after {seq}"))
            prev = hashlib.sha256(line).hexdigest()
            seq = rec.get("seq") if isinstance(rec.get("seq"), int) else None
            head = (rec.get("seq"), prev)
    return head, problems


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 2
    head, problems = verify(argv[1])
    if problems:
        for n, what in problems[:20]:
            print(f"line {n}: {what}")
        if len(problems) > 20:
            print(f"... and {len(problems) - 20} more")
        print("chain BROKEN")
        return 1
    if head is None:
        print("empty log")
        return 0
    if "--anchor" in argv:
        print(f"{head[0]}:{head[1]}")
    else:
        print(f"chain intact: records={head[0]} head={head[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
