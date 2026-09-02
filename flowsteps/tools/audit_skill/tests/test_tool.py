from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


TOOL = Path(__file__).resolve().parents[1] / "tool.py"
_spec = importlib.util.spec_from_file_location("audit_skill_tool", TOOL)
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


class AuditSkillToolTests(unittest.TestCase):
    def test_delegates_to_the_exact_audit_milestone(self) -> None:
        expected = {"outputs": {"source_audit": {"status": "PASS"}}}
        fake = FakeSteps(expected)
        original = tool._STEPS
        tool._STEPS = fake
        self.addCleanup(setattr, tool, "_STEPS", original)
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            request = {"request": {"target": "skill", "codebase": "repo"}}
            result = tool.run(request, run_dir=run_dir, task={"attempt": 1})
        self.assertEqual(result, expected)
        self.assertEqual(fake.calls[0][0], request)
        self.assertEqual(fake.calls[0][1]["step_id"], "audit_complete")
        self.assertEqual(fake.calls[0][2], run_dir.resolve())

    def test_rejects_missing_context_and_wrong_milestone(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires run_dir"):
            tool.run({"request": {"target": "skill"}})
        with self.assertRaisesRegex(ValueError, "cannot run milestone"):
            tool.run(
                {"request": {"target": "skill"}},
                run_dir="run",
                task={"step_id": "toolbox_ready"},
            )


if __name__ == "__main__":
    unittest.main()
