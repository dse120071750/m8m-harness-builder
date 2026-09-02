from __future__ import annotations

import json
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import support  # noqa: F401
import session_layout

from flowstep_runtime import FlowError, read_json
from generate_harness import generate_tool, generate_v3_flow
from run_flow import advance
from session_layout import (
    assert_image_provider_on_execution_volume,
    default_harness_root,
    default_run_dir,
    ensure_session_tree,
    load_run_roster,
    load_chosen_output,
    materialize_chosen_output,
    materialize_request_file_refs,
    recover_incomplete_chosen_output,
    slot_rel,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


class SessionTreeTests(unittest.TestCase):
    def test_admit_candidate_rejects_flat_single_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / "skill"
            run_dir = root / "run"
            _write(
                skill / "schemas" / "legacy.json",
                json.dumps({"type": "object", "required": ["ready"], "properties": {"ready": {"type": "boolean"}}}),
            )
            step = {
                "id": "request_ready",
                "output_schema": "schemas/legacy.json",
                "outputs": [
                    {"id": "result", "name": "Request Ready", "kind": "json", "cardinality": "one", "required": True}
                ],
            }
            with self.assertRaisesRegex(FlowError, "only outputs"):
                session_layout.admit_candidate(run_dir, skill, step, {"ready": True})

    def test_admit_candidate_does_not_guess_legacy_multi_port_routing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / "skill"
            _write(skill / "schemas" / "candidate.json", json.dumps({"type": "object"}))
            step = {
                "id": "render_ready",
                "output_schema": "schemas/candidate.json",
                "outputs": [
                    {"id": "preview", "name": "Preview", "kind": "image", "cardinality": "one", "required": True},
                    {"id": "notes", "name": "Notes", "kind": "json", "cardinality": "one", "required": False},
                ],
            }
            with self.assertRaisesRegex(FlowError, "only outputs"):
                session_layout.admit_candidate(root / "run", skill, step, {"ready": True})

    def test_admit_candidate_rejects_normal_candidate_control_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / "skill"
            _write(
                skill / "schemas" / "candidate.json",
                json.dumps(
                    {
                        "type": "object",
                        "required": ["outputs"],
                        "properties": {"outputs": {"type": "object"}},
                    }
                ),
            )
            step = {
                "id": "result_ready",
                "output_schema": "schemas/candidate.json",
                "outputs": [
                    {"id": "result", "name": "Result", "kind": "json", "cardinality": "one", "required": True}
                ],
            }
            for field, value in (
                ("receipt", {"ok": True}),
                ("ok", True),
                ("branch", "direct"),
                ("cycle", "pass"),
            ):
                with self.subTest(field=field):
                    with self.assertRaisesRegex(FlowError, "only outputs"):
                        session_layout.admit_candidate(
                            root / f"run-{field}",
                            skill,
                            step,
                            {
                                "outputs": {"result": {"ready": True}},
                                field: value,
                            },
                        )

    def test_environment_override_selects_the_harness_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            configured = Path(temp) / "configured-runtime"
            with patch.dict(os.environ, {"M8M_HARNESS_ROOT": str(configured)}):
                self.assertEqual(default_harness_root(), configured.resolve())

    def test_ensure_tree_and_default_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            dest = default_run_dir(codebase, "demo_v1")
            self.assertEqual(dest.parent, codebase / "runs")
            self.assertNotIn("flowsteps", dest.parts)
            self.assertIn("runs", dest.parts)
            self.assertTrue(dest.name.startswith("demo_v1_"))
            flow = {"flow_id": "demo_v1", "steps": [{"id": "source_ready", "loop": "none"}]}
            ensure_session_tree(dest, flow)
            self.assertTrue((dest / "milestones" / "source_ready" / "out" / "members").is_dir())
            self.assertTrue((dest / "manifest.json").is_file())
            self.assertTrue((dest / "roster.json").is_file())
            roster = load_run_roster(dest)
            self.assertEqual(roster["schema"], "m8m_run_roster_v1")
            self.assertEqual(roster["status"], "running")
            self.assertEqual([row["status"] for row in roster["rows"]], ["unfinished"])
            self.assertEqual(slot_rel("source_ready", kind="image"), "milestones/source_ready/work/candidate/files/asset.png")

    def test_file_asset_is_copied_into_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            generate_v3_flow(
                codebase,
                "slot_v1",
                ["source_ready"],
                tools=["hash_bind"],
                milestone_specs=[
                    {
                        "id": "source_ready",
                        "success": "The exact run-local file is available.",
                        "output_contract": "source_ready_v1",
                        "output_schema_object": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["asset"],
                            "properties": {
                                "asset": {
                                    "type": "object",
                                    "required": ["path"],
                                    "properties": {"path": {"type": "string"}},
                                }
                            },
                        },
                        "outputs": [
                            {
                                "id": "result",
                                "name": "Run-local file",
                                "kind": "file",
                                "cardinality": "one",
                                "required": True,
                            }
                        ],
                        "intelligence": "none",
                        "tools": ["hash_bind"],
                        "flowsteps": [
                            {
                                "id": "hash_bind",
                                "tool": "hash_bind@1.0.0",
                            }
                        ],
                        "execution": {
                            "candidate_executor": {
                                "ref": "handler.slot_v1.source_ready@3.1.0"
                            },
                            "tool_bindings": [
                                {"tool": "hash_bind", "ref": "hash_bind@1.0.0"}
                            ],
                        },
                    }
                ],
            )
            harness = codebase / "flowsteps" / "flows" / "slot_v1"
            src = Path(temp) / "outside.txt"
            src.write_text("hello-slot\n", encoding="utf-8")
            _write(
                harness / "milestones" / "source_ready" / "assemble.py",
                "import shutil\n"
                "from pathlib import Path\n"
                f"SRC = r'''{src}'''\n"
                "def run(input_data, draft=None, run_dir=None, **_):\n"
                "    local = Path(run_dir) / 'work' / 'generated' / 'outside.txt'\n"
                "    local.parent.mkdir(parents=True, exist_ok=True)\n"
                "    shutil.copy2(SRC, local)\n"
                "    return {'outputs': {'result': {'asset': {'path': str(local)}}}}\n",
            )
            _write(harness / "milestones" / "source_ready" / "tests" / "test_assemble.py", "def test_ok():\n    assert True\n")
            request = Path(temp) / "request.json"
            request.write_text("{}", encoding="utf-8")
            run_dir = Path(temp) / "run-slot"
            done = advance(harness, run_dir, request_path=request)
            self.assertEqual(done["state"], "COMPLETE", done)
            slot = run_dir / "milestones" / "source_ready" / "out" / "members" / "result" / "asset.txt"
            self.assertTrue(slot.is_file())
            self.assertEqual(slot.read_text(encoding="utf-8"), "hello-slot\n")
            chosen = read_json(run_dir / "milestones" / "source_ready" / "out" / "chosen-output.json")
            self.assertEqual(chosen["members"][0]["path"], "milestones/source_ready/out/members/result/asset.txt")
            manifest = read_json(run_dir / "manifest.json")
            self.assertTrue(any(item.get("milestone") == "source_ready" for item in manifest.get("chosen_outputs") or []))
            self.assertTrue((run_dir / "milestones" / "source_ready" / "out" / "judge-receipt.json").is_file())

    def test_admit_candidate_rejects_existing_file_outside_run_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / "skill"
            run = root / "run"
            source = root / "outside.txt"
            source.write_text("outside", encoding="utf-8")
            _write(
                skill / "schemas" / "candidate.json",
                json.dumps(
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["outputs"],
                        "properties": {"outputs": {"type": "object"}},
                    }
                ),
            )
            step = {
                "id": "file_ready",
                "output_schema": "schemas/candidate.json",
                "outputs": [
                    {
                        "id": "file",
                        "name": "File",
                        "kind": "file",
                        "cardinality": "one",
                        "required": True,
                    }
                ],
            }
            with self.assertRaisesRegex(FlowError, "active run boundary"):
                session_layout.admit_candidate(
                    run,
                    skill,
                    step,
                    {"outputs": {"file": {"path": str(source)}}},
                )

    def test_path_outside_run_without_file_does_not_leak_on_json(self) -> None:
        self.assertIn("items/003/", slot_rel("pages_bound", kind="image", item_index=3))
        self.assertIn("attempts/02/", slot_rel("card_aligned", kind="image", attempt=2))

    def test_file_refs_are_hash_verified_deduplicated_and_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source" / "photo.jpg"
            source.parent.mkdir()
            source.write_bytes(b"source-image-bytes")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            run = root / "runtime" / "runs" / "asset-run"
            request = {
                "first": {"path": str(source), "sha256": digest, "mime": "image/jpeg"},
                "again": {"path": str(source), "sha256": digest, "mime": "image/jpeg"},
            }

            rewritten = materialize_request_file_refs(run, request, source_base=source.parent)
            manifest = read_json(run / "source-assets-manifest.json")

            self.assertEqual(len(manifest["assets"]), 1)
            self.assertEqual(rewritten["first"]["path"], rewritten["again"]["path"])
            materialized = Path(rewritten["first"]["path"])
            self.assertTrue(materialized.is_file())
            self.assertEqual(materialized.read_bytes(), source.read_bytes())
            self.assertIn((run / "inputs" / "source-assets").resolve(), materialized.resolve().parents)

    def test_image_provider_in_run_is_committed_once_to_chosen_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / "runtime" / "runs" / "image-run"
            provider = run / "work" / "provider" / "image.png"
            provider.parent.mkdir(parents=True)
            provider.write_bytes(b"provider-image")
            step = {
                "id": "image_ready",
                "success": "The staged image is present and readable.",
                "intelligence": "image",
                "loop": "judge",
                "max_attempts": 2,
                "output_contract": "image_ready_v1",
                "outputs": [
                    {"id": "image", "name": "Image", "kind": "image", "cardinality": "one", "required": True}
                ],
            }
            flow = {"flow_id": "image_flow_v1"}

            assert_image_provider_on_execution_volume(run, provider)
            manifest = materialize_chosen_output(
                run,
                flow,
                step,
                {"outputs": {"image": {"path": str(provider)}}},
                attempt=2,
            )

            staged = run / "milestones" / "image_ready" / "work" / "attempts" / "02" / "provider-outputs" / "image" / "asset.png"
            self.assertFalse(staged.exists())
            chosen = run / "milestones" / "image_ready" / "out" / "members" / "image" / "asset.png"
            self.assertTrue(chosen.is_file())
            self.assertEqual(chosen.read_bytes(), provider.read_bytes())
            self.assertEqual(manifest["status"], "chosen")

    def test_manifest_last_commit_recovers_without_rerunning_candidate_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            step = {
                "id": "result_ready",
                "success": "The exact JSON result is present.",
                "output_contract": "result_ready_v1",
                "outputs": [
                    {
                        "id": "result",
                        "name": "Result",
                        "kind": "json",
                        "cardinality": "one",
                        "required": True,
                    }
                ],
                "loop": "judge",
                "max_attempts": 3,
            }
            flow = {"flow_id": "recovery_v1"}
            target = run / "milestones" / "result_ready" / "out" / "chosen-output.json"
            real_write = session_layout.write_json

            def interrupt_manifest(path: Path, value: object, *, overwrite: bool = True) -> None:
                if Path(path) == target:
                    raise FlowError("simulated process interruption before commit marker")
                real_write(Path(path), value, overwrite=overwrite)

            with patch.object(session_layout, "write_json", side_effect=interrupt_manifest):
                with self.assertRaisesRegex(FlowError, "simulated process interruption"):
                    materialize_chosen_output(
                        run,
                        flow,
                        step,
                        {"outputs": {"result": {"value": 7}}},
                        attempt=3,
                    )

            self.assertFalse(target.exists())
            self.assertTrue((target.parent / "members" / "result" / "asset.json").is_file())
            recovered = recover_incomplete_chosen_output(run, flow, step)
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered["status"], "chosen")
            self.assertEqual(
                load_chosen_output(run, "result_ready", step=step),
                recovered,
            )
            receipt = read_json(target.parent / "judge-receipt.json")
            self.assertEqual(receipt["attempt"], 3)
            self.assertEqual(receipt["decision"], "PASS")

    def test_step_aware_readback_rejects_receipt_semantic_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            step = {
                "id": "result_ready",
                "success": "The exact current result is accepted.",
                "worker": "result_ready_judge",
                "loop": "judge",
                "max_attempts": 3,
                "output_contract": "result_ready_v1",
                "outputs": [
                    {
                        "id": "result",
                        "name": "Result",
                        "kind": "json",
                        "cardinality": "one",
                        "required": True,
                    }
                ],
            }
            flow = {"flow_id": "receipt_readback_v1"}
            materialize_chosen_output(
                run,
                flow,
                step,
                {"outputs": {"result": {"value": 7}}},
                attempt=2,
            )
            receipt_path = run / "milestones" / "result_ready" / "out" / "judge-receipt.json"
            baseline = read_json(receipt_path)
            cases = (
                ("success_rule", "A different success rule.", "success rule"),
                ("judge_ref", "other_judge", "judge reference"),
                ("max_attempts", 4, "attempt budget"),
                ("attempt", 4, "attempt exceeds"),
            )
            for field, value, error in cases:
                with self.subTest(field=field):
                    tampered = dict(baseline)
                    tampered[field] = value
                    _write(receipt_path, json.dumps(tampered, indent=2) + "\n")
                    with self.assertRaisesRegex(FlowError, error):
                        load_chosen_output(run, "result_ready", step=step)
            _write(receipt_path, json.dumps(baseline, indent=2) + "\n")
            self.assertEqual(
                load_chosen_output(run, "result_ready", step=step)["status"],
                "chosen",
            )

    @unittest.skipUnless(os.name == "nt" and Path("D:\\").is_dir(), "requires Windows with a D: volume")
    def test_image_provider_junction_resolving_to_d_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as c_temp, tempfile.TemporaryDirectory(dir="D:\\") as d_temp:
            c_root = Path(c_temp)
            d_root = Path(d_temp)
            run = c_root / "runtime" / "runs" / "image-run"
            run.mkdir(parents=True)
            provider = d_root / "image.png"
            provider.write_bytes(b"provider-image")
            junction = c_root / "provider-junction"
            linked = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(d_root)],
                capture_output=True,
                text=True,
                check=False,
            )
            if linked.returncode != 0:
                self.skipTest(f"junction creation unavailable: {linked.stderr.strip()}")
            try:
                with self.assertRaisesRegex(FlowError, "execution volume"):
                    assert_image_provider_on_execution_volume(run, junction / "image.png")
            finally:
                junction.rmdir()
