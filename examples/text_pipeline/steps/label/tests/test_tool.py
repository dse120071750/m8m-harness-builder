from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("label_tool", TOOL_DIR / "tool.py")
assert _spec is not None and _spec.loader is not None
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class LabelToolTests(unittest.TestCase):
    def test_requests_typed_draft(self) -> None:
        result = tool.run({"segment": {"sentences": ["Hello, world."], "sentence_count": 1}})
        self.assertEqual(result["_flowstep"], "NEED_MODEL")
        self.assertEqual(result["model_request"]["sentence"], "Hello, world.")

    def test_materializes_enum_label(self) -> None:
        result = tool.run(
            {"segment": {"sentences": ["Hello, world."], "sentence_count": 1}},
            draft={"label": "statement"},
        )
        self.assertEqual(result, {"label": "statement", "sentence": "Hello, world."})


if __name__ == "__main__":
    unittest.main()
