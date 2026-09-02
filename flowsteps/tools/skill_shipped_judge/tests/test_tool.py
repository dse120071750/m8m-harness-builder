from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


TOOL = Path(__file__).resolve().parents[1] / "tool.py"
_spec = importlib.util.spec_from_file_location("skill_shipped_judge_tool", TOOL)
assert _spec is not None and _spec.loader is not None
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class SkillShippedJudgeTests(unittest.TestCase):
    def test_requires_a_digest_bound_local_installation(self) -> None:
        receipt = {
            "status": "PASS",
            "runnable": True,
            "non_runnable": False,
            "installed_files": ["skill/SKILL.md"],
            "source_bundle_digest": "sha256:abc",
            "staged_members_digest": "sha256:def",
            "source_bundle_sha256": "abc",
        }
        self.assertTrue(
            tool.run({"candidate": {"outputs": {"installation_receipt": receipt}}})["ok"]
        )
        receipt["installed_files"] = []
        self.assertFalse(
            tool.run({"candidate": {"outputs": {"installation_receipt": receipt}}})["ok"]
        )


if __name__ == "__main__":
    unittest.main()
