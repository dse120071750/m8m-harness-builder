from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tool.py"
_spec = importlib.util.spec_from_file_location("ok_receipt_tool", TOOL)
assert _spec is not None and _spec.loader is not None
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class OkReceiptTests(unittest.TestCase):
    def test_pass_and_fail(self) -> None:
        self.assertTrue(tool.run({"ok": True})["ok"])
        self.assertFalse(tool.run({"ok": False})["ok"])
        self.assertEqual(tool.run({"ok": True})["code"], "pass")


if __name__ == "__main__":
    unittest.main()
