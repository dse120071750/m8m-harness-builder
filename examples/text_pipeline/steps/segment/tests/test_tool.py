from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("segment_tool", TOOL_DIR / "tool.py")
assert _spec is not None and _spec.loader is not None
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class SegmentToolTests(unittest.TestCase):
    def test_splits_sentences(self) -> None:
        result = tool.run({"ingest": {"text": "Hello, world. This is a test.", "char_count": 29}})
        self.assertEqual(result["sentence_count"], 2)
        self.assertEqual(result["sentences"][0], "Hello, world.")


if __name__ == "__main__":
    unittest.main()
