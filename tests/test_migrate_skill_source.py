from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from m8m_build_steps import _audit, _generate, _toolbox
from migrate_skill_source import (
    classify_skill,
    write_candidate_bind_tool,
    write_migrated_skill_source,
)
from skill_source import compile_skill_source, load_skill_source
from flowstep_tools import validate_library_tool


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


class MigrateSkillSourceTests(unittest.TestCase):
    def test_audit_context_reuses_scripts_and_lists_repo_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "old-caption"
            _write(
                root / "SKILL.md",
                "---\nname: old-caption\ndescription: Write captions.\n---\n\n"
                "# old-caption\n\nWrite captions from a case record.\n",
            )
            _write(
                root / "scripts" / "fetch_record.py",
                "def run(input_data, **_):\n    return {'id': 'row'}\n",
            )
            form = classify_skill(root)
            reuse = form["source_context"]["reuse"]
            self.assertEqual(form["source_context"]["task"], "Write captions.")
            self.assertFalse(reuse["canvas"])
            self.assertIn("fetch_record", reuse["tools"])
            self.assertEqual(form["authoring_mode"], "from_context")
            self.assertEqual(form["runtime_ownership"], "none")
            self.assertTrue(form["source_context"]["gaps"])
            self.assertFalse(form["equivalence_claimed"])

    def test_mixed_leftovers_are_inventory_not_a_single_form(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "legacy-m8m"
            _write(
                root / "scripts" / "m8m_run.py",
                "import subprocess, sys\n"
                "subprocess.run([sys.executable, r'C:/Users/x/.codex/skills/"
                "m8m-harness-builder/scripts/run_flow.py'])\n",
            )
            _write(
                root / "scripts" / "fetch_record.py",
                "def run(input_data, **_):\n    return {'id': 'row'}\n",
            )
            _write(root / "flow.yaml", "schema: flowstep_flow_v4\nflow_id: old_v1\nversion: 1\n")
            _write(root / "references" / "source_ready.md", "# Source\n\n## `bind`\n\nBind source.\n")
            form = classify_skill(root, flow_id="old_v1")
            reuse = form["source_context"]["reuse"]
            self.assertEqual(reuse["flow_schema"], "flowstep_flow_v4")
            self.assertIn("fetch_record", reuse["tools"])
            self.assertIn("source_ready", reuse["gems"])
            self.assertEqual(form["runtime_ownership"], "builder")
            self.assertTrue(form["builder_runtime_hits"])
            self.assertGreaterEqual(len(form["source_context"]["gaps"]), 2)

    def test_migrated_non_m8m_source_compiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / "skill"
            dest = root / "migrated"
            _write(
                skill / "SKILL.md",
                "---\nname: case-caption\ndescription: Caption a frozen case.\n---\n\n"
                "# case-caption\n\nCaption one case.\n",
            )
            audit = {
                "audited_skill": {"name": "case-caption", "description": "Caption a frozen case."},
                "proposed_milestones": [
                    {
                        "id": "source_ready",
                        "success": "The source record is bound.",
                        "output_contract": "source_ready_v1",
                        "intelligence": "none",
                        "tools": ["fetch_record"],
                        "outputs": [
                            {
                                "id": "result",
                                "name": "Source",
                                "kind": "json",
                                "cardinality": "one",
                                "required": True,
                            }
                        ],
                    },
                    {
                        "id": "caption_frozen",
                        "success": "Caption copy is frozen.",
                        "output_contract": "caption_frozen_v1",
                        "intelligence": "none",
                        "tools": ["candidate_bind"],
                        "inputs": {"source_ready": "source_ready.source_ready_v1"},
                        "outputs": [
                            {
                                "id": "result",
                                "name": "Caption",
                                "kind": "json",
                                "cardinality": "one",
                                "required": True,
                            }
                        ],
                    },
                ],
                "python_standardization": [{"tool_id": "fetch_record"}],
            }
            result = write_migrated_skill_source(
                dest,
                audit,
                flow_id="case_caption_v1",
                skill_name="case-caption",
                source_root=skill,
            )
            self.assertEqual(result["milestones"], ["source_ready", "caption_frozen"])
            source = load_skill_source(dest)
            compiled = compile_skill_source(dest)
            self.assertEqual(source["canvas"]["flow_id"], "case_caption_v1")
            self.assertEqual([item["id"] for item in compiled["milestones"]], result["milestones"])
            self.assertTrue((dest / "references" / "source_ready.md").is_file())
            self.assertTrue((dest / "agents" / "openai.yaml").is_file())

    def test_candidate_bind_tool_is_not_a_passthrough(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "flowsteps" / "tools" / "candidate_bind"
            write_candidate_bind_tool(dest)
            blockers = validate_library_tool(Path(temp), "candidate_bind")
            self.assertEqual(blockers, [])

    def test_builder_audit_migrates_a_skill_without_canvas(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / "skill"
            run_dir = root / "run"
            run_dir.mkdir()
            _write(
                skill / "SKILL.md",
                "---\nname: notes-skill\ndescription: Turn notes into a package.\n---\n\n"
                "# notes-skill\n\nPackage operator notes.\n",
            )
            _write(skill / "scripts" / "compact_plan.py", "def run(input_data, **_):\n    return {'ok': True}\n")
            result = _audit(
                {
                    "request": {
                        "target": str(skill),
                        "codebase": str(root / "repo"),
                        "flow_id": None,
                        "skill_name": None,
                        "overwrite": False,
                    }
                },
                run_dir,
            )
            audit = result["outputs"]["result"]
            self.assertEqual(audit["authoring_mode"], "from_context")
            self.assertFalse(audit["source_context"]["reuse"]["canvas"])
            self.assertTrue(audit["proposed_milestones"])
            toolbox = _toolbox(
                {
                    "request": {
                        "target": str(skill),
                        "codebase": str(root / "repo"),
                        "flow_id": None,
                        "skill_name": None,
                        "overwrite": False,
                    },
                    "source_audit": audit,
                },
                run_dir,
            )["outputs"]["result"]
            generated = _generate(
                {
                    "request": {
                        "target": str(skill),
                        "codebase": str(root / "repo"),
                        "flow_id": None,
                        "skill_name": None,
                        "overwrite": False,
                    },
                    "source_audit": audit,
                    "toolbox_manifest": toolbox,
                },
                run_dir,
            )["outputs"]
            self.assertIn("workflow_source_bundle", generated)
            staged = generated["staged_harness"]
            harness = Path(staged["harness_dir"])
            self.assertTrue((harness / "agents" / "openai.yaml").is_file())
            self.assertTrue((harness / "launch.py").is_file())
            self.assertFalse(staged.get("equivalence_claimed", True))


if __name__ == "__main__":
    unittest.main()
