from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tool.py"
_spec = importlib.util.spec_from_file_location("ledger_receipt_tool", TOOL)
assert _spec is not None and _spec.loader is not None
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class LedgerReceiptTests(unittest.TestCase):
    def test_remaining_zero_is_ok(self) -> None:
        ledger = [{"id": "a"}, {"id": "b"}]
        mid = tool.run({"ledger": ledger, "done": [{"id": "a"}]})
        self.assertFalse(mid["ok"])
        self.assertEqual(mid["remaining"], 1)
        done = tool.run({"ledger": ledger, "done": ledger})
        self.assertTrue(done["ok"])
        self.assertEqual(done["remaining"], 0)


if __name__ == "__main__":
    unittest.main()
