from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

import support  # noqa: F401  # puts scripts/ on sys.path

from flowstep_tools import load_library_tool, validate_library_tool
from m8m_build_steps import run as run_builder_step
from skill_source import compile_skill_source


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_TOOL_IDS = (
    "audit_skill",
    "install_toolbox",
    "generate_from_audit",
    "validate_harness",
    "install_local_skill",
)
FLOWSTEP_IDS = (
    "audit_source",
    "construct_toolbox",
    "compile_source_bundle",
    "validate_source_bundle",
    "install_local_skill",
)
TOOL_IDS = CANDIDATE_TOOL_IDS
STAGED_TOOL_IDS = CANDIDATE_TOOL_IDS
OUTPUT_CONTRACTS = {
    "audit_skill": "m8m_builder_source_audit_v2.schema.json",
    "install_toolbox": "m8m_builder_toolbox_manifest_v2.schema.json",
    "generate_from_audit": "m8m_builder_source_bundle_v2.schema.json",
    "validate_harness": "m8m_builder_validation_report_v2.schema.json",
    "install_local_skill": "m8m_builder_installation_receipt_v2.schema.json",
}


class BuilderToolboxPackageTests(unittest.TestCase):
    def test_every_declared_builder_tool_is_real_closed_and_locally_runnable(self) -> None:
        for tool_id in TOOL_IDS:
            with self.subTest(tool=tool_id):
                tool_root = ROOT / "flowsteps" / "tools" / tool_id
                self.assertFalse((tool_root / "BUILD_REQUIRED").exists())
                self.assertEqual(validate_library_tool(ROOT, tool_id), [])
                self.assertTrue(callable(load_library_tool(ROOT, tool_id).run))

                input_schema = json.loads(
                    (tool_root / "input.schema.json").read_text(encoding="utf-8")
                )
                output_schema = json.loads(
                    (tool_root / "output.schema.json").read_text(encoding="utf-8")
                )
                Draft202012Validator.check_schema(input_schema)
                Draft202012Validator.check_schema(output_schema)
                self.assertIs(input_schema["additionalProperties"], False)
                self.assertTrue(input_schema["required"])
                if tool_id in CANDIDATE_TOOL_IDS:
                    self.assertEqual(
                        input_schema["properties"]["request"],
                        {"$ref": "../../../contracts/m8m_builder_request_v3.schema.json"},
                    )
                else:
                    self.assertEqual(
                        input_schema["properties"]["request"],
                        {"type": "object"},
                    )
                if tool_id in OUTPUT_CONTRACTS:
                    self.assertEqual(
                        output_schema["$ref"],
                        f"../../../contracts/{OUTPUT_CONTRACTS[tool_id]}",
                    )
                    referenced = ROOT / "contracts" / OUTPUT_CONTRACTS[tool_id]
                    contract = json.loads(referenced.read_text(encoding="utf-8"))
                    self.assertIs(contract["additionalProperties"], False)
                else:
                    self.assertIs(output_schema["additionalProperties"], False)
                    self.assertEqual(
                        output_schema["required"],
                        ["ok", "code", "reasons"],
                    )

                source = (tool_root / "tool.py").read_text(encoding="utf-8")
                for forbidden in (
                    "NotImplementedError",
                    "NEED_MODEL",
                    "requests.",
                    "urllib",
                    "socket.",
                    "subprocess",
                ):
                    self.assertNotIn(forbidden, source)
                if tool_id in CANDIDATE_TOOL_IDS:
                    self.assertNotIn("BUILD_REQUIRED", source)

    def test_toolbox_ready_stages_all_five_real_builder_tools(self) -> None:
        compiled_flow = compile_skill_source(ROOT)
        declared_tools = tuple(
            tool_id
            for milestone in compiled_flow["milestones"]
            for tool_id in milestone["tools"]
        )
        self.assertEqual(declared_tools, FLOWSTEP_IDS)

        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_builder_step(
                {
                    "request": {
                        "codebase": str(ROOT),
                        "target": str(ROOT),
                    },
                    "audit": {"compiled_flow": compiled_flow},
                },
                task={"step_id": "toolbox_ready"},
                run_dir=Path(temp_dir),
            )
            manifest = result["outputs"]["toolbox_manifest"]
            stage = Path(manifest["stage_codebase"])

            self.assertEqual(manifest["status"], "PASS")
            self.assertTrue(manifest["runnable"])
            self.assertFalse(manifest["non_runnable"])
            self.assertEqual(manifest["build_required_tools"], [])
            self.assertEqual(
                tuple(item["tool_id"] for item in manifest["tools"]),
                STAGED_TOOL_IDS,
            )
            for item in manifest["tools"]:
                with self.subTest(staged_tool=item["tool_id"]):
                    self.assertEqual(item["status"], "PASS")
                    self.assertEqual(item["origin"], "local-implementation")
                    self.assertTrue(item["runnable"])
                    self.assertFalse(item["non_runnable"])
                    staged_tool = stage / "flowsteps" / "tools" / item["tool_id"]
                    self.assertTrue(staged_tool.is_dir())
                    self.assertFalse((staged_tool / "BUILD_REQUIRED").exists())
                    self.assertEqual(validate_library_tool(stage, item["tool_id"]), [])


if __name__ == "__main__":
    unittest.main()
