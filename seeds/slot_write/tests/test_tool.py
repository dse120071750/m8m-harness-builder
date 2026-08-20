from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tool.py"
_spec = importlib.util.spec_from_file_location("slot_write_tool", TOOL)
assert _spec is not None and _spec.loader is not None
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class SlotWriteTests(unittest.TestCase):
    def test_copies_into_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            src = Path(temp) / "in.txt"
            src.write_text("hello\n", encoding="utf-8")
            dest = Path(temp) / "milestones" / "x" / "out" / "files" / "asset.bin"
            result = tool.run(
                {
                    "path": str(src),
                    "address": {"write_to": str(dest), "slot": "milestones/x/out/files/asset.bin"},
                }
            )
            self.assertTrue(dest.is_file())
            self.assertEqual(result["sha256"], hashlib.sha256(dest.read_bytes()).hexdigest())
            self.assertEqual(result["slot"], "milestones/x/out/files/asset.bin")


if __name__ == "__main__":
    unittest.main()
