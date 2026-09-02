from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from flowstep_runtime import FlowError, assert_external_phase_journal, load_flow


SPEC = {
    "id": "published_live",
    "side_effects": "external",
    "phase_journal": {
        "path": "publication/phase-journal.json",
        "operator_result_path": "publication/operator-result.json",
        "resume": "query_exact_operation",
    },
}


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class ExternalPhaseJournalTests(unittest.TestCase):
    def test_runtime_requires_matching_operator_and_live_readback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            operation_id = "case_write_abc"
            plan_sha = "a" * 64
            write_json(
                run / "publication" / "phase-journal.json",
                {
                    "schema": "m8m_external_phase_journal_v1",
                    "milestone_id": "published_live",
                    "operation_id": operation_id,
                    "plan_sha256": plan_sha,
                    "status": "readback_verified",
                    "exact_operation_queried": True,
                    "operator_result_path": "publication/operator-result.json",
                    "readback_path": "publication/readback.json",
                    "phases": [
                        {"phase": "plan_frozen"},
                        {"phase": "operation_queried"},
                        {"phase": "operator_persisted"},
                        {"phase": "readback_verified"},
                    ],
                },
            )
            with self.assertRaisesRegex(FlowError, "operator result is missing"):
                assert_external_phase_journal(run, SPEC)
            write_json(
                run / "publication" / "operator-result.json",
                {
                    "status": "completed",
                    "operation_id": operation_id,
                    "plan_sha256": plan_sha,
                },
            )
            write_json(
                run / "publication" / "readback.json",
                {"status": "PASS", "operation_id": operation_id},
            )
            assert_external_phase_journal(run, SPEC)

    def test_loader_rejects_publish_without_external_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "flow.yaml").write_text(
                "\n".join(
                    [
                        "schema: flowstep_flow_v4",
                        "flow_id: publish_v1",
                        "version: 1",
                        "milestones:",
                        "  - id: published_live",
                        "    success: Live readback passes.",
                        "    output_contract: published_v1",
                        "    output_schema: output.schema.json",
                        "    outputs:",
                        "      - {id: result, name: Published, kind: json, cardinality: one, required: true}",
                        "    handler: assemble.py",
                        "    inputs: {request: user.request}",
                        "    tools: [publish_case]",
                        "    flowsteps:",
                        "      - {id: publish_case, tool: 'publish_case@1.0.0'}",
                        "    execution:",
                        "      candidate_executor: {ref: 'handler.publish_v1.published_live@3.1.0'}",
                        "      tool_bindings:",
                        "        - {tool: publish_case, ref: 'publish_case@1.0.0'}",
                        "    intelligence: none",
                        "    on_tool_fail: retryable",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FlowError, "declare side_effects: external"):
                load_flow(root)


if __name__ == "__main__":
    unittest.main()
