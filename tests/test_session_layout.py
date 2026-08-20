from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from flowstep_runtime import read_json
from generate_harness import generate_tool, generate_v3_flow
from run_flow import advance
from session_layout import default_run_dir, ensure_session_tree, slot_rel


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


class SessionTreeTests(unittest.TestCase):
    def test_ensure_tree_and_default_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            dest = default_run_dir(codebase, "demo_v1")
            self.assertIn("flowsteps", dest.parts)
            self.assertIn("runs", dest.parts)
            self.assertEqual(dest.parent.name, "demo_v1")
            flow = {"flow_id": "demo_v1", "steps": [{"id": "source_ready", "loop": "none"}]}
            ensure_session_tree(dest, flow)
            self.assertTrue((dest / "milestones" / "source_ready" / "out" / "files").is_dir())
            self.assertTrue((dest / "manifest.json").is_file())
            self.assertEqual(slot_rel("source_ready", kind="image"), "milestones/source_ready/out/files/asset.png")

    def test_file_asset_is_copied_into_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            generate_v3_flow(codebase, "slot_v1", ["source_ready"], tools=["hash_bind"])
            harness = codebase / "flowsteps" / "flows" / "slot_v1"
            src = Path(temp) / "outside.txt"
            src.write_text("hello-slot\n", encoding="utf-8")
            _write(
                harness / "milestones" / "source_ready" / "assemble.py",
                "from pathlib import Path\n"
                f"SRC = r'''{src}'''\n"
                "def run(input_data, draft=None, **_):\n"
                "    return {'asset': {'path': SRC, 'sha256': 'x'}}\n",
            )
            _write(harness / "milestones" / "source_ready" / "tests" / "test_assemble.py", "def test_ok():\n    assert True\n")
            request = Path(temp) / "request.json"
            request.write_text("{}", encoding="utf-8")
            run_dir = Path(temp) / "run-slot"
            done = advance(harness, run_dir, request_path=request)
            self.assertEqual(done["state"], "COMPLETE")
            slot = run_dir / "milestones" / "source_ready" / "out" / "files" / "asset.bin"
            self.assertTrue(slot.is_file())
            self.assertEqual(slot.read_text(encoding="utf-8"), "hello-slot\n")
            env = read_json(run_dir / "artifacts" / "source_ready.source_ready_v1.json")
            self.assertTrue(str(env["data"]["asset"]["path"]).replace("\\", "/").endswith("milestones/source_ready/out/files/asset.bin") or Path(env["data"]["asset"]["path"]).resolve() == slot.resolve())
            manifest = read_json(run_dir / "manifest.json")
            self.assertTrue(any(item.get("milestone") == "source_ready" for item in manifest.get("slots") or []))
            self.assertTrue((run_dir / "milestones" / "source_ready" / "out" / "asset.json").is_file())

    def test_path_outside_run_without_file_does_not_leak_on_json(self) -> None:
        self.assertIn("items/003/", slot_rel("pages_bound", kind="image", item_index=3))
        self.assertIn("attempts/02/", slot_rel("card_aligned", kind="image", attempt=2))
