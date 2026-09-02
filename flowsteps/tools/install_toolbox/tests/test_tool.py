from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


TOOL = Path(__file__).resolve().parents[1] / "tool.py"
_spec = importlib.util.spec_from_file_location("install_toolbox_tool", TOOL)
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


class InstallToolboxToolTests(unittest.TestCase):
    def test_delegates_with_the_chosen_audit(self) -> None:
        expected = {"outputs": {"toolbox_manifest": {"status": "PASS"}}}
        fake = FakeSteps(expected)
        original = tool._STEPS
        tool._STEPS = fake
        self.addCleanup(setattr, tool, "_STEPS", original)
        input_data = {"request": {"codebase": "repo"}, "audit": {"status": "PASS"}}
        with tempfile.TemporaryDirectory() as temp:
            result = tool.run(
                input_data,
                params={"run_dir": temp, "task": {"attempt": 2}},
            )
        self.assertEqual(result, expected)
        self.assertIs(fake.calls[0][0]["audit"], input_data["audit"])
        self.assertEqual(fake.calls[0][1]["step_id"], "toolbox_ready")

    def test_rejects_an_unexpected_output_port(self) -> None:
        original = tool._STEPS
        tool._STEPS = FakeSteps({"outputs": {"wrong": {}}})
        self.addCleanup(setattr, tool, "_STEPS", original)
        with self.assertRaisesRegex(RuntimeError, "unexpected output ports"):
            tool.run(
                {"request": {"codebase": "repo"}, "audit": {"status": "PASS"}},
                run_dir="run",
            )


if __name__ == "__main__":
    unittest.main()
