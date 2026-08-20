from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tool.py"
_spec = importlib.util.spec_from_file_location("branch_receipt_tool", TOOL)
assert _spec is not None and _spec.loader is not None
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class BranchReceiptTests(unittest.TestCase):
    def test_direct_default_when_not_source_case(self) -> None:
        receipt = tool.run(
            {
                "paths": ["direct", "floorplan_source_case"],
                "default": "direct",
                "case_type": "restyle",
            }
        )
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["branch"], "direct")
        self.assertEqual(receipt["skipped"], ["floorplan_source_case"])

    def test_source_case_path(self) -> None:
        receipt = tool.run(
            {
                "paths": ["direct", "floorplan_source_case"],
                "default": "direct",
                "case_type": "source_case",
            }
        )
        self.assertEqual(receipt["branch"], "floorplan_source_case")
        self.assertEqual(receipt["skipped"], ["direct"])

    def test_draft_wins(self) -> None:
        receipt = tool.run(
            {
                "paths": ["direct", "floorplan_source_case"],
                "default": "direct",
                "draft": {"recommended_branch": "floorplan_source_case"},
            }
        )
        self.assertEqual(receipt["branch"], "floorplan_source_case")

    def test_unknown_branch_not_ok(self) -> None:
        receipt = tool.run(
            {
                "paths": ["direct", "floorplan_source_case"],
                "recommended_branch": "other",
            }
        )
        self.assertFalse(receipt["ok"])


if __name__ == "__main__":
    unittest.main()
