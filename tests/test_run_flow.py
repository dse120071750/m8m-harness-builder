from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from support import EXAMPLE

from flowstep_runtime import FlowError, read_json
from run_flow import advance


class RunFlowTests(unittest.TestCase):
    def _request(self, folder: Path, text: str) -> Path:
        path = folder / "request.json"
        path.write_text(json.dumps({"text": text, "created_at": "2026-01-01T00:00:00Z"}), encoding="utf-8")
        return path

    def test_deterministic_steps_then_model_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run-1"
            request = self._request(Path(temp), "Hello,   world. This is a test.")
            first = advance(EXAMPLE, run_dir, request_path=request)
            self.assertEqual(first["state"], "ACTION_REQUIRED")
            self.assertEqual(first["step_id"], "label")
            self.assertEqual(first["model"], "completion")
            ingest = read_json(run_dir / "artifacts" / "ingest.ingest_v1.json")
            self.assertEqual(ingest["data"]["text"], "Hello, world. This is a test.")
            segment = read_json(run_dir / "artifacts" / "segment.segment_v1.json")
            self.assertEqual(segment["data"]["sentence_count"], 2)
            model_request = read_json(run_dir / first["model_request_path"])
            self.assertEqual(model_request["sentence"], "Hello, world.")

            draft = Path(temp) / "draft.json"
            draft.write_text(json.dumps({"label": "statement"}), encoding="utf-8")
            done = advance(EXAMPLE, run_dir, draft_path=draft)
            self.assertEqual(done["state"], "COMPLETE")
            label = read_json(run_dir / "artifacts" / "label.label_v1.json")
            self.assertEqual(label["data"], {"label": "statement", "sentence": "Hello, world."})
            self.assertEqual(label["evidence"]["handler"], "steps/label/tool.py")

    def test_invalid_draft_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run-2"
            request = self._request(Path(temp), "Is this a question?")
            first = advance(EXAMPLE, run_dir, request_path=request)
            self.assertEqual(first["state"], "ACTION_REQUIRED")
            draft = Path(temp) / "draft.json"
            draft.write_text(json.dumps({"label": "poem"}), encoding="utf-8")
            blocked = advance(EXAMPLE, run_dir, draft_path=draft)
            self.assertEqual(blocked["state"], "BLOCKED")
            self.assertTrue(
                any("draft.schema.json" in item or "draft.label" in item for item in blocked["blockers"])
            )

    def test_schema_mismatch_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run-3"
            request = Path(temp) / "request.json"
            request.write_text(json.dumps({"not_text": True}), encoding="utf-8")
            blocked = advance(EXAMPLE, run_dir, request_path=request)
            self.assertEqual(blocked["state"], "BLOCKED")
            self.assertEqual(blocked["step_id"], "ingest")

    def test_cannot_continue_blocked_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run-4"
            request = Path(temp) / "request.json"
            request.write_text(json.dumps({"not_text": True}), encoding="utf-8")
            advance(EXAMPLE, run_dir, request_path=request)
            with self.assertRaises(FlowError):
                advance(EXAMPLE, run_dir)


if __name__ == "__main__":
    unittest.main()
