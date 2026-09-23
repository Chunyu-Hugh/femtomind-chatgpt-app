"""Tests for validation_registry (stamps, lineage, commit gate).

Run: python3 validation_registry_test.py
"""

import tempfile
from pathlib import Path

from validation_registry import (
    GateError,
    ValidationRegistry,
)

ROSTER = {
    "humans": ["nobuo.sato@jlab.org", "chunyu.hu@odu.edu"],
    "automated": ["compare_with_paper"],
}


def main():
    with tempfile.TemporaryDirectory() as td:
        reg = ValidationRegistry(path=Path(td) / "vr.jsonl", roster=ROSTER)
        art = Path(td) / "msr.bin"
        art.write_bytes(b"fake-msr-bytes")

        print("== stamp -> pending ==")
        s1 = reg.stamp(
            artifact=str(art),
            claim="JAM25 step-4 reproduces paper exp chi2/N within 0.03",
            method="compare_with_paper",
            evidence={"computed": 0.79, "target": 0.79},
            created_by="agent:codex",
        )
        assert s1["status"] == "pending"
        print("  OK", s1["stamp_id"])

        print("== commit gate blocks unvalidated promotion ==")
        try:
            reg.authorize_promotion(str(art), "publish as official prior")
            raise AssertionError("gate should have blocked")
        except GateError as e:
            print("  OK blocked:", str(e)[:80])

        print("== non-roster approver is refused ==")
        try:
            reg.approve(s1["stamp_id"], "random.person@example.com")
            raise AssertionError("should refuse non-roster approver")
        except GateError:
            print("  OK refused")

        print("== roster human accepts ==")
        reg.approve(s1["stamp_id"], "nobuo.sato@jlab.org", note="matches paper Table 4")
        assert reg.get(s1["stamp_id"])["status"] == "accepted"
        print("  OK")

        print("== gate now allows promotion ==")
        auth = reg.authorize_promotion(str(art), "publish as official prior")
        assert auth["stamp_id"] == s1["stamp_id"]
        print("  OK authorized by", auth["stamp_id"])

        print("== lineage chain ==")
        s2 = reg.stamp(
            artifact=str(art) + ".next",
            claim="child result builds on parent",
            method="human_review",
            parents=[s1["stamp_id"]],
        )
        lin = reg.lineage(s2["stamp_id"])
        assert [r["stamp_id"] for r in lin] == [s1["stamp_id"], s2["stamp_id"]]
        print("  OK chain:", [r["stamp_id"] for r in lin])

        print("== automated validator can accept ==")
        s3 = reg.stamp(str(art) + ".auto", "auto claim", method="per_point_diagnostic")
        reg.approve(s3["stamp_id"], "compare_with_paper")
        assert reg.get(s3["stamp_id"])["status"] == "accepted"
        print("  OK")

        print("== rejection only by humans ==")
        s4 = reg.stamp(str(art) + ".rej", "reject me", method="human_review")
        try:
            reg.reject(s4["stamp_id"], "compare_with_paper", "nope")
            raise AssertionError("automated must not reject")
        except GateError:
            print("  OK refused")
        reg.reject(s4["stamp_id"], "chunyu.hu@odu.edu", "not reproducible")
        assert reg.get(s4["stamp_id"])["status"] == "rejected"
        print("  OK rejected")

        print("\nALL VALIDATION REGISTRY TESTS PASSED")


if __name__ == "__main__":
    main()
