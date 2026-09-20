from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import support  # noqa: F401

from flowstep_runtime import FlowError, load_flow, read_json
from run_flow import advance
from session_layout import chosen_output_path, load_chosen_output, resolve_chosen_output


OPEN_INPUT = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
}

OPEN_CANDIDATE = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["outputs"],
    "properties": {
        "outputs": {"type": "object", "minProperties": 1},
        "receipt": {"type": "object"},
    },
}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _request(root: Path) -> Path:
    path = root / "request.json"
    _write_json(path, {"message": "build"})
    return path


class ChosenOutputTests(unittest.TestCase):
    def _harness(self, root: Path) -> Path:
        harness = root / "harness"
        flow = {
            "schema": "flowstep_flow_v4",
            "flow_id": "chosen_demo_v1",
            "version": 1,
            "artifact_root": "artifacts",
            "milestones": [
                {
                    "id": "producer",
                    "success": "The mixed bundle is accepted.",
                    "output_contract": "producer_v1",
                    "output_schema": "schemas/candidate.json",
                    "outputs": [
                        {"id": "receipt", "name": "Receipt", "kind": "json", "cardinality": "one", "required": True},
                        {"id": "images", "name": "Images", "kind": "image", "cardinality": "many", "required": True},
                        {"id": "video", "name": "Video", "kind": "video", "cardinality": "one", "required": True},
                    ],
                    "handler": "handler.py",
                    "input_schema": "schemas/input.json",
                    "inputs": {"request": "user.request"},
                    "execution": {
                        "candidate_executor": {
                            "ref": "handler.chosen_demo_v1.producer@3.1.0"
                        },
                        "tool_bindings": [],
                    },
                    "intelligence": "none",
                    "on_tool_fail": "BLOCKED",
                },
                {
                    "id": "consumer",
                    "success": "Named upstream members are queryable.",
                    "output_contract": "consumer_v1",
                    "output_schema": "schemas/candidate.json",
                    "outputs": [
                        {"id": "result", "name": "Resolved inputs", "kind": "json", "cardinality": "one", "required": True}
                    ],
                    "handler": "handler.py",
                    "input_schema": "schemas/input.json",
                    "inputs": {
                        "whole": "producer.producer_v1",
                        "receipt": {"from": "producer.producer_v1", "output": "receipt"},
                        "images": {"from": "producer.producer_v1", "output": "images"},
                        "hero": {"from": "producer.producer_v1", "output": "images", "member": "hero_image"},
                        "video": {"from": "producer.producer_v1", "output": "video"},
                    },
                    "execution": {
                        "candidate_executor": {
                            "ref": "handler.chosen_demo_v1.consumer@3.1.0"
                        },
                        "tool_bindings": [],
                    },
                    "intelligence": "none",
                    "on_tool_fail": "BLOCKED",
                },
            ],
        }
        harness.mkdir(parents=True)
        (harness / "flow.yaml").write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
        _write_json(harness / "schemas" / "input.json", OPEN_INPUT)
        _write_json(harness / "schemas" / "candidate.json", OPEN_CANDIDATE)
        (harness / "handler.py").write_text(
            "from pathlib import Path\n"
            "def run(input_data, draft=None, task=None, run_dir=None, **_):\n"
            "    step = task['step_id']\n"
            "    count_dir = Path(run_dir) / 'diagnostic-counts'\n"
            "    count_dir.mkdir(parents=True, exist_ok=True)\n"
            "    count_path = count_dir / f'{step}.txt'\n"
            "    count = int(count_path.read_text() or '0') + 1 if count_path.exists() else 1\n"
            "    count_path.write_text(str(count))\n"
            "    if step == 'producer':\n"
            "        generated = Path(run_dir) / 'work' / 'generated'\n"
            "        generated.mkdir(parents=True, exist_ok=True)\n"
            "        hero = generated / 'hero.png'; hero.write_bytes(b'hero')\n"
            "        detail = generated / 'detail.png'; detail.write_bytes(b'detail')\n"
            "        video = generated / 'clip.mp4'; video.write_bytes(b'video')\n"
            "        return {'outputs': {\n"
            "            'receipt': {'accepted': True},\n"
            "            'images': [\n"
            "                {'id': 'hero_image', 'name': 'Hero image', 'path': str(hero)},\n"
            "                {'id': 'detail_image', 'name': 'Detail image', 'path': str(detail)},\n"
            "            ],\n"
            "            'video': {'path': str(video)},\n"
            "        }}\n"
            "    return {'outputs': {'result': {\n"
            "        'accepted': input_data['receipt']['accepted'],\n"
            "        'image_count': len(input_data['images']),\n"
            "        'hero_id': input_data['hero']['id'],\n"
            "        'video_kind': input_data['video']['kind'],\n"
            "        'whole_keys': sorted(input_data['whole']),\n"
            "    }}}\n",
            encoding="utf-8",
        )
        return harness

    def test_mixed_bundle_and_member_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = self._harness(root)
            run_dir = root / "run"
            done = advance(harness, run_dir, request_path=_request(root))
            self.assertEqual(done["state"], "COMPLETE", done)
            manifest = load_chosen_output(run_dir, "producer")
            self.assertEqual([item["id"] for item in manifest["members"]], ["receipt", "hero_image", "detail_image", "video"])
            self.assertTrue(all((run_dir / item["path"]).is_file() for item in manifest["members"]))
            self.assertEqual(resolve_chosen_output(run_dir, "producer", output_id="receipt"), {"accepted": True})
            result = resolve_chosen_output(run_dir, "consumer", output_id="result")
            self.assertEqual(result, {
                "accepted": True,
                "image_count": 2,
                "hero_id": "hero_image",
                "video_kind": "video",
                "whole_keys": ["images", "receipt", "video"],
            })

    def test_resume_reuses_chosen_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = self._harness(root)
            run_dir = root / "run"
            advance(harness, run_dir, request_path=_request(root))
            second = advance(harness, run_dir)
            self.assertEqual(second["state"], "COMPLETE")
            self.assertEqual((run_dir / "diagnostic-counts" / "producer.txt").read_text(), "1")
            self.assertEqual((run_dir / "diagnostic-counts" / "consumer.txt").read_text(), "1")

    def test_media_admission_and_resume_need_no_output_hashes_or_image_judge(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = self._harness(root)
            run_dir = root / "run"
            with mock.patch("session_layout._sha256", side_effect=AssertionError("unrequested output hash")), \
                 mock.patch("run_flow.run_library_tool", side_effect=AssertionError("unrequested image review")):
                first = advance(harness, run_dir, request_path=_request(root))
                self.assertEqual(first["state"], "COMPLETE", first)
                second = advance(harness, run_dir)
                self.assertEqual(second["state"], "COMPLETE", second)
                manifest = load_chosen_output(run_dir, "producer")
            self.assertTrue(all("sha256" not in item for item in manifest["members"]))
            self.assertEqual((run_dir / "diagnostic-counts" / "producer.txt").read_text(), "1")
            for path in (run_dir / "materialized").glob("*.json"):
                self.assertNotIn("artifact_sha256", read_json(path))

    def test_resume_rejects_corrupt_chosen_json_without_rerunning_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = self._harness(root)
            run_dir = root / "run"
            done = advance(harness, run_dir, request_path=_request(root))
            self.assertEqual(done["state"], "COMPLETE", done)
            manifest = read_json(chosen_output_path(run_dir, "producer"))
            receipt = next(item for item in manifest["members"] if item["id"] == "receipt")
            (run_dir / receipt["path"]).write_bytes(b'{"accepted":')

            with self.assertRaisesRegex(FlowError, "asset is invalid JSON"):
                advance(harness, run_dir)
            self.assertEqual((run_dir / "diagnostic-counts" / "producer.txt").read_text(), "1")
            self.assertEqual((run_dir / "diagnostic-counts" / "consumer.txt").read_text(), "1")

    def test_resume_revalidates_chosen_json_against_pinned_output_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = self._harness(root)
            _write_json(
                harness / "schemas" / "candidate.json",
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["outputs"],
                    "properties": {
                        "outputs": {
                            "type": "object",
                            "additionalProperties": True,
                            "anyOf": [
                                {"required": ["receipt"]},
                                {"required": ["result"]},
                            ],
                            "properties": {
                                "receipt": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["accepted"],
                                    "properties": {"accepted": {"type": "boolean"}},
                                },
                                "result": {},
                            },
                        }
                    },
                },
            )
            run_dir = root / "run"
            done = advance(harness, run_dir, request_path=_request(root))
            self.assertEqual(done["state"], "COMPLETE", done)
            manifest = read_json(chosen_output_path(run_dir, "producer"))
            receipt = next(item for item in manifest["members"] if item["id"] == "receipt")
            _write_json(run_dir / receipt["path"], {"accepted": "yes"})

            with self.assertRaisesRegex(FlowError, r"candidate\.json failed.*accepted"):
                advance(harness, run_dir)
            self.assertEqual((run_dir / "diagnostic-counts" / "producer.txt").read_text(), "1")
            self.assertEqual((run_dir / "diagnostic-counts" / "consumer.txt").read_text(), "1")

    def test_resume_rejects_media_bytes_of_the_wrong_declared_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = self._harness(root)
            run_dir = root / "run"
            done = advance(harness, run_dir, request_path=_request(root))
            self.assertEqual(done["state"], "COMPLETE", done)
            manifest = read_json(chosen_output_path(run_dir, "producer"))
            hero = next(item for item in manifest["members"] if item["id"] == "hero_image")
            # A minimal ISO-BMFF header is recognizably video even though the
            # chosen member still has its declared .png path and image MIME.
            (run_dir / hero["path"]).write_bytes(
                b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
            )

            with self.assertRaisesRegex(FlowError, "recognizable as video"):
                advance(harness, run_dir)
            self.assertEqual((run_dir / "diagnostic-counts" / "producer.txt").read_text(), "1")
            self.assertEqual((run_dir / "diagnostic-counts" / "consumer.txt").read_text(), "1")

    def test_load_chosen_output_rejects_a_missing_member_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = self._harness(root)
            run_dir = root / "run"
            advance(harness, run_dir, request_path=_request(root))
            manifest = read_json(chosen_output_path(run_dir, "producer"))
            missing = run_dir / manifest["members"][0]["path"]
            missing.unlink()
            with self.assertRaisesRegex(FlowError, "member asset is missing"):
                load_chosen_output(run_dir, "producer")

    def test_step_aware_readback_rejects_a_missing_required_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = self._harness(root)
            run_dir = root / "run"
            advance(harness, run_dir, request_path=_request(root))
            path = chosen_output_path(run_dir, "producer")
            manifest = read_json(path)
            manifest["outputs"] = [item for item in manifest["outputs"] if item["id"] != "receipt"]
            manifest["members"] = [item for item in manifest["members"] if item["output_id"] != "receipt"]
            for index, member in enumerate(manifest["members"]):
                member["order"] = index
            _write_json(path, manifest)
            step = load_flow(harness)["steps"][0]
            with self.assertRaisesRegex(FlowError, "required chosen output receipt is missing"):
                load_chosen_output(run_dir, "producer", step=step)

    def test_replace_invalidates_downstream_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = self._harness(root)
            run_dir = root / "run"
            advance(harness, run_dir, request_path=_request(root))
            replaced = advance(harness, run_dir, replace_milestone="consumer")
            self.assertEqual(replaced["state"], "COMPLETE", replaced)
            self.assertEqual((run_dir / "diagnostic-counts" / "producer.txt").read_text(), "1")
            self.assertEqual((run_dir / "diagnostic-counts" / "consumer.txt").read_text(), "2")
            replaced = advance(harness, run_dir, replace_milestone="producer")
            self.assertEqual(replaced["state"], "COMPLETE", replaced)
            self.assertEqual((run_dir / "diagnostic-counts" / "producer.txt").read_text(), "2")
            self.assertEqual((run_dir / "diagnostic-counts" / "consumer.txt").read_text(), "3")

    def test_legacy_flow_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "flow.yaml").write_text(
                "schema: flowstep_flow_v3\nflow_id: old_v1\nversion: 1\nmilestones: []\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FlowError, "m8m-harness-builder 3.1"):
                load_flow(root)

    def test_v4_milestone_accepts_binary_slot_asset_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = self._harness(root)
            flow_path = harness / "flow.yaml"
            flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
            flow["milestones"][0]["asset"] = {"kind": "image"}
            flow_path.write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
            loaded = load_flow(harness)
            self.assertEqual(loaded["steps"][0]["asset"]["kind"], "image")

    def test_duplicate_collection_member_blocks_without_chosen_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = self._harness(root)
            handler = harness / "handler.py"
            text = handler.read_text(encoding="utf-8").replace("'detail_image'", "'hero_image'")
            handler.write_text(text, encoding="utf-8")
            run_dir = root / "run"
            blocked = advance(harness, run_dir, request_path=_request(root))
            self.assertEqual(blocked["state"], "BLOCKED")
            self.assertFalse(chosen_output_path(run_dir, "producer").is_file())

    def test_missing_required_output_blocks_without_chosen_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = self._harness(root)
            handler = harness / "handler.py"
            text = handler.read_text(encoding="utf-8").replace(
                "            'receipt': {'accepted': True},\n",
                "",
            )
            handler.write_text(text, encoding="utf-8")
            run_dir = root / "run"
            blocked = advance(harness, run_dir, request_path=_request(root))
            self.assertEqual(blocked["state"], "BLOCKED")
            self.assertTrue(any("required output receipt" in item for item in blocked["blockers"]))
            self.assertFalse(chosen_output_path(run_dir, "producer").is_file())

    def test_missing_media_file_blocks_without_chosen_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = self._harness(root)
            handler = harness / "handler.py"
            text = handler.read_text(encoding="utf-8").replace(
                "video = generated / 'clip.mp4'; video.write_bytes(b'video')",
                "video = generated / 'missing.mp4'",
            )
            handler.write_text(text, encoding="utf-8")
            run_dir = root / "run"
            blocked = advance(harness, run_dir, request_path=_request(root))
            self.assertEqual(blocked["state"], "BLOCKED")
            self.assertTrue(any("does not exist" in item for item in blocked["blockers"]))
            self.assertFalse(chosen_output_path(run_dir, "producer").is_file())

    def test_malformed_candidate_schema_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = self._harness(root)
            (harness / "schemas" / "candidate.json").write_text("{bad json", encoding="utf-8")
            run_dir = root / "run"
            blocked = advance(harness, run_dir, request_path=_request(root))
            self.assertEqual(blocked["state"], "BLOCKED")
            self.assertTrue(any("invalid JSON" in item for item in blocked["blockers"]))
            self.assertFalse(chosen_output_path(run_dir, "producer").is_file())

    def test_invalid_candidate_schema_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = self._harness(root)
            _write_json(harness / "schemas" / "candidate.json", {"type": "not-a-json-type"})
            run_dir = root / "run"
            blocked = advance(harness, run_dir, request_path=_request(root))
            self.assertEqual(blocked["state"], "BLOCKED")
            self.assertTrue(any("invalid schema" in item for item in blocked["blockers"]))
            self.assertFalse(chosen_output_path(run_dir, "producer").is_file())

    def test_step_aware_resume_rejects_legacy_compact_judge_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = self._harness(root)
            run_dir = root / "run"
            done = advance(harness, run_dir, request_path=_request(root))
            self.assertEqual(done["state"], "COMPLETE", done)
            receipt_path = run_dir / "milestones" / "producer" / "out" / "judge-receipt.json"
            _write_json(receipt_path, {"ok": True, "mode": "schema", "attempt": 1})
            step = load_flow(harness)["steps"][0]
            with self.assertRaisesRegex(FlowError, "m8m_milestone_judge_receipt_v1"):
                load_chosen_output(run_dir, "producer", step=step)

    def test_judge_loops_then_commits_only_the_accepted_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = root / "judge-harness"
            flow = {
                "schema": "flowstep_flow_v4",
                "flow_id": "judge_demo_v1",
                "version": 1,
                "artifact_root": "artifacts",
                "milestones": [
                    {
                        "id": "image_ready",
                        "success": "The judge accepts the current generated image receipt.",
                        "output_contract": "image_ready_v1",
                        "output_schema": "schemas/candidate.json",
                        "outputs": [
                            {"id": "result", "name": "Accepted receipt", "kind": "json", "cardinality": "one", "required": True}
                        ],
                        "handler": "handler.py",
                        "execution": {
                            "candidate_executor": {
                                "ref": "handler.judge_demo_v1.image_ready@3.1.0"
                            },
                            "judge": {
                                "ref": "image_ready_judge@4.5.6"
                            },
                            "tool_bindings": [],
                        },
                        "input_schema": "schemas/input.json",
                        "inputs": {"request": "user.request"},
                        "intelligence": "none",
                        "loop": "judge",
                        "worker": "image_ready_judge@4.5.6",
                        "judge_abi": "m8m_milestone_judge_v1",
                        "receipt_schema": "schemas/receipt.json",
                        "max_attempts": 3,
                        "on_tool_fail": "BLOCKED",
                    }
                ],
            }
            harness.mkdir(parents=True)
            (harness / "flow.yaml").write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
            _write_json(harness / "schemas" / "input.json", OPEN_INPUT)
            _write_json(harness / "schemas" / "candidate.json", OPEN_CANDIDATE)
            _write_json(
                harness / "schemas" / "receipt.json",
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["decision", "reasons", "blockers"],
                    "properties": {
                        "decision": {"enum": ["PASS", "RETRY", "BLOCKED"]},
                        "reasons": {"type": "array", "items": {"type": "string"}},
                        "blockers": {"type": "array", "items": {"type": "string"}},
                    },
                },
            )
            (harness / "handler.py").write_text(
                "from pathlib import Path\n"
                "def run(input_data, draft=None, task=None, run_dir=None, **_):\n"
                "    counter = Path(run_dir) / 'judge-calls.txt'\n"
                "    attempt = int(counter.read_text()) + 1 if counter.exists() else 1\n"
                "    counter.write_text(str(attempt))\n"
                "    return {'outputs': {'result': {'accepted_attempt': attempt}}}\n",
                encoding="utf-8",
            )
            judge = harness / "flowsteps" / "tools" / "image_ready_judge"
            judge.mkdir(parents=True)
            (judge / "tool.py").write_text(
                "def run(input_data, params=None, **_):\n"
                "    attempt = input_data['candidate']['outputs']['result']['accepted_attempt']\n"
                "    return {\n"
                "        'decision': 'PASS' if attempt >= 2 else 'RETRY',\n"
                "        'reasons': [f'attempt {attempt}'],\n"
                "        'blockers': [] if attempt >= 2 else ['not ready'],\n"
                "    }\n",
                encoding="utf-8",
            )
            _write_json(
                judge / "input.schema.json",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["schema", "milestone_id", "attempt", "max_attempts", "expectation", "inputs", "candidate"],
                    "properties": {
                        "schema": {"const": "m8m.milestone_judge_request.v1"},
                        "milestone_id": {"type": "string"},
                        "attempt": {"type": "integer"},
                        "max_attempts": {"type": "integer"},
                        "expectation": {"type": "object"},
                        "inputs": {"type": "object"},
                        "candidate": {"type": "object"},
                    },
                },
            )
            _write_json(judge / "output.schema.json", json.loads((harness / "schemas" / "receipt.json").read_text()))
            run_dir = root / "run"
            done = advance(harness, run_dir, request_path=_request(root))
            self.assertEqual(done["state"], "COMPLETE", done)
            self.assertEqual((run_dir / "judge-calls.txt").read_text(), "2")
            self.assertEqual(
                resolve_chosen_output(run_dir, "image_ready", output_id="result"),
                {"accepted_attempt": 2},
            )
            receipt = read_json(run_dir / "milestones" / "image_ready" / "out" / "judge-receipt.json")
            self.assertEqual(receipt["schema"], "m8m.milestone_judge_receipt.v1")
            self.assertEqual(receipt["milestone_id"], "image_ready")
            self.assertEqual(receipt["attempt"], 2)
            self.assertEqual(receipt["decision"], "PASS")
            self.assertEqual(receipt["judge_ref"], "image_ready_judge@4.5.6")
            self.assertEqual(receipt["max_attempts"], 3)
            self.assertEqual(receipt["blockers"], [])
            retry = read_json(
                run_dir
                / "milestones"
                / "image_ready"
                / "work"
                / "attempts"
                / "attempt-001"
                / "judge-receipt.json"
            )
            self.assertEqual(retry["decision"], "RETRY")
            self.assertEqual(retry["attempt"], 1)

    def test_strict_judge_abi_runs_the_declared_worker_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = root / "strict-judge"
            tool = harness / "flowsteps" / "tools" / "current_result_judge"
            (tool / "tests").mkdir(parents=True)
            flow = {
                "schema": "flowstep_flow_v4",
                "flow_id": "strict_judge_v1",
                "version": 1,
                "artifact_root": "artifacts",
                "milestones": [
                    {
                        "id": "result_ready",
                        "success": "The current judge independently accepts the candidate.",
                        "output_contract": "strict_result_v1",
                        "output_schema": "schemas/candidate.json",
                        "outputs": [
                            {
                                "id": "result",
                                "name": "Current result",
                                "kind": "json",
                                "cardinality": "one",
                                "required": True,
                            }
                        ],
                        "handler": "handler.py",
                        "execution": {
                            "candidate_executor": {
                                "ref": "handler.strict_judge_v1.result_ready@3.1.0"
                            },
                            "judge": {
                                "ref": "current_result_judge@4.5.6"
                            },
                            "tool_bindings": [],
                        },
                        "input_schema": "schemas/input.json",
                        "inputs": {"request": "user.request"},
                        "intelligence": "none",
                        "loop": "judge",
                        "worker": "current_result_judge@4.5.6",
                        "judge_abi": "m8m_milestone_judge_v1",
                        "receipt_schema": "flowsteps/tools/current_result_judge/output.schema.json",
                        "max_attempts": 3,
                        "on_tool_fail": "BLOCKED",
                    }
                ],
            }
            harness.mkdir(exist_ok=True)
            (harness / "flow.yaml").write_text(
                yaml.safe_dump(flow, sort_keys=False), encoding="utf-8"
            )
            _write_json(harness / "schemas" / "input.json", OPEN_INPUT)
            _write_json(harness / "schemas" / "candidate.json", OPEN_CANDIDATE)
            (harness / "handler.py").write_text(
                "from pathlib import Path\n"
                "def run(input_data, draft=None, task=None, run_dir=None, **_):\n"
                "    marker = Path(run_dir) / 'strict-handler-calls.txt'\n"
                "    attempt = int(marker.read_text()) + 1 if marker.exists() else 1\n"
                "    marker.write_text(str(attempt))\n"
                "    return {'outputs': {'result': {'attempt': attempt}}}\n",
                encoding="utf-8",
            )
            (tool / "tool.py").write_text(
                "from pathlib import Path\n"
                "def run(input_data, params=None, **_):\n"
                "    assert set(input_data) == {'schema', 'milestone_id', 'attempt', 'max_attempts', 'expectation', 'inputs', 'candidate'}\n"
                "    assert input_data['expectation']['success'] == 'The current judge independently accepts the candidate.'\n"
                "    marker = Path(__file__).resolve().parents[3] / 'strict-judge-calls.txt'\n"
                "    count = int(marker.read_text()) + 1 if marker.exists() else 1\n"
                "    marker.write_text(str(count))\n"
                "    return {\n"
                "        'decision': 'PASS' if count >= 2 else 'RETRY',\n"
                "        'reasons': ['worker-pass' if count >= 2 else 'worker-retry'],\n"
                "        'blockers': [] if count >= 2 else ['not ready'],\n"
                "    }\n",
                encoding="utf-8",
            )
            _write_json(
                tool / "input.schema.json",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["schema", "milestone_id", "attempt", "max_attempts", "expectation", "inputs", "candidate"],
                    "properties": {
                        "schema": {"const": "m8m.milestone_judge_request.v1"},
                        "milestone_id": {"type": "string"},
                        "attempt": {"type": "integer"},
                        "max_attempts": {"type": "integer"},
                        "expectation": {"type": "object"},
                        "inputs": {"type": "object"},
                        "candidate": {"type": "object"},
                    },
                },
            )
            _write_json(
                tool / "output.schema.json",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["decision", "reasons", "blockers"],
                    "properties": {
                        "decision": {"enum": ["PASS", "RETRY", "BLOCKED"]},
                        "reasons": {"type": "array", "items": {"type": "string"}},
                        "blockers": {"type": "array", "items": {"type": "string"}},
                    },
                },
            )
            run_dir = root / "run"
            done = advance(harness, run_dir, request_path=_request(root))
            self.assertEqual(done["state"], "COMPLETE", done)
            self.assertEqual((run_dir / "strict-handler-calls.txt").read_text(), "2")
            self.assertEqual((harness / "strict-judge-calls.txt").read_text(), "2")
            receipt = read_json(
                run_dir / "milestones" / "result_ready" / "out" / "judge-receipt.json"
            )
            self.assertEqual(
                receipt["judge_ref"], "current_result_judge@4.5.6"
            )
            task = read_json(
                run_dir
                / "runtime-tasks"
                / "result_ready"
                / "attempt-002"
                / "candidate-request.json"
            )
            self.assertEqual(
                task["execution"]["candidate_executor"]["ref"],
                "handler.strict_judge_v1.result_ready@3.1.0",
            )
            self.assertEqual(receipt["attempt"], 2)
            self.assertEqual(receipt["reasons"], ["worker-pass"])


if __name__ == "__main__":
    unittest.main()
