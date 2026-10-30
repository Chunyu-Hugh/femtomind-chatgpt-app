"""E2 ablation switches: each one removes exactly one layer, every reply that
ran with a layer removed says so, and a deployed gateway (no test token)
ignores the switch."""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

APP = os.environ.get("FEMTOMIND_APP_ROOT")
if not APP:
    raise SystemExit("set FEMTOMIND_APP_ROOT to the femtomind-chatgpt-app checkout")
sys.path.insert(0, os.path.join(APP, "lqcdmcp-deploy"))
sys.path.insert(0, APP)
os.environ.pop("JAM_GW_TOOLS_DENY", None)

import jam_gateway as g  # noqa: E402

CARD = {"source": {"analysis_id": "JAM24.final"}, "job": {"replicas": 1},
        "slurm": {"time": "00:30:00", "mem": "4G", "cpus": 1}}


def _validate(ablate):
    """_validate_campaign with every cluster call stubbed; returns (reply, calls)."""
    calls = []

    def data_warnings(u, norm):
        calls.append("data")
        return ["a data warning"], 5853

    def validated(u, norm):
        calls.append("validated")
        return {"summary": {}, "confirmation_phrase": "CONFIRM JAM SUBMIT 0123456789"}

    old = g.ABLATE
    g.ABLATE = frozenset(ablate)
    try:
        with patch.object(g, "_checked_card", return_value=(CARD, None)), \
             patch.object(g, "_data_warnings", data_warnings), \
             patch.object(g, "_no_data_refusal", return_value=None), \
             patch.object(g, "_validated", validated), \
             patch.object(g, "_need_user", return_value=("chunyu", None)), \
             patch.object(g, "_calibration_note", return_value=None):
            reply = g._validate_campaign("chunyu", CARD)
    finally:
        g.ABLATE = old
    return reply, calls


class AblationTests(unittest.TestCase):
    def test_validate_ablation_does_not_read_the_data(self):
        reply, calls = _validate({"validate"})
        self.assertNotIn("data", calls)
        self.assertEqual(reply["warnings"], [])
        self.assertIsNone(reply["summary"]["points_after_cuts"])
        self.assertEqual(reply["ablation"], ["validate"])

    def test_no_ablation_reads_the_data_and_says_nothing(self):
        reply, calls = _validate(())
        self.assertIn("data", calls)
        self.assertEqual(reply["warnings"], ["a data warning"])
        self.assertEqual(reply["summary"]["points_after_cuts"], 5853)
        self.assertNotIn("ablation", reply)

    def test_catalog_ablation_skips_the_parity_comparison(self):
        old = g.ABLATE
        g.ABLATE = frozenset({"catalog"})
        called = []
        try:
            norm = {"source": {"analysis_id": "JAM24.final"}, "job": {"replicas": 1},
                    "slurm": {"time": "00:30:00", "mem": "4G", "cpus": 1}}
            with patch.object(g, "_catalog_comparison", lambda n: called.append(1) or {"verdict": "differs"}), \
                 patch.object(g, "_card_summary", return_value={"free_parameters": 215}), \
                 patch.object(g, "_slurm_recommendation", return_value={"time": "00:30:00", "mem": "4G", "cpus": 1}), \
                 patch.object(g, "_array_script", return_value="#!/bin/sh"), \
                 patch.object(g, "_confirm_phrase", return_value="CONFIRM"), \
                 patch.object(g, "_workspace", return_value="/w/x"):
                g._validated("chunyu", norm)
        finally:
            g.ABLATE = old
        self.assertEqual(called, [])

    def _boot(self, env):
        code = ("import json, sys; sys.path.insert(0, %r); import jam_gateway as g; "
                "print(json.dumps({'ablate': sorted(g.ABLATE), 'gate': g.HUMAN_GATE}))" % (os.path.join(APP, "lqcdmcp-deploy"),))
        full = dict(os.environ)
        full.pop("JAM_GW_ABLATE", None)
        full.pop("JAM_GW_TEST_TOKEN", None)
        full.pop("JAM_GW_HUMAN_GATE", None)
        full.update(env)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=full, timeout=120)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_gate_ablation_disables_the_human_gate(self):
        boot = self._boot({"JAM_GW_ABLATE": "gate", "JAM_GW_TEST_TOKEN": "x", "JAM_GW_HUMAN_GATE": "1"})
        self.assertEqual(boot["ablate"], ["gate"])
        self.assertFalse(boot["gate"])

    def test_a_deployed_gateway_ignores_the_switch(self):
        boot = self._boot({"JAM_GW_ABLATE": "validate,catalog"})
        self.assertEqual(boot["ablate"], [])

    def test_an_unknown_feature_is_ignored_entirely(self):
        boot = self._boot({"JAM_GW_ABLATE": "validate,nonsense", "JAM_GW_TEST_TOKEN": "x"})
        self.assertEqual(boot["ablate"], [])


class AblationMatrixTests(unittest.TestCase):
    """The E2 table: a fault is attributed to the layer whose removal lets it
    through, and nothing is attributed where the verdict does not move."""

    def _combined(self):
        def rows(verdicts):
            out = [{"id": "baseline", "outcome": "accepted"}]
            for fault, v in verdicts.items():
                out.append({"id": fault, "outcome": v, "code": "CATALOG_MISMATCH" if v == "refused" else None})
            return out
        return {"ablations": ["", "validate", "catalog", "gate"],
                "runs": {"": {"rows": rows({"q0_squared": "refused", "loose_tolerance": "accepted"})},
                         "validate": {"rows": rows({"q0_squared": "refused", "loose_tolerance": "accepted"})},
                         "catalog": {"rows": rows({"q0_squared": "accepted", "loose_tolerance": "accepted"})},
                         "gate": {"rows": rows({"q0_squared": "refused", "loose_tolerance": "accepted"})}}}

    def test_attribution(self):
        from evaluation.e2.runner import caught_by, markdown
        by = caught_by(self._combined())
        self.assertEqual(by["q0_squared"], ["catalog"])
        self.assertEqual(by["loose_tolerance"], [])
        text = markdown(self._combined())
        self.assertIn("`q0_squared`", text)
        self.assertIn("catalog", text)
        self.assertIn("not caught here", text)
        self.assertEqual(len([ln for ln in text.splitlines() if ln.startswith("|")]), 4)


if __name__ == "__main__":
    unittest.main()
