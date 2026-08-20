"""Unit test for the __STEP_ID__ gem worker."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("__STEP_ID___tool", TOOL_DIR / "tool.py")
assert _spec is not None and _spec.loader is not None
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class ToolTests(unittest.TestCase):
    def test_draft_ok_writes_receipt(self) -> None:
        out = tool.run({"gem_path": "references/box.md", "draft": {"ok": True}})
        self.assertTrue(out["ok"])
        self.assertEqual(out["code"], "pass")

    def test_missing_ok_raises(self) -> None:
        with self.assertRaises(ValueError):
            tool.run({"gem_path": "references/box.md", "asset": {"path": "a.png"}})


if __name__ == "__main__":
    unittest.main()
