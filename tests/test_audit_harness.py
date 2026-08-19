from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from support import EXAMPLE

from audit_harness import (
    AUDIT_SCHEMA,
    audit_harness,
    audit_skill,
    main,
    render_audit_markdown,
)
from flowstep_runtime import validate_against_schema


ARTICLE = Path(r"C:\Users\gasil\.codex\skills\article-infographic-maker")
AUDIT_CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "flowstep_skill_audit_v1.schema.json"
REQUIRED_HEADINGS = (
    "## Audited skill",
    "## Goal",
    "## Tool vs intelligence",
    "## Current tools",
    "## Proposed milestone split",
    "## Toolbox plan",
    "## Tools to standardize to Python",
    "## Schema control",
    "## FlowStep input and output schemas",
)


class AuditHarnessTests(unittest.TestCase):
    def test_article_repo_flow_is_milestone_toolbox(self) -> None:
        repo = Path(r"D:\nisan-n8n\flowsteps\flows\article_infographic_zh_hant_v2")
        if not repo.is_dir():
            self.skipTest("article repo flow not installed")
        report = audit_harness(repo)
        self.assertEqual(report["verdict"], "MILESTONE_TOOLBOX")
        self.assertEqual(report["flow_schema"], "flowstep_flow_v3")
        self.assertEqual(report["p0_count"], 0)

    def test_v2_fixture_is_not_milestone_toolbox(self) -> None:
        report = audit_harness(EXAMPLE)
        self.assertEqual(report["verdict"], "NEEDS_UPGRADE")
        self.assertEqual(report["flow_schema"], "flowstep_flow_v2")


class AuditWorkerTests(unittest.TestCase):
    def test_text_pipeline_audit_writes_required_sections(self) -> None:
        report = audit_skill(EXAMPLE)
        self.assertEqual(report["schema"], AUDIT_SCHEMA)
        self.assertEqual(report["audited_skill"]["name"], "text-pipeline")
        self.assertIn("Separate", report["goal"])
        ids = [item["id"] for item in report["proposed_milestones"]]
        self.assertIn("source_ready", ids)
        self.assertTrue(any(item["id"].endswith("_frozen") or item["id"] == "label" for item in report["proposed_milestones"]))
        source = next(item for item in report["proposed_milestones"] if item["id"] == "source_ready")
        self.assertIn("ingest", source["tools"])
        self.assertIn("segment", source["tools"])
        labeled = next(item for item in report["proposed_milestones"] if "label" in item["id"])
        self.assertEqual(labeled["intelligence"], "completion")
        self.assertIn("tool_vs_intelligence", report)
        self.assertEqual(report["tool_vs_intelligence"]["schema"], "tool_vs_intelligence_table_v1")
        self.assertTrue(report["tool_vs_intelligence"]["rows"])
        self.assertIn("sentences", source["output_schema"]["properties"])
        self.assertIn("label", labeled["output_schema"]["properties"])
        self.assertIn("source_ready", labeled["inputs"])
        self.assertTrue(labeled["tools"])
        markdown = render_audit_markdown(report)
        for heading in REQUIRED_HEADINGS:
            self.assertIn(heading, markdown)
        self.assertIn("**Input schema**", markdown)
        self.assertIn("**Output schema**", markdown)
        validate_against_schema(report, AUDIT_CONTRACT)

    def test_action_named_steps_become_python_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "toy-skill"
            (root / "flows").mkdir(parents=True)
            (root / "SKILL.md").write_text(
                "---\nname: toy-skill\ndescription: Crop then plan.\n---\n\n# toy\n",
                encoding="utf-8",
            )
            (root / "flows" / "toy_v1.yaml").write_text(
                "\n".join(
                    [
                        "schema: flowstep_flow_v2",
                        "flow_id: toy_v1",
                        "version: 1",
                        "steps:",
                        "  - id: crop_4x5",
                        "    class: tool",
                        "    handler: steps/crop_4x5/tool.py",
                        "    model: none",
                        "    inputs:",
                        "      request: user.request",
                        "    output_contract: crop_v1",
                        "    input_schema: steps/crop_4x5/input.schema.json",
                        "    output_schema: steps/crop_4x5/output.schema.json",
                        "  - id: plan_frozen",
                        "    class: intelligence",
                        "    handler: steps/plan_frozen/tool.py",
                        "    model: completion",
                        "    inputs:",
                        "      crop_4x5: crop_4x5.crop_v1",
                        "    output_contract: plan_v1",
                        "    input_schema: steps/plan_frozen/input.schema.json",
                        "    output_schema: steps/plan_frozen/output.schema.json",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report = audit_skill(root)
            ids = [item["id"] for item in report["proposed_milestones"]]
            self.assertEqual(ids[0], "source_ready")
            self.assertIn("plan_frozen", ids)
            self.assertNotIn("crop_4x5", ids)
            source = report["proposed_milestones"][0]
            self.assertIn("crop_4x5", source["tools"])
            tool_ids = [item["tool_id"] for item in report["python_standardization"]]
            self.assertIn("crop_4x5", tool_ids)
            self.assertIn("flowsteps/tools/crop_4x5/", report["python_standardization"][0]["destination"])

    def test_no_flow_skill_proposes_from_scripts_and_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "bare-skill"
            (root / "scripts").mkdir(parents=True)
            (root / "contracts").mkdir()
            (root / "agents").mkdir()
            (root / "SKILL.md").write_text(
                "---\nname: bare-skill\ndescription: Fetch a record and package it.\n---\n\n# bare\n",
                encoding="utf-8",
            )
            (root / "scripts" / "fetch_record.py").write_text("def run(input_data, **kwargs):\n    return input_data\n", encoding="utf-8")
            (root / "scripts" / "package_bundle.py").write_text("def run(input_data, **kwargs):\n    return input_data\n", encoding="utf-8")
            (root / "scripts" / "run.py").write_text("print('driver')\n", encoding="utf-8")
            (root / "contracts" / "article_source_v2.schema.json").write_text(
                json.dumps(
                    {
                        "type": "object",
                        "required": ["source_path", "source_sha256"],
                        "properties": {
                            "source_path": {"type": "string"},
                            "source_sha256": {"type": "string"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / "agents" / "release_judge_worker.yaml").write_text(
                "agent_id: release_judge_worker\nrole: persistent_workflow_worker\n",
                encoding="utf-8",
            )
            report = audit_skill(root)
            self.assertEqual(report["verdict"], "NO_FLOW")
            ids = [item["id"] for item in report["proposed_milestones"]]
            self.assertIn("source_ready", ids)
            self.assertTrue(any(item["id"] == "release_packaged" or "package" in item["id"] for item in report["proposed_milestones"]))
            current_ids = {item["id"] for item in report["current_tools"]}
            self.assertIn("fetch_record", current_ids)
            self.assertIn("package_bundle", current_ids)
            self.assertIn("run", current_ids)
            source = next(item for item in report["proposed_milestones"] if item["id"] == "source_ready")
            self.assertIn("source_path", source["output_schema"]["properties"])
            tool_ids = [item["tool_id"] for item in report["python_standardization"]]
            self.assertIn("fetch_record", tool_ids)
            self.assertNotIn("run", tool_ids)

    def test_main_writes_planning_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report_path = Path(temp) / "flowstep-audit.md"
            code = main(["--target", str(EXAMPLE), "--write-report", str(report_path)])
            self.assertEqual(code, 3)
            text = report_path.read_text(encoding="utf-8")
            for heading in REQUIRED_HEADINGS:
                self.assertIn(heading, text)
            self.assertIn("text-pipeline", text)

    def test_case_io_skill_follows_linked_repo_flow(self) -> None:
        skill = Path(r"C:\Users\gasil\.codex\skills\nisan-case-io")
        repo = Path(r"D:\nisan-n8n\flowsteps\flows\nisan_case_io_v1")
        if not skill.is_dir() or not repo.is_dir():
            self.skipTest("nisan-case-io flow not installed")
        report = audit_skill(skill)
        ids = [item["id"] for item in report["proposed_milestones"]]
        self.assertEqual(ids, ["live_case_bound", "package_admitted", "write_verified"])
        self.assertIn("existing-case-patch-worker", {item["id"] for item in report["current_tools"]})

    def test_article_skill_keeps_six_milestones(self) -> None:
        if not ARTICLE.is_dir():
            self.skipTest("article skill not installed")
        report = audit_skill(ARTICLE)
        ids = [item["id"] for item in report["proposed_milestones"]]
        for expected in (
            "source_ready",
            "plan_frozen",
            "prompts_frozen",
            "assets_bound",
            "cards_rendered",
            "release_packaged",
        ):
            self.assertIn(expected, ids)
        rendered = next(item for item in report["proposed_milestones"] if item["id"] == "cards_rendered")
        self.assertIn("render_html_shell", rendered["tools"])
        self.assertGreaterEqual(len(rendered["inputs"]), 2)
        markdown = render_audit_markdown(report)
        self.assertIn("## Goal", markdown)
        self.assertIn("render_html_shell", markdown)


if __name__ == "__main__":
    unittest.main()
