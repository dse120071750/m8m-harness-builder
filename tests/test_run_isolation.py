from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import yaml

import support  # noqa: F401

from flowstep_runtime import FlowError, action_schema_path, read_json, validate_against_schema
from run_flow import advance, main as run_main
from run_goal import advance_goal
from session_layout import resolve_chosen_output


OPEN_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
}

CANDIDATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["outputs"],
    "properties": {
        "outputs": {
            "type": "object",
            "additionalProperties": False,
            "required": ["result"],
            "properties": {"result": {"type": "object", "additionalProperties": True}},
        },
        "receipt": {"type": "object"},
    },
}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _request(path: Path, message: str = "hello") -> Path:
    _write_json(path, {"message": message})
    return path


def _handler(path: Path, milestone: str, *, version: str = "v1", invalid: bool = False) -> None:
    result = "{}" if invalid else (
        "{'outputs': {'result': {'message': input_data.get('request', input_data.get('source')), "
        f"'version': '{version}', 'call': count}}}}}}"
    )
    path.write_text(
        "from pathlib import Path\n"
        "def run(input_data, draft=None, run_dir=None, **_):\n"
        f"    marker = Path(__file__).with_name('_{milestone}_calls.txt')\n"
        "    count = int(marker.read_text()) + 1 if marker.is_file() else 1\n"
        "    marker.write_text(str(count))\n"
        f"    return {result}\n",
        encoding="utf-8",
    )


def _scaffold(
    root: Path,
    *,
    two_steps: bool = False,
    model: bool = False,
    model_second: bool = False,
    invalid: bool = False,
) -> tuple[Path, Path]:
    codebase = root / "repo"
    harness = codebase / "flowsteps" / "flows" / "isolation_v1"
    harness.mkdir(parents=True)
    milestones = [
        {
            "id": "source_ready",
            "success": "The source is accepted.",
            "output_contract": "source_ready_v1",
            "output_schema": "schemas/candidate.json",
            "outputs": [
                {"id": "result", "name": "Result", "kind": "json", "cardinality": "one", "required": True}
            ],
            "handler": "source.py",
            "input_schema": "schemas/input.json",
            "inputs": {"request": "user.request"},
            "intelligence": "completion" if model else "none",
            "on_tool_fail": "need_model",
        }
    ]
    if model:
        milestones[0]["draft_schema"] = "schemas/draft.json"
    if two_steps:
        milestones.append(
            {
                "id": "result_ready",
                "success": "The result is accepted.",
                "output_contract": "result_ready_v1",
                "output_schema": "schemas/candidate.json",
                "outputs": [
                    {
                        "id": "result",
                        "name": "Result",
                        "kind": "json",
                        "cardinality": "one",
                        "required": True,
                    }
                ],
                "handler": "result.py",
                "input_schema": "schemas/input.json",
                "inputs": {
                    "source": {"from": "source_ready.source_ready_v1", "output": "result"}
                },
                "intelligence": "completion" if model_second else "none",
                "model_justification": "The isolated worker drafts the final result." if model_second else "",
                "draft_schema": "schemas/draft.json" if model_second else None,
                "on_tool_fail": "need_model" if model_second else "BLOCKED",
            }
        )
        milestones[-1] = {key: value for key, value in milestones[-1].items() if value is not None}
    flow = {
        "schema": "flowstep_flow_v4",
        "flow_id": "isolation_v1",
        "version": 1,
        "context_policy": "isolated",
        "artifact_root": "artifacts",
        "milestones": milestones,
    }
    (harness / "flow.yaml").write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
    _write_json(harness / "schemas" / "input.json", OPEN_SCHEMA)
    _write_json(harness / "schemas" / "candidate.json", CANDIDATE_SCHEMA)
    _write_json(harness / "schemas" / "draft.json", OPEN_SCHEMA)
    if model:
        (harness / "source.py").write_text(
            "def run(input_data, draft=None, **_):\n"
            "    if not draft:\n"
            "        return {'_flowstep': 'NEED_MODEL', 'model': 'completion', "
            "'model_request': {'instruction': 'produce the isolated draft'}}\n"
            "    return {'outputs': {'result': draft}}\n",
            encoding="utf-8",
        )
    else:
        _handler(harness / "source.py", "source", invalid=invalid)
    if two_steps and model_second:
        (harness / "result.py").write_text(
            "def run(input_data, draft=None, **_):\n"
            "    if not draft:\n"
            "        return {'_flowstep': 'NEED_MODEL', 'model': 'completion', "
            "'model_request': {'instruction': 'produce the downstream draft'}}\n"
            "    return {'outputs': {'result': draft}}\n",
            encoding="utf-8",
        )
    elif two_steps:
        _handler(harness / "result.py", "result")
    return codebase, harness


class FreshIsolationTests(unittest.TestCase):
    def test_cli_defaults_to_two_fresh_cache_off_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codebase, _ = _scaffold(root)
            request = _request(root / "request.json")
            args = ["--codebase", str(codebase), "--flow-id", "isolation_v1", "--request", str(request)]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run_main(args), 0)
                self.assertEqual(run_main(args), 0)
            runs = sorted((codebase / "flowsteps" / "runs" / "isolation_v1").iterdir())
            self.assertEqual(len(runs), 2)
            for run in runs:
                context = read_json(run / "run-context.json")
                record = read_json(run / "flow-execution-record.json")
                self.assertFalse(context["chat_history_allowed"])
                self.assertEqual(context["origin"], "fresh")
                self.assertEqual(record["cache"]["mode"], "off")

    def test_fresh_cli_refuses_an_existing_run_without_poisoning_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codebase, harness = _scaffold(root)
            run = root / "run"
            advance(harness, run, request_path=_request(root / "request.json"))
            args = [
                "--codebase",
                str(codebase),
                "--flow-id",
                "isolation_v1",
                "--run-dir",
                str(run),
            ]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run_main(args), 3)
            self.assertEqual(read_json(run / "flow-execution-record.json")["status"], "COMPLETE")

    def test_fresh_cli_refuses_any_nonempty_target_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codebase, _ = _scaffold(root)
            run = root / "not-a-run"
            run.mkdir()
            (run / "remembered.txt").write_text("old context", encoding="utf-8")
            args = [
                "--codebase",
                str(codebase),
                "--flow-id",
                "isolation_v1",
                "--run-dir",
                str(run),
                "--request",
                str(_request(root / "request.json")),
            ]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run_main(args), 3)
            self.assertFalse((run / "run-context.json").exists())

    def test_action_required_exposes_no_history_context_capsule(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, harness = _scaffold(root, model=True)
            run = root / "run"
            action = advance(harness, run, request_path=_request(root / "request.json"))
            self.assertEqual(action["state"], "ACTION_REQUIRED")
            self.assertEqual(action["context_policy"], "isolated")
            capsule = read_json(run / action["context_capsule_path"])
            self.assertFalse(capsule["chat_history_allowed"])
            self.assertIn("fresh no-history model worker", capsule["instruction"])
            self.assertTrue(all("flowsteps/cache" not in item.replace("\\", "/") for item in capsule["allowed_files"]))
            validate_against_schema(action, action_schema_path())

    def test_context_capsule_includes_bound_chosen_member_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, harness = _scaffold(root, two_steps=True, model_second=True)
            run = root / "run"
            action = advance(harness, run, request_path=_request(root / "request.json"))
            self.assertEqual(action["step_id"], "result_ready")
            capsule = read_json(run / action["context_capsule_path"])
            chosen = run / "milestones" / "source_ready" / "out" / "chosen-output.json"
            manifest = read_json(chosen)
            member = run / manifest["members"][0]["path"]
            self.assertIn(str(chosen.resolve()), capsule["allowed_files"])
            self.assertIn(str(member.resolve()), capsule["allowed_files"])


class EditedWorkflowContinuationTests(unittest.TestCase):
    def test_continue_after_edit_preserves_upstream_and_replaces_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, harness = _scaffold(root, two_steps=True)
            run = root / "run"
            advance(harness, run, request_path=_request(root / "request.json"))
            _handler(harness / "result.py", "result", version="v2")
            with self.assertRaisesRegex(FlowError, "implementation drift"):
                advance(harness, run)
            done = advance(harness, run, continue_after_edit="result_ready")
            self.assertEqual(done["state"], "COMPLETE", done)
            self.assertEqual((harness / "_source_calls.txt").read_text(encoding="utf-8"), "1")
            self.assertEqual((harness / "_result_calls.txt").read_text(encoding="utf-8"), "2")
            result = resolve_chosen_output(run, "result_ready", output_id="result")
            self.assertEqual(result["version"], "v2")
            adoption = read_json(run / "workflow-adoption.json")
            self.assertEqual(adoption["continued_from"], "result_ready")

    def test_continue_after_edit_rejects_changes_to_preserved_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, harness = _scaffold(root, two_steps=True)
            run = root / "run"
            advance(harness, run, request_path=_request(root / "request.json"))
            _handler(harness / "source.py", "source", version="v2")
            with self.assertRaisesRegex(FlowError, "preserved milestones"):
                advance(harness, run, continue_after_edit="result_ready")

    def test_continue_after_edit_rejects_milestone_reordering(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, harness = _scaffold(root, two_steps=True)
            run = root / "run"
            advance(harness, run, request_path=_request(root / "request.json"))
            raw = yaml.safe_load((harness / "flow.yaml").read_text(encoding="utf-8"))
            raw["milestones"] = list(reversed(raw["milestones"]))
            (harness / "flow.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(FlowError, "identity or order"):
                advance(harness, run, continue_after_edit="source_ready")


class GoalIsolationTests(unittest.TestCase):
    def test_goal_uses_one_fresh_cache_off_child_session_per_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codebase, harness = _scaffold(root)
            goal = root / "goal.json"
            _write_json(
                goal,
                {
                    "goal_id": "ten_cases",
                    "rows": [
                        {"id": "case_001", "name": "One", "request": {"message": "one"}},
                        {"id": "case_002", "name": "Two", "request": {"message": "two"}},
                    ],
                },
            )
            goal_dir = codebase / "flowsteps" / "goals" / "isolation_v1" / "goal"
            done = advance_goal(harness, goal_dir, goal_path=goal)
            self.assertEqual(done["state"], "COMPLETE", done)
            ledger = read_json(goal_dir / "goal-ledger.json")
            self.assertEqual([item["status"] for item in ledger["rows"]], ["done", "done"])
            child_dirs = [Path(item["child_run_dir"]) for item in ledger["rows"]]
            self.assertEqual(len(set(child_dirs)), 2)
            for child in child_dirs:
                context = read_json(child / "run-context.json")
                record = read_json(child / "flow-execution-record.json")
                roster = read_json(child / "roster.json")
                self.assertEqual(context["origin"], "goal_child")
                self.assertFalse(context["chat_history_allowed"])
                self.assertEqual(record["cache"]["mode"], "off")
                self.assertEqual(roster["status"], "complete")
            self.assertFalse((codebase / "flowsteps" / "cache").exists())

    def test_blocked_goal_continues_same_child_after_workflow_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codebase, harness = _scaffold(root, invalid=True)
            goal = root / "goal.json"
            _write_json(
                goal,
                {"goal_id": "repair_goal", "rows": [{"id": "case_001", "request": {"message": "one"}}]},
            )
            goal_dir = codebase / "flowsteps" / "goals" / "isolation_v1" / "repair"
            blocked = advance_goal(harness, goal_dir, goal_path=goal)
            self.assertEqual(blocked["state"], "BLOCKED", blocked)
            child_before = read_json(goal_dir / "goal-ledger.json")["rows"][0]["child_run_dir"]
            _handler(harness / "source.py", "source", version="fixed")
            done = advance_goal(harness, goal_dir, continue_after_edit="source_ready")
            self.assertEqual(done["state"], "COMPLETE", done)
            row = read_json(goal_dir / "goal-ledger.json")["rows"][0]
            self.assertEqual(row["child_run_dir"], child_before)
            self.assertEqual(row["attempt"], 1)
            result = resolve_chosen_output(Path(child_before), "source_ready", output_id="result")
            self.assertEqual(result["version"], "fixed")


if __name__ == "__main__":
    unittest.main()
