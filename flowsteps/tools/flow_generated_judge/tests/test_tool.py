from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


TOOL = Path(__file__).resolve().parents[1] / "tool.py"
_spec = importlib.util.spec_from_file_location("flow_generated_judge_tool", TOOL)
assert _spec is not None and _spec.loader is not None
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class FlowGeneratedJudgeTests(unittest.TestCase):
    def test_requires_matching_source_bundle_digest(self) -> None:
        bundle = {"source_bundle_proof": {"source_bundle_digest": "sha256:abc"}}
        staged = {
            "schema": "flowstep_harness_generate_v4",
            "status": "PASS",
            "source_bundle_digest": "sha256:abc",
            "staged_members": [{"path": "SKILL.md"}],
            "staged_members_digest": "sha256:def",
        }
        candidate = {"outputs": {"workflow_source_bundle": bundle, "staged_harness": staged}}
        self.assertTrue(tool.run({"candidate": candidate})["ok"])
        staged["source_bundle_digest"] = "sha256:different"
        self.assertFalse(tool.run({"candidate": candidate})["ok"])


if __name__ == "__main__":
    unittest.main()
