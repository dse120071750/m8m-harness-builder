from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from audit_harness import infer_schema_control, needs_judge
from flowstep_runtime import FlowError, read_json
from generate_harness import generate_tool, generate_v3_flow
from m8m_flowchart import render_flowchart, render_mermaid
from run_flow import advance
from validate_harness import validate_harness


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _ok_test(path: Path) -> None:
    _write(path, "def test_ok():\n    assert True\n")


def _for_assemble(path: Path) -> None:
    _write(
        path,
        "def run(input_data, draft=None, **_):\n"
        "    item = input_data.get('item') if isinstance(input_data.get('item'), dict) else {}\n"
        "    done = list(input_data.get('done') or [])\n"
        "    ledger = list(input_data.get('ledger') or [])\n"
        "    done2 = done + [item]\n"
        "    remaining = max(0, len(ledger) - len(done2))\n"
        "    return {\n"
        "        'item': item,\n"
        "        'receipt': {'ok': True, 'remaining': remaining, 'done': len(done2)},\n"
        "    }\n",
    )


def _judge_assemble(path: Path, fail_first: bool = False) -> None:
    flag = "True" if fail_first else "False"
    _write(
        path,
        "from pathlib import Path\n"
        "def run(input_data, draft=None, **_):\n"
        f"    fail_first = {flag}\n"
        "    marker = Path(__file__).with_name('_judge_attempt.txt')\n"
        "    attempt = int(marker.read_text()) if marker.is_file() else 0\n"
        "    attempt += 1\n"
        "    marker.write_text(str(attempt))\n"
        "    ok = True if not fail_first else attempt >= 2\n"
        "    return {\n"
        "        'label': 'ok',\n"
        "        'sentence': 'x',\n"
        "        'receipt': {'ok': ok, 'attempt': attempt, 'code': 'pass' if ok else 'fail'},\n"
        "    }\n",
    )


@unittest.skip("for replaced by cycle over a frozen ledger")
class ForLedgerTests(unittest.TestCase):
    def test_two_items_complete_eight_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            generate_tool(codebase, "ledger_receipt")
            generate_v3_flow(codebase, "loop_v1", ["source_ready", "pages_bound"], tools=["hash_bind"])
            harness = codebase / "flowsteps" / "flows" / "loop_v1"
            _write(
                harness / "schemas" / "source_ready_v1.json",
                json.dumps(
                    {
                        "type": "object",
                        "required": ["pages"],
                        "properties": {
                            "pages": {
                                "type": "array",
                                "maxItems": 7,
                                "items": {"type": "object"},
                            }
                        },
                    }
                ),
            )
            _write(
                harness / "schemas" / "page_item_v1.json",
                json.dumps({"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}),
            )
            _write(
                harness / "schemas" / "pages_bound_v1.json",
                json.dumps(
                    {
                        "type": "object",
                        "required": ["pages", "receipt"],
                        "properties": {
                            "pages": {"type": "array"},
                            "receipt": {"type": "object"},
                        },
                    }
                ),
            )
            _write(
                harness / "schemas" / "pages_bound_receipt_v1.json",
                json.dumps({"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}),
            )
            text = (harness / "flow.yaml").read_text(encoding="utf-8")
            text = text.replace(
                "    handler: milestones/pages_bound/assemble.py\n",
                "    handler: milestones/pages_bound/assemble.py\n"
                "    loop: for\n"
                "    worker: ledger_receipt\n"
                "    receipt_schema: schemas/pages_bound_receipt_v1.json\n"
                "    max_attempts: 8\n"
                "    ledger:\n"
                "      path: pages\n"
                "      item_schema: schemas/page_item_v1.json\n"
                "      max_items: 7\n",
            )
            (harness / "flow.yaml").write_text(text, encoding="utf-8")
            _write(
                harness / "milestones" / "source_ready" / "assemble.py",
                "def run(input_data, draft=None, **_):\n"
                "    req = input_data.get('request') if isinstance(input_data.get('request'), dict) else input_data\n"
                "    return {'pages': req.get('pages') or []}\n",
            )
            _ok_test(harness / "milestones" / "source_ready" / "tests" / "test_assemble.py")
            _for_assemble(harness / "milestones" / "pages_bound" / "assemble.py")
            _ok_test(harness / "milestones" / "pages_bound" / "tests" / "test_assemble.py")
            request = Path(temp) / "request.json"
            request.write_text(json.dumps({"pages": [{"id": "a"}, {"id": "b"}]}), encoding="utf-8")
            done = advance(harness, Path(temp) / "run-2", request_path=request)
            self.assertEqual(done["state"], "COMPLETE")
            out = read_json(Path(temp) / "run-2" / "artifacts" / "pages_bound.pages_bound_v1.json")
            self.assertEqual(len(out["data"]["pages"]), 2)
            self.assertTrue(out["data"]["receipt"]["ok"])
            self.assertEqual(out["data"]["receipt"]["remaining"], 0)

            request8 = Path(temp) / "request8.json"
            request8.write_text(json.dumps({"pages": [{"id": str(i)} for i in range(8)]}), encoding="utf-8")
            blocked = advance(harness, Path(temp) / "run-8", request_path=request8)
            self.assertEqual(blocked["state"], "BLOCKED")


class JudgeLoopTests(unittest.TestCase):
    def test_retries_until_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            generate_tool(codebase, "ok_receipt")
            generate_v3_flow(codebase, "judge_v1", ["card_aligned"], tools=["ok_receipt"])
            harness = codebase / "flowsteps" / "flows" / "judge_v1"
            _write(
                harness / "schemas" / "card_aligned_v1.json",
                json.dumps(
                    {
                        "type": "object",
                        "required": ["label", "sentence", "receipt"],
                        "properties": {
                            "label": {"type": "string"},
                            "sentence": {"type": "string"},
                            "receipt": {"type": "object"},
                        },
                    }
                ),
            )
            _write(
                harness / "schemas" / "card_aligned_receipt_v1.json",
                json.dumps({"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}),
            )
            text = (harness / "flow.yaml").read_text(encoding="utf-8")
            text = text.replace(
                "    handler: milestones/card_aligned/assemble.py\n",
                "    handler: milestones/card_aligned/assemble.py\n"
                "    loop: judge\n"
                "    worker: ok_receipt\n"
                "    receipt_schema: schemas/card_aligned_receipt_v1.json\n"
                "    max_attempts: 4\n",
            )
            (harness / "flow.yaml").write_text(text, encoding="utf-8")
            _judge_assemble(harness / "milestones" / "card_aligned" / "assemble.py", fail_first=True)
            _ok_test(harness / "milestones" / "card_aligned" / "tests" / "test_assemble.py")
            request = Path(temp) / "request.json"
            request.write_text(json.dumps({"kind": "image"}), encoding="utf-8")
            done = advance(harness, Path(temp) / "run-j", request_path=request)
            self.assertEqual(done["state"], "COMPLETE")
            out = read_json(Path(temp) / "run-j" / "artifacts" / "card_aligned.card_aligned_v1.json")
            self.assertTrue(out["data"]["receipt"]["ok"])
            self.assertGreaterEqual(out["data"]["receipt"]["attempt"], 2)

    def test_budget_exhausted_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            generate_tool(codebase, "ok_receipt")
            generate_v3_flow(codebase, "judge_fail_v1", ["card_aligned"], tools=["ok_receipt"])
            harness = codebase / "flowsteps" / "flows" / "judge_fail_v1"
            _write(
                harness / "schemas" / "card_aligned_v1.json",
                json.dumps({"type": "object", "additionalProperties": True}),
            )
            _write(
                harness / "schemas" / "card_aligned_receipt_v1.json",
                json.dumps({"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}),
            )
            text = (harness / "flow.yaml").read_text(encoding="utf-8")
            text = text.replace(
                "    handler: milestones/card_aligned/assemble.py\n",
                "    handler: milestones/card_aligned/assemble.py\n"
                "    loop: judge\n"
                "    worker: ok_receipt\n"
                "    receipt_schema: schemas/card_aligned_receipt_v1.json\n"
                "    max_attempts: 2\n",
            )
            (harness / "flow.yaml").write_text(text, encoding="utf-8")
            _write(
                harness / "milestones" / "card_aligned" / "assemble.py",
                "def run(input_data, draft=None, **_):\n"
                "    return {'receipt': {'ok': False, 'code': 'fail'}}\n",
            )
            _ok_test(harness / "milestones" / "card_aligned" / "tests" / "test_assemble.py")
            request = Path(temp) / "request.json"
            request.write_text("{}", encoding="utf-8")
            blocked = advance(harness, Path(temp) / "run-fail", request_path=request)
            self.assertEqual(blocked["state"], "BLOCKED")


class ReceiptGuardTests(unittest.TestCase):
    def test_missing_receipt_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            generate_tool(codebase, "ok_receipt")
            generate_v3_flow(codebase, "noreceipt_v1", ["card_aligned"], tools=["hash_bind"])
            harness = codebase / "flowsteps" / "flows" / "noreceipt_v1"
            _write(harness / "schemas" / "card_aligned_v1.json", json.dumps({"type": "object", "additionalProperties": True}))
            _write(
                harness / "schemas" / "card_aligned_receipt_v1.json",
                json.dumps({"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}),
            )
            text = (harness / "flow.yaml").read_text(encoding="utf-8")
            text = text.replace(
                "    handler: milestones/card_aligned/assemble.py\n",
                "    handler: milestones/card_aligned/assemble.py\n"
                "    loop: judge\n"
                "    worker: ok_receipt\n"
                "    receipt_schema: schemas/card_aligned_receipt_v1.json\n",
            )
            (harness / "flow.yaml").write_text(text, encoding="utf-8")
            _write(
                harness / "milestones" / "card_aligned" / "assemble.py",
                "def run(input_data, draft=None, **_):\n    return {'label': 'x'}\n",
            )
            _ok_test(harness / "milestones" / "card_aligned" / "tests" / "test_assemble.py")
            request = Path(temp) / "request.json"
            request.write_text("{}", encoding="utf-8")
            blocked = advance(harness, Path(temp) / "run-miss", request_path=request)
            self.assertEqual(blocked["state"], "BLOCKED")
            self.assertTrue(any("receipt" in str(item).lower() for item in blocked.get("blockers") or []))


class InferLoopTests(unittest.TestCase):
    def test_infer_for_from_previous_ledger(self) -> None:
        milestones = [
            {
                "id": "source_ready",
                "intelligence": "none",
                "tools": ["hash_bind"],
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "pages": {"type": "array", "maxItems": 7, "items": {"type": "object"}},
                    },
                },
            },
            {"id": "pages_bound", "intelligence": "none", "tools": ["hash_bind"], "output_schema": {}},
        ]
        infer_schema_control(milestones)
        self.assertEqual(milestones[1].get("on_cycle"), "pages")
        self.assertEqual(milestones[1]["cycle"]["ledger"], "source_ready")
        self.assertEqual(milestones[1]["worker"], "cycle_receipt")
        self.assertNotIn("next", milestones[0])

    def test_infer_judge_for_image_milestone(self) -> None:
        milestones = [
            {"id": "source_ready", "intelligence": "none", "tools": ["hash_bind"], "output_schema": {}},
            {"id": "card_aligned", "intelligence": "image", "tools": ["hash_bind"], "output_schema": {}},
        ]
        infer_schema_control(milestones)
        self.assertEqual(milestones[1]["loop"], "judge")
        self.assertEqual(milestones[1]["worker"], "ok_receipt")
        self.assertNotEqual(milestones[0].get("loop"), "judge")
        self.assertTrue(milestones[0].get("success"))
        self.assertIn("Retry until the worker receipt is ok", milestones[1]["success"])

    def test_does_not_judge_every_asset_milestone(self) -> None:
        milestones = [
            {"id": "source_ready", "intelligence": "none", "tools": ["hash_bind"], "output_schema": {}, "asset": {"kind": "file"}},
            {"id": "plan_frozen", "intelligence": "none", "tools": ["hash_bind"], "output_schema": {}, "asset": {"kind": "json"}},
            {"id": "release_packaged", "intelligence": "none", "tools": ["hash_bind"], "output_schema": {}, "asset": {"kind": "file"}},
        ]
        infer_schema_control(milestones)
        self.assertTrue(all(str(item.get("loop") or "none") != "judge" for item in milestones))
        self.assertTrue(all(item.get("success") for item in milestones))

    def test_needs_judge_is_quality_not_every_checkpoint(self) -> None:
        self.assertFalse(needs_judge({"id": "source_ready"}))
        self.assertFalse(needs_judge({"id": "plan_frozen"}))
        self.assertTrue(needs_judge({"id": "card_aligned"}))
        self.assertTrue(needs_judge({"id": "slot_generated"}))
        self.assertTrue(needs_judge({"id": "design_frozen", "intelligence": "image"}))


class ChartLoopTests(unittest.TestCase):
    def test_chart_labels_for_and_judge(self) -> None:
        items = [
            {"id": "source_ready", "intelligence": "none", "tools": ["hash_bind"]},
            {
                "id": "pages_bound",
                "intelligence": "none",
                "tools": ["hash_bind", "cycle_receipt"],
                "on_cycle": "pages",
                "cycle": {
                    "worker": "cycle_receipt",
                    "ledger": "source_ready",
                    "start": "pages_bound",
                    "pass": "row image exists",
                },
            },
            {
                "id": "card_aligned",
                "intelligence": "image",
                "tools": ["ok_receipt"],
                "loop": "judge",
                "worker": "ok_receipt",
            },
        ]
        mermaid = render_mermaid(items)
        self.assertIn("judge until ok", mermaid)
        self.assertNotIn("else BLOCKED", mermaid)
        text = render_flowchart(items, title="toy", flow_id="toy")
        self.assertIn("## Cycle", text)
        self.assertIn("## Judge (until ok)", text)
        self.assertNotIn("## Gates (if / else)", text)


@unittest.skip("for replaced by cycle over a frozen ledger")
class ValidateLoopTests(unittest.TestCase):
    def test_for_without_maxitems_on_previous_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            generate_tool(codebase, "ledger_receipt")
            generate_v3_flow(codebase, "badloop_v1", ["source_ready", "pages_bound"], tools=["hash_bind"])
            harness = codebase / "flowsteps" / "flows" / "badloop_v1"
            _write(
                harness / "schemas" / "source_ready_v1.json",
                json.dumps(
                    {
                        "type": "object",
                        "required": ["pages"],
                        "additionalProperties": False,
                        "properties": {"pages": {"type": "array"}},
                    }
                ),
            )
            _write(harness / "schemas" / "page_item_v1.json", json.dumps({"type": "object"}))
            _write(
                harness / "schemas" / "pages_bound_receipt_v1.json",
                json.dumps({"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}),
            )
            text = (harness / "flow.yaml").read_text(encoding="utf-8")
            text = text.replace(
                "    handler: milestones/pages_bound/assemble.py\n",
                "    handler: milestones/pages_bound/assemble.py\n"
                "    loop: for\n"
                "    worker: ledger_receipt\n"
                "    receipt_schema: schemas/pages_bound_receipt_v1.json\n"
                "    ledger:\n"
                "      path: pages\n"
                "      item_schema: schemas/page_item_v1.json\n"
                "      max_items: 7\n",
            )
            (harness / "flow.yaml").write_text(text, encoding="utf-8")
            with self.assertRaises(FlowError) as ctx:
                validate_harness(codebase=codebase, flow_id="badloop_v1")
            self.assertIn("maxItems", str(ctx.exception))
