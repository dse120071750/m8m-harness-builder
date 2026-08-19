from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from audit_harness import audit_skill, infer_schema_control, render_audit_markdown, write_audit_markdown
from m8m_flowchart import render_mermaid, write_flowchart
from toolbox_plan import build_toolbox_plan, render_toolbox_plan_markdown
from flowstep_runtime import FlowError, load_flow, read_json, step_class_hint
from generate_harness import generate_from_audit, generate_tool, generate_v3_flow
from run_flow import advance
from schema_gate import is_control_name
from validate_harness import validate_harness


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _passthrough_assemble(path: Path) -> None:
    _write(
        path,
        "def run(input_data, draft=None, **_):\n"
        "    req = input_data.get('request') if isinstance(input_data.get('request'), dict) else input_data\n"
        "    if len(input_data) == 1:\n"
        "        only = next(iter(input_data.values()))\n"
        "        if isinstance(only, dict) and 'request' not in input_data:\n"
        "            req = only\n"
        "    return dict(req)\n",
    )


def _ok_test(path: Path) -> None:
    _write(path, "def test_ok():\n    assert True\n")


class ControlNameTests(unittest.TestCase):
    def test_if_loop_names_still_draw(self) -> None:
        self.assertTrue(is_control_name("if_ready"))
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            result = generate_v3_flow(codebase, "bad_v1", ["if_ready"], tools=["hash_bind"])
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(any("if_ready" in note for note in result.get("notes") or []))
            self.assertTrue(Path(result["flowchart_path"]).is_file())


class GateTests(unittest.TestCase):
    def test_schema_gate_selects_url_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            generate_v3_flow(
                codebase,
                "gate_v1",
                ["kind_bound", "url_ready", "file_ready", "source_ready"],
                tools=["hash_bind"],
            )
            harness = codebase / "flowsteps" / "flows" / "gate_v1"
            (harness / "schemas" / "gates").mkdir(parents=True, exist_ok=True)
            _write(
                harness / "schemas" / "kind_bound_v1.json",
                json.dumps(
                    {
                        "type": "object",
                        "required": ["kind"],
                        "properties": {
                            "kind": {"enum": ["url", "file"]},
                            "path": {"type": "string"},
                        },
                    }
                ),
            )
            _write(
                harness / "schemas" / "gates" / "kind_url.schema.json",
                json.dumps({"type": "object", "required": ["kind"], "properties": {"kind": {"const": "url"}}}),
            )
            _write(
                harness / "schemas" / "gates" / "kind_file.schema.json",
                json.dumps({"type": "object", "required": ["kind"], "properties": {"kind": {"const": "file"}}}),
            )
            for mid in ("url_ready", "file_ready", "source_ready"):
                _write(
                    harness / "schemas" / f"{mid}_v1.json",
                    json.dumps(
                        {
                            "type": "object",
                            "required": ["kind"],
                            "properties": {
                                "kind": {"type": "string"},
                                "path": {"type": "string"},
                                "sha256": {"type": "string"},
                            },
                        }
                    ),
                )
            flow = (harness / "flow.yaml").read_text(encoding="utf-8")
            flow += (
                "\n# patched in test\n"
            )
            raw = harness / "flow.yaml"
            text = raw.read_text(encoding="utf-8")
            insert = (
                "    next:\n"
                "      - when: schemas/gates/kind_url.schema.json\n"
                "        then: url_ready\n"
                "      - when: schemas/gates/kind_file.schema.json\n"
                "        then: file_ready\n"
                "    else: BLOCKED\n"
            )
            text = text.replace("    handler: milestones/kind_bound/assemble.py\n", "    handler: milestones/kind_bound/assemble.py\n" + insert)
            text = text.replace(
                "    handler: milestones/source_ready/assemble.py\n",
                "    handler: milestones/source_ready/assemble.py\n    join: [url_ready, file_ready]\n",
            )
            raw.write_text(text, encoding="utf-8")
            _write(
                harness / "milestones" / "source_ready" / "input.schema.json",
                json.dumps({"type": "object", "additionalProperties": True}),
            )
            for mid in ("kind_bound", "url_ready", "file_ready", "source_ready"):
                _passthrough_assemble(harness / "milestones" / mid / "assemble.py")
                _ok_test(harness / "milestones" / mid / "tests" / "test_assemble.py")
            loaded = load_flow(harness)
            self.assertEqual(len(loaded["steps"][0]["next"]), 2)
            asset = Path(temp) / "page.txt"
            asset.write_text("x", encoding="utf-8")
            request = Path(temp) / "request.json"
            request.write_text(json.dumps({"kind": "url", "path": str(asset)}), encoding="utf-8")
            result = advance(harness, Path(temp) / "run-url", request_path=request)
            self.assertEqual(result["state"], "COMPLETE")
            record = read_json(Path(temp) / "run-url" / "flow-execution-record.json")
            ids = [item["step_id"] for item in record["steps"]]
            self.assertIn("url_ready", ids)
            self.assertNotIn("file_ready", ids)
            self.assertIn("file_ready", record.get("skipped") or [])

    def test_no_gate_match_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            generate_v3_flow(codebase, "gate2_v1", ["kind_bound", "url_ready"], tools=["hash_bind"])
            harness = codebase / "flowsteps" / "flows" / "gate2_v1"
            (harness / "schemas" / "gates").mkdir(parents=True, exist_ok=True)
            _write(
                harness / "schemas" / "kind_bound_v1.json",
                json.dumps({"type": "object", "required": ["kind"], "properties": {"kind": {"type": "string"}}}),
            )
            _write(
                harness / "schemas" / "gates" / "kind_url.schema.json",
                json.dumps({"type": "object", "required": ["kind"], "properties": {"kind": {"const": "url"}}}),
            )
            text = (harness / "flow.yaml").read_text(encoding="utf-8")
            text = text.replace(
                "    handler: milestones/kind_bound/assemble.py\n",
                "    handler: milestones/kind_bound/assemble.py\n"
                "    next:\n"
                "      - when: schemas/gates/kind_url.schema.json\n"
                "        then: url_ready\n"
                "    else: BLOCKED\n",
            )
            (harness / "flow.yaml").write_text(text, encoding="utf-8")
            for mid in ("kind_bound", "url_ready"):
                _passthrough_assemble(harness / "milestones" / mid / "assemble.py")
                _ok_test(harness / "milestones" / mid / "tests" / "test_assemble.py")
            request = Path(temp) / "request.json"
            request.write_text(json.dumps({"kind": "other"}), encoding="utf-8")
            blocked = advance(harness, Path(temp) / "run-miss", request_path=request)
            self.assertEqual(blocked["state"], "BLOCKED")
            self.assertIn("no gate matched", blocked["blockers"][0])


class ForeachTests(unittest.TestCase):
    def test_foreach_two_items_pass_eight_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            generate_v3_flow(codebase, "loop_v1", ["source_ready", "pages_bound"], tools=["hash_bind"])
            harness = codebase / "flowsteps" / "flows" / "loop_v1"
            _write(
                harness / "schemas" / "source_ready_v1.json",
                json.dumps(
                    {
                        "type": "object",
                        "required": ["pages"],
                        "properties": {
                            "pages": {
                                "type": "array",
                                "maxItems": 7,
                                "items": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
                            }
                        },
                    }
                ),
            )
            _write(
                harness / "schemas" / "page_v1.json",
                json.dumps({"type": "object", "required": ["path"], "properties": {"path": {"type": "string", "minLength": 1}}}),
            )
            _write(
                harness / "schemas" / "pages_bound_v1.json",
                json.dumps(
                    {
                        "type": "object",
                        "required": ["pages", "item_count"],
                        "properties": {
                            "pages": {"type": "array"},
                            "item_count": {"type": "integer"},
                        },
                    }
                ),
            )
            text = (harness / "flow.yaml").read_text(encoding="utf-8")
            text = text.replace(
                "    handler: milestones/pages_bound/assemble.py\n",
                "    handler: milestones/pages_bound/assemble.py\n"
                "    foreach:\n"
                "      path: pages\n"
                "      item_schema: schemas/page_v1.json\n"
                "      tools: [hash_bind]\n"
                "      max_items: 7\n"
                "      collect: pages\n",
            )
            (harness / "flow.yaml").write_text(text, encoding="utf-8")
            _passthrough_assemble(harness / "milestones" / "source_ready" / "assemble.py")
            _ok_test(harness / "milestones" / "source_ready" / "tests" / "test_assemble.py")
            _passthrough_assemble(harness / "milestones" / "pages_bound" / "assemble.py")
            _ok_test(harness / "milestones" / "pages_bound" / "tests" / "test_assemble.py")
            files = []
            for i in range(2):
                path = Path(temp) / f"p{i}.txt"
                path.write_text(f"{i}", encoding="utf-8")
                files.append({"path": str(path)})
            request = Path(temp) / "request.json"
            request.write_text(json.dumps({"pages": files}), encoding="utf-8")
            done = advance(harness, Path(temp) / "run-2", request_path=request)
            self.assertEqual(done["state"], "COMPLETE")
            out = read_json(Path(temp) / "run-2" / "artifacts" / "pages_bound.pages_bound_v1.json")
            self.assertEqual(out["data"]["item_count"], 2)
            self.assertEqual(len(out["data"]["pages"][0]["sha256"]), 64)

            extra = []
            for i in range(8):
                path = Path(temp) / f"q{i}.txt"
                path.write_text(f"{i}", encoding="utf-8")
                extra.append({"path": str(path)})
            request8 = Path(temp) / "request8.json"
            request8.write_text(json.dumps({"pages": extra}), encoding="utf-8")
            blocked = advance(harness, Path(temp) / "run-8", request_path=request8)
            self.assertEqual(blocked["state"], "BLOCKED")


class ValidateControlTests(unittest.TestCase):
    def test_foreach_without_maxitems_on_schema_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            generate_v3_flow(codebase, "badloop_v1", ["source_ready", "pages_bound"], tools=["hash_bind"])
            harness = codebase / "flowsteps" / "flows" / "badloop_v1"
            _write(
                harness / "schemas" / "source_ready_v1.json",
                json.dumps(
                    {
                        "type": "object",
                        "required": ["pages"],
                        "additionalProperties": False,
                        "properties": {"pages": {"type": "array"}},
                    }
                ),
            )
            _write(harness / "schemas" / "page_v1.json", json.dumps({"type": "object"}))
            text = (harness / "flow.yaml").read_text(encoding="utf-8")
            text = text.replace(
                "    handler: milestones/pages_bound/assemble.py\n",
                "    handler: milestones/pages_bound/assemble.py\n"
                "    foreach:\n"
                "      path: pages\n"
                "      item_schema: schemas/page_v1.json\n"
                "      tools: [hash_bind]\n"
                "      max_items: 7\n",
            )
            (harness / "flow.yaml").write_text(text, encoding="utf-8")
            for mid in ("source_ready", "pages_bound"):
                _passthrough_assemble(harness / "milestones" / mid / "assemble.py")
                _ok_test(harness / "milestones" / mid / "tests" / "test_assemble.py")
            with self.assertRaises(FlowError) as ctx:
                validate_harness(codebase=codebase, flow_id="badloop_v1")
            self.assertIn("maxItems", str(ctx.exception))


class SkillWriterControlTests(unittest.TestCase):
    def test_infer_gate_from_enum_not_from_model(self) -> None:
        milestones = [
            {
                "id": "kind_bound",
                "intelligence": "none",
                "tools": ["hash_bind"],
                "output_schema": {
                    "type": "object",
                    "properties": {"kind": {"enum": ["url", "file"]}},
                },
            },
            {"id": "url_ready", "intelligence": "none", "tools": ["hash_bind"], "output_schema": {}},
            {"id": "file_ready", "intelligence": "none", "tools": ["hash_bind"], "output_schema": {}},
            {"id": "source_ready", "intelligence": "none", "tools": ["hash_bind"], "output_schema": {}},
        ]
        infer_schema_control(milestones)
        self.assertEqual(len(milestones[0]["next"]), 2)
        self.assertEqual(milestones[0]["else"], "BLOCKED")
        thens = {edge["then"] for edge in milestones[0]["next"]}
        self.assertEqual(thens, {"url_ready", "file_ready"})
        self.assertEqual(milestones[0]["next"][0]["schema"]["properties"]["kind"]["const"], "url")
        self.assertEqual(milestones[3]["join"], ["url_ready", "file_ready"])

    def test_infer_foreach_from_maxitems_array(self) -> None:
        milestones = [
            {
                "id": "source_ready",
                "intelligence": "none",
                "tools": ["hash_bind"],
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "pages": {
                            "type": "array",
                            "maxItems": 7,
                            "items": {"type": "object", "required": ["path"]},
                        }
                    },
                },
            },
            {
                "id": "pages_bound",
                "intelligence": "none",
                "tools": ["hash_bind"],
                "output_schema": {},
            },
        ]
        infer_schema_control(milestones)
        fe = milestones[1]["foreach"]
        self.assertEqual(fe["path"], "pages")
        self.assertEqual(fe["max_items"], 7)
        self.assertEqual(fe["tools"], ["hash_bind"])

    def test_intelligence_does_not_get_foreach(self) -> None:
        milestones = [
            {
                "id": "source_ready",
                "intelligence": "none",
                "tools": ["hash_bind"],
                "output_schema": {
                    "type": "object",
                    "properties": {"pages": {"type": "array", "maxItems": 7, "items": {"type": "object"}}},
                },
            },
            {"id": "pages_frozen", "intelligence": "completion", "tools": ["hash_bind"], "output_schema": {}},
        ]
        infer_schema_control(milestones)
        self.assertNotIn("foreach", milestones[1])

    def test_audit_then_generate_writes_gate_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "gated-skill"
            (root / "flows").mkdir(parents=True)
            (root / "schemas").mkdir()
            (root / "SKILL.md").write_text(
                "---\nname: gated-skill\ndescription: URL or file intake.\n---\n\n# gated\n",
                encoding="utf-8",
            )
            (root / "schemas" / "kind_bound_v1.json").write_text(
                json.dumps(
                    {
                        "type": "object",
                        "required": ["kind"],
                        "properties": {"kind": {"enum": ["url", "file"]}},
                    }
                ),
                encoding="utf-8",
            )
            (root / "flows" / "gated_v1.yaml").write_text(
                "\n".join(
                    [
                        "schema: flowstep_flow_v3",
                        "flow_id: gated_v1",
                        "version: 1",
                        "max_run_seconds: 60",
                        "artifact_root: artifacts",
                        "milestones:",
                        "  - id: kind_bound",
                        "    output_contract: kind_bound_v1",
                        "    output_schema: schemas/kind_bound_v1.json",
                        "    tools: [hash_bind]",
                        "    intelligence: none",
                        "    handler: milestones/kind_bound/assemble.py",
                        "  - id: url_ready",
                        "    output_contract: url_ready_v1",
                        "    output_schema: schemas/url_ready_v1.json",
                        "    tools: [hash_bind]",
                        "    intelligence: none",
                        "    handler: milestones/url_ready/assemble.py",
                        "  - id: file_ready",
                        "    output_contract: file_ready_v1",
                        "    output_schema: schemas/file_ready_v1.json",
                        "    tools: [hash_bind]",
                        "    intelligence: none",
                        "    handler: milestones/file_ready/assemble.py",
                        "  - id: source_ready",
                        "    output_contract: source_ready_v1",
                        "    output_schema: schemas/source_ready_v1.json",
                        "    tools: [hash_bind]",
                        "    intelligence: none",
                        "    handler: milestones/source_ready/assemble.py",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report = audit_skill(root)
            kind_bound = next(item for item in report["proposed_milestones"] if item["id"] == "kind_bound")
            self.assertTrue(kind_bound.get("next"))
            self.assertEqual(kind_bound["else"], "BLOCKED")
            self.assertTrue(any(row["kind"] == "gate" for row in report["control"]))
            markdown = render_audit_markdown(report)
            self.assertIn("## Schema control", markdown)
            self.assertIn("json_schema", markdown)
            codebase = Path(temp) / "repo"
            result = generate_from_audit(codebase, report, flow_id="gated_v1", skill_name="gated-skill")
            harness = Path(result["harness_dir"])
            flow = (harness / "flow.yaml").read_text(encoding="utf-8")
            self.assertIn("when: schemas/gates/kind_url.schema.json", flow)
            self.assertIn("then: url_ready", flow)
            self.assertIn("else: BLOCKED", flow)
            self.assertIn("join:", flow)
            gate = json.loads((harness / "schemas" / "gates" / "kind_url.schema.json").read_text(encoding="utf-8"))
            self.assertEqual(gate["properties"]["kind"]["const"], "url")
            chart = (harness / "planning" / "m8m-flowchart.md").read_text(encoding="utf-8")
            self.assertIn("```mermaid", chart)
            self.assertIn("kind=url", chart)
            self.assertIn("url_ready", chart)
            self.assertIn("else BLOCKED", chart)
            self.assertIn("## Loops (foreach)", chart)


class FlowchartMarkdownTests(unittest.TestCase):
    def test_chart_draws_gate_and_foreach(self) -> None:
        items = [
            {
                "id": "kind_bound",
                "intelligence": "none",
                "tools": ["hash_bind"],
                "next": [
                    {
                        "when": "schemas/gates/kind_url.schema.json",
                        "then": "url_ready",
                        "schema": {"required": ["kind"], "properties": {"kind": {"const": "url"}}},
                    },
                    {
                        "when": "schemas/gates/kind_file.schema.json",
                        "then": "file_ready",
                        "schema": {"required": ["kind"], "properties": {"kind": {"const": "file"}}},
                    },
                ],
                "else": "BLOCKED",
            },
            {"id": "url_ready", "intelligence": "none", "tools": ["hash_bind"]},
            {"id": "file_ready", "intelligence": "none", "tools": ["hash_bind"]},
            {"id": "source_ready", "intelligence": "none", "tools": ["hash_bind"], "join": ["url_ready", "file_ready"]},
            {
                "id": "pages_bound",
                "intelligence": "none",
                "tools": ["hash_bind"],
                "foreach": {
                    "path": "pages",
                    "item_schema": "schemas/page_v1.json",
                    "tools": ["hash_bind"],
                    "max_items": 7,
                    "collect": "pages",
                },
            },
            {"id": "release_packaged", "intelligence": "none", "tools": ["hash_bind"]},
        ]
        mermaid = render_mermaid(items)
        self.assertIn("flowchart TD", mermaid)
        self.assertIn('kind_bound -->|"kind=url"| url_ready', mermaid)
        self.assertIn('kind_bound -->|"kind=file"| file_ready', mermaid)
        self.assertIn("else BLOCKED", mermaid)
        self.assertIn('url_ready -->|"join"| source_ready', mermaid)
        self.assertIn("foreach pages max=7", mermaid)
        self.assertIn('pages_bound -->|"each pages hash_bind"| pages_bound', mermaid)
        self.assertIn("collect pages", mermaid)
        self.assertNotIn("url_ready --> file_ready", mermaid)

    def test_audit_writes_single_flowchart_md(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "toy"
            root.mkdir()
            (root / "SKILL.md").write_text(
                "---\nname: toy\ndescription: Bind a file.\n---\n\n# toy\n",
                encoding="utf-8",
            )
            (root / "scripts").mkdir()
            (root / "scripts" / "hash_bind.py").write_text("def run(x):\n    return x\n", encoding="utf-8")
            report = audit_skill(root)
            path = write_audit_markdown(report, root / "planning" / "flowstep-audit.md")
            chart = root / "planning" / "m8m-flowchart.md"
            self.assertTrue(chart.is_file())
            self.assertEqual(path.parent, chart.parent)
            text = chart.read_text(encoding="utf-8")
            self.assertIn("# M8M flowchart:", text)
            self.assertIn("```mermaid", text)
            self.assertIn("## Loops (foreach)", text)
            self.assertIn("## Gates (if / else)", text)
            self.assertIn("## Toolbox plan", text)
            audit_md = (root / "planning" / "flowstep-audit.md").read_text(encoding="utf-8")
            self.assertNotIn("```mermaid", audit_md)
            self.assertIn("## Toolbox plan", audit_md)
            self.assertIn("| Milestone | Intelligence | Existing toolbox | Promote from a skill script | Generate new |", audit_md)


class ToolboxPlanTests(unittest.TestCase):
    def test_existing_promote_generate_columns(self) -> None:
        milestones = [
            {
                "id": "carousel_captured",
                "intelligence": "none",
                "tools": ["hash_bind", "fastdl_carousel_download", "instagram_url_canonicalize"],
            },
            {
                "id": "board_distilled",
                "intelligence": "image",
                "model_justification": "look at frames, write transferable text",
                "tools": ["hash_bind", "schema_validate", "reference_board_validate"],
            },
        ]
        std = [
            {
                "tool_id": "fastdl_carousel_download",
                "source": "scripts/automate_fastdl.py",
                "action": "standardize_to_python",
            },
            {
                "tool_id": "reference_board_validate",
                "source": "distill-automotive-reference-board/scripts/validate_reference_board.py",
                "action": "standardize_to_python",
            },
        ]
        plan = build_toolbox_plan(milestones, std)
        first = plan[0]
        self.assertIn("hash_bind", first["existing"])
        self.assertEqual(first["promote"][0]["tool_id"], "fastdl_carousel_download")
        self.assertEqual(first["promote"][0]["source"], "scripts/automate_fastdl.py")
        self.assertIn("instagram_url_canonicalize", first["generate"])
        md = render_toolbox_plan_markdown(plan)
        self.assertIn("`image` (look at frames, write transferable text)", md)
        self.assertIn("`fastdl_carousel_download` ← `scripts/automate_fastdl.py`", md)
        self.assertIn("`instagram_url_canonicalize`", md)


class ToolFailRecoveryTests(unittest.TestCase):
    def test_listed_tool_fail_keeps_asking_until_schema_or_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            generate_v3_flow(
                codebase,
                "recover_v1",
                ["source_ready"],
                tools=["hash_bind"],
                milestone_specs=[
                    {
                        "id": "source_ready",
                        "tools": ["hash_bind"],
                        "intelligence": "completion",
                        "model_justification": "recover when the listed downloader fails",
                        "on_tool_fail": "need_model",
                        "max_model_attempts": 2,
                    }
                ],
            )
            harness = codebase / "flowsteps" / "flows" / "recover_v1"
            _write(
                harness / "milestones" / "source_ready" / "assemble.py",
                "def run(input_data, draft=None, **_):\n"
                "    raise RuntimeError('download failed')\n",
            )
            _write(
                harness / "milestones" / "source_ready" / "draft.schema.json",
                json.dumps({"type": "object", "additionalProperties": True}),
            )
            _write(
                harness / "schemas" / "source_ready_v1.json",
                json.dumps({"type": "object", "additionalProperties": True}),
            )
            request = Path(temp) / "request.json"
            request.write_text(json.dumps({"ok": True}), encoding="utf-8")
            run = Path(temp) / "run-rec"
            first = advance(harness, run, request_path=request)
            self.assertEqual(first["state"], "ACTION_REQUIRED")
            self.assertEqual(first["attempt"], 1)
            draft = Path(temp) / "draft.json"
            draft.write_text(json.dumps({"retry": 1}), encoding="utf-8")
            second = advance(harness, run, draft_path=draft)
            self.assertEqual(second["state"], "ACTION_REQUIRED")
            self.assertEqual(second["attempt"], 2)
            draft.write_text(json.dumps({"retry": 2}), encoding="utf-8")
            third = advance(harness, run, draft_path=draft)
            self.assertEqual(third["state"], "BLOCKED")

    def test_tool_fail_without_recovery_stays_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            generate_v3_flow(
                codebase,
                "rigid_v1",
                ["source_ready"],
                tools=["hash_bind"],
                milestone_specs=[
                    {
                        "id": "source_ready",
                        "tools": ["hash_bind"],
                        "on_tool_fail": "BLOCKED",
                    }
                ],
            )
            harness = codebase / "flowsteps" / "flows" / "rigid_v1"
            _write(
                harness / "milestones" / "source_ready" / "assemble.py",
                "def run(input_data, draft=None, **_):\n    raise RuntimeError('download failed')\n",
            )
            request = Path(temp) / "request.json"
            request.write_text(json.dumps({"ok": True}), encoding="utf-8")
            blocked = advance(harness, Path(temp) / "run-rigid", request_path=request)
            self.assertEqual(blocked["state"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
