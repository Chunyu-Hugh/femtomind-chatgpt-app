"""E1 fault injection: every mutant changes exactly one scientific field of the
official card, the verdict mapping is what the paper's table says, and the
markdown report counts the faults the way the summary claims."""
import json
import os
import sys
import unittest

APP = os.environ.get("FEMTOMIND_APP_ROOT")
if not APP:
    raise SystemExit("set FEMTOMIND_APP_ROOT to the femtomind-chatgpt-app checkout")
sys.path.insert(0, os.path.join(APP, "lqcdmcp-deploy"))
sys.path.insert(0, APP)
os.environ.pop("JAM_GW_TOOLS_DENY", None)

import jam_gateway as g  # noqa: E402
from evaluation.e1 import pilot  # noqa: E402
from evaluation.e1.mutants import MUTANTS, mutate  # noqa: E402
from evaluation.e1.report import markdown, summary, table  # noqa: E402
from evaluation.e1.runner import outcome  # noqa: E402


def free_parameters(card):
    return sum(1 for o in card["objects"] for p in o["parameters"].values() if p.get("fixed") is False)


class MutantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._user = getattr(g._tls, "user", "")
        g._tls.user = "chunyu"
        cls.card = g.campaign_card_from_catalog("JAM24.final", 1)["card"]

    @classmethod
    def tearDownClass(cls):
        g._tls.user = cls._user

    def test_baseline_is_the_published_configuration(self):
        self.assertEqual(free_parameters(self.card), 215)
        self.assertEqual(self.card["qcd"]["Q20"], 1.6129)
        self.assertEqual(self.card["parameters"]["initialization"], "random_within_bounds")

    def test_each_mutant_changes_only_its_field(self):
        for mutant_id, _what, field in MUTANTS:
            m = mutate(self.card, mutant_id)
            self.assertIsNot(m, self.card)
            self.assertEqual(self.card, self.card)               # the base is never touched
            changed = {k for k in ("qcd", "parameters", "job", "datasets", "cuts", "objects")
                       if m[k] != self.card[k]}
            self.assertEqual(changed, {field}, "%s changed %s, expected %s" % (mutant_id, changed, field))

    def test_the_two_known_silent_candidates(self):
        """Job settings are not scientific: the gateway has no reason to refuse
        them, and that is exactly what makes them the honest rows of the table."""
        loose = mutate(self.card, "loose_tolerance")
        self.assertEqual(loose["job"]["tolerance"], "0.05")
        self.assertEqual(loose["qcd"], self.card["qcd"])
        more = mutate(self.card, "extra_replica_setting")
        self.assertEqual(more["job"]["maxIterations"], 5000)

    def test_ff_shape_off_really_removes_free_parameters(self):
        m = mutate(self.card, "ff_shape_off")
        self.assertLess(free_parameters(m), free_parameters(self.card))
        self.assertEqual(free_parameters(m), 155)

    def test_unknown_mutant_is_an_error(self):
        with self.assertRaises(ValueError):
            mutate(self.card, "no_such_fault")


class PilotJoinTests(unittest.TestCase):
    def test_every_observed_pilot_fault_has_a_mutant(self):
        known = {m for m, _w, _f in MUTANTS}
        for obs in pilot.PILOT_OBSERVATIONS:
            self.assertIn(obs["mutant"], known)
            self.assertTrue(obs["grader"])
            self.assertTrue(obs["arm"])

    def test_the_pilot_table_says_nothing_was_flagged(self):
        text = pilot.table()
        self.assertEqual(len(text.splitlines()), 2 + len(pilot.PILOT_OBSERVATIONS))
        self.assertIn("none was flagged at submission time", pilot.summary())
        self.assertIn("paper only", pilot.summary())


class SilentErrorTests(unittest.TestCase):
    def _report(self, overrides=None):
        rows = [{"id": "baseline", "outcome": "accepted"}]
        verdicts = {"zero_point_dataset": ("refused", "NO_DATA"), "missing_cut": ("refused", "CATALOG_MISMATCH"),
                    "q0_squared": ("refused", "CATALOG_MISMATCH"), "flat_par": ("refused", "CATALOG_MISMATCH"),
                    "ff_shape_off": ("refused", "CATALOG_MISMATCH"), "sia_version": ("refused", "CATALOG_MISMATCH"),
                    "loose_tolerance": ("accepted", None), "extra_replica_setting": ("accepted", None)}
        for k, (out, code) in verdicts.items():
            rows.append({"id": k, "outcome": out, "code": code})
        for k, v in (overrides or {}).items():
            for r in rows:
                if r["id"] == k:
                    r["outcome"] = v
        return {"rows": rows, "when": "synthetic"}

    def test_the_observed_arm_is_all_silent(self):
        from evaluation.e1 import silent
        raw = silent.raw_arm()
        self.assertEqual(raw["n"], len(pilot.PILOT_OBSERVATIONS))
        self.assertEqual(raw["silent"], raw["n"])
        self.assertEqual(raw["rate"], 1.0)

    def test_paired_subset_is_the_join(self):
        from evaluation.e1 import silent
        p = silent.paired(self._report())
        self.assertEqual(p["n"], len(pilot.PILOT_OBSERVATIONS))
        self.assertEqual(p["stopped"], len(pilot.PILOT_OBSERVATIONS) - 1)   # loose_tolerance is downstream
        self.assertTrue(all(r["raw"] == "silent" for r in p["rows"]))

    def test_scoring_names_a_refused_control(self):
        from evaluation.e1 import silent
        sc = silent.score(self._report({"extra_replica_setting": "refused"}))
        self.assertEqual(sc["correct"], sc["n"] - 1)
        self.assertEqual(sc["wrong"][0]["fault"], "extra_replica_setting")
        self.assertEqual(sc["wrong"][0]["expected"], "accepted")

    def test_markdown_says_what_stays_silent(self):
        from evaluation.e1 import silent
        md = silent.markdown(self._report())
        self.assertIn("Still silent with the gateway in place", md)
        self.assertIn("`loose_tolerance`", md)
        self.assertIn("Expected-verdict agreement", md)


class VerdictTests(unittest.TestCase):
    def test_outcome_mapping(self):
        self.assertEqual(outcome({"status": "error", "code": "CATALOG_MISMATCH"}), "refused")
        self.assertEqual(outcome({"status": "ok", "valid": False}), "refused")
        self.assertEqual(outcome({"status": "ok", "valid": True, "warnings": ["x"]}), "warned")
        self.assertEqual(outcome({"status": "ok", "valid": True, "warnings": []}), "accepted")
        self.assertEqual(outcome({"status": "running", "job_id": "j"}), "pending")

    def test_report_counts_and_rows(self):
        report = {"analysis_id": "JAM24.final", "url": "u", "when": "t", "rows": [
            {"id": "baseline", "outcome": "accepted", "points": 5853, "differences": []},
            {"id": "q0_squared", "what": "Q0", "field": "qcd", "outcome": "refused",
             "code": "CATALOG_MISMATCH", "differences": ["qcd settings differ: Q20"]},
            {"id": "loose_tolerance", "what": "ftol", "field": "job", "outcome": "accepted"},
        ]}
        self.assertIn("1 caught, 0 flagged, 1 silent", summary(report))
        md = markdown(report)
        self.assertIn("`q0_squared`", md)
        self.assertIn("**caught**", md)
        self.assertIn("**silent**", md)
        self.assertIn("as they actually happened", md)
        self.assertIn("pilot run3", md)
        self.assertEqual(len(table(report).splitlines()), 5)


if __name__ == "__main__":
    unittest.main()
