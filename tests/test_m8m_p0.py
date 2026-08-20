from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  # puts scripts/ on sys.path

from audit_harness import audit_harness, audit_skill, render_audit_markdown
from m8m_factory import run_factory
from flowstep_runtime import FlowError, is_passthrough_schema
from flowstep_tools import run_library_tool, validate_library_tool
from generate_harness import (
    generate_from_audit,
    generate_harness,
    generate_tool,
    generate_v3_flow,
    main as generate_main,
)



class SeedToolboxTests(unittest.TestCase):
    def test_hash_bind_seed_is_real(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            result = generate_tool(codebase, "hash_bind")
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(result["seeded"])
            self.assertEqual(validate_library_tool(codebase, "hash_bind"), [])
            path = Path(temp) / "a.txt"
            path.write_text("x", encoding="utf-8")
            bound = run_library_tool(codebase, "hash_bind", {"path": str(path)})
            self.assertEqual(len(bound["sha256"]), 64)
            source = (codebase / "flowsteps" / "tools" / "hash_bind" / "tool.py").read_text(encoding="utf-8")
            self.assertNotIn("NotImplementedError", source)

    def test_unknown_tool_is_generate_new_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            result = generate_tool(codebase, "not_a_seed_tool")
            self.assertEqual(result["status"], "PASS")
            self.assertFalse(result["seeded"])
            self.assertEqual(result.get("origin"), "generate-new")
            self.assertIn("generate-new", result.get("note") or "")
            self.assertTrue((codebase / "flowsteps" / "tools" / "not_a_seed_tool" / "tool.py").is_file())


class AuditDrivesGenerateTests(unittest.TestCase):
    def test_from_audit_uses_per_milestone_tools_and_asset_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "toy-skill"
            (skill / "scripts").mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: toy-skill\ndescription: Bind a file.\n---\n\n# toy\n",
                encoding="utf-8",
            )
            (skill / "scripts" / "hash_bind.py").write_text("def run(x):\n    return x\n", encoding="utf-8")
            codebase = Path(temp) / "repo"
            audit = audit_skill(skill)
            result = generate_from_audit(codebase, audit, flow_id="toy_v1", skill_name="toy-skill")
            harness = Path(result["harness_dir"])
            flow = (harness / "flow.yaml").read_text(encoding="utf-8")
            self.assertIn("hash_bind", flow)
            self.assertIn("asset:", flow)
            self.assertIn("kind:", flow)
            last_id = result["milestones"][-1]
            last_schema = harness / "schemas" / f"{last_id}_v1.json"
            self.assertTrue(last_schema.is_file())
            for mid in result["milestones"]:
                schema = json.loads((harness / "schemas" / f"{mid}_v1.json").read_text(encoding="utf-8"))
                self.assertFalse(is_passthrough_schema(schema), mid)
                self.assertEqual(schema.get("additionalProperties"), False)
                self.assertTrue(schema.get("required"), mid)
            self.assertTrue((harness / "planning" / "m8m-flowchart.md").is_file())
            self.assertEqual(result["status"], "PASS")
            self.assertTrue((codebase / ".agents" / "skills" / "toy-skill" / "SKILL.md").is_file())
            self.assertTrue((codebase / ".claude" / "skills" / "toy-skill" / "SKILL.md").is_file())
            self.assertEqual(audit["tool_vs_intelligence"]["schema"], "tool_vs_intelligence_table_v1")
            table_path = harness / "planning" / "tool-vs-intelligence.json"
            self.assertTrue(table_path.is_file())
            table = json.loads(table_path.read_text(encoding="utf-8"))
            self.assertTrue(table["rows"])
            self.assertTrue(all({"id", "class", "test", "why"} <= set(row) for row in table["rows"]))
            skill_md = (codebase / ".agents" / "skills" / "toy-skill" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("m8m-flowchart.md", skill_md)
            instruction = (harness / "planning" / "flowstep-instruction.md").read_text(encoding="utf-8")
            self.assertIn("m8m-flowchart.md", instruction)

    def test_from_audit_copies_teaching_contracts_onto_the_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "case-skill"
            (skill / "scripts").mkdir(parents=True)
            (skill / "references").mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: case-skill\ndescription: Case infographic.\n---\n\n# case\n",
                encoding="utf-8",
            )
            (skill / "scripts" / "hash_bind.py").write_text("def run(x):\n    return x\n", encoding="utf-8")
            (skill / "references" / "fact-contract.md").write_text(
                "# Fact-resolution contract\n\nUsable area is one integer.\n",
                encoding="utf-8",
            )
            (skill / "references" / "canvas-contract.md").write_text(
                "# Canvas contract\n\nThree 4:5 pages.\n",
                encoding="utf-8",
            )
            audit = audit_skill(skill)
            names = {row["name"] for row in audit["teaching_plan"]}
            self.assertIn("fact-contract.md", names)
            self.assertTrue(any(row["action"] == "promote" for row in audit["teaching_plan"]))
            markdown = render_audit_markdown(audit)
            self.assertIn("## Teaching contracts", markdown)
            self.assertIn("fact-contract", markdown)
            codebase = Path(temp) / "repo"
            result = generate_from_audit(codebase, audit, flow_id="case_v1", skill_name="case-skill")
            harness = Path(result["harness_dir"])
            self.assertTrue((harness / "references" / "fact-contract.md").is_file())
            self.assertTrue((harness / "references" / "canvas-contract.md").is_file())
            instruction = (harness / "planning" / "flowstep-instruction.md").read_text(encoding="utf-8")
            self.assertIn("references/fact-contract.md", instruction)
            self.assertIn("Teaching contracts", instruction)
            skill_md = (codebase / ".agents" / "skills" / "case-skill" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("m8m-flowchart.md", skill_md)


class DefaultV3Tests(unittest.TestCase):
    def test_cli_rejects_v2_step(self) -> None:
        code = generate_main(["--codebase", str(Path("C:/tmp")), "--flow-id", "x_v1", "--step", "alpha"])
        self.assertEqual(code, 2)


class FactoryTests(unittest.TestCase):
    def test_run_factory_ships_product_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "bare-skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: bare-skill\ndescription: Package one file.\n---\n\n# bare\n",
                encoding="utf-8",
            )
            codebase = Path(temp) / "repo"
            result = run_factory(skill, codebase, flow_id="bare_v1", skill_name="bare-skill")
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(Path(result["audit_json"]).is_file())
            self.assertTrue(Path(result["product_skill"]).is_file())
            self.assertTrue((codebase / ".claude" / "skills" / "bare-skill" / "SKILL.md").is_file())
            chart = Path(result["flowchart_path"])
            self.assertTrue(chart.is_file())
            self.assertTrue(chart.with_suffix(".jpg").is_file())
            self.assertIn("```text", chart.read_text(encoding="utf-8"))
            self.assertNotIn("```mermaid", chart.read_text(encoding="utf-8"))
            self.assertIn("m8m-flowchart.jpg", chart.read_text(encoding="utf-8"))
            skill_md = Path(result["product_skill"]).read_text(encoding="utf-8")
            self.assertIn("m8m-flowchart.md", skill_md)

    def test_factory_passes_when_tools_are_generate_new(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "crop-skill"
            (skill / "scripts").mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: crop-skill\ndescription: Crop a page.\n---\n\n# crop\n",
                encoding="utf-8",
            )
            (skill / "scripts" / "crop_4x5.py").write_text(
                "def run(input_data, **kwargs):\n    return input_data\n",
                encoding="utf-8",
            )
            codebase = Path(temp) / "repo"
            result = run_factory(skill, codebase, flow_id="crop_v1", skill_name="crop-skill")
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(Path(result["flowchart_path"]).is_file())
            stub = codebase / "flowsteps" / "tools" / "crop_4x5" / "tool.py"
            self.assertTrue(stub.is_file())
            self.assertIn("NotImplementedError", stub.read_text(encoding="utf-8"))
            notes = " ".join(result.get("notes") or [])
            self.assertIn("generate-new", notes)
            self.assertIn("crop_4x5", notes)
            validation = result["milestones"]["harness_validated"]
            self.assertTrue(validation.get("optional"))

    def test_audit_name_hints_are_notes_not_p0(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo" / "flowsteps" / "flows" / "hint_v1"
            root.mkdir(parents=True)
            (root / "flow.yaml").write_text(
                "\n".join(
                    [
                        "schema: flowstep_flow_v3",
                        "flow_id: hint_v1",
                        "version: 1",
                        "milestones:",
                        "  - id: crop_4x5",
                        "    output_contract: crop_v1",
                        "    tools: [not_yet_a_seed]",
                        "    intelligence: none",
                        "  - id: if_ready",
                        "    output_contract: ready_v1",
                        "    tools: [hash_bind]",
                        "    intelligence: none",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report = audit_harness(root)
            self.assertEqual(report["p0_count"], 0)
            self.assertEqual(report["status"], "PASS")
            issues = [issue for step in report["steps"] for issue in step.get("issues") or []]
            self.assertTrue(any("looks like a tool" in issue for issue in issues))
            self.assertTrue(any("looks like control" in issue for issue in issues))
            self.assertTrue(any("generate-new" in issue for issue in issues))


class LegacyV2StillImportable(unittest.TestCase):
    def test_python_api_legacy_v2_still_scaffolds_for_fixture_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "legacy"
            result = generate_harness(skill, flow_id="legacy_v1", step_ids=["alpha"])
            self.assertEqual(result["schema"], "flowstep_harness_generate_v2")
            self.assertTrue((skill / "steps" / "alpha" / "tool.py").is_file())


if __name__ == "__main__":
    unittest.main()
