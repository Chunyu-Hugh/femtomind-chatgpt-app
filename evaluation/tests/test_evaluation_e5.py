"""E5 atlas plan: every variant is one declared override on the catalog card,
the control changes nothing, and the variants that cannot be expressed as an
override are named instead of being approximated."""
import os
import sys
import unittest

APP = os.environ.get("FEMTOMIND_APP_ROOT")
if not APP:
    raise SystemExit("set FEMTOMIND_APP_ROOT to the femtomind-chatgpt-app checkout")
sys.path.insert(0, os.path.join(APP, "lqcdmcp-deploy"))
sys.path.insert(0, APP)

from evaluation.e5 import plan  # noqa: E402


class PlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = plan.build("JAM24.final")
        cls.by_id = {r["id"]: r for r in cls.plan["variants"]}

    def test_control_is_the_published_card(self):
        c = self.by_id["control"]
        self.assertEqual(c["changed"], [])
        self.assertEqual(c["free_parameters"], 215)

    def test_each_variant_moves_the_section_it_claims(self):
        expected = {"no_wcharm": "reactions", "no_sidis": "reactions", "no_sia": "reactions",
                    "sidis_z09": "cuts", "sidis_q2_2": "cuts", "q0_paper": "qcd", "tmc_off": "qcd",
                    "positivity_on": "qcd", "no_bootstrap": "job", "more_iterations": "job",
                    "normals_free": "normalizations", "one_ff_shape": "objects", "fixed_kaon": "objects"}
        for vid, section in expected.items():
            self.assertEqual(self.by_id[vid]["changed"], [section], vid)

    def test_shape_variants_remove_free_parameters(self):
        self.assertLess(self.by_id["one_ff_shape"]["free_parameters"], 215)
        self.assertEqual(self.by_id["fixed_kaon"]["free_parameters"], 155)

    def test_unexpressible_variants_are_named(self):
        names = set(self.plan["not_expressible"])
        self.assertIn("s2_independent", names)
        self.assertIn("su3_sea", names)
        self.assertIn("nnlo", names)
        self.assertTrue(all(self.plan["not_expressible"][n] for n in names))

    def test_the_plan_costs_one_replica_per_row(self):
        self.assertGreaterEqual(len(self.plan["variants"]), 12)


if __name__ == "__main__":
    unittest.main()
