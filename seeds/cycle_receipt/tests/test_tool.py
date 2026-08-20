from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tool.py"
_spec = importlib.util.spec_from_file_location("cycle_receipt_tool", TOOL)
assert _spec is not None and _spec.loader is not None
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class CycleReceiptTests(unittest.TestCase):
    def test_pass_and_fail(self) -> None:
        self.assertEqual(tool.run({"recommended_cycle": "pass", "row": "001"})["cycle"], "pass")
        self.assertEqual(tool.run({"draft": {"recommended_cycle": "fail"}, "row": "002"})["cycle"], "fail")

    def test_undecided_not_ok(self) -> None:
        receipt = tool.run({"row": "001"})
        self.assertFalse(receipt["ok"])


if __name__ == "__main__":
    unittest.main()
