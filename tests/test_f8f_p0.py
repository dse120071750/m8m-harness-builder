from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  # puts scripts/ on sys.path

from audit_harness import audit_skill
from f8f_factory import run_factory
from flowstep_runtime import FlowError
from flowstep_tools import run_library_tool, validate_library_tool
from generate_harness import (
    generate_from_audit,
    generate_harness,
    generate_tool,
    generate_v3_flow,
    main as generate_main,
)
from validate_harness import validate_harness


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

    def test_unknown_tool_is_findings_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = generate_tool(Path(temp) / "repo", "not_a_seed_tool")
            self.assertEqual(result["status"], "FINDINGS")
            self.assertFalse(result["seeded"])


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
            last_id = result["milestones"][-1]
            last = json.loads((harness / "schemas" / f"{last_id}_v1.json").read_text(encoding="utf-8"))
            self.assertIn("asset", last["properties"])
            self.assertTrue((codebase / ".agents" / "skills" / "toy-skill" / "SKILL.md").is_file())
            self.assertEqual(audit["tool_vs_intelligence"]["schema"], "tool_vs_intelligence_table_v1")
            table_path = harness / "planning" / "tool-vs-intelligence.json"
            self.assertTrue(table_path.is_file())
            table = json.loads(table_path.read_text(encoding="utf-8"))
            self.assertTrue(table["rows"])
            self.assertTrue(all({"id", "class", "test", "why"} <= set(row) for row in table["rows"]))
            skill_md = (codebase / ".agents" / "skills" / "toy-skill" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("Tool vs intelligence", skill_md)
            instruction = (harness / "planning" / "flowstep-instruction.md").read_text(encoding="utf-8")
            self.assertIn("Tool vs intelligence", instruction)


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
            self.assertIn(result["status"], {"PASS", "FINDINGS"})
            self.assertTrue(Path(result["audit_json"]).is_file())
            self.assertTrue(Path(result["product_skill"]).is_file())
            skill_md = Path(result["product_skill"]).read_text(encoding="utf-8")
            self.assertIn("flowsteps/tools", skill_md)
            self.assertIn("asset", skill_md)
            validated = validate_harness(codebase=codebase, flow_id="bare_v1")
            self.assertEqual(validated["status"], "PASS")


class LegacyV2StillImportable(unittest.TestCase):
    def test_python_api_legacy_v2_still_scaffolds_for_fixture_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "legacy"
            result = generate_harness(skill, flow_id="legacy_v1", step_ids=["alpha"])
            self.assertEqual(result["schema"], "flowstep_harness_generate_v2")
            self.assertTrue((skill / "steps" / "alpha" / "tool.py").is_file())


if __name__ == "__main__":
    unittest.main()
