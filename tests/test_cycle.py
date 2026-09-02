from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import support  # noqa: F401
import run_flow

from generate_harness import generate_tool, generate_v3_flow
from m8m_flowchart import render_flowchart
from run_flow import advance, replace_milestone_state
from flowstep_runtime import load_flow
from flowstep_runtime import read_json
from flowstep_tools import validate_library_tool
from session_layout import load_ledger


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _ok_test(path: Path) -> None:
    _write(path, "def test_ok():\n    assert True\n")


CYCLE_SPEC = [
    {
        "id": "pages_ledger_frozen",
        "success": "The bounded page ledger rows are frozen for this run.",
        "output_contract": "pages_ledger_frozen_v1",
        "outputs": [
            {
                "id": "result",
                "name": "Frozen page ledger",
                "kind": "json",
                "cardinality": "one",
                "required": True,
            }
        ],
        "asset": {"kind": "json"},
        "tools": ["hash_bind"],
        "flowsteps": [
            {"id": "hash_bind", "tool": "hash_bind@1.0.0"}
        ],
        "execution": {
            "candidate_executor": {
                "ref": "handler.cycle_v1.pages_ledger_frozen@3.1.0"
            },
            "tool_bindings": [
                {"tool": "hash_bind", "ref": "hash_bind@1.0.0"}
            ],
        },
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
        "success": "The current ledger row is bound to one page identifier.",
        "output_contract": "page_bound_v1",
        "outputs": [
            {
                "id": "result",
                "name": "Bound page",
                "kind": "json",
                "cardinality": "one",
                "required": True,
            }
        ],
        "on_cycle": "pages",
        "asset": {"kind": "json"},
        "tools": ["hash_bind"],
        "flowsteps": [
            {"id": "hash_bind", "tool": "hash_bind@1.0.0"}
        ],
        "execution": {
            "candidate_executor": {
                "ref": "handler.cycle_v1.page_bound@3.1.0"
            },
            "tool_bindings": [
                {"tool": "hash_bind", "ref": "hash_bind@1.0.0"}
            ],
        },
        "output_schema_object": {
            "type": "object",
            "additionalProperties": False,
            "required": ["page"],
            "properties": {"page": {"type": "string"}},
        },
    },
    {
        "id": "page_rendered",
        "success": "The current page result is ready for cycle control.",
        "output_contract": "page_rendered_v1",
        "outputs": [
            {
                "id": "result",
                "name": "Rendered page result",
                "kind": "json",
                "cardinality": "one",
                "required": True,
            }
        ],
        "on_cycle": "pages",
        "asset": {"kind": "json"},
        "intelligence": "none",
        "tools": ["cycle_receipt"],
        "flowsteps": [
            {
                "id": "cycle_receipt",
                "tool": "cycle_receipt@1.0.0",
            }
        ],
        "execution": {
            "candidate_executor": {
                "ref": "handler.cycle_v1.page_rendered@3.1.0"
            },
            "tool_bindings": [
                {
                    "tool": "cycle_receipt",
                    "ref": "cycle_receipt@1.0.0",
                }
            ],
        },
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
            "required": ["page", "ready"],
            "properties": {
                "page": {"type": "string"},
                "ready": {"type": "boolean"},
            },
        },
    },
    {
        "id": "release_packaged",
        "success": "The release package is marked ready after all cycle rows pass.",
        "output_contract": "release_packaged_v1",
        "outputs": [
            {
                "id": "result",
                "name": "Release readiness",
                "kind": "json",
                "cardinality": "one",
                "required": True,
            }
        ],
        "asset": {"kind": "json"},
        "tools": ["hash_bind"],
        "flowsteps": [
            {"id": "hash_bind", "tool": "hash_bind@1.0.0"}
        ],
        "execution": {
            "candidate_executor": {
                "ref": "handler.cycle_v1.release_packaged@3.1.0"
            },
            "tool_bindings": [
                {"tool": "hash_bind", "ref": "hash_bind@1.0.0"}
            ],
        },
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


def _scaffold(
    temp: str,
    *,
    fail_first: bool = False,
    cache_page_bound: bool = False,
    judge_page_bound: bool = False,
) -> tuple[Path, Path]:
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
    if cache_page_bound:
        flow_path = harness / "flow.yaml"
        flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
        page_bound = next(item for item in flow["milestones"] if item["id"] == "page_bound")
        page_bound["cache"] = {"reuse": "candidate", "ttl_seconds": 3600, "side_effects": "none"}
        flow_path.write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
    if judge_page_bound:
        generate_tool(codebase, "page_bound_judge")
        flow_path = harness / "flow.yaml"
        flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
        page_bound = next(item for item in flow["milestones"] if item["id"] == "page_bound")
        page_bound["loop"] = "judge"
        page_bound["worker"] = "page_bound_judge@3.1.0"
        page_bound["judge_abi"] = "m8m_milestone_judge_v1"
        page_bound["receipt_schema"] = "schemas/page_bound_receipt_v1.json"
        page_bound["max_attempts"] = 1
        page_bound["execution"]["judge"] = {
            "ref": "page_bound_judge@3.1.0"
        }
        flow_path.write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
        judge_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision", "reasons", "blockers"],
            "properties": {
                "decision": {"enum": ["PASS", "RETRY", "BLOCKED"]},
                "reasons": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "items": {"type": "string"},
                },
                "blockers": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {"type": "string"},
                },
            },
        }
        _write(
            harness / "schemas" / "page_bound_receipt_v1.json",
            json.dumps(judge_schema),
        )
        judge_dir = codebase / "flowsteps" / "tools" / "page_bound_judge"
        _write(
            judge_dir / "tool.py",
            "def run(input_data, **_):\n"
            "    return {'decision': 'PASS', 'reasons': ['page bound'], 'blockers': []}\n",
        )
        _write(judge_dir / "output.schema.json", json.dumps(judge_schema))
        (judge_dir / "BUILD_REQUIRED").unlink()
        blockers = validate_library_tool(codebase, "page_bound_judge")
        if blockers:
            raise AssertionError(f"invalid page_bound_judge fixture: {blockers}")
    _assemble(
        harness / "milestones" / "pages_ledger_frozen" / "assemble.py",
        "    req = input_data.get('request') if isinstance(input_data.get('request'), dict) else input_data\n"
        "    return {'outputs': {'result': {'rows': req.get('rows') or [{'id': '001'}, {'id': '002'}]}}}\n",
    )
    _assemble(
        harness / "milestones" / "page_bound" / "assemble.py",
        "    from pathlib import Path\n"
        "    marker = Path(__file__).with_name('_handler_calls.txt')\n"
        "    calls = int(marker.read_text()) + 1 if marker.is_file() else 1\n"
        "    marker.write_text(str(calls))\n"
        "    row = input_data.get('row') or '001'\n"
        "    return {'outputs': {'result': {'page': 'p-' + str(row)}}}\n",
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
        "    ready = not (fail_first and n == 1)\n"
        "    return {'outputs': {'result': {'page': 'p-' + str(row), 'ready': ready}}}\n",
    )
    _assemble(
        harness / "milestones" / "release_packaged" / "assemble.py",
        "    return {'outputs': {'result': {'ready': True}}}\n",
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
    def test_cycle_resume_promotes_committed_row_after_ledger_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase, harness = _scaffold(temp)
            run_dir = codebase / "runs" / "cycle-ledger-crash"
            real_promote = run_flow.promote_cycle_round
            interrupted = False

            def promote_then_interrupt(*args, **kwargs):
                nonlocal interrupted
                slot = real_promote(*args, **kwargs)
                if not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt("simulated crash before ledger promotion")
                return slot

            with patch.object(
                run_flow,
                "promote_cycle_round",
                side_effect=promote_then_interrupt,
            ):
                with self.assertRaisesRegex(KeyboardInterrupt, "simulated crash"):
                    advance(harness, run_dir, request_path=_request(run_dir))

            ledger = load_ledger(run_dir, "pages")
            self.assertEqual([row["status"] for row in ledger["rows"]], ["unfinished", "unfinished"])
            self.assertTrue(
                (run_dir / "milestones" / "page_rendered" / "items" / "001" / "out").is_dir()
            )
            self.assertEqual(
                (harness / "milestones" / "page_rendered" / "_cycle_round.txt").read_text(),
                "1",
            )
            row_one_request = (
                run_dir
                / "runtime-tasks"
                / "page_bound"
                / "items"
                / "001"
                / "attempt-001"
                / "candidate-request.json"
            )
            row_one_bytes = row_one_request.read_bytes()

            result = advance(harness, run_dir)

            self.assertEqual(result["state"], "COMPLETE", result)
            ledger = load_ledger(run_dir, "pages")
            self.assertEqual([row["status"] for row in ledger["rows"]], ["done", "done"])
            self.assertEqual(
                (harness / "milestones" / "page_rendered" / "_cycle_round.txt").read_text(),
                "2",
            )
            self.assertEqual(row_one_request.read_bytes(), row_one_bytes)
            self.assertTrue(
                (
                    run_dir
                    / "runtime-tasks"
                    / "page_bound"
                    / "items"
                    / "002"
                    / "attempt-001"
                    / "candidate-request.json"
                ).is_file()
            )
            for row_id in ("001", "002"):
                self.assertTrue(
                    (run_dir / "milestones" / "page_rendered" / "items" / row_id / "out").is_dir()
                )

    def test_cycle_judge_budget_resets_after_each_successful_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase, harness = _scaffold(temp, judge_page_bound=True)
            run_dir = codebase / "runs" / "per-row-judge-budget"

            result = advance(harness, run_dir, request_path=_request(run_dir))

            self.assertEqual(result["state"], "COMPLETE", result)
            self.assertFalse((run_dir / "work" / "page_bound" / "judge_state.json").exists())

    def test_upstream_replacement_clears_wrapped_cycle_judge_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase, harness = _scaffold(temp)
            run_dir = codebase / "runs" / "cycle-budget-reset"
            first = advance(harness, run_dir, request_path=_request(run_dir))
            self.assertEqual(first["state"], "COMPLETE", first)
            stale = run_dir / "work" / "page_bound" / "judge_state.json"
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_text('{"attempts": 4}\n', encoding="utf-8")

            flow = load_flow(harness, harness / "flow.yaml")
            affected = replace_milestone_state(run_dir, flow, "pages_ledger_frozen")

            self.assertIn("page_bound", affected)
            self.assertIn("page_rendered", affected)
            self.assertFalse(stale.exists())

    def test_row_candidates_cache_independently_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase, harness = _scaffold(temp, cache_page_bound=True)
            first_run = codebase / "runs" / "cache-first"
            second_run = codebase / "runs" / "cache-second"
            first = advance(
                harness,
                first_run,
                request_path=_request(first_run),
                cache_mode="read-write",
                cache_namespace="tenant-a",
            )
            second = advance(
                harness,
                second_run,
                request_path=_request(second_run),
                cache_mode="read-write",
                cache_namespace="tenant-a",
            )
            self.assertEqual(first["state"], "COMPLETE", first)
            self.assertEqual(second["state"], "COMPLETE", second)
            marker = harness / "milestones" / "page_bound" / "_handler_calls.txt"
            self.assertEqual(marker.read_text(encoding="utf-8"), "2")
            for row_id in ("001", "002"):
                receipt = read_json(
                    second_run
                    / "milestones"
                    / "page_bound"
                    / "items"
                    / row_id
                    / "work"
                    / "cache-receipt.json"
                )
                self.assertEqual(receipt["lookup_status"], "hit")
                self.assertEqual(receipt["cycle_row"], row_id)

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

    def test_replacing_upstream_with_completed_cycle_joins_without_empty_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase, harness = _scaffold(temp)
            run_dir = codebase / "runs" / "completed-cycle-replaced-upstream"
            first = advance(harness, run_dir, request_path=_request(run_dir))
            self.assertEqual(first["state"], "COMPLETE", first)

            resumed = advance(
                harness,
                run_dir,
                replace_milestone="pages_ledger_frozen",
            )

            self.assertEqual(resumed["state"], "COMPLETE", resumed)
            ledger = load_ledger(run_dir, "pages")
            self.assertEqual(
                [row["status"] for row in ledger["rows"]],
                ["done", "done"],
            )
            record = read_json(run_dir / "flow-execution-record.json")
            self.assertTrue(record.get("cycle_done"))
            self.assertEqual(record.get("cycle_round"), 2)

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
            page_bound_tasks = run_dir / "runtime-tasks" / "page_bound" / "items"
            self.assertTrue(
                (page_bound_tasks / "001" / "attempt-001" / "candidate-request.json").is_file()
            )
            self.assertTrue(
                (page_bound_tasks / "001" / "attempt-002" / "candidate-request.json").is_file()
            )
            self.assertTrue(
                (page_bound_tasks / "002" / "attempt-001" / "candidate-request.json").is_file()
            )
            self.assertFalse((page_bound_tasks / "002" / "attempt-002").exists())

    def test_candidate_provided_cycle_receipt_blocks_before_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase, harness = _scaffold(temp)
            _assemble(
                harness / "milestones" / "page_rendered" / "assemble.py",
                "    return {'outputs': {'result': {'page': 'p', 'ready': True}}, "
                "'receipt': {'ok': True, 'cycle': 'pass'}}\n",
            )
            run_dir = codebase / "runs" / "noreceipt"
            action = advance(harness, run_dir, request_path=_request(run_dir))
            self.assertEqual(action.get("state"), "BLOCKED")

    def test_cycle_budget_exhaustion_preserves_real_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase, harness = _scaffold(temp)
            flow_path = harness / "flow.yaml"
            flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
            owner = next(item for item in flow["milestones"] if item["id"] == "page_rendered")
            owner["cycle"]["max_rounds"] = 1
            flow_path.write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
            _assemble(
                harness / "milestones" / "page_rendered" / "assemble.py",
                "    row = input_data.get('row') or '001'\n"
                "    return {'outputs': {'result': {'page': 'p-' + str(row), 'ready': False}}}\n",
            )
            run_dir = codebase / "runs" / "budget-exhausted"

            action = advance(harness, run_dir, request_path=_request(run_dir))

            self.assertEqual(action.get("state"), "BLOCKED")
            self.assertTrue(
                any("cycle budget 1 exhausted" in blocker for blocker in action.get("blockers") or []),
                action,
            )
            self.assertFalse(
                any("immutable output already exists" in blocker for blocker in action.get("blockers") or []),
                action,
            )
