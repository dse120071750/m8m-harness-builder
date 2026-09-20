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
                        "success": "The declared alignment guidance and image output are available.",
                        "output_contract": "card_aligned_v1",
                        "outputs": [
                            {
                                "id": "result",
                                "name": "Aligned image",
                                "kind": "image",
                                "cardinality": "one",
                                "required": True,
                            }
                        ],
                        "output_schema_object": {
                            "type": "object",
                            "required": ["path"],
                            "properties": {"path": {"type": "string"}},
                        },
                        "asset": {"kind": "image"},
                        "intelligence": "none",
                        "tools": [
                            "align_compare",
                            "draw_red_circles",
                            "align_edit",
                            "hash_bind",
                        ],
                        "flowsteps": [
                            {
                                "id": "align_compare",
                                "tool": "align_compare@1.0.0",
                            },
                            {
                                "id": "draw_red_circles",
                                "tool": "draw_red_circles@1.0.0",
                            },
                            {
                                "id": "align_edit",
                                "tool": "align_edit@1.0.0",
                            },
                            {
                                "id": "hash_bind",
                                "tool": "hash_bind@1.0.0",
                            },
                        ],
                        "execution": {
                            "candidate_executor": {
                                "ref": "handler.align_v1.card_aligned@3.1.0"
                            },
                            "tool_bindings": [
                                {
                                    "tool": tool,
                                    "ref": f"{tool}@1.0.0",
                                }
                                for tool in (
                                    "align_compare",
                                    "draw_red_circles",
                                    "align_edit",
                                    "hash_bind",
                                )
                            ],
                        },
                    }
                ],
            )
            harness = Path(result["harness_dir"])
            gem = (harness / "references" / "card_aligned.md").read_text(encoding="utf-8")
            self.assertNotIn("Rule of success", gem)
            self.assertIn("machine completion", gem)
            self.assertIn("## Tool versus intelligence", gem)
            self.assertIn("deterministic tool work", gem)
            self.assertIn("not shell-heavy", gem)
            self.assertNotIn("__CLASSIFICATION__", gem)
            self.assertIn("## `align_compare`", gem)
            self.assertIn("## `draw_red_circles`", gem)
            self.assertIn("## `align_edit`", gem)
            self.assertIn("## `hash_bind`", gem)
            chart = (harness / "planning" / "m8m-flowchart.md").read_text(encoding="utf-8")
            self.assertIn("Milestone master prompt", chart)
            self.assertIn("references/card_aligned.md", chart)
            self.assertNotIn("references/card_aligned.md#", chart)

            # Supplying the authored master prompt keeps it intact without adding
            # it as a second public workflow field or rewriting its sections.
            from test_v3_milestones import _closed_milestone_spec
            prompt = "MASTER PROMPT — SOURCE\n\nUse the supplied file and return its exact path.\n"
            spec = _closed_milestone_spec("master_prompt_v1", "source_ready")
            spec["master_prompt"] = prompt
            authored = generate_v3_flow(
                codebase, "master_prompt_v1", ["source_ready"],
                tools=["hash_bind"], milestone_specs=[spec],
            )
            source_root = Path(authored["harness_dir"])
            self.assertEqual((source_root / "references/source_ready.md").read_text(encoding="utf-8"), prompt)
            self.assertNotIn("master_prompt:", (source_root / "flow.yaml").read_text(encoding="utf-8"))

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
                        "success": "The model supplies one bounded comparison proof candidate.",
                        "output_contract": "card_aligned_v1",
                        "outputs": [
                            {
                                "id": "result",
                                "name": "Comparison proof",
                                "kind": "json",
                                "cardinality": "one",
                                "required": True,
                            }
                        ],
                        "asset": {"kind": "json"},
                        "intelligence": "image",
                        "model_justification": "Visual comparison requires image reasoning.",
                        "loop": "none",
                        "tools": ["align_compare"],
                        "flowsteps": [
                            {
                                "id": "align_compare",
                                "tool": "align_compare@1.0.0",
                            }
                        ],
                        "execution": {
                            "candidate_executor": {
                                "ref": "handler.prompt_v1.card_aligned@3.1.0",
                                "profile": {
                                    "ref": "agent_profile.prompt_v1.card_aligned.candidate.v1",
                                    "model_configuration": {
                                        "model": "codex",
                                        "reasoning": "medium",
                                    },
                                    "token_budget": {
                                        "max_input_tokens": 4096,
                                        "max_output_tokens": 1024,
                                    },
                                    "timeout_seconds": 120,
                                    "tools": ["align_compare"],
                                    "capabilities": [],
                                },
                            },
                            "tool_bindings": [
                                {
                                    "tool": "align_compare",
                                    "ref": "align_compare@1.0.0",
                                }
                            ],
                        },
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
