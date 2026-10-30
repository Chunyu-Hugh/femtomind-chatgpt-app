"""2026-10-30 review fixes: fs_read/fs_list report the true size and the window
they return (no silent cut at the ssh helper's 4 KB cap), and
campaign_card_from_catalog is brief by default and carries the catalog's
calibration record (JAM24 was calibrated in six stages, not runnable)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

APP = os.environ.get("FEMTOMIND_APP_ROOT")
if not APP:
    raise SystemExit("set FEMTOMIND_APP_ROOT to the femtomind-chatgpt-app checkout")
sys.path.insert(0, os.path.join(APP, "lqcdmcp-deploy"))
os.environ.pop("JAM_GW_TOOLS_DENY", None)

import jam_gateway as g  # noqa: E402


def run_locally(u, code, apptainer=False, timeout=300, cap=4000, **kw):
    p = subprocess.run([sys.executable, "-"], input=code, capture_output=True, text=True, timeout=timeout)
    return {"rc": p.returncode, "out": p.stdout, "err": p.stderr}


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        g._tls.user = "chunyu"
        for p in (patch.object(g, "_run_ifarm_py", run_locally),
                  patch.object(g, "_need_user", lambda username="": ("chunyu", None)),
                  patch.object(g, "_outside_read_roots", lambda path, u="": None)):
            p.start()
            self.addCleanup(p.stop)

    def write(self, name, text):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_read_pages_through_a_file_and_never_lies(self):
        text = "".join(f"line {i:03d} {'x' * 60}\n" for i in range(1, 201))
        path = self.write("big.log", text)
        first = g.fs_read(path)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["total_bytes"], len(text))
        self.assertEqual(first["offset"], 0)
        self.assertTrue(first["truncated"])
        self.assertEqual(first["next_offset"], first["returned_bytes"])
        self.assertIn("offset=", first["hint"])
        self.assertEqual(len(first["content"].encode()), first["returned_bytes"])
        # Page through until the end and rebuild the file exactly.
        seen, offset = "", 0
        while True:
            part = g.fs_read(path, 0, offset, 20000)
            self.assertEqual(part["status"], "ok")
            self.assertTrue(part["returned_bytes"] > 0)
            seen += part["content"]
            if not part["truncated"]:
                self.assertIsNone(part["next_offset"])
                break
            offset = part["next_offset"]
        self.assertEqual(seen, text)

    def test_read_reports_a_small_file_as_complete(self):
        path = self.write("small.txt", "hello\nworld\n")
        out = g.fs_read(path)
        self.assertFalse(out["truncated"])
        self.assertEqual(out["returned_lines"], 2)
        self.assertEqual(out["content"], "hello\nworld\n")
        self.assertNotIn("hint", out)

    def test_read_honours_max_lines_and_max_bytes(self):
        path = self.write("lines.txt", "a\n" * 100)
        out = g.fs_read(path, max_lines=5, max_bytes=20000)
        self.assertEqual(out["returned_lines"], 5)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["next_offset"], 10)
        out = g.fs_read(path, max_bytes=100000)          # clamped to 20000
        self.assertLessEqual(out["returned_bytes"], 20000)

    def test_read_refuses_a_directory_and_a_missing_file(self):
        out = g.fs_read(self.tmp.name)
        self.assertEqual(out["status"], "error")
        self.assertIn("fs_list", out["message"])
        out = g.fs_read(os.path.join(self.tmp.name, "nope.log"))
        self.assertEqual(out["status"], "error")
        self.assertIn("FileNotFoundError", out["message"])
        out = g.fs_read("")
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["code"], "BAD_ARGUMENT")

    def test_list_reports_how_many_it_left_out(self):
        for i in range(30):
            self.write(f"f{i:02d}.txt", "x")
        out = g.fs_list(self.tmp.name, 10)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["total_entries"], 30)
        self.assertEqual(out["returned"], 10)
        self.assertTrue(out["truncated"])
        self.assertIn("30 entries", out["hint"])
        self.assertEqual([e["name"] for e in out["entries"]][0], "f00.txt")
        out = g.fs_list(self.tmp.name, 200)
        self.assertFalse(out["truncated"])
        self.assertEqual(out["returned"], 30)


class CardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._user = getattr(g._tls, "user", "")
        g._tls.user = "chunyu"

    @classmethod
    def tearDownClass(cls):
        g._tls.user = cls._user

    def test_brief_is_the_tool_default_and_small(self):
        out = g.TOOLS["campaign_card_from_catalog"]["fn"]({"replicas": 1})
        self.assertEqual(out["detail"], "brief")
        self.assertEqual(sorted(out["card"]), ["job", "slurm", "source"])
        self.assertLess(len(json.dumps(out["card"])), 2000)
        self.assertLess(len(json.dumps(out)), 8000)
        self.assertEqual(out["card"]["job"]["replicas"], 1)
        self.assertEqual(out["expected"]["free_parameters"], 215)

    def test_full_still_carries_every_table(self):
        out = g.campaign_card_from_catalog("JAM24.final", 1, None, "full")
        self.assertEqual(out["detail"], "full")
        self.assertIn("objects", out["card"])
        self.assertIn("datasets", out["card"])
        self.assertGreater(len(json.dumps(out["card"])), 40000)

    def test_both_forms_carry_the_calibration_record(self):
        brief = g.campaign_card_from_catalog("JAM24.final", 1, None, "brief")
        self.assertFalse(brief["calibration"]["runnable"])
        self.assertEqual(len(brief["calibration"]["stages"]), 6)
        self.assertIn("6 stages", brief["calibration"]["warning"])
        self.assertTrue(any("calibrated in 6 stages" in n for n in brief["notes"]))
        full = g.campaign_card_from_catalog("JAM24.final", 1, None, "full")
        self.assertEqual(full["calibration"], brief["calibration"])
        self.assertTrue(any("calibrated in 6 stages" in n for n in full["notes"]))

    def test_unknown_analysis_has_no_calibration_note(self):
        self.assertIsNone(g._calibration_note("no-such-analysis"))

    def test_validation_warns_about_the_single_stage(self):
        """_validate_campaign adds the calibration warning next to the data
        warnings, so the summary the assistant shows cannot leave it out."""
        card = {"source": {"analysis_id": "JAM24.final"}, "job": {"replicas": 1},
                "slurm": {"time": "00:30:00", "mem": "4G", "cpus": 1}}
        warnings = ["some data warning"]
        with patch.object(g, "_checked_card", return_value=(card, None)), \
             patch.object(g, "_data_warnings", return_value=(warnings, 5853)), \
             patch.object(g, "_no_data_refusal", return_value=None), \
             patch.object(g, "_validated", return_value={"summary": {}}), \
             patch.object(g, "_need_user", return_value=("chunyu", None)):
            out = g.validate_campaign({"source": {"analysis_id": "JAM24.final"}})
        self.assertEqual(out["status"], "ok")
        self.assertEqual(len(out["warnings"]), 2)
        self.assertIn("6 stages", out["warnings"][1])
        self.assertEqual(out["calibration"]["stages"][0], "PDF DIS calibration")
        self.assertIn("warnings", out["next"])


if __name__ == "__main__":
    unittest.main()
