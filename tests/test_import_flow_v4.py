from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

import support  # noqa: F401

from flowstep_runtime import FlowError
from import_flow_v4 import (
    ACCEPTANCE_REQUEST_SCHEMA,
    ImportFlowV4Error,
    accept_import,
    inspect_flow_v4,
    stage_import,
    verify_staged_import,
)
from m8m_build_steps import _audit


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _json(path: Path, value: object) -> None:
    _write(path, json.dumps(value, indent=2) + "\n")


def _schema(*, closed: bool = False) -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": not closed,
        "properties": {},
    }


def _output_schema() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["outputs"],
        "properties": {
            "outputs": {
                "type": "object",
                "additionalProperties": False,
                "required": ["result"],
                "properties": {
                    "result": {"type": "object", "additionalProperties": True}
                },
            }
        },
    }


def _tool(root: Path, tool_id: str) -> None:
    package = root / "flowsteps" / "tools" / tool_id
    _write(package / "tool.py", "def run(input_data, **_):\n    return dict(input_data)\n")
    _json(package / "input.schema.json", _schema())
    _json(package / "output.schema.json", _schema())


def _milestone(root: Path, milestone_id: str, *, inputs: dict, flowsteps: list[dict], **extra) -> dict:
    for item in flowsteps:
        _tool(root, item["tool"])
    _write(
        root / "milestones" / milestone_id / "assemble.py",
        "def run(input_data, **_):\n    return {'outputs': {'result': {'ok': True}}}\n",
    )
    _write(root / "milestones" / milestone_id / "tests" / "test_assemble.py", "def test_ok():\n    assert True\n")
    _json(root / "schemas" / f"{milestone_id}-input.schema.json", _schema())
    _json(root / "schemas" / f"{milestone_id}-output.schema.json", _output_schema())
    headings = "\n\n".join(f"## `{item['id']}`\n\nRun {item['tool']}." for item in flowsteps)
    _write(
        root / "references" / f"{milestone_id}.md",
        f"# {milestone_id}\n\n## Rule of success\n\n{milestone_id} is complete.\n\n{headings}\n",
    )
    row = {
        "id": milestone_id,
        "success": f"{milestone_id} is complete.",
        "output_contract": f"{milestone_id}_v1",
        "output_schema": f"schemas/{milestone_id}-output.schema.json",
        "outputs": [
            {"id": "result", "name": f"{milestone_id} result", "kind": "json", "cardinality": "one", "required": True}
        ],
        "handler": f"milestones/{milestone_id}/assemble.py",
        "test": f"milestones/{milestone_id}/tests/test_assemble.py",
        "input_schema": f"schemas/{milestone_id}-input.schema.json",
        "inputs": inputs,
        "flowsteps": flowsteps,
        "tools": list(dict.fromkeys(item["id"] for item in flowsteps)),
        "intelligence": "none",
        "on_tool_fail": "BLOCKED",
        "loop": "none",
        "gem": f"references/{milestone_id}.md",
    }
    row.update(extra)
    return row


def _workflow_contracts(root: Path) -> None:
    _json(root / "schemas" / "workflow-request.schema.json", _schema(closed=True))
    _json(root / "schemas" / "workflow-configuration.schema.json", _schema(closed=True))
    result = _schema(closed=True)
    result["required"] = ["final_result"]
    result["properties"] = {"final_result": {"type": "object"}}
    _json(root / "schemas" / "workflow-result.schema.json", result)


def _flow(root: Path, kind: str) -> None:
    if kind == "linear":
        rows = [
            _milestone(root, "start", inputs={"request": "user.request"}, flowsteps=[{"id": "start_step", "tool": "start_tool"}]),
            _milestone(root, "finish", inputs={"start": "start.start_v1"}, flowsteps=[{"id": "finish_step", "tool": "finish_tool"}]),
        ]
    elif kind == "branch":
        receipt = "schemas/route-receipt.schema.json"
        _json(root / receipt, _schema())
        rows = [
            _milestone(
                root,
                "route",
                inputs={"request": "user.request"},
                flowsteps=[
                    {"id": "route_step", "tool": "route_tool"},
                    {"id": "branch_receipt", "tool": "branch_receipt"},
                ],
                worker="branch_receipt",
                receipt_schema=receipt,
                branch={
                    "worker": "branch_receipt",
                    "default": "left_path",
                    "join": "joined",
                    "receipt_schema": receipt,
                    "paths": [
                        {"id": "left_path", "then": "left"},
                        {"id": "right_path", "then": "right"},
                    ],
                },
            ),
            _milestone(root, "left", inputs={"route": "route.route_v1"}, flowsteps=[{"id": "left_step", "tool": "left_tool"}], on_path="left_path"),
            _milestone(root, "right", inputs={"route": "route.route_v1"}, flowsteps=[{"id": "right_step", "tool": "right_tool"}], on_path="right_path"),
            _milestone(
                root,
                "joined",
                inputs={"left": "left.left_v1", "right": "right.right_v1"},
                flowsteps=[{"id": "join_step", "tool": "join_tool"}],
            ),
        ]
    else:
        receipt = "schemas/cycle-receipt.schema.json"
        _json(root / receipt, _schema())
        rows = [
            _milestone(root, "ledger", inputs={"request": "user.request"}, flowsteps=[{"id": "ledger_step", "tool": "ledger_tool"}]),
            _milestone(root, "item", inputs={"ledger": "ledger.ledger_v1"}, flowsteps=[{"id": "item_step", "tool": "item_tool"}], on_cycle="pages"),
            _milestone(
                root,
                "checked",
                inputs={"item": "item.item_v1"},
                flowsteps=[
                    {"id": "check_step", "tool": "check_tool"},
                    {"id": "cycle_receipt", "tool": "cycle_receipt"},
                ],
                worker="cycle_receipt",
                receipt_schema=receipt,
                on_cycle="pages",
                cycle={
                    "id": "pages",
                    "worker": "cycle_receipt",
                    "ledger": "ledger",
                    "start": "item",
                    "join": "joined",
                    "max_rounds": 3,
                    "pass": "the current row is complete",
                    "receipt_schema": receipt,
                },
            ),
            _milestone(root, "joined", inputs={"checked": "checked.checked_v1"}, flowsteps=[{"id": "join_step", "tool": "join_tool"}]),
        ]
    _workflow_contracts(root)
    flow = {
        "schema": "flowstep_flow_v4",
        "flow_id": f"{kind}_flow",
        "version": 7,
        "context_policy": "isolated",
        "max_run_seconds": 90,
        "artifact_root": "artifacts",
        "milestones": rows,
    }
    _write(root / "flow.yaml", yaml.safe_dump(flow, sort_keys=False, allow_unicode=True))


def _acceptance_request(root: Path, inspection: dict) -> dict:
    terminal = inspection["terminals"][0]
    terminal_agent = next(row["agent"] for row in inspection["milestones"] if row["id"] == terminal)
    return {
        "schema": ACCEPTANCE_REQUEST_SCHEMA,
        "inspection_digest": inspection["inspection_digest"],
        "acknowledgements": {
            "execution_declarations_are_operator_supplied": True,
            "staging_only": True,
            "no_install_or_deploy": True,
        },
        "workflow_contracts": {
            "request_schema": "schemas/workflow-request.schema.json",
            "configuration_schema": "schemas/workflow-configuration.schema.json",
            "result_schema": "schemas/workflow-result.schema.json",
            "terminal_bindings": [
                {
                    "name": "final_result",
                    "from": f"{terminal}.{terminal_agent['output_contract']}",
                    "output": "result",
                }
            ],
        },
        "milestones": [
            {
                "id": row["id"],
                "execution": {
                    "candidate_executor": {
                        "ref": (
                            f"handler.{inspection['flow_id']}.{row['id']}@3.1.0"
                        )
                    },
                    "tool_bindings": [
                        {
                            "tool": flowstep["id"],
                            "ref": f"{flowstep['tool']}@1.0.0",
                        }
                        for flowstep in row["agent"]["flowsteps"]
                    ],
                },
                "capabilities": [],
            }
            for row in inspection["milestones"]
        ],
    }


class FlowV4ImporterTests(unittest.TestCase):
    def test_linear_branch_and_cycle_round_trip_with_frozen_handoff(self) -> None:
        for kind in ("linear", "branch", "cycle"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "legacy"
                root.mkdir()
                _flow(root, kind)
                inspection = inspect_flow_v4(root)
                self.assertTrue(inspection["lossless"], inspection["blockers"])
                acceptance = accept_import(
                    inspection, _acceptance_request(root, inspection), source_root=root
                )
                stage = Path(temp) / "stage"
                result = stage_import(inspection, acceptance, stage, source_root=root)
                self.assertEqual(result["status"], "PASS")
                self.assertRegex(result["source_semantic_digest"], r"^sha256:[0-9a-f]{64}$")
                self.assertRegex(result["staged_semantic_digest"], r"^sha256:[0-9a-f]{64}$")
                self.assertNotEqual(
                    result["source_semantic_digest"],
                    result["staged_semantic_digest"],
                )
                self.assertFalse(result["installed"])
                self.assertFalse(result["deployed"])
                handoff = stage / "planning" / "import-flow-v4"
                self.assertTrue((handoff / "inspection.json").is_file())
                self.assertTrue((handoff / "acceptance.json").is_file())
                self.assertTrue((handoff / "resource-manifest.json").is_file())
                self.assertTrue((handoff / "equivalence-proof.json").is_file())
                self.assertEqual(verify_staged_import(inspection, acceptance, stage)["status"], "PASS")

    def test_inspection_and_acceptance_digests_are_cross_root_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "first"
            second = Path(temp) / "second"
            first.mkdir()
            _flow(first, "linear")
            shutil.copytree(first, second)
            left = inspect_flow_v4(first)
            right = inspect_flow_v4(second)
            self.assertEqual(left, right)
            accepted_left = accept_import(left, _acceptance_request(first, left), source_root=first)
            accepted_right = accept_import(right, _acceptance_request(second, right), source_root=second)
            self.assertEqual(accepted_left, accepted_right)

    def test_digest_and_tool_package_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "legacy"
            root.mkdir()
            _flow(root, "linear")
            inspection = inspect_flow_v4(root)
            request = _acceptance_request(root, inspection)
            stale = copy.deepcopy(request)
            stale["inspection_digest"] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(ImportFlowV4Error, "not bound"):
                accept_import(inspection, stale, source_root=root)
            acceptance = accept_import(inspection, request, source_root=root)
            _write(root / "flowsteps" / "tools" / "start_tool" / "tool.py", "def run(*_):\n    return {'changed': True}\n")
            with self.assertRaisesRegex(ImportFlowV4Error, "tool-package bytes changed"):
                stage_import(inspection, acceptance, Path(temp) / "stage", source_root=root)

    def test_lossy_routing_and_automatic_builder_fallback_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "legacy"
            root.mkdir()
            _flow(root, "linear")
            flow = yaml.safe_load((root / "flow.yaml").read_text(encoding="utf-8"))
            flow["milestones"][0]["next"] = ["finish"]
            _write(root / "flow.yaml", yaml.safe_dump(flow, sort_keys=False))
            inspection = inspect_flow_v4(root)
            self.assertFalse(inspection["lossless"])
            with self.assertRaisesRegex(ImportFlowV4Error, "not lossless"):
                accept_import(inspection, _acceptance_request(root, inspection), source_root=root)

            run_dir = Path(temp) / "run"
            run_dir.mkdir()
            audit = _audit(
                {"request": {"target": str(root), "codebase": str(Path(temp) / "repo")}},
                run_dir,
            )["outputs"]["result"]
            self.assertEqual(audit["authoring_mode"], "from_context")
            self.assertFalse(audit["equivalence_claimed"])
            self.assertEqual(audit["source_context"]["reuse"]["flow_schema"], "flowstep_flow_v4")

    def test_tool_package_symlink_is_a_lossless_import_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "legacy"
            root.mkdir()
            _flow(root, "linear")
            link = root / "flowsteps" / "tools" / "start_tool" / "linked.py"
            try:
                link.symlink_to(root / "flowsteps" / "tools" / "start_tool" / "tool.py")
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            inspection = inspect_flow_v4(root)
            self.assertFalse(inspection["lossless"])
            self.assertTrue(any("symbolic links" in item for item in inspection["blockers"]))

    def test_accept_rejects_missing_ai_profile_and_inspect_rejects_open_strict_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "legacy"
            root.mkdir()
            _flow(root, "linear")
            flow_path = root / "flow.yaml"
            flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
            first = flow["milestones"][0]
            first["intelligence"] = "completion"
            first["model_justification"] = "operator-authored AI candidate"
            first["draft_schema"] = "schemas/start-draft.schema.json"
            _json(root / "schemas" / "start-draft.schema.json", _schema())
            _write(flow_path, yaml.safe_dump(flow, sort_keys=False))
            inspection = inspect_flow_v4(root)
            self.assertTrue(inspection["lossless"], inspection["blockers"])
            with self.assertRaisesRegex(ImportFlowV4Error, "AI candidate profile"):
                accept_import(
                    inspection,
                    _acceptance_request(root, inspection),
                    source_root=root,
                )

            strict = Path(temp) / "strict"
            strict.mkdir()
            _flow(strict, "linear")
            strict_flow_path = strict / "flow.yaml"
            strict_flow = yaml.safe_load(strict_flow_path.read_text(encoding="utf-8"))
            strict_first = strict_flow["milestones"][0]
            strict_first.update(
                {
                    "loop": "judge",
                    "worker": "strict_judge@1.0.0",
                    "judge_abi": "m8m_milestone_judge_v1",
                    "receipt_schema": "schemas/start-receipt.schema.json",
                    "max_attempts": 1,
                }
            )
            strict_first["flowsteps"][0]["tool"] = "start_tool@1.0.0"
            strict_first["execution"] = {
                "candidate_executor": {
                    "ref": "handler.linear_flow.start@3.1.0"
                },
                "judge": {"ref": "strict_judge@1.0.0"},
                "tool_bindings": [
                    {"tool": "start_step", "ref": "start_tool@1.0.0"}
                ],
            }
            _tool(strict, "strict_judge")
            _json(strict / "schemas" / "start-receipt.schema.json", _schema())
            _write(strict_flow_path, yaml.safe_dump(strict_flow, sort_keys=False))
            strict_inspection = inspect_flow_v4(strict)
            self.assertFalse(strict_inspection["lossless"])
            self.assertTrue(any("strict judge result schema" in item for item in strict_inspection["blockers"]))

    def test_versioned_judge_ref_resolves_unversioned_tool_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "strict"
            root.mkdir()
            _flow(root, "linear")
            flow_path = root / "flow.yaml"
            flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
            first = flow["milestones"][0]
            first.update(
                {
                    "loop": "judge",
                    "worker": "strict_judge@1.0.0",
                    "judge_abi": "m8m_milestone_judge_v1",
                    "receipt_schema": "schemas/start-receipt.schema.json",
                    "max_attempts": 1,
                }
            )
            first["flowsteps"][0]["tool"] = "start_tool@1.0.0"
            first["execution"] = {
                "candidate_executor": {
                    "ref": "handler.linear_flow.start@3.1.0"
                },
                "judge": {"ref": "strict_judge@1.0.0"},
                "tool_bindings": [
                    {"tool": "start_step", "ref": "start_tool@1.0.0"}
                ],
            }
            _tool(root, "strict_judge")
            _json(
                root / "schemas" / "start-receipt.schema.json",
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
            _write(flow_path, yaml.safe_dump(flow, sort_keys=False))
            inspection = inspect_flow_v4(root)
            self.assertTrue(
                any(row["tool_id"] == "strict_judge" for row in inspection["tool_packages"])
            )
            self.assertFalse(
                any("strict_judge@1.0.0" in item for item in inspection["blockers"])
            )


if __name__ == "__main__":
    unittest.main()
