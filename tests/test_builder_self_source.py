from __future__ import annotations

import json
import re
import unittest

import yaml
from jsonschema import Draft202012Validator, ValidationError

from support import SKILL_ROOT, SCRIPTS  # noqa: F401

from skill_source import (  # noqa: E402
    canonical_json_bytes,
    compile_skill_source,
    compile_skill_source_bytes,
    load_skill_source,
)


ROOT = SKILL_ROOT
CANONICAL = ROOT / "flows" / "m8m_build_v2.yaml"
MILESTONE_IDS = [
    "audit_complete",
    "toolbox_ready",
    "flow_generated",
    "harness_validated",
    "skill_shipped",
]


class BuilderSelfSourceTests(unittest.TestCase):
    def test_canvas_is_the_exact_linear_five_milestone_builder_graph(self) -> None:
        source = load_skill_source(ROOT)
        canvas = source["canvas"]
        self.assertEqual(canvas["flow_id"], "m8m_build_v2")
        self.assertEqual(canvas["version"], 2)
        self.assertEqual(canvas["context_policy"], "isolated")
        canonical = yaml.safe_load(CANONICAL.read_text(encoding="utf-8"))
        self.assertEqual(
            canvas["implementation_dependencies"],
            canonical["implementation_dependencies"],
        )
        self.assertIn("scripts/m8m_build_steps.py", canvas["implementation_dependencies"])
        self.assertIn(
            "contracts/m8m_builder_source_bundle_v2.schema.json",
            canvas["implementation_dependencies"],
        )
        self.assertIn(
            "contracts/m8m_workflow_source_bundle_v1.schema.json",
            canvas["implementation_dependencies"],
        )
        self.assertIn(
            "contracts/m8m_skill_canvas_v1.schema.json",
            canvas["implementation_dependencies"],
        )
        self.assertEqual(canvas["entry"], "audit_complete")
        self.assertEqual(canvas["milestones"], MILESTONE_IDS)
        self.assertEqual(
            canvas["graph"],
            [
                {"from": milestone_id, "to": MILESTONE_IDS[index + 1 : index + 2]}
                for index, milestone_id in enumerate(MILESTONE_IDS)
            ],
        )
        self.assertEqual(
            canvas["terminal_states"],
            {"success": ["skill_shipped"], "blocked": "BLOCKED"},
        )
        self.assertEqual(canvas["observer"]["title"], "M8M Builder 3 workflow")
        self.assertFalse((ROOT / "agents" / "audit_worker.yaml").exists())

    def test_compiled_source_matches_every_canonical_v2_milestone_field(self) -> None:
        compiled = compile_skill_source(ROOT)
        canonical = yaml.safe_load(CANONICAL.read_text(encoding="utf-8"))
        for field in (
            "schema",
            "flow_id",
            "version",
            "context_policy",
            "max_run_seconds",
            "artifact_root",
        ):
            self.assertEqual(compiled[field], canonical[field])

        self.assertEqual([row["id"] for row in compiled["milestones"]], MILESTONE_IDS)
        self.assertEqual([row["id"] for row in canonical["milestones"]], MILESTONE_IDS)
        for actual, expected in zip(compiled["milestones"], canonical["milestones"], strict=True):
            with self.subTest(milestone=expected["id"]):
                self.assertEqual(
                    {field: actual[field] for field in expected},
                    expected,
                )

        ports = [port for row in compiled["milestones"] for port in row["outputs"]]
        self.assertEqual(
            [port["id"] for port in ports],
            [
                "source_audit",
                "toolbox_manifest",
                "workflow_source_bundle",
                "staged_harness",
                "validation_report",
                "workflow_package",
                "installation_receipt",
            ],
        )
        self.assertEqual(
            [port["kind"] for port in ports],
            ["json", "json", "json", "json", "json", "file", "json"],
        )
        self.assertTrue(all(port["cardinality"] == "one" for port in ports))
        self.assertTrue(all(port["required"] is True for port in ports))

    def test_gems_and_observers_are_closed_and_human_readable(self) -> None:
        source = load_skill_source(ROOT)
        skill_text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("references/builder-authoring.md", skill_text)
        self.assertLessEqual(len(skill_text.rsplit("---", 1)[-1].encode("utf-8")), 2048)
        self.assertIn(
            {
                "owner": "audit_complete",
                "id": "builder_authoring",
                "kind": "reference",
                "path": "references/builder-authoring.md",
            },
            source["resources"],
        )
        expected_titles = [
            "Source audit",
            "Toolbox preparation",
            "Workflow generation",
            "Harness validation",
            "Local skill installation",
        ]
        self.assertEqual(
            [row["observer"]["title"] for row in source["milestones"]],
            expected_titles,
        )
        for milestone in source["milestones"]:
            with self.subTest(milestone=milestone["agent_id"]):
                milestone_id = milestone["agent_id"]
                self.assertEqual(milestone["loop"], "none")
                self.assertNotIn("worker", milestone)
                self.assertNotIn("judge_abi", milestone)
                self.assertNotIn("receipt_schema", milestone)
                self.assertNotIn("max_attempts", milestone)
                execution = milestone["execution"]
                self.assertEqual(
                    execution["candidate_executor"]["ref"],
                    f"handler.m8m_build_v2.{milestone_id}@3.1.0",
                )
                self.assertNotIn("profile", execution["candidate_executor"])
                self.assertNotIn("judge", execution)
                gem_path = ROOT / milestone["gem"]
                gem = gem_path.read_text(encoding="utf-8")
                headings = re.findall(r"^##\s+`?([^`\r\n]+?)`?\s*$", gem, flags=re.MULTILINE)
                self.assertEqual(
                    headings,
                    [milestone["flowsteps"][0]["id"]],
                )
                self.assertNotIn("Rule of success", gem)
                self.assertEqual(
                    [item["flowstep_id"] for item in milestone["observer"]["actions"]],
                    [milestone["flowsteps"][0]["id"]],
                )
                self.assertEqual(
                    [item["output_id"] for item in milestone["observer"]["outputs"]],
                    [item["id"] for item in milestone["outputs"]],
                )

    def test_compile_bytes_are_deterministic_and_have_no_remote_execution_controls(self) -> None:
        first = compile_skill_source_bytes(ROOT)
        second = compile_skill_source_bytes(ROOT)
        compiled = compile_skill_source(ROOT)
        self.assertEqual(first, second)
        self.assertEqual(first, canonical_json_bytes(compiled))
        self.assertTrue(first.endswith(b"\n"))
        self.assertNotIn(str(ROOT), json.dumps(compiled))
        for milestone in compiled["milestones"]:
            with self.subTest(milestone=milestone["id"]):
                self.assertNotIn("cache", milestone)
                self.assertNotIn("phase_journal", milestone)
                self.assertNotEqual(milestone.get("side_effects"), "external")
                self.assertEqual(milestone["intelligence"], "none")
                self.assertEqual(milestone["on_tool_fail"], "BLOCKED")

    def test_builder_dogfoods_closed_authoring_only_workflow_contracts(self) -> None:
        source = load_skill_source(ROOT)
        contracts = source["canvas"]["workflow_contracts"]
        self.assertEqual(
            contracts,
            {
                "request_schema": "contracts/m8m_builder_request_v3.schema.json",
                "configuration_schema": "contracts/m8m_builder_configuration_v1.schema.json",
                "result_schema": "contracts/m8m_builder_result_v3.schema.json",
                "terminal_bindings": [
                    {
                        "name": "installation_receipt",
                        "from": "skill_shipped.m8m_builder_installation_receipt_v2",
                        "output": "installation_receipt",
                    }
                ],
            },
        )
        for field, path in (
            ("request_schema", "contracts/m8m_builder_request_v3.schema.json"),
            ("configuration_schema", "contracts/m8m_builder_configuration_v1.schema.json"),
            ("result_schema", "contracts/m8m_builder_result_v3.schema.json"),
        ):
            self.assertIn(
                {"owner": "workflow", "id": field, "kind": "schema", "path": path},
                source["resources"],
            )

        compiled = compile_skill_source(ROOT)
        self.assertNotIn("workflow_contracts", compiled)

        request_schema = json.loads(
            (ROOT / contracts["request_schema"]).read_text(encoding="utf-8")
        )
        configuration_schema = json.loads(
            (ROOT / contracts["configuration_schema"]).read_text(encoding="utf-8")
        )
        result_schema = json.loads(
            (ROOT / contracts["result_schema"]).read_text(encoding="utf-8")
        )
        for schema in (request_schema, configuration_schema, result_schema):
            Draft202012Validator.check_schema(schema)
            self.assertFalse(schema["additionalProperties"])

        request_validator = Draft202012Validator(request_schema)
        request = {
            "target": "D:/skill",
            "codebase": "D:/repo",
            "flow_id": None,
            "skill_name": None,
            "overwrite": False,
        }
        request_validator.validate(request)
        with self.assertRaises(ValidationError):
            request_validator.validate({**request, "cloud_release_id": "forbidden"})

        configuration_validator = Draft202012Validator(configuration_schema)
        configuration_validator.validate({})
        with self.assertRaises(ValidationError):
            configuration_validator.validate({"implicit_context": True})

        self.assertEqual(
            result_schema["properties"]["installation_receipt"]["$ref"],
            "m8m_builder_installation_receipt_v2.schema.json#/properties/outputs/properties/installation_receipt",
        )
        self.assertNotIn("cloud", json.dumps(result_schema).lower())


if __name__ == "__main__":
    unittest.main()
