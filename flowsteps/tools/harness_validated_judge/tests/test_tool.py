from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


TOOL = Path(__file__).resolve().parents[1] / "tool.py"
_spec = importlib.util.spec_from_file_location("harness_validated_judge_tool", TOOL)
assert _spec is not None and _spec.loader is not None
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class HarnessValidatedJudgeTests(unittest.TestCase):
    def test_requires_full_fail_closed_pass(self) -> None:
        report = {
            "status": "PASS",
            "ok": True,
            "runnable": True,
            "non_runnable": False,
            "full_validation": {"status": "PASS"},
            "source_bundle_digest": "sha256:abc",
            "staged_members_digest": "sha256:def",
        }
        self.assertTrue(
            tool.run({"candidate": {"outputs": {"validation_report": report}}})["ok"]
        )
        report["full_validation"] = {"status": "BLOCKED"}
        self.assertFalse(
            tool.run({"candidate": {"outputs": {"validation_report": report}}})["ok"]
        )


if __name__ == "__main__":
    unittest.main()
