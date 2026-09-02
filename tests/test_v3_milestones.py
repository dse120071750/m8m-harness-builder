from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from support import EXAMPLE, optional_product_repo

from flowstep_runtime import FlowError, is_passthrough_schema, load_flow, step_class_hint
from flowstep_tools import run_library_tool, validate_library_tool
from generate_harness import generate_tool, generate_v3_flow
from run_flow import advance
from validate_harness import validate_harness


def _closed_milestone_spec(
    flow_id: str,
    milestone_id: str,
    *,
    tool_ref: str | None = "hash_bind@1.0.0",
    cache: dict | None = None,
) -> dict:
    flowsteps = []
    tool_bindings = []
    if tool_ref:
        tool_id = tool_ref.rsplit("@", 1)[0]
        flowsteps = [{"id": tool_id, "tool": tool_ref}]
        tool_bindings = [{"tool": tool_id, "ref": tool_ref}]
    spec = {
        "id": milestone_id,
        "success": f"The {milestone_id} result satisfies its declared output.",
        "output_contract": f"m8m.{milestone_id}.v1",
        "output_schema_object": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{milestone_id}.payload.schema.json",
            "type": "object",
            "additionalProperties": False,
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "sha256": {"type": "string", "minLength": 1},
            },
        },
        "outputs": [
            {
                "id": "result",
                "name": "Result asset",
                "kind": "file",
                "cardinality": "one",
                "required": True,
            }
        ],
        "flowsteps": flowsteps,
        "tools": [item["id"] for item in flowsteps],
        "execution": {
            "candidate_executor": {
                "ref": f"handler.{flow_id}.{milestone_id}@3.1.0"
            },
            "tool_bindings": tool_bindings,
        },
        "loop": "none",
    }
    if cache is not None:
        spec["cache"] = dict(cache)
    return spec


class ToolLibraryTests(unittest.TestCase):
    def test_library_ignores_shadowed_runtime(self) -> None:
        import importlib
        import sys
        import types

        fake = types.ModuleType("flowstep_runtime")
        previous = sys.modules.get("flowstep_runtime")
        sys.modules["flowstep_runtime"] = fake
        sys.modules.pop("flowstep_local_runtime", None)
        sys.modules.pop("flowstep_tools", None)
        try:
            tools = importlib.import_module("flowstep_tools")
            with tempfile.TemporaryDirectory() as temp:
                codebase = Path(temp) / "repo"
                generate_tool(codebase, "hash_bind")
                path = Path(temp) / "x.txt"
                path.write_text("x", encoding="utf-8")
                result = tools.run_library_tool(codebase, "hash_bind", {"path": str(path)})
            self.assertEqual(len(result["sha256"]), 64)
        finally:
            if previous is None:
                sys.modules.pop("flowstep_runtime", None)
            else:
                sys.modules["flowstep_runtime"] = previous

    def test_seed_hash_bind(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            errors = validate_library_tool(codebase, "hash_bind")
            self.assertEqual(errors, [])
            path = Path(temp) / "x.txt"
            path.write_text("x", encoding="utf-8")
            result = run_library_tool(codebase, "hash_bind", {"path": str(path)})
            self.assertEqual(len(result["sha256"]), 64)

    def test_two_flows_share_hash_bind(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind", overwrite=True)
            first = generate_v3_flow(
                codebase,
                "flow_a_v1",
                ["source_ready"],
                tools=["hash_bind"],
                milestone_specs=[
                    _closed_milestone_spec("flow_a_v1", "source_ready")
                ],
            )
            second = generate_v3_flow(
                codebase,
                "flow_b_v1",
                ["assets_bound"],
                tools=["hash_bind"],
                milestone_specs=[
                    _closed_milestone_spec("flow_b_v1", "assets_bound")
                ],
            )
            self.assertEqual(first["status"], "BUILD_REQUIRED")
            self.assertEqual(second["status"], "BUILD_REQUIRED")
            self.assertTrue(Path(first["harness_dir"]).is_dir())
            self.assertTrue(Path(second["harness_dir"]).is_dir())
            self.assertTrue(
                (Path(first["harness_dir"]) / "BUILD_REQUIRED_RUNTIME").is_file()
            )
            self.assertTrue(
                (Path(second["harness_dir"]) / "BUILD_REQUIRED_RUNTIME").is_file()
            )
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

    def test_crop_named_checkpoint_still_draws(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            result = generate_v3_flow(
                codebase,
                "bad_v1",
                ["crop_4x5"],
                tools=["hash_bind"],
                milestone_specs=[_closed_milestone_spec("bad_v1", "crop_4x5")],
            )
            self.assertEqual(result["status"], "BUILD_REQUIRED")
            self.assertEqual(result["build_required_handlers"], ["crop_4x5"])
            self.assertTrue(any("crop_4x5" in note for note in result.get("notes") or []))
            self.assertTrue(Path(result["flowchart_path"]).is_file())
            self.assertTrue(
                (Path(result["harness_dir"]) / "BUILD_REQUIRED_RUNTIME").is_file()
            )

    def test_every_milestone_has_closed_asset_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            result = generate_v3_flow(
                codebase,
                "proof_v1",
                ["source_ready", "plan_frozen"],
                tools=["hash_bind"],
                milestone_specs=[
                    _closed_milestone_spec("proof_v1", "source_ready"),
                    _closed_milestone_spec("proof_v1", "plan_frozen"),
                ],
            )
            harness = Path(result["harness_dir"])
            flow_text = (harness / "flow.yaml").read_text(encoding="utf-8")
            for mid in ("source_ready", "plan_frozen"):
                schema = json.loads((harness / "schemas" / f"{mid}_v1.json").read_text(encoding="utf-8"))
                self.assertFalse(is_passthrough_schema(schema), mid)
                self.assertEqual(schema.get("additionalProperties"), False)
                self.assertTrue(schema.get("required"), mid)
                self.assertIn("outputs:", flow_text)
                self.assertIn("cardinality: one", flow_text)
            chart = Path(result["flowchart_path"]).read_text(encoding="utf-8")
            self.assertIn("out:result", chart)
            loaded = load_flow(harness)
            self.assertEqual(loaded["steps"][0]["asset"]["kind"], "file")
            self.assertTrue(loaded["steps"][0].get("flowsteps"))
            self.assertEqual(
                loaded["steps"][0]["flowsteps"][0]["tool"],
                "hash_bind@1.0.0",
            )
            self.assertIn("## FlowSteps (guide)", chart)

    def test_empty_tools_still_draws(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            result = generate_v3_flow(
                codebase,
                "empty_v1",
                ["source_ready"],
                tools=[],
                milestone_specs=[
                    _closed_milestone_spec(
                        "empty_v1", "source_ready", tool_ref=None
                    )
                ],
            )
            self.assertEqual(result["status"], "BUILD_REQUIRED")
            harness = Path(result["harness_dir"])
            loaded = load_flow(harness)
            self.assertEqual(loaded["steps"][0]["tools"], [])
            self.assertEqual(loaded["steps"][0]["flowsteps"], [])
            schema = json.loads((harness / "schemas" / "source_ready_v1.json").read_text(encoding="utf-8"))
            self.assertFalse(is_passthrough_schema(schema))

    def test_generator_preserves_only_explicit_cache_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            policy = {"reuse": "candidate", "ttl_seconds": 900, "side_effects": "none"}
            result = generate_v3_flow(
                codebase,
                "cache_policy_v1",
                ["source_ready", "result_ready"],
                tools=[],
                milestone_specs=[
                    _closed_milestone_spec(
                        "cache_policy_v1",
                        "source_ready",
                        tool_ref=None,
                        cache=policy,
                    ),
                    _closed_milestone_spec(
                        "cache_policy_v1", "result_ready", tool_ref=None
                    ),
                ],
            )
            harness = Path(result["harness_dir"])
            loaded = load_flow(harness)
            self.assertEqual(loaded["steps"][0]["cache"], policy)
            self.assertIsNone(loaded["steps"][1]["cache"])
            flow_text = (harness / "flow.yaml").read_text(encoding="utf-8")
            self.assertEqual(flow_text.count("reuse: candidate"), 1)
            chart = Path(result["flowchart_path"]).read_text(encoding="utf-8")
            self.assertIn("cache:candidate", chart)
            self.assertIn("TTL 900s", chart)

    def test_default_tool_fail_recovers_like_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            result = generate_v3_flow(
                codebase,
                "agent_v1",
                ["source_ready"],
                tools=["hash_bind"],
                milestone_specs=[
                    _closed_milestone_spec("agent_v1", "source_ready")
                ],
            )
            harness = Path(result["harness_dir"])
            assemble = harness / "milestones" / "source_ready" / "assemble.py"
            assemble.write_text(
                "def run(input_data, draft=None, **_):\n    raise RuntimeError('download failed')\n",
                encoding="utf-8",
            )
            request = Path(temp) / "request.json"
            request.write_text(json.dumps({"ok": True}), encoding="utf-8")
            first = advance(harness, Path(temp) / "run-agent", request_path=request)
            self.assertEqual(first["state"], "ACTION_REQUIRED")

    def test_intelligence_tries_tool_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            result = generate_v3_flow(
                codebase,
                "intel_v1",
                ["plan_frozen"],
                tools=["hash_bind"],
                intelligence=["plan_frozen"],
                milestone_specs=[
                    _closed_milestone_spec("intel_v1", "plan_frozen")
                ],
            )
            assemble = Path(result["harness_dir"]) / "milestones" / "plan_frozen" / "assemble.py"
            source = assemble.read_text(encoding="utf-8")
            self.assertIn("FLOWSTEPS", source)
            self.assertNotIn("Write a draft that the toolbox can admit", source)
            self.assertIn("Preferred tool", source)

    def test_missing_milestone_asset_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            result = generate_v3_flow(
                codebase,
                "block_v1",
                ["source_ready"],
                tools=["hash_bind"],
                milestone_specs=[
                    _closed_milestone_spec("block_v1", "source_ready")
                ],
            )
            harness = Path(result["harness_dir"])
            assemble = harness / "milestones" / "source_ready" / "assemble.py"
            assemble.write_text(
                "def run(input_data, draft=None, **_):\n    return {}\n",
                encoding="utf-8",
            )
            request = Path(temp) / "request.json"
            request.write_text(json.dumps({"ok": True}), encoding="utf-8")
            blocked = advance(harness, Path(temp) / "run-miss", request_path=request)
            self.assertEqual(blocked["state"], "BLOCKED")
            self.assertTrue(
                any(
                    "candidate must return an outputs object" in item
                    for item in blocked.get("blockers") or []
                )
            )

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
                milestone_specs=[
                    _closed_milestone_spec("demo_v1", "source_ready"),
                    _closed_milestone_spec("demo_v1", "assets_bound"),
                ],
            )
            text = Path(result["instruction_path"]).read_text(encoding="utf-8")
            self.assertIn("planning/m8m-flowchart.md", text)
            self.assertIn("`hash_bind`", text)
            chart = Path(result["flowchart_path"]).read_text(encoding="utf-8")
            self.assertIn("```text", chart)
            self.assertNotIn("```mermaid", chart)
            self.assertIn("flowchart TD", chart)
            self.assertIn("source_ready", chart)
            self.assertIn("### `source_ready`", text)
            flow = load_flow(Path(result["harness_dir"]))
            self.assertTrue(flow.get("_v4"))
            self.assertEqual(flow["steps"][1]["intelligence"], "completion")

    def test_article_v3_flow_validates(self) -> None:
        repo = optional_product_repo()
        if repo is None:
            self.skipTest("set M8M_PRODUCT_REPO to validate a live product flow")
        article = repo / "flowsteps" / "flows" / "article_infographic_zh_hant_v2"
        if not article.is_dir():
            self.skipTest("sample article flow not in M8M_PRODUCT_REPO")
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

    def test_v4_fixture_loads(self) -> None:
        flow = load_flow(EXAMPLE)
        self.assertTrue(flow.get("_v4"))
        self.assertEqual(flow["steps"][0]["id"], "ingest")


if __name__ == "__main__":
    unittest.main()
