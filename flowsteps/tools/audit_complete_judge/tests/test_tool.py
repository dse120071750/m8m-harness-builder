from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


TOOL = Path(__file__).resolve().parents[1] / "tool.py"
_spec = importlib.util.spec_from_file_location("audit_complete_judge_tool", TOOL)
assert _spec is not None and _spec.loader is not None
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class AuditCompleteJudgeTests(unittest.TestCase):
    def test_accepts_only_a_closed_source_audit(self) -> None:
        audit = {
            "schema": "flowstep_skill_audit_v1",
            "status": "FINDINGS",
            "verdict": "NEEDS_UPGRADE",
            "builder_request": {"target": "skill"},
            "proposed_milestones": [{"id": "ready"}],
        }
        accepted = tool.run({"candidate": {"outputs": {"source_audit": audit}}})
        rejected = tool.run({"candidate": {"outputs": {"source_audit": {}}}})
        self.assertTrue(accepted["ok"])
        self.assertEqual(accepted["code"], "source_audit_closed")
        self.assertFalse(rejected["ok"])


if __name__ == "__main__":
    unittest.main()
