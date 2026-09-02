from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tool.py"
_spec = importlib.util.spec_from_file_location("cycle_receipt_tool", TOOL)
assert _spec is not None and _spec.loader is not None
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class CycleReceiptTests(unittest.TestCase):
    @staticmethod
    def request(*, ready: bool | None, row: str) -> dict:
        result = {"page": f"p-{row}"}
        if ready is not None:
            result["ready"] = ready
        return {
            "schema": "m8m.cycle_control_request.v1",
            "milestone_id": "page_rendered",
            "inputs": {"row": row},
            "candidate": {"outputs": {"result": result}},
            "control": {
                "kind": "cycle",
                "cycle_id": "pages",
                "row": row,
                "round": 1,
                "max_rounds": 8,
                "pass_rule": "the page is ready",
            },
        }

    def test_pass_and_fail(self) -> None:
        self.assertEqual(tool.run(self.request(ready=True, row="001"))["cycle"], "pass")
        self.assertEqual(tool.run(self.request(ready=False, row="002"))["cycle"], "fail")

    def test_structurally_admitted_candidate_defaults_to_pass(self) -> None:
        receipt = tool.run(self.request(ready=None, row="001"))
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["cycle"], "pass")


if __name__ == "__main__":
    unittest.main()
