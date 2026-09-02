from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


TOOL = Path(__file__).resolve().parents[1] / "tool.py"
_spec = importlib.util.spec_from_file_location("generate_from_audit_tool", TOOL)
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


class GenerateFromAuditToolTests(unittest.TestCase):
    def test_requires_and_preserves_both_generation_ports(self) -> None:
        expected = {
            "outputs": {
                "workflow_source_bundle": {"schema": "m8m.workflow_source_bundle.v1"},
                "staged_harness": {"status": "PASS"},
            }
        }
        fake = FakeSteps(expected)
        original = tool._STEPS
        tool._STEPS = fake
        self.addCleanup(setattr, tool, "_STEPS", original)
        input_data = {
            "request": {"codebase": "repo"},
            "audit": {"status": "PASS"},
            "toolbox": {"status": "PASS"},
        }
        with tempfile.TemporaryDirectory() as temp:
            result = tool.run(input_data, run_dir=temp)
        self.assertEqual(result, expected)
        self.assertEqual(fake.calls[0][1]["step_id"], "flow_generated")
        self.assertEqual(set(result["outputs"]), set(tool.OUTPUT_IDS))

    def test_rejects_a_generation_result_missing_either_port(self) -> None:
        original = tool._STEPS
        tool._STEPS = FakeSteps({"outputs": {"staged_harness": {"status": "PASS"}}})
        self.addCleanup(setattr, tool, "_STEPS", original)
        with self.assertRaisesRegex(RuntimeError, "unexpected output ports"):
            tool.run(
                {
                    "request": {"codebase": "repo"},
                    "audit": {"status": "PASS"},
                    "toolbox": {"status": "PASS"},
                },
                run_dir="run",
            )


if __name__ == "__main__":
    unittest.main()
