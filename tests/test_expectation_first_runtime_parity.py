"""Common linear expectation-first runtime profile shared with the platform."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import yaml

import support  # noqa: F401
from flowstep_runtime import FlowError, load_flow, read_json
from milestone_expectation import derive_milestone_expectation
from run_flow import advance
from session_layout import admit_candidate
from support import SKILL_ROOT


FIXTURE_ROOT = (
    SKILL_ROOT / "contracts" / "conformance" / "expectation_first_runtime"
)
FIXTURE_SHA256 = {
    "common-linear.json": "2604fc1ec29ef6bbef1cd766fbe66de782b4a90de76767af798005e7d341d7b4",
    "candidate-cases.json": "04a69a059492997eb6a469acb9d89039d9d11e1a6f9887870248dc02b4948955",
    "schemas/source.output.schema.json": "a7ec995bc3ba0c1e93db9f07861a454754ad709801185de9d5b42699561c9200",
    "schemas/result.output.schema.json": "c55a3fddfab0fb9744fae53f0db43c265ad2182866a9d25b79b386738c3b798e",
    "schemas/judge-decision.schema.json": "4aa862480ad2b8662092bbcdd06d6232ace898b3b55c2f3ad862f2d1270ffc10",
}


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _prepare_harness(root: Path, flow: dict, cases: dict) -> Path:
    harness = root / "harness"
    harness.mkdir(parents=True)
    (harness / "flow.yaml").write_text(
        yaml.safe_dump(flow, sort_keys=False), encoding="utf-8"
    )
    for relative in FIXTURE_SHA256:
        if not relative.startswith("schemas/"):
            continue
        destination = harness / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((FIXTURE_ROOT / relative).read_bytes())
    judge_schema = json.loads(
        (FIXTURE_ROOT / "schemas/judge-decision.schema.json").read_text(
            encoding="utf-8"
        )
    )
    for milestone_id in ("source_ready", "result_ready"):
        _json(
            harness / "milestones" / milestone_id / "input.schema.json",
            {"type": "object", "additionalProperties": True},
        )
    for milestone in flow["milestones"]:
        gem = harness / str(milestone["gem"])
        gem.parent.mkdir(parents=True, exist_ok=True)
        gem.write_text(f"# {milestone['id']}\n", encoding="utf-8")

    handlers = harness / "handlers"
    handlers.mkdir()
    (handlers / "source.py").write_text(
        "def run(input_data, **_):\n"
        "    return {'outputs': {'source': {'number': 7}}}\n",
        encoding="utf-8",
    )
    (handlers / "result.py").write_text(
        "def run(input_data, **_):\n"
        "    return {'outputs': {'result': {\n"
        "        'source_value': input_data['source']['number'],\n"
        "        'approved': True,\n"
        "    }}}\n",
        encoding="utf-8",
    )

    for package, result in (
        ("bind_source", cases["valid_candidates"]["source_ready"]),
        ("build_result", cases["valid_candidates"]["result_ready"]),
    ):
        tool = harness / "flowsteps" / "tools" / package
        tool.mkdir(parents=True)
        (tool / "tool.py").write_text(
            "def run(input_data, **_):\n"
            f"    return {result!r}\n",
            encoding="utf-8",
        )
        _json(tool / "input.schema.json", {"type": "object"})
        _json(tool / "output.schema.json", {"type": "object"})

    judge = harness / "flowsteps" / "tools" / "result_ready_judge"
    judge.mkdir(parents=True)
    (judge / "tool.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "def run(input_data, **_):\n"
        "    Path(__file__).with_name('seen-request.json').write_text(\n"
        "        json.dumps(input_data, sort_keys=True), encoding='utf-8')\n"
        f"    return {cases['judge_decision']!r}\n",
        encoding="utf-8",
    )
    _json(judge / "input.schema.json", {"type": "object"})
    _json(judge / "output.schema.json", judge_schema)
    return harness


class ExpectationFirstRuntimeParityTests(unittest.TestCase):
    def test_common_linear_profile_admission_judge_and_tool_identity(self) -> None:
        for relative, expected in FIXTURE_SHA256.items():
            self.assertEqual(
                hashlib.sha256((FIXTURE_ROOT / relative).read_bytes()).hexdigest(),
                expected,
            )
        flow_path = FIXTURE_ROOT / "common-linear.json"
        cases_path = FIXTURE_ROOT / "candidate-cases.json"
        flow_source = json.loads(flow_path.read_text(encoding="utf-8"))
        cases = json.loads(cases_path.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = _prepare_harness(root, flow_source, cases)
            flow = load_flow(harness, harness / "flow.yaml")
            by_id = {item["id"]: item for item in flow["steps"]}
            self.assertEqual(
                {key: derive_milestone_expectation(by_id[key]) for key in by_id},
                cases["expected_expectations"],
            )

            for index, case in enumerate(cases["admission_cases"], start=1):
                admitted = True
                try:
                    admit_candidate(
                        root / f"admission-{index}",
                        harness,
                        by_id[case["milestone_id"]],
                        deepcopy(case["candidate"]),
                    )
                except FlowError:
                    admitted = False
                self.assertEqual(admitted, case["admitted"], case["id"])

            request_path = root / "request.json"
            _json(request_path, {})
            run_dir = root / "run"
            done = advance(harness, run_dir, request_path=request_path)
            self.assertEqual(done["state"], "COMPLETE", done)
            seen = read_json(
                harness
                / "flowsteps"
                / "tools"
                / "result_ready_judge"
                / "seen-request.json"
            )
            self.assertEqual(seen, cases["expected_judge_request"])
            source_receipt = read_json(
                run_dir / "milestones/source_ready/out/judge-receipt.json"
            )
            result_receipt = read_json(
                run_dir / "milestones/result_ready/out/judge-receipt.json"
            )
            self.assertEqual(source_receipt, cases["expected_structural_receipt"])
            self.assertEqual(result_receipt, cases["expected_semantic_receipt"])

            mismatch = deepcopy(flow_source)
            mismatch["milestones"][0]["execution"]["tool_bindings"][0]["ref"] = (
                "other_source@1.0.0"
            )
            mismatch_path = root / "mismatch.yaml"
            mismatch_path.write_text(
                yaml.safe_dump(mismatch, sort_keys=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(FlowError, "must exactly equal flowstep.tool"):
                load_flow(harness, mismatch_path)

            unknown = deepcopy(flow_source)
            unknown["milestones"][0]["flowsteps"][0]["tool"] = (
                "unknown_source@1.0.0"
            )
            unknown["milestones"][0]["execution"]["tool_bindings"][0]["ref"] = (
                "unknown_source@1.0.0"
            )
            unknown_harness = _prepare_harness(root / "unknown", unknown, cases)
            unknown_run = root / "unknown-run"
            blocked = advance(unknown_harness, unknown_run, request_path=request_path)
            self.assertEqual(blocked["state"], "BLOCKED", blocked)
            self.assertIn("bound FlowStep tool", json.dumps(blocked))


if __name__ == "__main__":
    unittest.main()
