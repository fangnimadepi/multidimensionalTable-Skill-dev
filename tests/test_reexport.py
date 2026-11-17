"""Keep presentation-only exports bound to their checked material version."""

import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "project-report/scripts"))
import reexport_checked


class ReexportTests(unittest.TestCase):
    def make_fixture(self, directory, status):
        root = Path(directory).resolve()
        run = root / "workspace/project/run"
        (run / "raw/taskbook").mkdir(parents=True)
        (run / "raw/taskbook/plan.docx").write_bytes(b"modified material")
        receipt = run / "reports/old/checked.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"status": status, "material_hashes": {"plan": "old-hash"}}))
        config = root / "config.json"
        config.write_text(json.dumps({"workspace_root": str(root / "workspace")}))
        args = argparse.Namespace(config=config, run_dir=run, receipt=receipt,
                                  taskbook="raw/taskbook/plan.docx", report=None)
        return root, run, args

    def test_changed_material_cannot_reuse_old_checked_conclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            root, run, args = self.make_fixture(directory, "checked")
            with patch.object(reexport_checked, "setup_runtime", return_value=(root / "workspace/_runtime", {})):
                with self.assertRaisesRegex(ValueError, "材料已变化"):
                    reexport_checked.reexport(args)
            self.assertEqual([p.name for p in (run / "reports").iterdir()], ["old"])

    def test_unchecked_receipt_is_not_published_as_a_style_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _, args = self.make_fixture(directory, "failed")
            with patch.object(reexport_checked, "setup_runtime", return_value=(root / "workspace/_runtime", {})):
                with self.assertRaisesRegex(ValueError, "已经通过检查"):
                    reexport_checked.reexport(args)


if __name__ == "__main__":
    unittest.main()
