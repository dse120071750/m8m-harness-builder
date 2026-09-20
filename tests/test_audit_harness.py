from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from support import EXAMPLE, optional_product_repo, optional_sample_skill

from audit_harness import (
    AUDIT_SCHEMA,
    _is_action_step,
    audit_harness,
    audit_skill,
    main,
    render_audit_markdown,
)
from flowstep_runtime import validate_against_schema


AUDIT_CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "flowstep_skill_audit_v1.schema.json"
REQUIRED_HEADINGS = (
    "## Audited skill",
    "## Goal",
    "## Tool vs intelligence",
    "## Current tools",
    "## Proposed milestone split",
    "## Toolbox plan",
    "## Teaching contracts",
    "## Tools to standardize to Python",
    "## Schema control",
    "## FlowStep input and output schemas",
)


class AuditHarnessTests(unittest.TestCase):
    def test_external_state_suffixes_remain_milestones(self) -> None:
        for step_id in ("base_written", "case_io_complete"):
            self.assertFalse(
                _is_action_step(
                    {
                        "id": step_id,
                        "intelligence": "none",
                        "handler": f"milestones/{step_id}/assemble.py",
                    }
                )
            )

    def test_article_repo_flow_is_milestone_toolbox(self) -> None:
        product = optional_product_repo()
        if product is None:
            self.skipTest("set M8M_PRODUCT_REPO to audit a live product flow")
        repo = product / "flowsteps" / "flows" / "article_infographic_zh_hant_v2"
        if not repo.is_dir():
            self.skipTest("sample article flow not in M8M_PRODUCT_REPO")
        report = audit_harness(repo)
        self.assertEqual(report["verdict"], "MILESTONE_TOOLBOX")
        self.assertEqual(report["flow_schema"], "flowstep_flow_v3")
        self.assertEqual(report["p0_count"], 0)

    def test_v4_fixture_is_milestone_toolbox(self) -> None:
        report = audit_harness(EXAMPLE)
        self.assertEqual(report["verdict"], "MILESTONE_TOOLBOX")
        self.assertEqual(report["flow_schema"], "flowstep_flow_v4")

    def test_invalid_cache_policy_is_a_p0_finding(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo" / "flowsteps" / "flows" / "bad_cache_v1"
            root.mkdir(parents=True)
            (root / "flow.yaml").write_text(
                "\n".join(
                    [
                        "schema: flowstep_flow_v4",
                        "flow_id: bad_cache_v1",
                        "version: 1",
                        "artifact_root: artifacts",
                        "milestones:",
                        "  - id: result_ready",
                        "    success: A result is ready.",
                        "    output_contract: result_v1",
                        "    output_schema: output.schema.json",
                        "    outputs:",
                        "      - {id: result, name: Result, kind: json, cardinality: one, required: true}",
                        "    handler: assemble.py",
                        "    input_schema: input.schema.json",
                        "    inputs: {request: user.request}",
                        "    intelligence: none",
                        "    cache:",
                        "      reuse: candidate",
                        "      side_effects: none",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report = audit_harness(root)
            self.assertEqual(report["verdict"], "NEEDS_UPGRADE")
            self.assertTrue(
                any(
                    item["severity"] == "P0" and "ttl_seconds" in item["note"]
                    for item in report["findings"]
                )
            )
            text = (root / "flow.yaml").read_text(encoding="utf-8")
            text = text.replace(
                "    cache:\n",
                "    tools: [upload_asset]\n    cache:\n",
            ).replace(
                "      side_effects: none\n",
                "      ttl_seconds: 60\n      side_effects: none\n",
            )
            (root / "flow.yaml").write_text(text, encoding="utf-8")
            report = audit_harness(root)
            self.assertTrue(
                any(
                    item["severity"] == "P0" and "external side effects" in item["note"]
                    for item in report["findings"]
                )
            )

    def test_external_milestone_requires_phase_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo" / "flowsteps" / "flows" / "publish_v1"
            root.mkdir(parents=True)
            (root / "flow.yaml").write_text(
                "\n".join(
                    [
                        "schema: flowstep_flow_v4",
                        "flow_id: publish_v1",
                        "version: 1",
                        "milestones:",
                        "  - id: published_live",
                        "    success: Live readback passes.",
                        "    output_contract: published_v1",
                        "    output_schema: output.schema.json",
                        "    outputs:",
                        "      - {id: result, name: Published, kind: json, cardinality: one, required: true}",
                        "    handler: assemble.py",
                        "    inputs: {request: user.request}",
                        "    tools: [publish_case]",
                        "    intelligence: none",
                        "    on_tool_fail: retryable",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report = audit_harness(root)
            self.assertTrue(
                any(
                    item["severity"] == "P0" and "phase_journal" in item["note"]
                    for item in report["findings"]
                )
            )

    def test_v4_blank_expectation_fields_are_p0_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo" / "flowsteps" / "flows" / "blank_v1"
            root.mkdir(parents=True)
            (root / "flow.yaml").write_text(
                "\n".join(
                    [
                        "schema: flowstep_flow_v4",
                        "flow_id: blank_v1",
                        "version: 1",
                        "milestones:",
                        "  - id: result_ready",
                        "    success: '   '",
                        "    output_contract: '   '",
                        "    output_schema: '   '",
                        "    outputs:",
                        "      - {id: '   ', name: '   ', kind: json, cardinality: one, required: true}",
                        "    handler: assemble.py",
                        "    intelligence: none",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report = audit_harness(root)
            notes = {item["note"] for item in report["findings"] if item["severity"] == "P0"}
            self.assertIn("missing milestone success goal", notes)
            self.assertIn("missing non-blank output_contract", notes)
            self.assertIn("missing non-blank output_schema", notes)
            self.assertIn("outputs[0].id must be non-blank", notes)
            self.assertIn("outputs[0].name must be non-blank", notes)

    def test_v4_output_schema_must_close_root_and_outputs_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo" / "flowsteps" / "flows" / "open_schema_v1"
            root.mkdir(parents=True)
            (root / "flow.yaml").write_text(
                "\n".join(
                    [
                        "schema: flowstep_flow_v4",
                        "flow_id: open_schema_v1",
                        "version: 1",
                        "milestones:",
                        "  - id: result_ready",
                        "    success: A result is ready.",
                        "    output_contract: result_v1",
                        "    output_schema: output.schema.json",
                        "    outputs:",
                        "      - {id: result, name: Result, kind: json, cardinality: one, required: true}",
                        "    handler: assemble.py",
                        "    intelligence: none",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "output.schema.json").write_text(
                json.dumps(
                    {
                        "type": "object",
                        "properties": {
                            "outputs": {
                                "type": "object",
                                "properties": {"result": {"type": "object"}},
                                "required": ["result"],
                            }
                        },
                        "required": ["outputs"],
                    }
                ),
                encoding="utf-8",
            )
            report = audit_harness(root)
            notes = {item["note"] for item in report["findings"] if item["severity"] == "P0"}
            self.assertIn(
                "output_schema root must be an object with additionalProperties: false",
                notes,
            )
            self.assertIn(
                "output_schema outputs object must set additionalProperties: false",
                notes,
            )

    def test_v4_audit_enforces_exact_judge_binding_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo" / "flowsteps" / "flows" / "judge_shape_v1"
            root.mkdir(parents=True)
            common = [
                "schema: flowstep_flow_v4",
                "flow_id: judge_shape_v1",
                "version: 1",
                "milestones:",
                "  - id: result_ready",
                "    success: A result is ready.",
                "    output_contract: result_v1",
                "    output_schema: output.schema.json",
                "    outputs:",
                "      - {id: result, name: Result, kind: json, cardinality: one, required: true}",
                "    handler: assemble.py",
                "    intelligence: none",
            ]
            (root / "flow.yaml").write_text(
                "\n".join(common + ["    loop: judge"]) + "\n",
                encoding="utf-8",
            )
            report = audit_harness(root)
            self.assertTrue(
                any(
                    item["severity"] == "P0"
                    and "loop=judge requires closed" in item["note"]
                    for item in report["findings"]
                )
            )
            (root / "flow.yaml").write_text(
                "\n".join(
                    common
                    + [
                        "    loop: none",
                        "    worker: result_judge@1.0.0",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report = audit_harness(root)
            self.assertTrue(
                any(
                    item["severity"] == "P0"
                    and "loop=none forbids" in item["note"]
                    for item in report["findings"]
                )
            )

class AuditWorkerTests(unittest.TestCase):
    def test_external_writer_script_outside_linked_flow_is_p0(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            flow = repo / "flowsteps" / "flows" / "sample_v1"
            skill = repo / "skill"
            (skill / "scripts").mkdir(parents=True)
            flow.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: sample-skill\ndescription: Sample publisher.\n---\n\n"
                f"Live flow: `{flow}`\n",
                encoding="utf-8",
            )
            writer = skill / "scripts" / "publish_case.py"
            writer.write_text(
                "def run(client):\n    return client.post('/commit', {})\n",
                encoding="utf-8",
            )
            (flow / "flow.yaml").write_text(
                "\n".join(
                    [
                        "schema: flowstep_flow_v4",
                        "flow_id: sample_v1",
                        "version: 1",
                        "milestones:",
                        "  - id: source_ready",
                        "    success: Source is ready.",
                        "    output_contract: source_v1",
                        "    output_schema: output.schema.json",
                        "    outputs:",
                        "      - {id: result, name: Source, kind: json, cardinality: one, required: true}",
                        "    handler: assemble.py",
                        "    inputs: {request: user.request}",
                        "    tools: [hash_bind]",
                        "    intelligence: none",
                        "    on_tool_fail: BLOCKED",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report = audit_skill(skill)
            self.assertTrue(
                any(
                    item["severity"] == "P0" and "outside the linked M8M flow" in item["note"]
                    for item in report["grade"]["findings"]
                )
            )
            writer.write_text(
                'M8M_EXTERNAL_SIDE_EFFECT_OWNER = "other-writer-skill"\n'
                "def run(client):\n    return client.post('/commit', {})\n",
                encoding="utf-8",
            )
            report = audit_skill(skill)
            self.assertFalse(
                any("outside the linked M8M flow" in item["note"] for item in report["grade"]["findings"])
            )

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
        self.assertEqual(source["flowsteps"][0]["tool"], "ingest")
        labeled = next(item for item in report["proposed_milestones"] if "label" in item["id"])
        self.assertEqual(labeled["intelligence"], "completion")
        self.assertIn("tool_vs_intelligence", report)
        self.assertEqual(report["tool_vs_intelligence"]["schema"], "tool_vs_intelligence_table_v1")
        self.assertTrue(report["tool_vs_intelligence"]["rows"])
        source_result = source["output_schema"]["properties"]["outputs"]["properties"]["result"]
        label_result = labeled["output_schema"]["properties"]["outputs"]["properties"]["result"]
        self.assertIn("sentences", source_result["properties"])
        self.assertIn("label", label_result["properties"])
        self.assertIn("source_ready", labeled["inputs"])
        self.assertIn("flowsteps", labeled)
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
            self.assertEqual(ids, ["milestone01", "milestone02"])
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
            self.assertEqual(ids, ["milestone01", "milestone02"])
            current_ids = {item["id"] for item in report["current_tools"]}
            self.assertIn("fetch_record", current_ids)
            self.assertIn("package_bundle", current_ids)
            self.assertIn("run", current_ids)
            source = report["proposed_milestones"][0]
            self.assertIn("source_path", source["output_schema"]["properties"])
            tool_ids = [item["tool_id"] for item in report["python_standardization"]]
            self.assertIn("fetch_record", tool_ids)
            self.assertNotIn("run", tool_ids)

    def test_main_writes_planning_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report_path = Path(temp) / "flowstep-audit.md"
            code = main(["--target", str(EXAMPLE), "--write-report", str(report_path)])
            self.assertEqual(code, 0)
            text = report_path.read_text(encoding="utf-8")
            for heading in REQUIRED_HEADINGS:
                self.assertIn(heading, text)
            self.assertIn("text-pipeline", text)

    def test_case_io_skill_follows_linked_repo_flow(self) -> None:
        skill = optional_sample_skill()
        product = optional_product_repo()
        if skill is None or product is None:
            self.skipTest("set M8M_SAMPLE_SKILL and M8M_PRODUCT_REPO to audit a linked product skill")
        repo = product / "flowsteps" / "flows" / "nisan_case_io_v1"
        if not repo.is_dir():
            self.skipTest("sample case-io flow not in M8M_PRODUCT_REPO")
        report = audit_skill(skill)
        ids = [item["id"] for item in report["proposed_milestones"]]
        for expected in (
            "request_ready",
            "package_admitted",
            "restyle_with_floor_base_written",
            "restyle_with_floor_floor_written",
            "restyle_with_floor_dna_written",
            "restyle_with_floor_facts_written",
            "restyle_with_floor_final_verified",
            "restyle_without_floor_final_verified",
            "case_io_complete",
        ):
            self.assertIn(expected, ids)
        self.assertIn("existing-case-patch-worker", {item["id"] for item in report["current_tools"]})

    def test_article_skill_keeps_six_milestones(self) -> None:
        article = optional_sample_skill()
        if article is None:
            self.skipTest("set M8M_SAMPLE_SKILL to audit a live article skill")
        report = audit_skill(article)
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
