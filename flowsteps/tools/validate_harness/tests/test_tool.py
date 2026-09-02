from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


TOOL = Path(__file__).resolve().parents[1] / "tool.py"
_spec = importlib.util.spec_from_file_location("validate_harness_tool", TOOL)
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


class ValidateHarnessToolTests(unittest.TestCase):
    def test_delegates_both_generation_outputs_to_validation(self) -> None:
        expected = {"outputs": {"validation_report": {"status": "PASS"}}}
        fake = FakeSteps(expected)
        original = tool._STEPS
        tool._STEPS = fake
        self.addCleanup(setattr, tool, "_STEPS", original)
        bundle = {"schema": "m8m.workflow_source_bundle.v1"}
        staged = {"source_bundle_digest": "sha256:" + "a" * 64}
        input_data = {
            "request": {"codebase": "repo"},
            "workflow_source_bundle": bundle,
            "staged_harness": staged,
        }
        with tempfile.TemporaryDirectory() as temp:
            result = tool.run(input_data, run_dir=temp)
        self.assertEqual(result, expected)
        self.assertIs(fake.calls[0][0]["workflow_source_bundle"], bundle)
        self.assertIs(fake.calls[0][0]["staged_harness"], staged)
        self.assertEqual(fake.calls[0][1]["step_id"], "harness_validated")

    def test_rejects_a_draft_in_the_deterministic_validator(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not accept drafts"):
            tool.run(
                {
                    "request": {"codebase": "repo"},
                    "workflow_source_bundle": {},
                    "staged_harness": {},
                },
                run_dir="run",
                draft={"repair": True},
            )


if __name__ == "__main__":
    unittest.main()
