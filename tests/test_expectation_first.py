from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

import support  # noqa: F401

from flowstep_runtime import read_json, validate_against_schema
from milestone_expectation import (
    build_milestone_judge_request,
    derive_milestone_expectation,
)
from run_flow import advance


OPEN_INPUT = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
}
OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["outputs"],
    "properties": {
        "outputs": {
            "type": "object",
            "additionalProperties": False,
            "required": ["result"],
            "properties": {"result": {}},
        }
    },
}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _request(root: Path) -> Path:
    path = root / "request.json"
    _write_json(path, {"message": "hello"})
    return path


def _milestone(
    *, loop: str = "none", flow_id: str = "expectation_contract_v1"
) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "result_ready",
        "success": "The exact result is structurally complete.",
        "output_contract": "result_ready_v1",
        "output_schema": "schemas/output.json",
        "outputs": [
            {
                "id": "result",
                "name": "Exact result",
                "kind": "json",
                "cardinality": "one",
                "required": True,
            }
        ],
        "handler": "handler.py",
        "input_schema": "schemas/input.json",
        "inputs": {"request": "user.request"},
        "intelligence": "none",
        "execution": {
            "candidate_executor": {
                "ref": f"handler.{flow_id}.result_ready@3.1.0"
            },
            "tool_bindings": [],
        },
        "loop": loop,
        "on_tool_fail": "BLOCKED",
    }
    return row


class ExpectationFirstTests(unittest.TestCase):
    def test_projection_and_judge_request_match_closed_contracts(self) -> None:
        step = _milestone(loop="judge")
        step.update(
            {
                "worker": "result_ready_judge@1.0.0",
                "judge_abi": "m8m_milestone_judge_v1",
                "receipt_schema": "schemas/receipt.json",
                "max_attempts": 2,
            }
        )
        step["execution"]["judge"] = {"ref": "result_ready_judge@1.0.0"}
        expectation = derive_milestone_expectation(step)
        request = build_milestone_judge_request(
            step,
            attempt=1,
            inputs={"request": {"message": "hello"}},
            candidate={"outputs": {"result": {"value": 1}}},
        )
        contracts = support.SKILL_ROOT / "contracts"
        validate_against_schema(
            expectation,
            contracts / "m8m_milestone_expectation_v1.schema.json",
        )
        validate_against_schema(
            request,
            contracts / "m8m_milestone_judge_request_v1.schema.json",
        )
        self.assertEqual(
            set(request),
            {
                "schema",
                "milestone_id",
                "attempt",
                "max_attempts",
                "expectation",
                "inputs",
                "candidate",
            },
        )

    def test_structural_milestone_receives_expectation_and_calls_no_judge(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = root / "harness"
            harness.mkdir()
            flow = {
                "schema": "flowstep_flow_v4",
                "flow_id": "expectation_structural_v1",
                "version": 1,
                "milestones": [
                    _milestone(flow_id="expectation_structural_v1")
                ],
            }
            (harness / "flow.yaml").write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
            _write_json(harness / "schemas" / "input.json", OPEN_INPUT)
            _write_json(harness / "schemas" / "output.json", OUTPUT_SCHEMA)
            (harness / "handler.py").write_text(
                "import json\n"
                "from pathlib import Path\n"
                "def run(input_data, run_dir=None, **_):\n"
                "    Path(run_dir, 'seen-expectation.json').write_text(json.dumps(input_data['expectation']))\n"
                "    return {'outputs': {'result': {'message': input_data['request']['message']}}}\n",
                encoding="utf-8",
            )
            run_dir = root / "run"
            done = advance(harness, run_dir, request_path=_request(root))
            self.assertEqual(done["state"], "COMPLETE", done)
            self.assertEqual(
                read_json(run_dir / "seen-expectation.json"),
                derive_milestone_expectation(
                    _milestone(flow_id="expectation_structural_v1")
                ),
            )
            self.assertEqual(
                read_json(run_dir / "runtime-tasks" / "result_ready.json")["expectation"],
                derive_milestone_expectation(
                    _milestone(flow_id="expectation_structural_v1")
                ),
            )
            self.assertEqual(
                read_json(run_dir / "runtime-tasks" / "result_ready.json")["execution"],
                _milestone(flow_id="expectation_structural_v1")["execution"],
            )
            receipt = read_json(
                run_dir / "milestones" / "result_ready" / "out" / "judge-receipt.json"
            )
            self.assertEqual(receipt["judge_ref"], "m8m_structural_admission@1.0.0")
            self.assertEqual(
                receipt["reasons"],
                [
                    "Candidate passed the declared output and schema admission; no semantic judge was invoked."
                ],
            )

    def test_missing_media_blocks_before_semantic_judge_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = root / "harness"
            tool = harness / "flowsteps" / "tools" / "image_ready_judge"
            tool.mkdir(parents=True)
            milestone = _milestone(
                loop="judge", flow_id="expectation_media_v1"
            )
            milestone.update(
                {
                    "success": "The image is publishable.",
                    "outputs": [
                        {
                            "id": "result",
                            "name": "Publishable image",
                            "kind": "image",
                            "cardinality": "one",
                            "required": True,
                        }
                    ],
                    "worker": "image_ready_judge@1.0.0",
                    "judge_abi": "m8m_milestone_judge_v1",
                    "receipt_schema": "flowsteps/tools/image_ready_judge/output.schema.json",
                    "max_attempts": 2,
                }
            )
            milestone["execution"]["judge"] = {"ref": "image_ready_judge@1.0.0"}
            flow = {
                "schema": "flowstep_flow_v4",
                "flow_id": "expectation_media_v1",
                "version": 1,
                "milestones": [milestone],
            }
            (harness / "flow.yaml").write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
            _write_json(harness / "schemas" / "input.json", OPEN_INPUT)
            _write_json(harness / "schemas" / "output.json", OUTPUT_SCHEMA)
            (harness / "handler.py").write_text(
                "def run(input_data, **_):\n"
                "    return {'outputs': {'result': {'path': 'missing.png'}}}\n",
                encoding="utf-8",
            )
            (tool / "tool.py").write_text(
                "from pathlib import Path\n"
                "def run(input_data, **_):\n"
                "    Path(__file__).with_name('CALLED').write_text('yes')\n"
                "    return {'decision': 'PASS', 'reasons': ['accepted'], 'blockers': []}\n",
                encoding="utf-8",
            )
            _write_json(tool / "input.schema.json", {"type": "object"})
            _write_json(
                tool / "output.schema.json",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["decision", "reasons", "blockers"],
                    "properties": {
                        "decision": {"enum": ["PASS", "RETRY", "BLOCKED"]},
                        "reasons": {"type": "array", "items": {"type": "string"}},
                        "blockers": {"type": "array", "items": {"type": "string"}},
                    },
                },
            )
            run_dir = root / "run"
            done = advance(harness, run_dir, request_path=_request(root))
            self.assertEqual(done["state"], "BLOCKED", done)
            self.assertFalse((tool / "CALLED").exists())
            self.assertFalse(
                (run_dir / "milestones" / "result_ready" / "out" / "chosen-output.json").exists()
            )

    def test_retry_writes_bounded_candidate_request_and_isolated_attempt_capsule(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = root / "harness"
            tool = harness / "flowsteps" / "tools" / "result_ready_judge"
            tool.mkdir(parents=True)
            milestone = _milestone(
                loop="judge", flow_id="expectation_retry_v1"
            )
            milestone.update(
                {
                    "intelligence": "completion",
                    "draft_schema": "schemas/draft.json",
                    "on_tool_fail": "need_model",
                    "worker": "result_ready_judge@1.0.0",
                    "judge_abi": "m8m_milestone_judge_v1",
                    "receipt_schema": "flowsteps/tools/result_ready_judge/output.schema.json",
                    "max_attempts": 3,
                }
            )
            milestone["execution"]["judge"] = {
                "ref": "result_ready_judge@1.0.0"
            }
            flow = {
                "schema": "flowstep_flow_v4",
                "flow_id": "expectation_retry_v1",
                "version": 1,
                "milestones": [milestone],
            }
            (harness / "flow.yaml").write_text(
                yaml.safe_dump(flow, sort_keys=False), encoding="utf-8"
            )
            _write_json(harness / "schemas" / "input.json", OPEN_INPUT)
            _write_json(harness / "schemas" / "output.json", OUTPUT_SCHEMA)
            _write_json(harness / "schemas" / "draft.json", {"type": "object"})
            (harness / "handler.py").write_text(
                "def run(input_data, task=None, draft=None, **_):\n"
                "    if task['attempt'] > 1 and draft is None:\n"
                "        return {'_flowstep': 'NEED_MODEL', 'model_request': "
                "{'instruction': 'refine the candidate'}}\n"
                "    if draft is not None:\n"
                "        return {'outputs': {'result': {'message': draft['message']}}}\n"
                "    return {'outputs': {'result': {'message': 'first'}}}\n",
                encoding="utf-8",
            )
            (tool / "tool.py").write_text(
                "def run(input_data, **_):\n"
                "    message = input_data['candidate']['outputs']['result']['message']\n"
                "    if message == 'refined':\n"
                "        return {'decision': 'PASS', 'reasons': ['accepted'], 'blockers': []}\n"
                "    return {'decision': 'RETRY', 'reasons': ['  Needs   a clearer result.  '], "
                "'blockers': ['missing detail']}\n",
                encoding="utf-8",
            )
            _write_json(tool / "input.schema.json", {"type": "object"})
            _write_json(
                tool / "output.schema.json",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["decision", "reasons", "blockers"],
                    "properties": {
                        "decision": {"enum": ["PASS", "RETRY", "BLOCKED"]},
                        "reasons": {"type": "array", "items": {"type": "string"}},
                        "blockers": {"type": "array", "items": {"type": "string"}},
                    },
                },
            )
            run_dir = root / "run"
            action = advance(harness, run_dir, request_path=_request(root))
            self.assertEqual(action["state"], "ACTION_REQUIRED", action)
            self.assertEqual(action["attempt"], 2)
            candidate_request = read_json(run_dir / action["task_path"])
            self.assertEqual(candidate_request["attempt"], 2)
            self.assertEqual(candidate_request["execution"], milestone["execution"])
            self.assertEqual(
                candidate_request["prior_feedback"],
                {
                    "judge_attempt": 1,
                    "reasons": ["Needs a clearer result."],
                    "blockers": ["missing detail"],
                },
            )
            capsule = read_json(run_dir / action["context_capsule_path"])
            self.assertEqual(capsule["attempt"], 2)
            self.assertEqual(
                capsule["prior_feedback"], candidate_request["prior_feedback"]
            )
            self.assertIn(
                str((run_dir / action["task_path"]).resolve()),
                capsule["allowed_files"],
            )
            self.assertFalse(capsule["chat_history_allowed"])
            draft = root / "draft.json"
            _write_json(draft, {"message": "refined"})
            done = advance(
                harness,
                run_dir,
                draft_path=draft,
                draft_for="result_ready",
            )
            self.assertEqual(done["state"], "COMPLETE", done)
            receipt = read_json(
                run_dir
                / "milestones"
                / "result_ready"
                / "out"
                / "judge-receipt.json"
            )
            self.assertEqual(receipt["attempt"], 2)
            self.assertFalse(
                (
                    run_dir
                    / "runtime-tasks"
                    / "result_ready"
                    / "attempt-003"
                    / "candidate-request.json"
                ).exists()
            )


if __name__ == "__main__":
    unittest.main()
