from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  # puts scripts/ on sys.path

from audit_harness import audit_harness, audit_skill, render_audit_markdown
from m8m_factory import run_factory
from flowstep_runtime import FlowError, read_json
from flowstep_tools import run_library_tool, validate_library_tool
from generate_harness import (
    generate_from_audit,
    generate_harness,
    generate_tool,
    main as generate_main,
)
from teaching_contracts import copy_teaching_contracts



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

    def test_unknown_tool_is_build_required_and_non_runnable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            result = generate_tool(codebase, "not_a_seed_tool")
            self.assertEqual(result["status"], "BUILD_REQUIRED")
            self.assertFalse(result["seeded"])
            self.assertEqual(result.get("origin"), "generate-new")
            self.assertFalse(result["runnable"])
            self.assertTrue(result["non_runnable"])
            self.assertTrue(result["blockers"])
            self.assertTrue((codebase / "flowsteps" / "tools" / "not_a_seed_tool" / "tool.py").is_file())
            self.assertTrue((codebase / "flowsteps" / "tools" / "not_a_seed_tool" / "BUILD_REQUIRED").is_file())
            self.assertTrue(any("BUILD_REQUIRED" in item for item in validate_library_tool(codebase, "not_a_seed_tool")))
            with self.assertRaisesRegex(FlowError, "BUILD_REQUIRED"):
                run_library_tool(codebase, "not_a_seed_tool", {})

    def test_valid_manual_repair_is_preserved_during_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generated = generate_tool(codebase, "repairable_tool")
            tool_dir = Path(generated["tool_dir"])
            source = (
                "def run(input_data, **_):\n"
                "    return {'value': str(input_data.get('value') or 'ready')}\n"
            )
            (tool_dir / "tool.py").write_text(source, encoding="utf-8")
            (tool_dir / "input.schema.json").write_text(
                json.dumps({"type": "object", "additionalProperties": True}),
                encoding="utf-8",
            )
            (tool_dir / "output.schema.json").write_text(
                json.dumps(
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["value"],
                        "properties": {"value": {"type": "string", "minLength": 1}},
                    }
                ),
                encoding="utf-8",
            )
            (tool_dir / "tests" / "test_tool.py").write_text(
                "def test_returns_value():\n    assert True\n",
                encoding="utf-8",
            )
            (tool_dir / "BUILD_REQUIRED").unlink()

            refreshed = generate_tool(codebase, "repairable_tool", overwrite=True)

            self.assertEqual(refreshed["status"], "PASS")
            self.assertEqual(refreshed["origin"], "local-implementation")
            self.assertEqual((tool_dir / "tool.py").read_text(encoding="utf-8"), source)
            self.assertFalse((tool_dir / "BUILD_REQUIRED").exists())


class AuditDrivesGenerateTests(unittest.TestCase):
    def test_from_audit_requires_authored_milestone_authority(self) -> None:
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
            self.assertEqual(result["status"], "BUILD_REQUIRED")
            self.assertFalse(result["runnable"])
            self.assertTrue(result["non_runnable"])
            self.assertTrue(result["build_required_milestones"])
            self.assertFalse((harness / "flow.yaml").exists())
            self.assertFalse((harness / "BUILD_REQUIRED_RUNTIME").exists())
            self.assertFalse(
                (codebase / ".agents" / "skills" / "toy-skill" / "SKILL.md").exists()
            )
            self.assertFalse(
                (codebase / ".claude" / "skills" / "toy-skill" / "SKILL.md").exists()
            )
            self.assertEqual(audit["tool_vs_intelligence"]["schema"], "tool_vs_intelligence_table_v1")
            self.assertTrue(audit["tool_vs_intelligence"]["rows"])
            self.assertTrue(
                all(
                    {"id", "class", "test", "why"} <= set(row)
                    for row in audit["tool_vs_intelligence"]["rows"]
                )
            )

    def test_incomplete_audit_does_not_promote_teaching_contracts_implicitly(self) -> None:
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
            self.assertEqual(result["status"], "BUILD_REQUIRED")
            self.assertFalse(result["runnable"])
            self.assertFalse((harness / "flow.yaml").exists())
            self.assertFalse((harness / "references" / "fact-contract.md").exists())
            self.assertFalse((harness / "references" / "canvas-contract.md").exists())

            copied = copy_teaching_contracts(harness, audit)

            self.assertEqual(len(copied), 2)
            self.assertTrue((harness / "references" / "fact-contract.md").is_file())
            self.assertTrue((harness / "references" / "canvas-contract.md").is_file())
            self.assertFalse((harness / "BUILD_REQUIRED_RUNTIME").exists())
            self.assertFalse(
                (codebase / ".agents" / "skills" / "case-skill" / "SKILL.md").exists()
            )


class DefaultV3Tests(unittest.TestCase):
    def test_cli_rejects_v2_step(self) -> None:
        code = generate_main(["--codebase", str(Path("C:/tmp")), "--flow-id", "x_v1", "--step", "alpha"])
        self.assertEqual(code, 2)


class FactoryTests(unittest.TestCase):
    def test_incomplete_legacy_builder_import_stops_before_validation_and_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(__file__).resolve().parents[1]
            target = Path(temp) / "m8m-harness-builder"
            (target / "flows").mkdir(parents=True)
            shutil.copy2(source / "SKILL.md", target / "SKILL.md")
            shutil.copy2(source / "flows" / "m8m_build_v2.yaml", target / "flows" / "m8m_build_v2.yaml")
            codebase = Path(temp) / "repo"
            runtime = Path(temp) / "runtime"
            result = run_factory(
                target,
                codebase,
                flow_id="m8m_self_v1",
                skill_name="m8m-self",
                harness_root=runtime,
            )
            self.assertEqual(result["status"], "BLOCKED", result)
            run_dir = Path(result["run_dir"])
            run_context = read_json(run_dir / "run-context.json")
            execution = read_json(run_dir / "flow-execution-record.json")
            self.assertEqual(run_context["context_policy"], "isolated")
            self.assertFalse(run_context["chat_history_allowed"])
            self.assertEqual(execution["cache"]["mode"], "off")
            self.assertEqual(result["action"]["step_id"], "audit_complete")
            self.assertEqual(result["action"]["state"], "BLOCKED")
            self.assertIn("import_flow_v4.py", " ".join(result["action"]["blockers"]))
            self.assertFalse(
                (run_dir / "milestones" / "audit_complete" / "out" / "chosen-output.json").exists()
            )
            self.assertEqual(result["milestones"]["audit_complete"]["status"], "BLOCKED")
            self.assertEqual(result["milestones"]["toolbox_ready"]["status"], "PENDING")
            self.assertEqual(result["milestones"]["flow_generated"]["status"], "PENDING")
            self.assertEqual(result["milestones"]["harness_validated"]["status"], "PENDING")
            self.assertEqual(result["milestones"]["skill_shipped"]["status"], "PENDING")
            self.assertFalse((run_dir / "milestones" / "harness_validated" / "out" / "chosen-output.json").exists())
            self.assertFalse((run_dir / "milestones" / "skill_shipped" / "out" / "chosen-output.json").exists())
            self.assertFalse((codebase / "flowsteps" / "cache").exists())
            self.assertIn((runtime / "runs").resolve(), run_dir.resolve().parents)

    def test_run_factory_requires_explicit_import_for_a_bare_legacy_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "bare-skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: bare-skill\ndescription: Package one file.\n---\n\n# bare\n",
                encoding="utf-8",
            )
            codebase = Path(temp) / "repo"
            result = run_factory(
                skill,
                codebase,
                flow_id="bare_v1",
                skill_name="bare-skill",
                harness_root=Path(temp) / "runtime",
            )
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["action"]["step_id"], "audit_complete")
            self.assertIn("import_flow_v4.py", " ".join(result["action"]["blockers"]))
            self.assertEqual(result["milestones"]["flow_generated"]["status"], "PENDING")
            self.assertEqual(result["milestones"]["harness_validated"]["status"], "PENDING")
            self.assertEqual(result["milestones"]["skill_shipped"]["status"], "PENDING")
            self.assertNotIn("source_bundle_path", result)
            self.assertFalse((codebase / ".claude" / "skills" / "bare-skill" / "SKILL.md").exists())
            self.assertFalse((codebase / ".agents" / "skills" / "bare-skill" / "SKILL.md").exists())

    def test_factory_requires_explicit_import_before_legacy_sketch_generation(self) -> None:
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
            result = run_factory(
                skill,
                codebase,
                flow_id="crop_v1",
                skill_name="crop-skill",
                harness_root=Path(temp) / "runtime",
            )
            self.assertEqual(result["status"], "BLOCKED", result)
            self.assertEqual(result["action"]["step_id"], "audit_complete")
            self.assertIn("import_flow_v4.py", " ".join(result["action"]["blockers"]))
            run_dir = Path(result["run_dir"])
            self.assertFalse(
                (run_dir / "milestones" / "flow_generated" / "out" / "chosen-output.json").exists()
            )
            self.assertEqual(result["milestones"]["flow_generated"]["status"], "PENDING")
            self.assertEqual(result["milestones"]["harness_validated"]["status"], "PENDING")
            self.assertEqual(result["milestones"]["skill_shipped"]["status"], "PENDING")
            self.assertFalse((codebase / ".agents" / "skills" / "crop-skill").exists())

    def test_audit_name_hints_are_notes_not_p0(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo" / "flowsteps" / "flows" / "hint_v1"
            root.mkdir(parents=True)
            (root / "flow.yaml").write_text(
                "\n".join(
                    [
                        "schema: flowstep_flow_v4",
                        "flow_id: hint_v1",
                        "version: 1",
                        "milestones:",
                        "  - id: crop_4x5",
                        "    output_contract: crop_v1",
                        "    output_schema: schemas/crop_v1.json",
                        "    success: Crop candidate is chosen.",
                        "    outputs:",
                        "      - { id: result, name: Crop, kind: image, cardinality: one, required: true }",
                        "    tools: [not_yet_a_seed]",
                        "    intelligence: none",
                        "  - id: if_ready",
                        "    output_contract: ready_v1",
                        "    output_schema: schemas/ready_v1.json",
                        "    success: Ready result is chosen.",
                        "    outputs:",
                        "      - { id: result, name: Ready, kind: json, cardinality: one, required: true }",
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


class LegacyV2HardCutover(unittest.TestCase):
    def test_python_api_legacy_v2_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "legacy"
            with self.assertRaisesRegex(FlowError, "m8m-harness-builder 3.1"):
                generate_harness(skill, flow_id="legacy_v1", step_ids=["alpha"])
            self.assertFalse((skill / "steps").exists())


if __name__ == "__main__":
    unittest.main()
