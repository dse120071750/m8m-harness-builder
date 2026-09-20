from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from generate_harness import generate_tool, generate_v3_flow
from run_flow import advance
from flowstep_runtime import read_json
from flowstep_tools import validate_library_tool
from session_layout import find_paused_run, load_run_roster, resolve_chosen_output, wait_draft_path


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _ok_test(path: Path) -> None:
    _write(path, "def test_ok():\n    assert True\n")


WAIT_SPEC = [
    {
        "id": "source_ready",
        "success": "The source readiness record is available.",
        "output_contract": "source_ready_v1",
        "outputs": [
            {
                "id": "result",
                "name": "Source readiness",
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
                "ref": "handler.wait_v1.source_ready@3.1.0"
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
    {
        "id": "response_ready",
        "success": "One non-empty operator reply is available for the next milestone.",
        "output_contract": "response_ready_v1",
        "outputs": [
            {
                "id": "result",
                "name": "Operator reply",
                "kind": "json",
                "cardinality": "one",
                "required": True,
            }
        ],
        "asset": {"kind": "json"},
        "intelligence": "completion",
        "model_justification": "The operator reply arrives after a paused run.",
        "input_schema": "milestones/response_ready/input.schema.json",
        "draft_schema": "milestones/response_ready/draft.schema.json",
        "loop": "judge",
        "worker": "response_ready_judge@3.1.0",
        "judge_abi": "m8m_milestone_judge_v1",
        "receipt_schema": "schemas/response_ready_judge_v1.json",
        "max_attempts": 3,
        "tools": ["hash_bind"],
        "flowsteps": [
            {"id": "hash_bind", "tool": "hash_bind@1.0.0"}
        ],
        "execution": {
            "candidate_executor": {
                "ref": "handler.wait_v1.response_ready@3.1.0",
                "profile": {
                    "ref": "agent_profile.wait_v1.response_ready.candidate.v1",
                    "model_configuration": {
                        "model": "codex",
                        "reasoning": "low",
                    },
                    "token_budget": {
                        "max_input_tokens": 2048,
                        "max_output_tokens": 512,
                    },
                    "timeout_seconds": 60,
                    "tools": ["hash_bind"],
                    "capabilities": [],
                },
            },
            "judge": {"ref": "response_ready_judge@3.1.0"},
            "tool_bindings": [
                {"tool": "hash_bind", "ref": "hash_bind@1.0.0"}
            ],
        },
        "output_schema_object": {
            "type": "object",
            "additionalProperties": False,
            "required": ["reply", "receipt"],
            "properties": {
                "reply": {"type": "string"},
                "receipt": {"type": "object"},
            },
        },
    },
    {
        "id": "plan_frozen",
        "success": "The plan is frozen after the accepted reply.",
        "output_contract": "plan_frozen_v1",
        "outputs": [
            {
                "id": "result",
                "name": "Frozen plan",
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
                "ref": "handler.wait_v1.plan_frozen@3.1.0"
            },
            "tool_bindings": [
                {"tool": "hash_bind", "ref": "hash_bind@1.0.0"}
            ],
        },
        "output_schema_object": {
            "type": "object",
            "additionalProperties": False,
            "required": ["plan"],
            "properties": {"plan": {"type": "string"}},
        },
    },
]


def _assemble(path: Path, body: str) -> None:
    _write(path, "def run(input_data, draft=None, run_dir=None, **_):\n" + body)


def _scaffold(temp: str) -> tuple[Path, Path]:
    codebase = Path(temp) / "repo"
    generate_tool(codebase, "hash_bind")
    generate_tool(codebase, "response_ready_judge")
    generate_v3_flow(
        codebase,
        "wait_v1",
        [item["id"] for item in WAIT_SPEC],
        tools=["hash_bind"],
        milestone_specs=WAIT_SPEC,
    )
    harness = codebase / "flowsteps" / "flows" / "wait_v1"
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
        harness / "schemas" / "response_ready_judge_v1.json",
        json.dumps(judge_schema),
    )
    judge_dir = codebase / "flowsteps" / "tools" / "response_ready_judge"
    _write(
        judge_dir / "tool.py",
        "def run(input_data, **_):\n"
        "    reply = input_data['candidate']['outputs']['result'].get('reply')\n"
        "    passed = isinstance(reply, str) and bool(reply.strip())\n"
        "    return {'decision': 'PASS' if passed else 'BLOCKED', "
        "'reasons': ['reply checked'], 'blockers': [] if passed else ['reply missing']}\n",
    )
    _write(judge_dir / "output.schema.json", json.dumps(judge_schema))
    (judge_dir / "BUILD_REQUIRED").unlink()
    blockers = validate_library_tool(codebase, "response_ready_judge")
    if blockers:
        raise AssertionError(f"invalid response_ready_judge fixture: {blockers}")
    generate_v3_flow(
        codebase,
        "wait_v1",
        [item["id"] for item in WAIT_SPEC],
        tools=["hash_bind"],
        milestone_specs=WAIT_SPEC,
    )
    _assemble(
        harness / "milestones" / "source_ready" / "assemble.py",
        "    return {'outputs': {'result': {'ready': True}}}\n",
    )
    _assemble(
        harness / "milestones" / "response_ready" / "assemble.py",
        "    if not isinstance(draft, dict) or not draft.get('reply'):\n"
        "        return {'_flowstep': 'NEED_MODEL', 'model': 'completion',\n"
        "                'model_request': {'instruction': 'wait for reply'}}\n"
        "    return {'outputs': {'result': {'reply': str(draft['reply']), 'receipt': {'accepted': True}}}}\n",
    )
    _assemble(
        harness / "milestones" / "plan_frozen" / "assemble.py",
        "    return {'outputs': {'result': {'plan': 'ok'}}}\n",
    )
    for mid in [item["id"] for item in WAIT_SPEC]:
        _ok_test(harness / "milestones" / mid / "tests" / "test_assemble.py")
        _write(
            harness / "milestones" / mid / "input.schema.json",
            json.dumps({"type": "object", "additionalProperties": True}),
        )
    _write(
        harness / "milestones" / "response_ready" / "draft.schema.json",
        json.dumps({"type": "object", "additionalProperties": True}),
    )
    return codebase, harness


def _request(run_dir: Path) -> Path:
    path = run_dir / "request.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    return path


class RunRosterTests(unittest.TestCase):
    def test_init_freezes_unfinished_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase, harness = _scaffold(temp)
            run_dir = codebase / "runs" / "init"
            parked = advance(harness, run_dir, request_path=_request(run_dir))
            self.assertEqual(parked["state"], "ACTION_REQUIRED")
            roster = load_run_roster(run_dir)
            self.assertEqual(roster["schema"], "m8m_run_roster_v1")
            by_id = {row["id"]: row["status"] for row in roster["rows"]}
            self.assertEqual(by_id["source_ready"], "done")
            self.assertEqual(by_id["response_ready"], "waiting")
            self.assertEqual(by_id["plan_frozen"], "unfinished")
            self.assertEqual(roster["status"], "paused")
            self.assertEqual(roster["current"], "response_ready")
            self.assertTrue((run_dir / "roster.json").is_file())
            self.assertFalse((run_dir / "ledger.json").is_file())
            self.assertFalse((run_dir / "cycles").exists() and any((run_dir / "cycles").iterdir()))

    def test_wait_pauses_then_resume_judges_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase, harness = _scaffold(temp)
            run_dir = codebase / "runs" / "pause"
            first = advance(harness, run_dir, request_path=_request(run_dir))
            self.assertEqual(first["state"], "ACTION_REQUIRED")
            self.assertEqual(first["action"], "wait_for_response")
            self.assertTrue(first.get("paused"))
            self.assertEqual(first["draft_path"], "milestones/response_ready/work/draft.json")
            prompt = (harness / "references/response_ready.md").read_text(encoding="utf-8").strip()
            model_request = read_json(run_dir / first["model_request_path"])
            self.assertTrue(model_request["instruction"].startswith(prompt))
            self.assertEqual(first.get("roster_path"), "roster.json")
            roster = load_run_roster(run_dir)
            self.assertEqual(roster["status"], "paused")
            found = find_paused_run(codebase, "wait_v1")
            self.assertEqual(found.resolve(), run_dir.resolve())
            capsule = run_dir / first["context_capsule_path"]
            capsule_bytes = capsule.read_bytes()

            again = advance(harness, run_dir)
            self.assertEqual(again["state"], "ACTION_REQUIRED")
            self.assertEqual(again["context_capsule_path"], first["context_capsule_path"])
            self.assertEqual(capsule.read_bytes(), capsule_bytes)
            self.assertEqual(load_run_roster(run_dir)["status"], "paused")

            slot = wait_draft_path(run_dir, "response_ready")
            slot.parent.mkdir(parents=True, exist_ok=True)
            slot.write_text(json.dumps({"reply": "ship it"}), encoding="utf-8")
            done = advance(harness, run_dir)
            self.assertEqual(done["state"], "COMPLETE", done)
            roster = load_run_roster(run_dir)
            self.assertEqual(roster["status"], "complete")
            statuses = [row["status"] for row in roster["rows"]]
            self.assertEqual(statuses, ["done", "done", "done"])
            chosen = resolve_chosen_output(run_dir, "response_ready", output_id="result")
            self.assertEqual(chosen["reply"], "ship it")

    def test_resume_via_draft_arg(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase, harness = _scaffold(temp)
            run_dir = codebase / "runs" / "draftarg"
            advance(harness, run_dir, request_path=_request(run_dir))
            draft = Path(temp) / "reply.json"
            draft.write_text(json.dumps({"reply": "yes"}), encoding="utf-8")
            done = advance(harness, run_dir, draft_path=draft)
            self.assertEqual(done["state"], "COMPLETE", done)
            self.assertEqual(load_run_roster(run_dir)["status"], "complete")

    def test_complete_run_marks_roster_done(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp) / "repo"
            generate_tool(codebase, "hash_bind")
            generate_v3_flow(
                codebase,
                "plain_v1",
                ["source_ready"],
                tools=["hash_bind"],
                milestone_specs=[
                    {
                        "id": "source_ready",
                        "success": "One run-local source file is available.",
                        "output_contract": "source_ready_v1",
                        "outputs": [
                            {
                                "id": "result",
                                "name": "Run-local source file",
                                "kind": "file",
                                "cardinality": "one",
                                "required": True,
                            }
                        ],
                        "asset": {"kind": "file"},
                        "tools": ["hash_bind"],
                        "flowsteps": [
                            {
                                "id": "hash_bind",
                                "tool": "hash_bind@1.0.0",
                            }
                        ],
                        "execution": {
                            "candidate_executor": {
                                "ref": "handler.plain_v1.source_ready@3.1.0"
                            },
                            "tool_bindings": [
                                {
                                    "tool": "hash_bind",
                                    "ref": "hash_bind@1.0.0",
                                }
                            ],
                        },
                        "output_schema_object": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["path"],
                            "properties": {
                                "path": {"type": "string"}
                            },
                        },
                    }
                ],
            )
            harness = codebase / "flowsteps" / "flows" / "plain_v1"
            src = Path(temp) / "file.bin"
            src.write_bytes(b"abc")
            _assemble(
                harness / "milestones" / "source_ready" / "assemble.py",
                "    import shutil\n"
                "    from pathlib import Path\n"
                f"    SRC = r'''{src}'''\n"
                "    local = Path(run_dir) / 'work' / 'source.bin'\n"
                "    local.parent.mkdir(parents=True, exist_ok=True)\n"
                "    shutil.copy2(SRC, local)\n"
                "    return {'outputs': {'result': {'path': str(local)}}}\n",
            )
            _ok_test(harness / "milestones" / "source_ready" / "tests" / "test_assemble.py")
            run_dir = Path(temp) / "run-plain"
            done = advance(harness, run_dir, request_path=_request(run_dir))
            self.assertEqual(done["state"], "COMPLETE", done)
            roster = load_run_roster(run_dir)
            self.assertEqual(roster["status"], "complete")
            self.assertEqual(roster["rows"][0]["status"], "done")


if __name__ == "__main__":
    unittest.main()
