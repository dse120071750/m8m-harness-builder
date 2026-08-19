from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support import EXAMPLE

from flowstep_runtime import FlowError, load_flow, step_class_hint
from flowstep_tools import run_library_tool, validate_library_tool
from generate_harness import generate_tool, generate_v3_flow
from validate_harness import validate_harness


REPO = Path("D:/nisan-n8n")


class ToolLibraryTests(unittest.TestCase):
    def test_library_ignores_shadowed_runtime(self) -> None:
        import importlib
        import sys
        import types

        fake = types.ModuleType("flowstep_runtime")
        previous = sys.modules.get("flowstep_runtime")
        sys.modules["flowstep_runtime"] = fake
        sys.modules.pop("flowstep_builder_runtime", None)
        sys.modules.pop("flowstep_tools", None)
        try:
            tools = importlib.import_module("flowstep_tools")
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "x.txt"
                path.write_text("x", encoding="utf-8")
                result = tools.run_library_tool(REPO, "hash_bind", {"path": str(path)})
            self.assertEqual(len(result["sha256"]), 64)
        finally:
            if previous is None:
                sys.modules.pop("flowstep_runtime", None)
            else:
                sys.modules["flowstep_runtime"] = previous

    def test_seed_hash_bind(self) -> None:
        errors = validate_library_tool(REPO, "hash_bind")
        self.assertEqual(errors, [])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "x.txt"
            path.write_text("x", encoding="utf-8")
            result = run_library_tool(REPO, "hash_bind", {"path": str(path)})
            self.assertEqual(len(result["sha256"]), 64)

    def test_seed_crop_ratio(self) -> None:
        errors = validate_library_tool(REPO, "crop_4x5")
        self.assertEqual(errors, [])

    def test_two_flows_share_hash_bind(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind", overwrite=True)
            src = REPO / "flowsteps" / "tools" / "hash_bind" / "tool.py"
            dest = codebase / "flowsteps" / "tools" / "hash_bind" / "tool.py"
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            (codebase / "flowsteps" / "tools" / "hash_bind" / "output.schema.json").write_text(
                (REPO / "flowsteps" / "tools" / "hash_bind" / "output.schema.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (codebase / "flowsteps" / "tools" / "hash_bind" / "input.schema.json").write_text(
                (REPO / "flowsteps" / "tools" / "hash_bind" / "input.schema.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            test_src = REPO / "flowsteps" / "tools" / "hash_bind" / "tests" / "test_tool.py"
            test_dest = codebase / "flowsteps" / "tools" / "hash_bind" / "tests" / "test_tool.py"
            test_dest.parent.mkdir(parents=True, exist_ok=True)
            test_dest.write_text(test_src.read_text(encoding="utf-8"), encoding="utf-8")
            first = generate_v3_flow(codebase, "flow_a_v1", ["source_ready"], tools=["hash_bind"])
            second = generate_v3_flow(codebase, "flow_b_v1", ["assets_bound"], tools=["hash_bind"])
            self.assertTrue(Path(first["harness_dir"]).is_dir())
            self.assertTrue(Path(second["harness_dir"]).is_dir())
            self.assertTrue((codebase / "flowsteps" / "tools" / "hash_bind" / "tool.py").is_file())
            self.assertEqual(len(list((codebase / "flowsteps" / "tools").iterdir())), 1)


class MilestoneTests(unittest.TestCase):
    def test_outcome_names_are_not_tools(self) -> None:
        self.assertIsNone(step_class_hint("cards_rendered"))
        self.assertIsNone(step_class_hint("release_packaged"))
        self.assertIsNone(step_class_hint("source_ready"))
        self.assertEqual(step_class_hint("crop_4x5"), "tool")
        self.assertEqual(step_class_hint("render_html_shell"), "tool")
        self.assertEqual(step_class_hint("materialize_package"), "tool")

    def test_rejects_crop_milestone(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            with self.assertRaises(FlowError) as ctx:
                generate_v3_flow(codebase, "bad_v1", ["crop_4x5"], tools=["hash_bind"])
            self.assertIn("use --tool", str(ctx.exception))

    def test_v3_instruction_lists_toolbox(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            result = generate_v3_flow(
                codebase,
                "demo_v1",
                ["source_ready", "assets_bound"],
                tools=["hash_bind"],
                intelligence=["assets_bound"],
            )
            text = Path(result["instruction_path"]).read_text(encoding="utf-8")
            self.assertIn("flowchart LR", text)
            self.assertIn("`hash_bind`", text)
            self.assertIn("### `source_ready`", text)
            flow = load_flow(Path(result["harness_dir"]))
            self.assertTrue(flow.get("_v3"))
            self.assertEqual(flow["steps"][1]["intelligence"], "completion")

    def test_article_v3_flow_validates(self) -> None:
        article = REPO / "flowsteps" / "flows" / "article_infographic_zh_hant_v2"
        result = validate_harness(skill_dir=article)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(
            result["steps"],
            [
                "source_ready",
                "plan_frozen",
                "prompts_frozen",
                "assets_bound",
                "cards_rendered",
                "release_packaged",
            ],
        )

    def test_v2_fixture_still_loads(self) -> None:
        flow = load_flow(EXAMPLE)
        self.assertFalse(flow.get("_v3"))
        self.assertEqual(flow["steps"][0]["id"], "ingest")


if __name__ == "__main__":
    unittest.main()
