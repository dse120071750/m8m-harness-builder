from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from gem_text import read_gem_section, render_flowstep_gem_sections, split_gem_sections
from generate_harness import generate_tool, generate_v3_flow
from run_flow import advance
from teaching_contracts import write_milestone_gems


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


class GemTextTests(unittest.TestCase):
    def test_split_and_read_flowstep_section(self) -> None:
        text = (
            "# Card is aligned\n\n"
            "## Rule of success\n\n"
            "Rule of success: the slot image is aligned.\n\n"
            "## FlowSteps\n\n"
            "Guide only.\n\n"
            "## `align_compare`\n\n"
            "Circle every visible disagreement. Label each circle.\n"
            "Ceiling-only when the headwall also differs is a miss.\n\n"
            "## `draw_red_circles`\n\n"
            "Python draws all of those circles on one markup per slot.\n\n"
            "## `align_edit`\n\n"
            "One prompt per slot that names every circled area.\n"
        )
        sections = split_gem_sections(text)
        self.assertIn("align_compare", sections)
        self.assertIn("headwall", read_gem_section(text, "align_compare"))
        self.assertIn("one markup", read_gem_section(text, "draw_red_circles"))
        self.assertIn("every circled", read_gem_section(text, "align_edit"))
        self.assertEqual(read_gem_section(text, "missing_step"), "")

    def test_render_sections_from_flowsteps(self) -> None:
        body = render_flowstep_gem_sections(
            [
                {"id": "align_compare", "tool": "align_compare"},
                {"id": "hash_bind", "tool": "hash_bind"},
            ]
        )
        self.assertIn("## `align_compare`", body)
        self.assertIn("## `hash_bind`", body)
        self.assertIn("not a canvas node", body)


class GemWriteTests(unittest.TestCase):
    def test_generate_writes_flowstep_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            result = generate_v3_flow(
                codebase,
                "align_v1",
                ["card_aligned"],
                tools=["hash_bind"],
                milestone_specs=[
                    {
                        "id": "card_aligned",
                        "asset": {"kind": "image"},
                        "intelligence": "image",
                        "flowsteps": [
                            {"id": "align_compare", "tool": "align_compare"},
                            {"id": "draw_red_circles", "tool": "draw_red_circles"},
                            {"id": "align_edit", "tool": "align_edit"},
                            {"id": "hash_bind", "tool": "hash_bind"},
                        ],
                    }
                ],
            )
            harness = Path(result["harness_dir"])
            gem = (harness / "references" / "card_aligned.md").read_text(encoding="utf-8")
            self.assertIn("## Rule of success", gem)
            self.assertIn("Rule of success:", gem)
            self.assertIn("## `align_compare`", gem)
            self.assertIn("## `draw_red_circles`", gem)
            self.assertIn("## `align_edit`", gem)
            self.assertIn("## `hash_bind`", gem)
            chart = (harness / "planning" / "m8m-flowchart.md").read_text(encoding="utf-8")
            self.assertIn("Gem section", chart)
            self.assertIn("references/card_aligned.md#align_compare", chart)

    def test_need_model_loads_gem_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            result = generate_v3_flow(
                codebase,
                "prompt_v1",
                ["card_aligned"],
                tools=["hash_bind"],
                milestone_specs=[
                    {
                        "id": "card_aligned",
                        "asset": {"kind": "json"},
                        "intelligence": "image",
                        "loop": "judge",
                        "flowsteps": [{"id": "align_compare", "tool": "align_compare"}],
                        "output_schema_object": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["proof"],
                            "properties": {"proof": {"type": "object"}},
                        },
                    }
                ],
            )
            harness = Path(result["harness_dir"])
            gem_path = harness / "references" / "card_aligned.md"
            gem_path.write_text(
                "# Card is aligned\n\n"
                "## Rule of success\n\n"
                "Rule of success: aligned image in the slot.\n\n"
                "## `align_compare`\n\n"
                "CIRCLE_EVERY_VISIBLE_MISS label headwall ceiling-tray door.\n",
                encoding="utf-8",
            )
            _write(
                harness / "milestones" / "card_aligned" / "input.schema.json",
                json.dumps({"type": "object", "additionalProperties": True}),
            )
            _write(
                harness / "milestones" / "card_aligned" / "draft.schema.json",
                json.dumps({"type": "object", "additionalProperties": True}),
            )
            _write(
                harness / "milestones" / "card_aligned" / "tests" / "test_assemble.py",
                "def test_ok():\n    assert True\n",
            )
            run_dir = Path(temp) / "run"
            request = run_dir / "request.json"
            request.parent.mkdir(parents=True, exist_ok=True)
            request.write_text("{}", encoding="utf-8")
            action = advance(harness, run_dir, request_path=request)
            self.assertEqual(action.get("state"), "ACTION_REQUIRED", action)
            self.assertEqual(action.get("step_id"), "card_aligned")
            req = json.loads((run_dir / action["model_request_path"]).read_text(encoding="utf-8"))
            self.assertIn("CIRCLE_EVERY_VISIBLE_MISS", req.get("instruction") or "")
            self.assertEqual(req.get("flowstep"), "align_compare")

    def test_write_gems_keeps_one_file_not_per_flowstep_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "flow"
            written = write_milestone_gems(
                dest,
                [
                    {
                        "id": "card_aligned",
                        "success": "Card is aligned",
                        "asset": {"kind": "image"},
                        "loop": "judge",
                        "worker": "card_aligned_judge",
                        "flowsteps": [
                            {"id": "align_compare", "tool": "align_compare"},
                            {"id": "align_edit", "tool": "align_edit"},
                        ],
                    }
                ],
                overwrite=True,
            )
            self.assertEqual(len(written), 1)
            gem = Path(written[0])
            self.assertEqual(gem.name, "card_aligned.md")
            self.assertFalse((dest / "references" / "align_compare.md").is_file())
            self.assertFalse((dest / "references" / "align_edit.md").is_file())


if __name__ == "__main__":
    unittest.main()
