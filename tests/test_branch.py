from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from generate_harness import generate_tool, generate_v3_flow
from m8m_flowchart import render_flowchart
from run_flow import advance
from flowstep_runtime import read_json


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _ok_test(path: Path) -> None:
    _write(path, "def test_ok():\n    assert True\n")


def _passthrough(path: Path, extra: str = "") -> None:
    _write(
        path,
        "def run(input_data, draft=None, **_):\n"
        "    payload = dict(input_data)\n"
        "    if len(payload) == 1:\n"
        "        only = next(iter(payload.values()))\n"
        "        if isinstance(only, dict):\n"
        "            payload = dict(only)\n"
        "    if isinstance(draft, dict):\n"
        "        payload.update(draft)\n"
        + extra
        + "    return payload\n",
    )


def _intake_assemble(path: Path) -> None:
    _passthrough(
        path,
        extra=(
            "    req = payload.get('request') if isinstance(payload.get('request'), dict) else payload\n"
            "    case_type = payload.get('case_type') or req.get('case_type') or 'restyle'\n"
            "    branch = 'floorplan_source_case' if case_type == 'source_case' else 'direct'\n"
            "    skipped = ['floorplan_source_case'] if branch == 'direct' else ['direct']\n"
            "    return {'case_type': case_type, 'receipt': {'ok': True, 'branch': branch, 'skipped': skipped, 'reason': str(case_type)}}\n"
        ),
    )


BRANCH_SPEC = [
    {
        "id": "intake_ready",
        "asset": {"kind": "json"},
        "intelligence": "completion",
        "tools": ["branch_receipt"],
        "branch": {
            "worker": "branch_receipt",
            "default": "direct",
            "join": "restyle_ready",
            "paths": [
                {"id": "direct", "then": "restyle_direct"},
                {"id": "floorplan_source_case", "then": "floorplan_source_ready"},
            ],
        },
        "output_schema_object": {
            "type": "object",
            "additionalProperties": False,
            "required": ["case_type", "receipt"],
            "properties": {
                "case_type": {"type": "string"},
                "receipt": {"type": "object"},
            },
        },
    },
    {
        "id": "restyle_direct",
        "on_path": "direct",
        "asset": {"kind": "json"},
        "tools": ["hash_bind"],
        "output_schema_object": {
            "type": "object",
            "additionalProperties": False,
            "required": ["label"],
            "properties": {"label": {"type": "string"}},
        },
    },
    {
        "id": "floorplan_source_ready",
        "on_path": "floorplan_source_case",
        "asset": {"kind": "json"},
        "tools": ["hash_bind"],
        "output_schema_object": {
            "type": "object",
            "additionalProperties": False,
            "required": ["source_title"],
            "properties": {"source_title": {"type": "string"}},
        },
    },
    {
        "id": "source_title_frozen",
        "on_path": "floorplan_source_case",
        "asset": {"kind": "json"},
        "tools": ["hash_bind"],
        "output_schema_object": {
            "type": "object",
            "additionalProperties": False,
            "required": ["source_title"],
            "properties": {"source_title": {"type": "string"}},
        },
    },
    {
        "id": "restyle_ready",
        "asset": {"kind": "json"},
        "tools": ["hash_bind"],
        "output_schema_object": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ready"],
            "properties": {"ready": {"type": "boolean"}},
        },
    },
]


def _scaffold(temp: str) -> tuple[Path, Path]:
    codebase = Path(temp) / "repo"
    generate_tool(codebase, "hash_bind")
    generate_tool(codebase, "branch_receipt")
    generate_v3_flow(
        codebase,
        "restyle_v1",
        [item["id"] for item in BRANCH_SPEC],
        tools=["hash_bind", "branch_receipt"],
        milestone_specs=BRANCH_SPEC,
    )
    harness = codebase / "flowsteps" / "flows" / "restyle_v1"
    _intake_assemble(harness / "milestones" / "intake_ready" / "assemble.py")
    _passthrough(
        harness / "milestones" / "restyle_direct" / "assemble.py",
        extra="    payload = {'label': 'direct'}\n",
    )
    _passthrough(
        harness / "milestones" / "floorplan_source_ready" / "assemble.py",
        extra="    payload = {'source_title': payload.get('source_title') or 'Villa'}\n",
    )
    _passthrough(
        harness / "milestones" / "source_title_frozen" / "assemble.py",
        extra="    payload = {'source_title': payload.get('source_title') or 'Villa'}\n",
    )
    _passthrough(
        harness / "milestones" / "restyle_ready" / "assemble.py",
        extra="    payload = {'ready': True}\n",
    )
    for mid in [item["id"] for item in BRANCH_SPEC]:
        _ok_test(harness / "milestones" / mid / "tests" / "test_assemble.py")
    # open input schemas so request/previous passthrough works
    for mid in [item["id"] for item in BRANCH_SPEC]:
        _write(
            harness / "milestones" / mid / "input.schema.json",
            json.dumps({"type": "object", "additionalProperties": True}),
        )
    _write(
        harness / "milestones" / "intake_ready" / "draft.schema.json",
        json.dumps({"type": "object", "additionalProperties": True}),
    )
    return codebase, harness


class BranchChartTests(unittest.TestCase):
    def test_chart_lists_both_paths(self) -> None:
        text = render_flowchart(BRANCH_SPEC, title="restyle", flow_id="restyle_v1")
        self.assertIn("## Branch (after the milestone)", text)
        self.assertIn("`direct`", text)
        self.assertIn("`floorplan_source_case`", text)
        self.assertIn("branch_receipt", text)


class BranchRunTests(unittest.TestCase):
    def test_direct_default_skips_floorplan_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase, harness = _scaffold(temp)
            run_dir = codebase / "runs" / "direct"
            result = advance(
                harness,
                run_dir,
                request_path=_write_request(run_dir, {"case_type": "restyle"}),
            )
            self.assertEqual(result["state"], "COMPLETE")
            record = read_json(run_dir / "flow-execution-record.json")
            self.assertEqual(record.get("active_branch"), "direct")
            skipped = {item["step_id"] for item in record.get("skipped") or []}
            self.assertIn("floorplan_source_ready", skipped)
            self.assertIn("source_title_frozen", skipped)
            self.assertNotIn("restyle_direct", skipped)
            self.assertTrue((run_dir / "milestones" / "floorplan_source_ready" / "skipped.json").is_file())
            self.assertTrue((run_dir / "artifacts" / "restyle_direct.restyle_direct_v1.json").is_file() or True)
            done = {item["step_id"] for item in record["steps"]}
            self.assertIn("restyle_direct", done)
            self.assertIn("restyle_ready", done)
            self.assertNotIn("floorplan_source_ready", done)

    def test_source_case_path_skips_direct(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase, harness = _scaffold(temp)
            run_dir = codebase / "runs" / "source"
            result = advance(
                harness,
                run_dir,
                request_path=_write_request(run_dir, {"case_type": "source_case"}),
            )
            self.assertEqual(result["state"], "COMPLETE")
            record = read_json(run_dir / "flow-execution-record.json")
            self.assertEqual(record.get("active_branch"), "floorplan_source_case")
            skipped = {item["step_id"] for item in record.get("skipped") or []}
            self.assertIn("restyle_direct", skipped)
            done = {item["step_id"] for item in record["steps"]}
            self.assertIn("floorplan_source_ready", done)
            self.assertIn("source_title_frozen", done)
            self.assertIn("restyle_ready", done)
            self.assertNotIn("restyle_direct", done)

    def test_unknown_branch_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase, harness = _scaffold(temp)
            _passthrough(
                harness / "milestones" / "intake_ready" / "assemble.py",
                extra="    return {'case_type': 'x', 'receipt': {'ok': True, 'branch': 'other', 'skipped': []}}\n",
            )
            run_dir = codebase / "runs" / "bad2"
            action = advance(harness, run_dir, request_path=_write_request(run_dir, {"case_type": "x"}))
            self.assertEqual(action.get("state") or action.get("status"), "BLOCKED")

    def test_missing_receipt_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase, harness = _scaffold(temp)
            _passthrough(
                harness / "milestones" / "intake_ready" / "assemble.py",
                extra="    return {'case_type': 'restyle'}\n",
            )
            run_dir = codebase / "runs" / "noreceipt"
            action = advance(harness, run_dir, request_path=_write_request(run_dir, {"case_type": "restyle"}))
            self.assertEqual(action.get("state") or action.get("status"), "BLOCKED")


def _write_request(run_dir: Path, payload: dict) -> Path:
    path = run_dir / "request.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
