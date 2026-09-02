from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

import support  # noqa: F401  # puts scripts/ on sys.path

import m8m_factory
import run_m8m
from flowstep_runtime import FlowError, lint_file_payload_schema, load_flow
from flowstep_tools import validate_library_tool


ROOT = Path(__file__).resolve().parents[1]


def _json_port(output_id: str, name: str) -> dict[str, object]:
    return {
        "id": output_id,
        "name": name,
        "kind": "json",
        "cardinality": "one",
        "required": True,
    }


def _file_port(output_id: str, name: str) -> dict[str, object]:
    return {
        "id": output_id,
        "name": name,
        "kind": "file",
        "cardinality": "one",
        "required": True,
    }


EXPECTED_STAGES = {
    "audit_complete": (
        "m8m_builder_source_audit_v2",
        [_json_port("source_audit", "Closed source audit")],
        "contracts/m8m_builder_source_audit_v2.schema.json",
    ),
    "toolbox_ready": (
        "m8m_builder_toolbox_manifest_v2",
        [_json_port("toolbox_manifest", "Toolbox requirement manifest")],
        "contracts/m8m_builder_toolbox_manifest_v2.schema.json",
    ),
    "flow_generated": (
        "m8m_builder_source_bundle_v2",
        [
            _json_port("workflow_source_bundle", "Portable workflow source bundle"),
            _json_port("staged_harness", "Run-local staged harness report"),
        ],
        "contracts/m8m_builder_source_bundle_v2.schema.json",
    ),
    "harness_validated": (
        "m8m_builder_validation_report_v2",
        [
            _json_port("validation_report", "Fail-closed validation report"),
            _file_port("workflow_package", "Importable M8M workflow package"),
        ],
        "contracts/m8m_builder_validation_report_v2.schema.json",
    ),
    "skill_shipped": (
        "m8m_builder_installation_receipt_v2",
        [_json_port("installation_receipt", "Validated local installation receipt")],
        "contracts/m8m_builder_installation_receipt_v2.schema.json",
    ),
}

EXPECTED_IMPLEMENTATION_DEPENDENCIES = [
    "scripts/m8m_build_steps.py",
    "scripts/audit_harness.py",
    "scripts/migrate_skill_source.py",
    "scripts/candidate_cache.py",
    "scripts/execution_identity.py",
    "scripts/flowstep_instruction.py",
    "scripts/flowstep_runtime.py",
    "scripts/run_flow.py",
    "scripts/runtime_release.py",
    "scripts/flowchart_jpg.py",
    "scripts/generate_harness.py",
    "scripts/flowstep_tools.py",
    "scripts/gem_text.py",
    "scripts/humanize_chart.py",
    "scripts/m8m_flowchart.py",
    "scripts/milestone_expectation.py",
    "scripts/milestone_pair.py",
    "scripts/package_archive.py",
    "scripts/schema_gate.py",
    "scripts/session_layout.py",
    "scripts/skill_source.py",
    "scripts/source_bundle.py",
    "scripts/teaching_contracts.py",
    "scripts/toolbox_plan.py",
    "scripts/tool_vs_intelligence.py",
    "scripts/validate_harness.py",
    "templates/run.py",
    "templates/codebase-launcher.py",
    "contracts/file_ref_v2.schema.json",
    "contracts/flow_sequence_action_v2.schema.json",
    "contracts/flowstep_flow_v4.schema.json",
    "contracts/flowstep_output_v3.schema.json",
    "contracts/flowstep_skill_audit_v1.schema.json",
    "contracts/m8m_cache_receipt_v1.schema.json",
    "contracts/m8m_candidate_cache_entry_v1.schema.json",
    "contracts/m8m_chosen_output_v1.schema.json",
    "contracts/m8m_context_capsule_v1.schema.json",
    "contracts/m8m_contract_bundle_lock_v1.schema.json",
    "contracts/m8m_goal_ledger_v1.schema.json",
    "contracts/m8m_milestone_judge_receipt_v1.schema.json",
    "contracts/m8m_milestone_expectation_v1.schema.json",
    "contracts/m8m_milestone_judge_request_v1.schema.json",
    "contracts/m8m_run_context_v2.schema.json",
    "contracts/m8m_run_storage_contract_v1.schema.json",
    "contracts/m8m_runtime_lock_v1.schema.json",
    "contracts/m8m_runtime_release_v1.schema.json",
    "contracts/m8m_skill_canvas_v1.schema.json",
    "contracts/m8m_milestone_agent_v1.schema.json",
    "contracts/m8m_source_asset_manifest_v1.schema.json",
    "contracts/m8m_workflow_source_bundle_proof_v1.schema.json",
    "contracts/m8m_workflow_source_bundle_v1.schema.json",
    "contracts/m8m_workflow_package_archive_v1.schema.json",
    "contracts/m8m_builder_input_v1.schema.json",
    "contracts/m8m_builder_request_v3.schema.json",
    "contracts/m8m_builder_configuration_v1.schema.json",
    "contracts/m8m_builder_result_v3.schema.json",
    "contracts/m8m_builder_source_audit_v2.schema.json",
    "contracts/m8m_builder_toolbox_manifest_v2.schema.json",
    "contracts/m8m_builder_source_bundle_v2.schema.json",
    "contracts/m8m_builder_validation_report_v2.schema.json",
    "contracts/m8m_builder_installation_receipt_v2.schema.json",
    "contracts/tool_vs_intelligence_table_v1.schema.json",
]


class BuilderV2FlowTests(unittest.TestCase):
    def test_builder_v2_has_five_typed_compulsory_milestones(self) -> None:
        flow = load_flow(ROOT, ROOT / "flows" / "m8m_build_v2.yaml")

        self.assertEqual(flow["schema"], "flowstep_flow_v4")
        self.assertEqual(flow["flow_id"], "m8m_build_v2")
        self.assertEqual(flow["version"], 2)
        self.assertEqual(flow["context_policy"], "isolated")
        self.assertEqual(flow["implementation_dependencies"], EXPECTED_IMPLEMENTATION_DEPENDENCIES)
        self.assertEqual(
            len(flow["implementation_dependencies"]),
            len(set(flow["implementation_dependencies"])),
        )
        for relative in flow["implementation_dependencies"]:
            with self.subTest(implementation_dependency=relative):
                self.assertTrue((ROOT / relative).is_file(), relative)
        self.assertEqual([step["id"] for step in flow["steps"]], list(EXPECTED_STAGES))

        for step in flow["steps"]:
            contract, outputs, schema_path = EXPECTED_STAGES[step["id"]]
            self.assertEqual(step["output_contract"], contract)
            self.assertEqual(step["output_schema"], schema_path)
            self.assertNotEqual(step["output_schema"], "contracts/m8m_builder_output_v1.schema.json")
            self.assertEqual(step["outputs"], outputs)
            self.assertEqual(step["handler"], "scripts/m8m_build_steps.py")
            self.assertEqual(
                step["test"],
                {
                    "audit_complete": "flowsteps/tools/audit_skill/tests/test_tool.py",
                    "toolbox_ready": "flowsteps/tools/install_toolbox/tests/test_tool.py",
                    "flow_generated": "flowsteps/tools/generate_from_audit/tests/test_tool.py",
                    "harness_validated": "flowsteps/tools/validate_harness/tests/test_tool.py",
                    "skill_shipped": "flowsteps/tools/install_local_skill/tests/test_tool.py",
                }[step["id"]],
            )
            self.assertTrue((ROOT / step["test"]).is_file())
            self.assertEqual(step["intelligence"], "none")
            self.assertEqual(step["loop"], "none")
            self.assertIsNone(step["worker"])
            self.assertIsNone(step["judge_abi"])
            self.assertIsNone(step["receipt_schema"])
            self.assertNotIn("judge", step["execution"])
            self.assertEqual(step["on_tool_fail"], "BLOCKED")
            self.assertEqual(len(step["flowsteps"]), 1)
            flowstep = step["flowsteps"][0]
            binding = step["execution"]["tool_bindings"][0]
            self.assertEqual(flowstep["id"], step["tools"][0])
            self.assertEqual(binding["tool"], flowstep["id"])
            self.assertEqual(binding["ref"], flowstep["tool"])

    def test_builder_v2_bindings_address_exact_named_ports(self) -> None:
        flow = load_flow(ROOT, ROOT / "flows" / "m8m_build_v2.yaml")
        steps = {step["id"]: step for step in flow["steps"]}

        self.assertEqual(steps["toolbox_ready"]["inputs"]["audit"]["output"], "source_audit")
        self.assertEqual(steps["flow_generated"]["inputs"]["toolbox"]["output"], "toolbox_manifest")
        generation_bindings = {
            "workflow_source_bundle": {
                "from": "flow_generated.m8m_builder_source_bundle_v2",
                "output": "workflow_source_bundle",
            },
            "staged_harness": {
                "from": "flow_generated.m8m_builder_source_bundle_v2",
                "output": "staged_harness",
            },
        }
        for milestone_id in ("harness_validated", "skill_shipped"):
            with self.subTest(milestone=milestone_id):
                inputs = steps[milestone_id]["inputs"]
                for input_id, binding in generation_bindings.items():
                    self.assertEqual(inputs[input_id], binding)
                self.assertNotIn("generated", inputs)
        self.assertEqual(steps["skill_shipped"]["inputs"]["validation"]["output"], "validation_report")
        self.assertEqual(
            steps["skill_shipped"]["inputs"]["workflow_package"],
            {
                "from": "harness_validated.m8m_builder_validation_report_v2",
                "output": "workflow_package",
            },
        )

    def test_stage_schemas_are_closed_and_reject_the_v1_result_port(self) -> None:
        for milestone_id, (_, ports, relative) in EXPECTED_STAGES.items():
            schema = json.loads((ROOT / relative).read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            self.assertEqual(lint_file_payload_schema(schema, label=relative), [])
            self.assertFalse(schema["additionalProperties"], relative)
            self.assertEqual(schema["required"], ["outputs"], relative)
            outputs = schema["properties"]["outputs"]
            self.assertFalse(outputs["additionalProperties"], relative)
            output_ids = [str(port["id"]) for port in ports]
            self.assertEqual(outputs["required"], output_ids, relative)
            self.assertEqual(list(outputs["properties"]), output_ids, relative)
            for output_id in output_ids:
                payload = outputs["properties"][output_id]
                if milestone_id == "flow_generated" and output_id == "workflow_source_bundle":
                    self.assertEqual(
                        payload,
                        {"$ref": "m8m_workflow_source_bundle_v1.schema.json"},
                    )
                else:
                    self.assertFalse(payload["additionalProperties"], relative)
                    self.assertTrue(payload["required"], relative)
            self.assertFalse(
                Draft202012Validator(schema).is_valid({"outputs": {"result": {"ok": True}}}),
                relative,
            )

        generation_schema = json.loads(
            (ROOT / "contracts" / "m8m_builder_source_bundle_v2.schema.json").read_text(
                encoding="utf-8"
            )
        )
        staged = generation_schema["properties"]["outputs"]["properties"]["staged_harness"]
        self.assertIn("source_bundle_path", staged["required"])
        self.assertIn("source_bundle_digest", staged["required"])
        self.assertNotIn("compiled_source_bundle", staged["required"])
        self.assertNotIn("compiled_source_bundle", staged["properties"])


class BuilderV2LauncherTests(unittest.TestCase):
    def test_launcher_and_factory_select_v2_without_global_swapping(self) -> None:
        self.assertEqual(m8m_factory.BUILDER_FLOW, "m8m_build_v2.yaml")
        self.assertEqual(m8m_factory.BUILDER_FLOW_ID, "m8m_build_v2")
        self.assertEqual(m8m_factory.BUILDER_OUTPUTS["flow_generated"], "staged_harness")
        self.assertEqual(
            m8m_factory.BUILDER_WORKFLOW_SOURCE_BUNDLE_OUTPUT,
            "workflow_source_bundle",
        )
        self.assertEqual(run_m8m.BUILDER_FLOW, m8m_factory.BUILDER_FLOW)
        self.assertEqual(run_m8m.BUILDER_FLOW_ID, m8m_factory.BUILDER_FLOW_ID)
        self.assertFalse(hasattr(run_m8m, "run_builder3_factory"))

    def test_public_runner_help_is_renderable(self) -> None:
        rendered = run_m8m.build_parser().format_help()
        self.assertIn("%SystemDrive%\\NisanRuntime", rendered)

    def test_factory_projects_the_terminal_blocked_milestone(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "skill"
            codebase = root / "repo"
            target.mkdir()
            codebase.mkdir()
            action = {
                "schema": "flow_sequence_action_v2",
                "state": "BLOCKED",
                "step_id": "harness_validated",
                "blockers": ["validation failed"],
            }
            with patch.object(m8m_factory, "advance", return_value=action):
                result = m8m_factory.run_factory(
                    target,
                    codebase,
                    harness_root=root / "runtime",
                )

            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(
                result["milestones"]["harness_validated"],
                {"status": "BLOCKED", "blockers": ["validation failed"]},
            )
            self.assertEqual(result["milestones"]["skill_shipped"]["status"], "PENDING")

    def test_factory_authors_current_m8m_from_existing_skill_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / "bare-skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\n"
                "name: bare-skill\n"
                "description: Package one bounded file.\n"
                "---\n\n"
                "# Bare skill\n",
                encoding="utf-8",
            )
            result = m8m_factory.run_factory(
                skill,
                root / "repo",
                flow_id="bare_v1",
                skill_name="bare-skill",
                harness_root=root / "runtime",
            )

            self.assertEqual(result["milestones"]["audit_complete"]["status"], "PASS", result)
            blockers = " ".join(result.get("action", {}).get("blockers") or [])
            self.assertNotIn("import_flow_v4.py", blockers)
            self.assertNotEqual(result["milestones"]["toolbox_ready"]["status"], "PENDING")

    def test_launcher_rejects_an_initialized_v1_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "flow-execution-record.json").write_text(
                json.dumps({"flow_id": "m8m_build_v1"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FlowError, "cannot resume or execute a Builder 2 run"):
                run_m8m._assert_builder3_run(run_dir)

    def test_factory_rejects_resume_arguments_that_differ_from_frozen_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            run_dir.mkdir()
            first_target = root / "first-skill"
            second_target = root / "second-skill"
            first_codebase = root / "first-repo"
            second_codebase = root / "second-repo"
            first_target.mkdir()
            second_target.mkdir()
            (run_dir / "run-context.json").write_text(
                json.dumps({"storage_contract": {"execution_root": str(root / "runtime")}}),
                encoding="utf-8",
            )
            (run_dir / "flow-execution-record.json").write_text(
                json.dumps({"flow_id": "m8m_build_v2"}),
                encoding="utf-8",
            )
            (run_dir / "request.json").write_text(
                json.dumps(
                    {
                        "target": str(first_target.resolve()),
                        "codebase": str(first_codebase.resolve()),
                        "flow_id": "first_v1",
                        "skill_name": "first-skill",
                        "overwrite": False,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(FlowError, "frozen request"):
                m8m_factory.run_factory(
                    second_target,
                    second_codebase,
                    flow_id="second_v1",
                    skill_name="second-skill",
                    run_dir=run_dir,
                )


if __name__ == "__main__":
    unittest.main()
