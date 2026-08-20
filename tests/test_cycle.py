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
from session_layout import load_ledger


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _ok_test(path: Path) -> None:
    _write(path, "def test_ok():\n    assert True\n")


CYCLE_SPEC = [
    {
        "id": "pages_ledger_frozen",
        "asset": {"kind": "json"},
        "tools": ["hash_bind"],
        "output_schema_object": {
            "type": "object",
            "additionalProperties": False,
            "required": ["rows"],
            "properties": {
                "rows": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {"type": "object"},
                }
            },
        },
    },
    {
        "id": "page_bound",
        "on_cycle": "pages",
        "asset": {"kind": "json"},
        "tools": ["hash_bind"],
        "output_schema_object": {
            "type": "object",
            "additionalProperties": False,
            "required": ["page"],
            "properties": {"page": {"type": "string"}},
        },
    },
    {
        "id": "page_rendered",
        "on_cycle": "pages",
        "asset": {"kind": "json"},
        "intelligence": "completion",
        "tools": ["cycle_receipt"],
        "cycle": {
            "id": "pages",
            "worker": "cycle_receipt",
            "ledger": "pages_ledger_frozen",
            "start": "page_bound",
            "join": "release_packaged",
            "max_rounds": 8,
            "pass": "current row has a bound page string",
        },
        "output_schema_object": {
            "type": "object",
            "additionalProperties": False,
            "required": ["page", "receipt"],
            "properties": {
                "page": {"type": "string"},
                "receipt": {"type": "object"},
            },
        },
    },
    {
        "id": "release_packaged",
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


def _assemble(path: Path, body: str) -> None:
    _write(path, "def run(input_data, draft=None, **_):\n" + body)


def _scaffold(temp: str, *, fail_first: bool = False) -> tuple[Path, Path]:
    codebase = Path(temp) / "repo"
    generate_tool(codebase, "hash_bind")
    generate_tool(codebase, "cycle_receipt")
    generate_v3_flow(
        codebase,
        "cycle_v1",
        [item["id"] for item in CYCLE_SPEC],
        tools=["hash_bind", "cycle_receipt"],
        milestone_specs=CYCLE_SPEC,
    )
    harness = codebase / "flowsteps" / "flows" / "cycle_v1"
    _assemble(
        harness / "milestones" / "pages_ledger_frozen" / "assemble.py",
        "    req = input_data.get('request') if isinstance(input_data.get('request'), dict) else input_data\n"
        "    return {'rows': req.get('rows') or [{'id': '001'}, {'id': '002'}]}\n",
    )
    _assemble(
        harness / "milestones" / "page_bound" / "assemble.py",
        "    row = input_data.get('row') or '001'\n"
        "    return {'page': 'p-' + str(row)}\n",
    )
    fail = "True" if fail_first else "False"
    _assemble(
        harness / "milestones" / "page_rendered" / "assemble.py",
        "    from pathlib import Path\n"
        f"    fail_first = {fail}\n"
        "    marker = Path(__file__).with_name('_cycle_round.txt')\n"
        "    n = int(marker.read_text()) if marker.is_file() else 0\n"
        "    n += 1\n"
        "    marker.write_text(str(n))\n"
        "    row = input_data.get('row') or '001'\n"
        "    chosen = 'fail' if fail_first and n == 1 else 'pass'\n"
        "    return {'page': 'p-' + str(row), 'receipt': {'ok': True, 'cycle': chosen, 'row': row, 'reason': chosen}}\n",
    )
    _assemble(
        harness / "milestones" / "release_packaged" / "assemble.py",
        "    return {'ready': True}\n",
    )
    for mid in [item["id"] for item in CYCLE_SPEC]:
        _ok_test(harness / "milestones" / mid / "tests" / "test_assemble.py")
        _write(
            harness / "milestones" / mid / "input.schema.json",
            json.dumps({"type": "object", "additionalProperties": True}),
        )
    _write(
        harness / "milestones" / "page_rendered" / "draft.schema.json",
        json.dumps({"type": "object", "additionalProperties": True}),
    )
    return codebase, harness


def _request(run_dir: Path, rows: list[dict] | None = None) -> Path:
    path = run_dir / "request.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rows": rows or [{"id": "001"}, {"id": "002"}]}), encoding="utf-8")
    return path


class CycleChartTests(unittest.TestCase):
    def test_chart_lists_ledger_and_wrap(self) -> None:
        text = render_flowchart(CYCLE_SPEC, title="cycle", flow_id="cycle_v1")
        self.assertIn("## Cycle", text)
        self.assertIn("pages_ledger_frozen", text)
        self.assertIn("cycle_receipt", text)


class CycleRunTests(unittest.TestCase):
    def test_two_rows_pass_preserves_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase, harness = _scaffold(temp)
            run_dir = codebase / "runs" / "ok"
            result = advance(harness, run_dir, request_path=_request(run_dir))
            self.assertEqual(result["state"], "COMPLETE", result)
            ledger = load_ledger(run_dir, "pages")
            self.assertIsNotNone(ledger)
            statuses = [row["status"] for row in ledger["rows"]]
            self.assertEqual(statuses, ["done", "done"])
            self.assertTrue((run_dir / "milestones" / "page_rendered" / "items" / "001").is_dir())
            self.assertTrue((run_dir / "milestones" / "page_rendered" / "items" / "002").is_dir())
            record = read_json(run_dir / "flow-execution-record.json")
            done = {item["step_id"] for item in record["steps"]}
            self.assertIn("release_packaged", done)

    def test_fail_purges_and_redoes_same_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase, harness = _scaffold(temp, fail_first=True)
            run_dir = codebase / "runs" / "fail"
            result = advance(harness, run_dir, request_path=_request(run_dir))
            self.assertEqual(result["state"], "COMPLETE", result)
            ledger = load_ledger(run_dir, "pages")
            statuses = [row["status"] for row in ledger["rows"]]
            self.assertEqual(statuses, ["done", "done"])
            # first attempt failed: items/001 should still exist from the later pass
            self.assertTrue((run_dir / "milestones" / "page_rendered" / "items" / "001").is_dir())

    def test_missing_receipt_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase, harness = _scaffold(temp)
            _assemble(
                harness / "milestones" / "page_rendered" / "assemble.py",
                "    return {'page': 'p'}\n",
            )
            run_dir = codebase / "runs" / "noreceipt"
            action = advance(harness, run_dir, request_path=_request(run_dir))
            self.assertEqual(action.get("state"), "BLOCKED")
