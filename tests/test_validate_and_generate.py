from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import yaml

from support import EXAMPLE

from flowstep_instruction import mark_step, parse_statuses, write_instruction
from generate_harness import generate_from_audit, generate_harness, generate_v3_flow
from flowstep_runtime import FlowError, load_flow
from flowstep_tools import validate_library_tool
from validate_harness import validate_harness


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _explicit_spec(milestone_id: str, *, flow_id: str, tool: str) -> dict:
    return {
        "id": milestone_id,
        "success": f"{milestone_id} produces the declared exact result.",
        "output_contract": f"{milestone_id}_v1",
        "output_schema_object": {
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        "outputs": [
            {
                "id": "result",
                "name": "Exact result",
                "kind": "json",
                "cardinality": "one",
                "required": True,
            }
        ],
        "intelligence": "none",
        "tools": [tool],
        "flowsteps": [
            {"id": tool, "tool": f"{tool}@1.0.0"}
        ],
        "execution": {
            "candidate_executor": {
                "ref": f"handler.{flow_id}.{milestone_id}@3.1.0"
            },
            "tool_bindings": [
                {"tool": tool, "ref": f"{tool}@1.0.0"}
            ],
        },
    }


class ValidateHarnessTests(unittest.TestCase):
    def test_example_pipeline_passes(self) -> None:
        result = validate_harness(EXAMPLE)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["steps"], ["ingest", "segment", "label"])
        suite = unittest.TestSuite()
        loader = unittest.defaultTestLoader
        for test_file in sorted((EXAMPLE / "steps").glob("*/tests/test_tool.py")):
            name = f"example_{test_file.parent.parent.name}_tests"
            spec = importlib.util.spec_from_file_location(name, test_file)
            self.assertIsNotNone(spec)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            suite.addTests(loader.loadTestsFromModule(module))
        self.assertGreater(suite.countTestCases(), 0)
        result_tests = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result_tests.wasSuccessful())

    def test_v1_flow_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "legacy"
            (skill / "flows").mkdir(parents=True)
            (skill / "flows" / "old.yaml").write_text(
                "\n".join(
                    [
                        "schema: flowstep_flow_v1",
                        "flow_id: old_v1",
                        "version: 1",
                        "persistent_worker: worker",
                        "max_subagent_roles: 0",
                        "max_run_repair_cycles: 0",
                        "max_run_seconds: 60",
                        "steps:",
                        "  - id: plan",
                        "    kind: plan",
                        "    assigned_agent: worker",
                        "    inputs: {request: user.request}",
                        "    params: {execution_mode: in_process}",
                        "    output_contract: plan_v1",
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(FlowError) as ctx:
                validate_harness(skill)
            self.assertIn("flowstep_flow_v1", str(ctx.exception))

    def test_v4_build_required_tool_fails_full_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generated = generate_v3_flow(
                codebase,
                "build_required_v1",
                ["candidate_ready"],
                tools=["not_implemented_tool"],
                milestone_specs=[
                    _explicit_spec(
                        "candidate_ready",
                        flow_id="build_required_v1",
                        tool="not_implemented_tool",
                    )
                ],
            )
            self.assertEqual(generated["status"], "BUILD_REQUIRED")
            with self.assertRaisesRegex(FlowError, "BUILD_REQUIRED"):
                validate_harness(Path(generated["harness_dir"]))

    def test_removing_runtime_marker_does_not_make_piecemeal_harness_runnable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generated = generate_v3_flow(
                codebase,
                "runtime_required_v1",
                ["candidate_ready"],
                tools=["hash_bind"],
                milestone_specs=[
                    _explicit_spec(
                        "candidate_ready",
                        flow_id="runtime_required_v1",
                        tool="hash_bind",
                    )
                ],
            )
            harness = Path(generated["harness_dir"])
            (harness / "BUILD_REQUIRED_RUNTIME").unlink()
            with self.assertRaisesRegex(
                FlowError, "codebase-owned runtime packaging is incomplete"
            ):
                validate_harness(harness)

    def test_v4_requires_at_least_one_required_business_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generated = generate_v3_flow(
                codebase,
                "optional_only_v1",
                ["candidate_ready"],
                tools=["hash_bind"],
                milestone_specs=[
                    _explicit_spec(
                        "candidate_ready",
                        flow_id="optional_only_v1",
                        tool="hash_bind",
                    )
                ],
            )
            harness = Path(generated["harness_dir"])
            flow_path = harness / "flow.yaml"
            flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
            for output in flow["milestones"][0]["outputs"]:
                output["required"] = False
            flow_path.write_text(
                yaml.safe_dump(flow, sort_keys=False),
                encoding="utf-8",
                newline="\n",
            )

            with self.assertRaisesRegex(
                FlowError,
                r"flowstep_flow_v4\.schema\.json failed at \$\.milestones\[0\]\.outputs",
            ):
                load_flow(harness)

    def test_v4_input_passthrough_tools_cannot_validate_as_runnable(self) -> None:
        sources = {
            "direct": "def run(input_data, **_):\n    return input_data\n",
            "alias": (
                "def run(input_data, **_):\n"
                "    candidate = input_data\n"
                "    result = candidate\n"
                "    return result\n"
            ),
        }
        for label, source in sources.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                codebase = Path(temp) / "repo"
                generated = generate_v3_flow(
                    codebase,
                    f"passthrough_{label}_v1",
                    ["candidate_ready"],
                    tools=["passthrough_tool"],
                    milestone_specs=[
                        _explicit_spec(
                            "candidate_ready",
                            flow_id=f"passthrough_{label}_v1",
                            tool="passthrough_tool",
                        )
                    ],
                )
                tool_dir = codebase / "flowsteps" / "tools" / "passthrough_tool"
                _write(tool_dir / "tool.py", source)
                _write(
                    tool_dir / "input.schema.json",
                    '{"type":"object","additionalProperties":true}\n',
                )
                _write(
                    tool_dir / "output.schema.json",
                    '{"type":"object","additionalProperties":false,'
                    '"required":["value"],"properties":{"value":{"type":"string"}}}\n',
                )
                _write(
                    tool_dir / "tests" / "test_tool.py",
                    "def test_declared_shape():\n    assert True\n",
                )
                (tool_dir / "BUILD_REQUIRED").unlink()

                with self.assertRaisesRegex(
                    FlowError,
                    "identity tool / input passthrough is forbidden",
                ):
                    validate_harness(Path(generated["harness_dir"]))

    def test_missing_expectation_authority_emits_no_canonical_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generated = generate_v3_flow(
                codebase,
                "authority_missing_v1",
                ["candidate_ready"],
                tools=["hash_bind"],
            )
            self.assertEqual(generated["status"], "BUILD_REQUIRED")
            self.assertFalse(generated["runnable"])
            self.assertEqual(
                generated["build_required_milestones"]["candidate_ready"],
                [
                    "success",
                    "output_contract",
                    "output_schema",
                    "outputs",
                    "execution.candidate_executor",
                ],
            )
            self.assertFalse(
                (Path(generated["harness_dir"]) / "flow.yaml").exists()
            )

    def test_audit_inference_is_proposal_only_and_cannot_emit_or_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            audit = {
                "audited_skill": {"name": "proposal-only"},
                "grade": {"flow_schema": "legacy-proposal"},
                "python_standardization": [],
                "proposed_milestones": [
                    {
                        "id": "quality_ready",
                        "success": "The proposed candidate appears usable.",
                        "output_contract": "quality_ready_v1",
                        "output_schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["result"],
                            "properties": {"result": {"type": "string"}},
                        },
                        "outputs": [
                            {
                                "id": "result",
                                "name": "Proposed result",
                                "kind": "json",
                                "cardinality": "one",
                                "required": True,
                            }
                        ],
                        "loop": "judge",
                        "worker": "quality_ready_judge@1.0.0",
                        "judge_abi": "m8m_milestone_judge_v1",
                        "receipt_schema": "schemas/quality_ready_receipt_v1.json",
                        "max_attempts": 2,
                        "_judge_inferred": True,
                        "execution": {
                            "candidate_executor": {
                                "ref": "handler.proposal.quality_ready@1.0.0"
                            },
                            "judge": {
                                "ref": "quality_ready_judge@1.0.0"
                            },
                            "tool_bindings": [],
                        },
                    }
                ],
            }

            generated = generate_from_audit(
                codebase,
                audit,
                flow_id="proposal_only_v1",
                skill_name="proposal-only",
            )

            self.assertEqual(generated["status"], "BUILD_REQUIRED")
            self.assertFalse(generated["runnable"])
            gaps = generated["build_required_milestones"]["quality_ready"]
            self.assertIn("authored_judge_authority", gaps)
            self.assertIn("authored_expectation_authority", gaps)
            harness = Path(generated["harness_dir"])
            self.assertFalse((harness / "flow.yaml").exists())
            self.assertFalse((codebase / ".agents" / "skills" / "proposal-only").exists())
            self.assertFalse((codebase / ".claude" / "skills" / "proposal-only").exists())

    def test_authored_expectation_still_requires_candidate_handler_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generated = generate_v3_flow(
                codebase,
                "handler_required_v1",
                ["candidate_ready"],
                tools=["hash_bind"],
                milestone_specs=[
                    _explicit_spec(
                        "candidate_ready",
                        flow_id="handler_required_v1",
                        tool="hash_bind",
                    )
                ],
            )
            self.assertEqual(generated["status"], "BUILD_REQUIRED")
            self.assertFalse(generated["runnable"])
            self.assertTrue(generated["build_required_runtime"])
            self.assertEqual(generated["build_required_handlers"], ["candidate_ready"])
            handler = Path(generated["harness_dir"]) / "milestones" / "candidate_ready" / "assemble.py"
            self.assertTrue(handler.is_file())
            with self.assertRaisesRegex(FlowError, "candidate handler declares BUILD_REQUIRED"):
                validate_harness(Path(generated["harness_dir"]))

    @unittest.skip("legacy v2 generator removed by the M8M 2.0 hard cutover")
    def test_missing_tool_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "broken"
            generate_harness(skill, flow_id="broken_v1", step_ids=["alpha"])
            (skill / "steps" / "alpha" / "tool.py").unlink()
            with self.assertRaises(FlowError) as ctx:
                validate_harness(skill)
            self.assertIn("missing tool", str(ctx.exception).lower())

    @unittest.skip("legacy v2 generator removed by the M8M 2.0 hard cutover")
    def test_generated_stubs_fail_validate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "stubs"
            generate_harness(skill, flow_id="stubs_v1", step_ids=["alpha"])
            with self.assertRaises(FlowError) as ctx:
                validate_harness(skill)
            message = str(ctx.exception)
            self.assertIn("generated stub", message)
            self.assertIn("{ok: boolean}", message)

    @unittest.skip("legacy v2 generator removed by the M8M 2.0 hard cutover")
    def test_identity_tool_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "identity"
            generate_harness(skill, flow_id="identity_v1", step_ids=["alpha"])
            _write(
                skill / "steps" / "alpha" / "tool.py",
                "def run(input_data, draft=None, **_):\n    return draft\n",
            )
            _write(
                skill / "steps" / "alpha" / "output.schema.json",
                '{"type":"object","required":["label"],"properties":{"label":{"type":"string"}}}\n',
            )
            _write(
                skill / "steps" / "alpha" / "tests" / "test_tool.py",
                "def test_ok():\n    assert True\n",
            )
            with self.assertRaises(FlowError) as ctx:
                validate_harness(skill)
            self.assertIn("identity tool", str(ctx.exception))

    @unittest.skip("legacy v2 generator removed by the M8M 2.0 hard cutover")
    def test_model_without_justification_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "unjustified"
            generate_harness(skill, flow_id="unjustified_v1", step_ids=["alpha"])
            flow = (skill / "flows" / "unjustified_v1.yaml").read_text(encoding="utf-8")
            flow = flow.replace("class: tool", "class: intelligence", 1).replace("model: none", "model: completion", 1)
            (skill / "flows" / "unjustified_v1.yaml").write_text(flow, encoding="utf-8")
            with self.assertRaises(FlowError) as ctx:
                validate_harness(skill)
            self.assertIn("model_justification", str(ctx.exception))

    @unittest.skip("v4 file candidates require an existing path; selection hashes are not required")
    def test_path_without_hash_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "sidecar"
            generate_harness(skill, flow_id="sidecar_v1", step_ids=["alpha"])
            _write(
                skill / "steps" / "alpha" / "tool.py",
                "def run(input_data, draft=None, **_):\n    return {'plan_path': 'plan.json'}\n",
            )
            _write(
                skill / "steps" / "alpha" / "output.schema.json",
                '{"type":"object","required":["plan_path"],"properties":{"plan_path":{"type":"string"}}}\n',
            )
            _write(
                skill / "steps" / "alpha" / "tests" / "test_tool.py",
                "def test_ok():\n    assert True\n",
            )
            with self.assertRaises(FlowError) as ctx:
                validate_harness(skill)
            self.assertIn("sha256", str(ctx.exception))


@unittest.skip("legacy v2 generator removed; v4 generation is covered by test_v3_milestones")
class GenerateHarnessTests(unittest.TestCase):
    def test_scaffolds_tool_and_schemas(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "demo-skill"
            result = generate_harness(
                skill,
                flow_id="demo_v1",
                step_ids=["load", "shape"],
            )
            self.assertEqual(result["steps"], ["load", "shape"])
            self.assertTrue((skill / "steps" / "load" / "tool.py").is_file())
            self.assertTrue((skill / "steps" / "load" / "tests" / "test_tool.py").is_file())
            self.assertTrue((skill / "steps" / "shape" / "output.schema.json").is_file())
            self.assertTrue((skill / "flows" / "demo_v1.yaml").is_file())
            self.assertTrue((skill / "planning" / "flowstep-instruction.md").is_file())
            self.assertTrue((skill / "scripts" / "run.py").is_file())
            self.assertFalse((skill / "SKILL.md").exists())
            flow_text = (skill / "flows" / "demo_v1.yaml").read_text(encoding="utf-8")
            self.assertIn("load: load.load_v1", flow_text)
            self.assertNotIn("max_run_repair_cycles", flow_text)
            instruction = (skill / "planning" / "flowstep-instruction.md").read_text(encoding="utf-8")
            self.assertIn("### `load`", instruction)
            self.assertIn("assemble: `steps/load/tool.py`", instruction)
            self.assertIn("class: `tool`", instruction)
            self.assertIn("expected_return:", instruction)
            self.assertEqual(parse_statuses(instruction)["load"], "PENDING")

    def test_does_not_overwrite_existing_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "keep"
            generate_harness(skill, flow_id="keep_v1", step_ids=["only"])
            tool = skill / "steps" / "only" / "tool.py"
            tool.write_text("def run(input_data, draft=None, **_):\n    return {'ok': True}\n", encoding="utf-8")
            generate_harness(skill, flow_id="keep_v1", step_ids=["only"])
            self.assertIn("return {'ok': True}", tool.read_text(encoding="utf-8"))

    def test_codebase_writes_under_flowsteps(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            result = generate_harness(
                codebase=codebase,
                flow_id="case_detail_v1",
                step_ids=["fetch_case", "crop_4x5"],
            )
            harness = codebase / "flowsteps" / "flows" / "case_detail_v1"
            self.assertEqual(Path(result["harness_dir"]), harness)
            self.assertTrue((harness / "steps" / "fetch_case" / "tool.py").is_file())
            self.assertTrue((harness / "planning" / "flowstep-instruction.md").is_file())
            flow = load_flow(harness)
            self.assertEqual(flow["steps"][0]["class"], "tool")

    def test_rejects_codex_skills_codebase(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp) / ".codex" / "skills" / "other-skill"
            fake.mkdir(parents=True)
            with self.assertRaises(FlowError) as ctx:
                generate_harness(codebase=fake.parent, flow_id="nope_v1", step_ids=["alpha"])
            message = str(ctx.exception).replace("\\", "/")
            self.assertIn(".codex/skills", message)
            self.assertIn(".claude/skills", message)

    def test_rejects_claude_skills_codebase(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp) / ".claude" / "skills" / "other-skill"
            fake.mkdir(parents=True)
            with self.assertRaises(FlowError) as ctx:
                generate_harness(codebase=fake.parent, flow_id="nope_v1", step_ids=["alpha"])
            message = str(ctx.exception).replace("\\", "/")
            self.assertIn(".claude/skills", message)

    def test_tool_class_forbids_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "badclass"
            generate_harness(skill, flow_id="badclass_v1", step_ids=["alpha"])
            flow_path = skill / "flows" / "badclass_v1.yaml"
            text = flow_path.read_text(encoding="utf-8")
            flow_path.write_text(text.replace("model: none", "model: completion", 1), encoding="utf-8")
            with self.assertRaises(FlowError) as ctx:
                load_flow(skill)
            self.assertIn("class tool forbids model", str(ctx.exception))

    def test_name_hint_rejects_crop_as_intelligence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "cropbad"
            generate_harness(skill, flow_id="cropbad_v1", step_ids=["crop_4x5"])
            flow_path = skill / "flows" / "cropbad_v1.yaml"
            text = flow_path.read_text(encoding="utf-8")
            flow_path.write_text(
                text.replace("class: tool", "class: intelligence", 1).replace(
                    "model: none", "model: completion", 1
                ),
                encoding="utf-8",
            )
            with self.assertRaises(FlowError) as ctx:
                load_flow(skill)
            self.assertIn("structured transform", str(ctx.exception))


class InstructionTests(unittest.TestCase):
    @unittest.skip("legacy v2 step scaffold removed by the M8M 2.0 hard cutover")
    def test_mark_step_updates_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "led"
            generate_harness(skill, flow_id="led_v1", step_ids=["load", "shape"])
            mark_step(skill, "load", "DONE")
            text = (skill / "planning" / "flowstep-instruction.md").read_text(encoding="utf-8")
            statuses = parse_statuses(text)
            self.assertEqual(statuses["load"], "DONE")
            self.assertEqual(statuses["shape"], "PENDING")
            self.assertIn("expected_return:", text)

    def test_validate_marks_example_done(self) -> None:
        write_instruction(EXAMPLE)
        validate_harness(EXAMPLE)
        text = (EXAMPLE / "planning" / "flowstep-instruction.md").read_text(encoding="utf-8")
        statuses = parse_statuses(text)
        self.assertEqual(statuses["ingest"], "DONE")
        self.assertEqual(statuses["segment"], "DONE")
        self.assertEqual(statuses["label"], "DONE")
        self.assertIn('"outputs": "object"', text)
        self.assertIn("chosen bundle", text)
        self.assertIn("class: `intelligence`", text)


if __name__ == "__main__":
    unittest.main()
