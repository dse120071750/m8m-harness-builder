"""Canonical v4 and expectation-first contract conformance.

The digest and fixtures are shared with the platform suite.  Platform runtime
capability restrictions are semantic checks layered after this portable shape.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator, ValidationError
import yaml
from support import SKILL_ROOT
from flowstep_runtime import FlowError, load_flow


CONTRACTS = SKILL_ROOT / "contracts"
CONFORMANCE = CONTRACTS / "conformance" / "flowstep_flow_v4"
FLOW_SCHEMA_SHA256 = "5092331c14d18031c4f2fa1e6ac585484afa83fd45f3c7508bb707560a2780fd"
FIXTURE_SHA256 = {
    "valid/authoring-union.json": "d1a1964d023d3a8f82af3ae7e4a9ec082ccd4c7b032f9951ab6de6e0a0d83940",
    "valid/platform-dag.json": "11d9fe3495733fe1a55f4f4a788ead639f6e5e9f88f884311ef821e941fa7f3d",
    "invalid/judge-loop-unbound.json": "4b610c35a82ead8d78d9b9235fd8084b56688c5f57ef4364cae55513602cbae9",
    "invalid/malformed-execution.json": "86157f17276555267bb8ac1886e1ab2af910bd1d67fe3658680d56e13b73022f",
    "invalid/no-required-output.json": "a79d997370e0b3545db648c54e188acfca8704b646e0a11720dbf13d781a40ba",
    "invalid/nonjudge-bound.json": "823a319c66f7233abef02ebc6709c98cb80d64007deae6866cd47a39159d5a86",
    "invalid/unknown-field.json": "8d1312171b1dfe4cd41e1b1d25010f316e769fdc3ee9b8238aaa16a6395706c3",
}


def load_schema(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def milestone() -> dict:
    return {
        "id": "render_ready",
        "success": "One approved render and its receipt satisfy the brief.",
        "output_contract": "m8m.render_bundle.v1",
        "output_schema": "schemas/render-bundle.schema.json",
        "outputs": [
            {
                "id": "preview",
                "name": "Approved preview",
                "kind": "image",
                "cardinality": "one",
                "required": True,
            },
            {
                "id": "notes",
                "name": "Optional review notes",
                "kind": "json",
                "cardinality": "one",
                "required": False,
            },
        ],
        "handler": "handlers/render.py",
    }


def expectation(source: dict) -> dict:
    return {
        "schema": "m8m.milestone_expectation.v1",
        "milestone_id": source["id"],
        "success": source["success"],
        "output_contract": source["output_contract"],
        "output_schema_ref": source["output_schema"],
        "outputs": source["outputs"],
    }


class ExpectationFirstContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.expectation_schema = load_schema("m8m_milestone_expectation_v1.schema.json")
        cls.judge_schema = load_schema("m8m_milestone_judge_request_v1.schema.json")
        cls.flow_schema = load_schema("flowstep_flow_v4.schema.json")
        for schema in (cls.expectation_schema, cls.judge_schema, cls.flow_schema):
            Draft202012Validator.check_schema(schema)
        cls.expectation_validator = Draft202012Validator(cls.expectation_schema)
        cls.judge_validator = Draft202012Validator(cls.judge_schema)
        cls.flow_validator = Draft202012Validator(cls.flow_schema)

    def test_normative_v4_bytes_and_shared_conformance_fixtures(self) -> None:
        schema_bytes = (CONTRACTS / "flowstep_flow_v4.schema.json").read_bytes()
        self.assertEqual(hashlib.sha256(schema_bytes).hexdigest(), FLOW_SCHEMA_SHA256)
        for relative, expected_digest in FIXTURE_SHA256.items():
            path = CONFORMANCE / relative
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected_digest)
            candidate = json.loads(path.read_text(encoding="utf-8"))
            if relative.startswith("valid/"):
                self.flow_validator.validate(candidate)
            else:
                with self.assertRaises(ValidationError, msg=relative):
                    self.flow_validator.validate(candidate)

    def test_local_runtime_restriction_is_after_contract_admission(self) -> None:
        with self.assertRaisesRegex(
            FlowError,
            "contract-valid, but the Builder local runtime does not execute explicit DAGs",
        ):
            load_flow(SKILL_ROOT, CONFORMANCE / "valid" / "platform-dag.json")

    def test_builder31_local_admission_requires_closed_execution_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            flow_path = root / "flow.yaml"
            base = {
                "schema": "flowstep_flow_v4",
                "flow_id": "execution_cutover_v1",
                "version": 1,
                "milestones": [milestone()],
            }
            flow_path.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(
                FlowError,
                "missing closed execution binding; regenerate with m8m-harness-builder 3.1",
            ):
                load_flow(root, flow_path)

            declared = deepcopy(milestone())
            declared["tools"] = ["render_tool"]
            declared["execution"] = {
                "candidate_executor": {
                    "ref": "handler.execution_cutover_v1.render_ready@3.1.0"
                },
                "tool_bindings": [],
            }
            mismatched = deepcopy(declared)
            mismatched["execution"]["candidate_executor"]["ref"] = (
                "handler.unknown.render_ready@3.1.0"
            )
            base["milestones"] = [mismatched]
            flow_path.write_text(
                yaml.safe_dump(base, sort_keys=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                FlowError,
                "candidate_executor.ref must be "
                "handler.execution_cutover_v1.render_ready@3.1.0; "
                "regenerate with m8m-harness-builder 3.1",
            ):
                load_flow(root, flow_path)

            base["milestones"] = [declared]
            flow_path.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(
                FlowError,
                r"tool_bindings differ from FlowStep ids; missing=\['render_tool'\]",
            ):
                load_flow(root, flow_path)

            declared["tools"] = []
            declared["loop"] = "judge"
            declared["worker"] = "render_ready_judge@1.0.0"
            declared["judge_abi"] = "m8m_milestone_judge_v1"
            declared["receipt_schema"] = "schemas/render-judge.schema.json"
            declared["max_attempts"] = 2
            base["milestones"] = [declared]
            flow_path.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(
                FlowError,
                "schema flowstep_flow_v4.schema.json failed.*'judge' is a required property",
            ):
                load_flow(root, flow_path)

            declared["execution"]["judge"] = {"ref": "other_judge@1.0.0"}
            flow_path.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(
                FlowError,
                "worker must exactly equal execution.judge.ref; regenerate with m8m-harness-builder 3.1",
            ):
                load_flow(root, flow_path)

            declared["loop"] = "none"
            declared.pop("worker")
            declared.pop("judge_abi")
            declared.pop("receipt_schema")
            declared.pop("max_attempts")
            declared["execution"]["judge"] = {"ref": "render_ready_judge@1.0.0"}
            flow_path.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(FlowError, "schema flowstep_flow_v4.schema.json failed"):
                load_flow(root, flow_path)

    def test_v4_projection_is_a_closed_run_independent_expectation(self) -> None:
        projected = expectation(milestone())
        self.expectation_validator.validate(projected)
        self.assertEqual(
            set(projected),
            {
                "schema",
                "milestone_id",
                "success",
                "output_contract",
                "output_schema_ref",
                "outputs",
            },
        )
        for forbidden in ("run_id", "chat", "cache", "judge_ref", "guidance_ref"):
            self.assertNotIn(forbidden, json.dumps(projected))

    def test_expectation_and_v4_each_require_one_required_output(self) -> None:
        projected = expectation(milestone())
        projected["outputs"] = [
            {**item, "required": False} for item in projected["outputs"]
        ]
        with self.assertRaises(ValidationError):
            self.expectation_validator.validate(projected)

        flow = {
            "schema": "flowstep_flow_v4",
            "flow_id": "render_flow",
            "version": 1,
            "milestones": [{**milestone(), "outputs": projected["outputs"]}],
        }
        with self.assertRaises(ValidationError):
            self.flow_validator.validate(flow)

    def test_normative_v4_closes_semantic_judge_bindings(self) -> None:
        judged = milestone()
        judged.update(
            {
                "loop": "judge",
                "worker": "render_judge@1.0.0",
                "judge_abi": "m8m_milestone_judge_v1",
                "execution": {
                    "candidate_executor": {
                        "ref": "handler.render_flow.render_ready@3.1.0"
                    },
                    "judge": {"ref": "render_judge@1.0.0"},
                    "tool_bindings": [],
                },
            }
        )
        flow = {
            "schema": "flowstep_flow_v4",
            "flow_id": "render_flow",
            "version": 1,
            "milestones": [judged],
        }
        self.flow_validator.validate(flow)

        cases = {
            "worker": lambda item: item.pop("worker"),
            "judge_abi": lambda item: item.pop("judge_abi"),
            "execution.judge": lambda item: item["execution"].pop("judge"),
        }
        for label, mutate in cases.items():
            with self.subTest(missing=label):
                invalid = deepcopy(flow)
                mutate(invalid["milestones"][0])
                with self.assertRaises(ValidationError):
                    self.flow_validator.validate(invalid)

        nonjudged = deepcopy(flow)
        nonjudged["milestones"][0]["loop"] = "none"
        with self.assertRaises(ValidationError):
            self.flow_validator.validate(nonjudged)

    def test_normative_expectation_strings_are_nonblank_and_trimmed(self) -> None:
        flow = {
            "schema": "flowstep_flow_v4",
            "flow_id": "render_flow",
            "version": 1,
            "milestones": [milestone()],
        }
        mutations = {
            "success": lambda item: item.__setitem__("success", "   "),
            "output_contract": lambda item: item.__setitem__("output_contract", "   "),
            "output_schema": lambda item: item.__setitem__("output_schema", "   "),
            "output.id": lambda item: item["outputs"][0].__setitem__("id", "   "),
            "output.name": lambda item: item["outputs"][0].__setitem__("name", "   "),
        }
        for label, mutate in mutations.items():
            with self.subTest(field=label):
                invalid = deepcopy(flow)
                mutate(invalid["milestones"][0])
                with self.assertRaises(ValidationError):
                    self.flow_validator.validate(invalid)

        for field in ("success", "output_contract"):
            with self.subTest(expectation_field=field):
                invalid_expectation = expectation(milestone())
                invalid_expectation[field] = "   "
                with self.assertRaises(ValidationError):
                    self.expectation_validator.validate(invalid_expectation)

    def test_judge_request_has_only_expectation_inputs_and_current_candidate(self) -> None:
        self.assertEqual(
            self.judge_schema["properties"]["expectation"]["$ref"],
            "#/$defs/expectation",
        )
        self.assertNotIn(
            "m8m_milestone_expectation_v1.schema.json",
            json.dumps(self.judge_schema),
        )
        request = {
            "schema": "m8m.milestone_judge_request.v1",
            "milestone_id": "render_ready",
            "attempt": 1,
            "max_attempts": 3,
            "expectation": expectation(milestone()),
            "inputs": {"brief": {"room": "living"}},
            "candidate": {
                "outputs": {
                    "preview": {"path": "milestones/render_ready/work/candidate.png"},
                    "notes": {"text": "balanced composition"},
                }
            },
        }
        self.judge_validator.validate(request)
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
        for field in ("judge_ref", "guidance_ref", "run_id", "cache_key"):
            invalid = {**request, field: "forbidden"}
            with self.assertRaises(ValidationError):
                self.judge_validator.validate(invalid)
        invalid_candidate = deepcopy(request)
        invalid_candidate["candidate"]["receipt"] = {"ok": True}
        with self.assertRaises(ValidationError):
            self.judge_validator.validate(invalid_candidate)


if __name__ == "__main__":
    unittest.main()
