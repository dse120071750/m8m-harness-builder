from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ASSEMBLE = Path(__file__).resolve().parents[1] / "assemble.py"
_spec = importlib.util.spec_from_file_location("__STEP_ID___assemble", ASSEMBLE)
assert _spec is not None and _spec.loader is not None
assemble = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(assemble)


class AssembleTests(unittest.TestCase):
    def test_generated_handler_is_explicitly_non_runnable(self) -> None:
        self.assertEqual(assemble.M8M_BUILD_STATUS, "BUILD_REQUIRED")
        self.assertFalse(assemble.M8M_RUNNABLE)
        with self.assertRaisesRegex(RuntimeError, "BUILD_REQUIRED/non-runnable"):
            assemble.run({"request": {"text": "x"}})

    def test_candidate_adapter_requires_named_outputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit"):
            assemble._candidate({"result": {"value": "x"}})
        self.assertEqual(
            assemble._candidate({"outputs": {"result": {"value": "x"}}}),
            {"outputs": {"result": {"value": "x"}}},
        )


if __name__ == "__main__":
    unittest.main()
