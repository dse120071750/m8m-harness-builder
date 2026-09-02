from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import yaml

from support import EXAMPLE  # noqa: F401

from flowstep_runtime import FlowError, read_json, write_json
from run_flow import advance
from session_layout import promote_cycle_round


class DurableJsonTests(unittest.TestCase):
    def test_immutable_concurrent_writers_publish_exactly_one_document(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "chosen-output.json"

            def publish(index: int) -> bool:
                try:
                    write_json(target, {"winner": index}, overwrite=False)
                    return True
                except FlowError:
                    return False

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(publish, range(16)))

            self.assertEqual(sum(results), 1)
            self.assertIn(read_json(target)["winner"], range(16))
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows rename fallback")
    def test_immutable_writer_falls_back_when_volume_rejects_hard_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "artifact.json"
            unsupported = OSError("hard links unsupported")
            unsupported.winerror = 1

            with patch("flowstep_runtime.os.link", side_effect=unsupported):
                write_json(target, {"ok": True}, overwrite=False)

            self.assertEqual(read_json(target), {"ok": True})
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    def test_cycle_envelope_promotion_uses_same_volume_hard_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            flow = {"artifact_root": "artifacts"}
            step = {"id": "asset_bound", "output_contract": "asset_bound_v1"}
            envelope = run_dir / "artifacts" / "asset_bound.asset_bound_v1.json"
            envelope.parent.mkdir(parents=True)
            envelope.write_text('{"ok": true}\n', encoding="utf-8")

            with patch(
                "session_layout.shutil.copy2",
                side_effect=AssertionError("same-volume promotion must not copy bytes"),
            ):
                promote_cycle_round(run_dir, flow, [step], "001")

            promoted = run_dir / "milestones" / "asset_bound" / "items" / "001" / "asset.json"
            self.assertTrue(promoted.is_file())
            self.assertTrue(os.path.samefile(envelope, promoted))

    def test_cycle_envelope_promotion_falls_back_when_hard_links_are_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            flow = {"artifact_root": "artifacts"}
            step = {"id": "asset_bound", "output_contract": "asset_bound_v1"}
            envelope = run_dir / "artifacts" / "asset_bound.asset_bound_v1.json"
            envelope.parent.mkdir(parents=True)
            envelope.write_text('{"ok": true}\n', encoding="utf-8")

            with patch("session_layout.os.link", side_effect=OSError("unsupported")):
                promote_cycle_round(run_dir, flow, [step], "001")

            promoted = run_dir / "milestones" / "asset_bound" / "items" / "001" / "asset.json"
            self.assertEqual(read_json(promoted), {"ok": True})


class ResumeAndRetryTests(unittest.TestCase):
    @staticmethod
    def _request(folder: Path) -> Path:
        path = folder / "request.json"
        path.write_text(
            json.dumps({"text": "Hello. World.", "created_at": "2026-01-01T00:00:00Z"}),
            encoding="utf-8",
        )
        return path

    def test_resume_reconciles_materialized_step_missing_from_execution_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            first = advance(EXAMPLE, run_dir, request_path=self._request(Path(temp)))
            self.assertEqual(first["state"], "ACTION_REQUIRED")
            record_path = run_dir / "flow-execution-record.json"
            record = read_json(record_path)
            record["steps"] = [
                item for item in record["steps"] if item.get("step_id") != "segment"
            ]
            write_json(record_path, record, overwrite=True)

            resumed = advance(EXAMPLE, run_dir)

            self.assertEqual(resumed["state"], "ACTION_REQUIRED")
            self.assertEqual(resumed["step_id"], "label")
            repaired = read_json(record_path)
            self.assertEqual(
                sum(item.get("step_id") == "segment" for item in repaired["steps"]),
                1,
            )

    def test_parked_wall_time_does_not_consume_active_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            first = advance(EXAMPLE, run_dir, request_path=self._request(Path(temp)))
            self.assertEqual(first["state"], "ACTION_REQUIRED")
            record_path = run_dir / "flow-execution-record.json"
            record = read_json(record_path)
            record["created_at"] = "2000-01-01T00:00:00Z"
            record["active_seconds"] = 0.0
            write_json(record_path, record, overwrite=True)
            draft = Path(temp) / "draft.json"
            draft.write_text(json.dumps({"label": "statement"}), encoding="utf-8")

            done = advance(EXAMPLE, run_dir, draft_path=draft)

            self.assertEqual(done["state"], "COMPLETE")

    def test_retryable_tool_failure_never_requests_a_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = Path(temp) / "flow"
            shutil.copytree(EXAMPLE, harness)
            flow_path = harness / "flows" / "text_pipeline_v1.yaml"
            flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
            first = flow["milestones"][0]
            first["on_tool_fail"] = "retryable"
            first["max_tool_attempts"] = 3
            flow_path.write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
            handler = harness / str(first["handler"])
            handler.write_text(
                "def run(input_data, draft=None, **kwargs):\n"
                "    raise ConnectionRefusedError('backend unavailable')\n",
                encoding="utf-8",
            )
            run_dir = Path(temp) / "run"
            request = self._request(Path(temp))

            one = advance(harness, run_dir, request_path=request)
            two = advance(harness, run_dir)
            three = advance(harness, run_dir)

            for action in (one, two):
                self.assertEqual(action["state"], "ACTION_REQUIRED")
                self.assertEqual(action["action"], "retry_tool_then_resume")
                self.assertEqual(action["model"], "none")
                self.assertNotIn("model_request_path", action)
            self.assertEqual(three["state"], "BLOCKED")
            self.assertTrue(
                any("no model fallback" in item for item in three.get("blockers") or [])
            )


if __name__ == "__main__":
    unittest.main()
