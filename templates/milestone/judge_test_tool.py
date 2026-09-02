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
    def test_scaffold_requires_implementation_after_typed_request(self) -> None:
        request = {
            "schema": "m8m.milestone_judge_request.v1",
            "milestone_id": "example_ready",
            "attempt": 1,
            "max_attempts": 2,
            "expectation": {
                "schema": "m8m.milestone_expectation.v1",
                "milestone_id": "example_ready",
                "success": "The example is accepted.",
                "output_contract": "example_v1",
                "output_schema_ref": "schemas/example.json",
                "outputs": [],
            },
            "inputs": {},
            "candidate": {"outputs": {}},
        }
        with self.assertRaises(NotImplementedError):
            tool.run(request)

    def test_candidate_control_flag_is_not_a_judge_request(self) -> None:
        with self.assertRaises(ValueError):
            tool.run({"accepted": True, "candidate": {"outputs": {}}})


if __name__ == "__main__":
    unittest.main()
