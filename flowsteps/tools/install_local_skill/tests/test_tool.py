from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


TOOL = Path(__file__).resolve().parents[1] / "tool.py"
_spec = importlib.util.spec_from_file_location("install_local_skill_tool", TOOL)
assert _spec is not None and _spec.loader is not None
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class FakeSteps:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[tuple[dict, dict, Path]] = []

    def run(self, input_data: dict, *, task: dict, run_dir: Path) -> dict:
        self.calls.append((input_data, task, run_dir))
        return self.result


class InstallLocalSkillToolTests(unittest.TestCase):
    def test_delegates_the_bundle_stage_and_validation_together(self) -> None:
        expected = {"outputs": {"installation_receipt": {"status": "PASS"}}}
        fake = FakeSteps(expected)
        original = tool._STEPS
        tool._STEPS = fake
        self.addCleanup(setattr, tool, "_STEPS", original)
        input_data = {
            "request": {"codebase": "repo", "target": "skill"},
            "workflow_source_bundle": {"schema": "m8m.workflow_source_bundle.v1"},
            "staged_harness": {"status": "PASS"},
            "validation": {"status": "PASS"},
        }
        with tempfile.TemporaryDirectory() as temp:
            result = tool.run(input_data, run_dir=temp, task={"attempt": 1})
        self.assertEqual(result, expected)
        self.assertEqual(fake.calls[0][1]["step_id"], "skill_shipped")
        self.assertEqual(fake.calls[0][0], input_data)

    def test_rejects_a_task_for_another_milestone(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot run milestone"):
            tool.run(
                {
                    "request": {"codebase": "repo", "target": "skill"},
                    "workflow_source_bundle": {},
                    "staged_harness": {},
                    "validation": {},
                },
                run_dir="run",
                task={"step_id": "harness_validated"},
            )


if __name__ == "__main__":
    unittest.main()
