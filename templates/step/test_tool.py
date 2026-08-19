"""Unit test for the __STEP_ID__ tool. Replace this stub before validate_harness."""

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
    def test_run_returns_output_payload(self) -> None:
        raise NotImplementedError("write a fixture input and assert the typed payload")


if __name__ == "__main__":
    unittest.main()
