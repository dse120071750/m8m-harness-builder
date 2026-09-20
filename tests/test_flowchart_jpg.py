from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from flowchart_jpg import ARTICLE_DEMO, README_DEMO, write_flowchart_jpg, write_readme_chart
from flowstep_instruction import mark_step
from generate_harness import generate_tool, generate_v3_flow
from humanize_chart import humanize_flowstep, humanize_milestone, success_line, title_id
from humanize_chart_zh import title_id as title_id_zh
from m8m_flowchart import write_flowchart


def _deterministic_specs(flow_id: str) -> list[dict]:
    return [
        {
            "id": milestone_id,
            "success": f"{milestone_id} produces one structurally admitted result.",
            "output_contract": f"{milestone_id}_v1",
            "outputs": [
                {
                    "id": "result",
                    "name": f"{milestone_id} result",
                    "kind": "json",
                    "cardinality": "one",
                    "required": True,
                }
            ],
            "output_schema_object": {"type": "object"},
            "tools": ["hash_bind"],
            "flowsteps": [
                {"id": "hash_bind", "tool": "hash_bind@1.0.0"}
            ],
            "execution": {
                "candidate_executor": {
                    "ref": f"handler.{flow_id}.{milestone_id}@3.1.0"
                },
                "tool_bindings": [
                    {"tool": "hash_bind", "ref": "hash_bind@1.0.0"}
                ],
            },
        }
        for milestone_id in ("source_ready", "plan_frozen")
    ]


class HumanizeTests(unittest.TestCase):
    def test_milestone_and_flowstep_are_readable(self) -> None:
        self.assertEqual(title_id("source_ready"), "Source is ready")
        self.assertEqual(title_id("cards_rendered"), "Cards are rendered")
        self.assertEqual(title_id("plan_frozen"), "Plan is frozen")
        human = humanize_milestone({"id": "source_ready", "asset_kind": "file"})
        self.assertIn("file", human["caption"])
        self.assertEqual(human["title"], "Source is ready")
        fs = humanize_flowstep({"id": "fetch_record", "tool": "fetch_record"}, 1)
        self.assertIn("fetch_record", fs["caption"])
        self.assertIn("tool", fs["caption"])
        self.assertEqual(title_id_zh("source_ready"), "来源已就绪")
        self.assertEqual(title_id_zh("card_aligned"), "卡片已对齐")
        self.assertEqual(title_id_zh("pages_ledger_frozen"), "页账本已冻结")
        self.assertEqual(title_id_zh("intake_ready"), "入口已就绪")
        self.assertEqual(title_id("response_ready"), "Response is ready")
        self.assertEqual(title_id_zh("response_ready"), "回复已就绪")
        self.assertEqual(title_id_zh("wait_for_response"), "等待回复")
        self.assertIn("must choose a file", success_line({"id": "source_ready", "asset_kind": "file"}))
        self.assertEqual(
            success_line({"id": "source_ready", "asset_kind": "file", "success": "Source bytes are bound."}),
            "Source bytes are bound.",
        )
        self.assertIn("Retry until the semantic judge returns PASS", success_line({"id": "card_aligned", "asset_kind": "image", "loop": "judge"}))


class JpegWriteTests(unittest.TestCase):
    def test_cache_policy_is_canvas_metadata_not_a_node(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            items = [
                {
                    "id": "result_ready",
                    "success": "The result is accepted.",
                    "output_contract": "result_v1",
                    "outputs": [
                        {
                            "id": "result",
                            "name": "Result",
                            "kind": "json",
                            "cardinality": "one",
                            "required": True,
                        }
                    ],
                    "intelligence": "none",
                    "tools": [],
                    "cache": {"reuse": "candidate", "ttl_seconds": 3600, "side_effects": "none"},
                    "cache_status": "hit",
                }
            ]
            write_flowchart(root, items, title="cache", flow_id="cache_v1", source="test")
            text = (root / "planning" / "m8m-flowchart.md").read_text(encoding="utf-8")
            self.assertIn("cache:candidate", text)
            self.assertIn("TTL 3600s", text)
            self.assertIn("status `hit`", text)
            self.assertEqual(text.count('result_ready["'), 1)

    def test_generate_writes_jpg_next_to_md(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            result = generate_v3_flow(
                codebase,
                "chart_v1",
                ["source_ready", "plan_frozen"],
                tools=["hash_bind"],
                milestone_specs=_deterministic_specs("chart_v1"),
            )
            harness = Path(result["harness_dir"])
            md = harness / "planning" / "m8m-flowchart.md"
            jpg = harness / "planning" / "m8m-flowchart.jpg"
            self.assertTrue(md.is_file())
            self.assertTrue(jpg.is_file())
            text = md.read_text(encoding="utf-8")
            self.assertIn("m8m-flowchart.jpg", text)
            self.assertIn("Source is ready", text)
            self.assertIn("What it means", text)
            self.assertIn("Success", text)
            yaml_text = (harness / "flow.yaml").read_text(encoding="utf-8")
            self.assertIn("success:", yaml_text)
            self.assertIn("gem:", yaml_text)
            gem = harness / "references" / "source_ready.md"
            self.assertTrue(gem.is_file())
            gem_text = gem.read_text(encoding="utf-8")
            self.assertNotIn("Rule of success", gem_text)
            self.assertIn("machine completion", gem_text)
            self.assertNotIn('{"ok": True', (harness / "milestones" / "source_ready" / "assemble.py").read_text(encoding="utf-8"))
            self.assertIn("hash_bind", yaml_text)

    def test_quality_name_inference_cannot_emit_a_judge_or_runnable_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            result = generate_v3_flow(
                codebase,
                "pair_v1",
                ["card_aligned"],
                tools=["hash_bind"],
            )
            harness = Path(result["harness_dir"])
            self.assertEqual(result["status"], "BUILD_REQUIRED")
            self.assertFalse(result["runnable"])
            self.assertFalse((harness / "flow.yaml").exists())
            judge = codebase / "flowsteps" / "tools" / "card_aligned_judge" / "tool.py"
            self.assertFalse(judge.exists())

    def test_mark_step_rewrites_md_and_jpg(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            result = generate_v3_flow(
                codebase,
                "edit_v1",
                ["source_ready", "plan_frozen"],
                tools=["hash_bind"],
                milestone_specs=_deterministic_specs("edit_v1"),
            )
            harness = Path(result["harness_dir"])
            md = harness / "planning" / "m8m-flowchart.md"
            jpg = harness / "planning" / "m8m-flowchart.jpg"
            before_md = md.read_text(encoding="utf-8")
            before_jpg = jpg.read_bytes()
            self.assertIn("`PENDING`", before_md)
            self.assertNotIn("`DONE`", before_md)
            mark_step(harness, "source_ready", "DONE")
            after_md = md.read_text(encoding="utf-8")
            after_jpg = jpg.read_bytes()
            self.assertIn("`DONE`", after_md)
            self.assertIn("Source is ready", after_md)
            self.assertNotEqual(before_jpg, after_jpg)
            self.assertEqual(after_jpg[:2], b"\xff\xd8")

    def test_write_instruction_after_flowstep_change_updates_chart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "skill"
            items = [
                {
                    "id": "source_ready",
                    "asset": {"kind": "file"},
                    "tools": ["hash_bind"],
                    "flowsteps": [{"id": "hash_bind", "tool": "hash_bind"}],
                }
            ]
            write_flowchart(root, items, title="toy", flow_id="toy_v1", source="generate")
            md = root / "planning" / "m8m-flowchart.md"
            jpg = root / "planning" / "m8m-flowchart.jpg"
            before_md = md.read_text(encoding="utf-8")
            before_jpg = jpg.read_bytes()
            self.assertNotIn("fetch_record", before_md)
            items[0]["flowsteps"] = [
                {"id": "fetch_record", "tool": "fetch_record"},
                {"id": "hash_bind", "tool": "hash_bind"},
            ]
            items[0]["tools"] = ["fetch_record", "hash_bind"]
            write_flowchart(root, items, title="toy", flow_id="toy_v1", source="edit")
            after_md = md.read_text(encoding="utf-8")
            after_jpg = jpg.read_bytes()
            self.assertIn("fetch_record", after_md)
            self.assertIn("Fetch record, using tool `fetch_record`", after_md)
            self.assertNotEqual(before_jpg, after_jpg)

    def test_direct_render(self) -> None:
        self.assertEqual(README_DEMO[0]["worker"], "source_ready_judge")
        self.assertEqual(README_DEMO[0]["gem"], "references/source_ready.md")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "demo.jpg"
            write_flowchart_jpg(
                path,
                README_DEMO,
                title="demo",
                focus_id="source_ready",
            )
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes()[:2], b"\xff\xd8")
            self.assertGreater(path.stat().st_size, 1000)

    def test_readme_chart_helper_writes_jpeg(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "docs").mkdir()
            path = write_readme_chart(root)
            self.assertEqual(path, root / "docs" / "m8m-chart.jpg")
            self.assertEqual(path.read_bytes()[:2], b"\xff\xd8")

    def test_article_demo_renders(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "article.jpg"
            write_flowchart_jpg(
                path,
                ARTICLE_DEMO,
                title="article-infographic-maker",
                focus_id="cards_rendered",
            )
            self.assertEqual(path.read_bytes()[:2], b"\xff\xd8")
