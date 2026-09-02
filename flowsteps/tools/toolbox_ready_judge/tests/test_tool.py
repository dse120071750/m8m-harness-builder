from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


TOOL = Path(__file__).resolve().parents[1] / "tool.py"
_spec = importlib.util.spec_from_file_location("toolbox_ready_judge_tool", TOOL)
assert _spec is not None and _spec.loader is not None
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class ToolboxReadyJudgeTests(unittest.TestCase):
    def test_accepts_consistent_pass_or_build_required_state(self) -> None:
        passing = {
            "tools": [],
            "build_required_tools": [],
            "status": "PASS",
            "runnable": True,
            "non_runnable": False,
        }
        unfinished = {
            "tools": [{"tool_id": "draft"}],
            "build_required_tools": ["draft"],
            "status": "BUILD_REQUIRED",
            "runnable": False,
            "non_runnable": True,
        }
        invalid = {**passing, "runnable": False}
        self.assertTrue(tool.run({"candidate": {"outputs": {"toolbox_manifest": passing}}})["ok"])
        self.assertTrue(tool.run({"candidate": {"outputs": {"toolbox_manifest": unfinished}}})["ok"])
        self.assertFalse(tool.run({"candidate": {"outputs": {"toolbox_manifest": invalid}}})["ok"])


if __name__ == "__main__":
    unittest.main()
