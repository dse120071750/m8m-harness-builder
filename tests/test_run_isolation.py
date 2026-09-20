from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import support  # noqa: F401
import run_flow
import run_goal

from flowstep_runtime import FlowError, action_schema_path, read_json, validate_against_schema
from run_flow import _is_admissible_browser_evidence_bootstrap, advance, main as run_main
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
            "model_justification": (
                "The isolated worker drafts the requested source result."
                if model
                else ""
            ),
            "execution": {
                "candidate_executor": {
                    "ref": "handler.isolation_v1.source_ready@3.1.0"
                },
                "tool_bindings": [],
            },
            "on_tool_fail": "need_model" if model else "BLOCKED",
        }
    ]
    if not model:
        milestones[0].pop("model_justification")
    if model:
        milestones[0]["draft_schema"] = "schemas/draft.json"
        milestones[0]["execution"]["candidate_executor"]["profile"] = {
            "ref": "agent_profile.isolation_v1.source_ready.candidate.v1",
            "model_configuration": {"model": "codex", "reasoning": "medium"},
            "token_budget": {
                "max_input_tokens": 4096,
                "max_output_tokens": 1024,
            },
            "timeout_seconds": 120,
            "tools": [],
            "capabilities": [],
        }
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
                "execution": {
                    "candidate_executor": {
                        "ref": "handler.isolation_v1.result_ready@3.1.0"
                    },
                    "tool_bindings": [],
                },
                "on_tool_fail": "need_model" if model_second else "BLOCKED",
            }
        )
        milestones[-1] = {key: value for key, value in milestones[-1].items() if value is not None}
        if model_second:
            milestones[-1]["execution"]["candidate_executor"]["profile"] = {
                "ref": "agent_profile.isolation_v1.result_ready.candidate.v1",
                "model_configuration": {
                    "model": "codex",
                    "reasoning": "medium",
                },
                "token_budget": {
                    "max_input_tokens": 4096,
                    "max_output_tokens": 1024,
                },
                "timeout_seconds": 120,
                "tools": [],
                "capabilities": [],
            }
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
    def setUp(self) -> None:
        runtime_binding = patch.object(
            run_flow, "bind_runtime_to_run", return_value=None
        )
        runtime_binding.start()
        self.addCleanup(runtime_binding.stop)

    def test_cli_defaults_to_two_fresh_cache_off_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codebase, _ = _scaffold(root)
            request = _request(root / "request.json")
            runtime = root / "runtime"
            args = [
                "--codebase",
                str(codebase),
                "--flow-id",
                "isolation_v1",
                "--request",
                str(request),
                "--harness-root",
                str(runtime),
            ]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run_main(args), 0)
                self.assertEqual(run_main(args), 0)
            runs = sorted((runtime / "runs").iterdir())
            self.assertEqual(len(runs), 2)
            for run in runs:
                context = read_json(run / "run-context.json")
                record = read_json(run / "flow-execution-record.json")
                self.assertFalse(context["chat_history_allowed"])
                self.assertEqual(context["origin"], "fresh")
                self.assertEqual(record["cache"]["mode"], "off")
                self.assertEqual(context["schema"], "m8m_run_context_v2")
                self.assertEqual(Path(context["storage_contract"]["source_code_root"]), codebase.resolve())
                self.assertEqual(Path(context["storage_contract"]["execution_root"]), runtime.resolve())
                self.assertEqual(Path(context["storage_contract"]["active_run_dir"]), run.resolve())
                self.assertEqual(Path(context["storage_contract"]["cache_root"]), (runtime / "cache").resolve())
            self.assertFalse((codebase / "flowsteps" / "runs").exists())
            self.assertFalse((codebase / "flowsteps" / "cache").exists())

    def test_request_file_refs_are_materialized_before_the_first_milestone(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codebase, _ = _scaffold(root)
            source = root / "source-of-truth" / "photo.jpg"
            source.parent.mkdir()
            source.write_bytes(b"immutable-source")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            request = root / "request.json"
            _write_json(
                request,
                {
                    "message": "hello",
                    "primary": {"path": str(source), "sha256": digest},
                    "duplicate": {"path": str(source), "sha256": digest},
                },
            )
            runtime = root / "runtime"
            args = [
                "--codebase",
                str(codebase),
                "--flow-id",
                "isolation_v1",
                "--request",
                str(request),
                "--harness-root",
                str(runtime),
            ]

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run_main(args), 0)

            run = next((runtime / "runs").iterdir())
            frozen_request = read_json(run / "request.json")
            manifest = read_json(run / "source-assets-manifest.json")
            self.assertEqual(len(manifest["assets"]), 1)
            self.assertEqual(frozen_request["primary"]["path"], frozen_request["duplicate"]["path"])
            self.assertIn(str((run / "inputs" / "source-assets").resolve()), frozen_request["primary"]["path"])
            self.assertEqual(source.read_bytes(), b"immutable-source")
            self.assertFalse((codebase / "flowsteps" / "runs").exists())

    def test_fresh_explicit_run_outside_harness_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codebase, _ = _scaffold(root)
            runtime = root / "runtime"
            outside = root / "outside-run"
            args = [
                "--codebase",
                str(codebase),
                "--flow-id",
                "isolation_v1",
                "--harness-root",
                str(runtime),
                "--run-dir",
                str(outside),
                "--request",
                str(_request(root / "request.json")),
            ]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run_main(args), 3)
            self.assertFalse(outside.exists())

    def test_exact_legacy_run_resumes_but_default_discovery_ignores_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codebase, harness = _scaffold(root)
            legacy = codebase / "flowsteps" / "runs" / "isolation_v1" / "legacy-run"
            advance(harness, legacy, request_path=_request(root / "request.json"))
            context = read_json(legacy / "run-context.json")
            context["schema"] = "m8m_run_context_v1"
            context.pop("storage_contract", None)
            context.pop("source_asset_manifest_path", None)
            _write_json(legacy / "run-context.json", context)
            runtime = root / "runtime"

            exact_args = [
                "--codebase",
                str(codebase),
                "--flow-id",
                "isolation_v1",
                "--harness-root",
                str(runtime),
                "--run-mode",
                "resume",
                "--run-dir",
                str(legacy),
            ]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run_main(exact_args), 0)
                self.assertEqual(
                    run_main(
                        [
                            "--codebase",
                            str(codebase),
                            "--flow-id",
                            "isolation_v1",
                            "--harness-root",
                            str(runtime),
                            "--resume",
                        ]
                    ),
                    3,
                )

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

    def test_fresh_guard_admits_only_complete_hash_bound_browser_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            run.mkdir()
            payloads = {
                "source-article.html": b"<article>source</article>",
                "source-article.txt": b"source",
                "source-full-page.png": b"png-bytes",
                "source-visible-semantic-blocks.json": b'{"blocks":[]}',
            }
            for name, payload in payloads.items():
                (run / name).write_bytes(payload)
            receipt = {
                "schema": "article_infographic_source_browser_capture_receipt_v1",
                "status": "PASS",
                "requested_url": "https://example.com/source",
                "final_url": "https://example.com/source",
                "hashes": {name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()},
            }
            _write_json(run / "source-browser-capture-receipt.json", receipt)
            (run / "request.json").write_text("{}\n", encoding="utf-8")
            bootstrap = {".run.lock", "request.json"}
            self.assertTrue(_is_admissible_browser_evidence_bootstrap(run, bootstrap))

            (run / "source-article.txt").write_text("tampered", encoding="utf-8")
            self.assertFalse(_is_admissible_browser_evidence_bootstrap(run, bootstrap))
            (run / "source-article.txt").write_bytes(payloads["source-article.txt"])
            (run / "unexpected.json").write_text("{}\n", encoding="utf-8")
            self.assertFalse(_is_admissible_browser_evidence_bootstrap(run, bootstrap))

    def test_action_required_exposes_no_history_context_capsule(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, harness = _scaffold(root, model=True)
            run = root / "run"
            action = advance(harness, run, request_path=_request(root / "request.json"))
            self.assertEqual(action["state"], "ACTION_REQUIRED")
            self.assertEqual(action["context_policy"], "isolated")
            capsule = read_json(run / action["context_capsule_path"])
            task = read_json(run / "runtime-tasks" / "source_ready.json")
            self.assertFalse(capsule["chat_history_allowed"])
            self.assertEqual(capsule["expectation"], task["expectation"])
            self.assertEqual(
                capsule["expectation"]["schema"],
                "m8m.milestone_expectation.v1",
            )
            self.assertEqual(capsule["expectation"]["success"], "The source is accepted.")
            self.assertIn("fresh no-history model worker", capsule["instruction"])
            self.assertTrue(all("flowsteps/cache" not in item.replace("\\", "/") for item in capsule["allowed_files"]))
            validate_against_schema(action, action_schema_path())

    def test_invalid_model_draft_is_recoverable_in_same_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, harness = _scaffold(root, model=True)
            _write_json(
                harness / "schemas" / "draft.json",
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["label"],
                    "properties": {"label": {"type": "string"}},
                },
            )
            run = root / "run"
            action = advance(harness, run, request_path=_request(root / "request.json"))
            self.assertEqual(action["state"], "ACTION_REQUIRED")
            candidate_request = run / action["task_path"]
            context_capsule = run / action["context_capsule_path"]
            candidate_request_bytes = candidate_request.read_bytes()
            context_capsule_bytes = context_capsule.read_bytes()

            invalid = root / "invalid.json"
            _write_json(invalid, {"wrong": "shape"})
            retry = advance(
                harness,
                run,
                draft_path=invalid,
                draft_for="source_ready",
            )
            self.assertEqual(retry["state"], "ACTION_REQUIRED", retry)
            self.assertEqual(retry["step_id"], "source_ready")
            self.assertEqual(retry["attempt"], 1)
            self.assertTrue(retry["blockers"])
            self.assertEqual(candidate_request.read_bytes(), candidate_request_bytes)
            self.assertEqual(context_capsule.read_bytes(), context_capsule_bytes)
            self.assertTrue(
                (
                    run
                    / "work"
                    / "source_ready"
                    / "attempts"
                    / "attempt-001"
                    / "draft-validation.json"
                ).is_file()
            )
            self.assertFalse(
                (run / "runtime-tasks" / "source_ready" / "attempt-002").exists()
            )
            self.assertEqual(
                read_json(run / "flow-execution-record.json")["status"],
                "IN_PROGRESS",
            )

            valid = root / "valid.json"
            _write_json(valid, {"label": "recovered"})
            done = advance(
                harness,
                run,
                draft_path=valid,
                draft_for="source_ready",
            )
            self.assertEqual(done["state"], "COMPLETE", done)
            self.assertEqual(candidate_request.read_bytes(), candidate_request_bytes)
            self.assertEqual(context_capsule.read_bytes(), context_capsule_bytes)

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

    def test_context_capsule_includes_explicit_model_request_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, harness = _scaffold(root, model=True)
            planning = root / "contracts" / "planning.md"
            references = root / "contracts" / "references"
            planning.parent.mkdir(parents=True)
            references.mkdir(parents=True)
            planning.write_text("# Planning\n", encoding="utf-8")
            reference = references / "contract.md"
            reference.write_text("# Contract\n", encoding="utf-8")
            seed = root / "contracts" / "seed.json"
            seed.write_text("{}\n", encoding="utf-8")
            (references / "ignored.json").write_text("{}\n", encoding="utf-8")
            (harness / "source.py").write_text(
                "def run(input_data, draft=None, **_):\n"
                "    if not draft:\n"
                "        return {'_flowstep': 'NEED_MODEL', 'model': 'completion', "
                f"'model_request': {{'read': r'{planning}', 'seed_path': r'{seed}', 'references': [r'{reference}']}}}}\n"
                "    return {'outputs': {'result': draft}}\n",
                encoding="utf-8",
            )
            run = root / "run"
            action = advance(harness, run, request_path=_request(root / "request.json"))
            capsule = read_json(run / action["context_capsule_path"])
            self.assertIn(str(planning.resolve()), capsule["allowed_files"])
            self.assertIn(str(seed.resolve()), capsule["allowed_files"])
            self.assertIn(str(reference.resolve()), capsule["allowed_files"])
            self.assertNotIn(str((references / "ignored.json").resolve()), capsule["allowed_files"])


class EditedWorkflowContinuationTests(unittest.TestCase):
    def test_adoption_commit_crash_cannot_preserve_old_chosen_under_new_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, harness = _scaffold(root, two_steps=True)
            run = root / "run"
            first = advance(harness, run, request_path=_request(root / "request.json"))
            self.assertEqual(first["state"], "COMPLETE", first)
            _handler(harness / "result.py", "result", version="v2")

            with patch.object(
                run_flow,
                "_complete_adoption_journal",
                side_effect=KeyboardInterrupt("simulated crash after new implementation lock"),
            ):
                with self.assertRaisesRegex(KeyboardInterrupt, "simulated crash"):
                    advance(harness, run, continue_after_edit="result_ready")

            journal = read_json(run / "workflow-adoption.json")
            lock = read_json(run / "implementation-lock.json")
            self.assertEqual(journal["state"], "invalidated")
            self.assertEqual(
                lock["fingerprint_sha256"],
                journal["implementation_fingerprint_sha256"],
            )
            self.assertFalse(
                (run / "milestones" / "result_ready" / "out" / "chosen-output.json").exists()
            )
            self.assertTrue(
                (run / "milestones" / "source_ready" / "out" / "chosen-output.json").is_file()
            )

            done = advance(harness, run)

            self.assertEqual(done["state"], "COMPLETE", done)
            self.assertEqual(read_json(run / "workflow-adoption.json")["state"], "complete")
            self.assertEqual((harness / "_source_calls.txt").read_text(), "1")
            self.assertEqual((harness / "_result_calls.txt").read_text(), "2")
            self.assertEqual(
                resolve_chosen_output(run, "result_ready", output_id="result")["version"],
                "v2",
            )

    def test_continue_after_edit_preserves_draft_inside_invalidated_work_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, harness = _scaffold(root, two_steps=True, model_second=True)
            run = root / "run"
            action = advance(harness, run, request_path=_request(root / "request.json"))
            self.assertEqual(action["step_id"], "result_ready")
            draft = run / "work" / "result_ready" / "draft.json"
            _write_json(draft, {"label": "preserved"})
            handler = harness / "result.py"
            handler.write_text(handler.read_text(encoding="utf-8") + "\n# compatible edit\n", encoding="utf-8")

            done = advance(
                harness,
                run,
                continue_after_edit="result_ready",
                draft_path=draft,
                draft_for="result_ready",
            )

            self.assertEqual(done["state"], "COMPLETE", done)
            result = resolve_chosen_output(run, "result_ready", output_id="result")
            self.assertEqual(result["label"], "preserved")

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
    def setUp(self) -> None:
        runtime_binding = patch.object(
            run_goal, "bind_runtime_to_run", return_value=None
        )
        runtime_binding.start()
        self.addCleanup(runtime_binding.stop)

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
            runtime = root / "runtime"
            goal_dir = runtime / "runs" / "goal_isolation_v1"
            done = advance_goal(
                harness,
                goal_dir,
                goal_path=goal,
                harness_root=runtime,
                source_code_root=codebase,
            )
            self.assertEqual(done["state"], "COMPLETE", done)
            ledger = read_json(goal_dir / "goal-ledger.json")
            storage = read_json(goal_dir / "run-storage-contract.json")
            self.assertEqual(Path(storage["execution_root"]), runtime.resolve())
            self.assertEqual(Path(storage["source_code_root"]), codebase.resolve())
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
            runtime = root / "runtime"
            goal_dir = runtime / "runs" / "goal_repair"
            blocked = advance_goal(
                harness,
                goal_dir,
                goal_path=goal,
                harness_root=runtime,
                source_code_root=codebase,
            )
            self.assertEqual(blocked["state"], "BLOCKED", blocked)
            child_before = read_json(goal_dir / "goal-ledger.json")["rows"][0]["child_run_dir"]
            _handler(harness / "source.py", "source", version="fixed")
            done = advance_goal(
                harness,
                goal_dir,
                continue_after_edit="source_ready",
                harness_root=runtime,
                source_code_root=codebase,
            )
            self.assertEqual(done["state"], "COMPLETE", done)
            row = read_json(goal_dir / "goal-ledger.json")["rows"][0]
            self.assertEqual(row["child_run_dir"], child_before)
            self.assertEqual(row["attempt"], 1)
            self.assertNotIn("blockers", row)
            self.assertEqual(read_json(goal_dir / "goal-ledger.json")["status"], "complete")
            result = resolve_chosen_output(Path(child_before), "source_ready", output_id="result")
            self.assertEqual(result["version"], "fixed")

    def test_recovered_goal_waiting_for_model_clears_stale_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codebase, harness = _scaffold(root, invalid=True)
            goal = root / "goal.json"
            _write_json(goal, {"goal_id": "recover_model", "rows": [
                {"id": "case_001", "request": {"message": "one"}}
            ]})
            runtime = root / "runtime"
            goal_dir = runtime / "runs" / "goal_repair"
            blocked = advance_goal(harness, goal_dir, goal_path=goal,
                                   harness_root=runtime, source_code_root=codebase)
            self.assertEqual(blocked["state"], "BLOCKED")
            before = read_json(goal_dir / "goal-ledger.json")["rows"][0]
            self.assertTrue(before["blockers"])
            with patch.object(run_goal, "advance", return_value={"state": "ACTION_REQUIRED"}):
                pending = advance_goal(harness, goal_dir, continue_after_edit="source_ready",
                                       harness_root=runtime, source_code_root=codebase)
            self.assertEqual(pending["state"], "ACTION_REQUIRED")
            ledger = read_json(goal_dir / "goal-ledger.json")
            self.assertEqual(ledger["status"], "in_progress")
            self.assertEqual(ledger["rows"][0]["status"], "running")
            self.assertNotIn("blockers", ledger["rows"][0])
            self.assertEqual(ledger["rows"][0]["child_run_dir"], before["child_run_dir"])


if __name__ == "__main__":
    unittest.main()
