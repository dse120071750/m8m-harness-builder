from __future__ import annotations

import json
import copy
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

import support  # noqa: F401

from candidate_cache import (
    cache_root,
    milestone_implementation_fingerprint,
    prepare_candidate_cache,
    prune_expired,
)
from flowstep_runtime import FlowError, candidate_cache_schema_path, load_flow, read_json, validate_against_schema
from run_flow import advance
from session_layout import load_chosen_output, resolve_chosen_output


OPEN_INPUT = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
}

CANDIDATE = {
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


def _request(root: Path, name: str = "same") -> Path:
    path = root / f"request-{name}.json"
    _write_json(path, {"message": name})
    return path


class CandidateCacheTests(unittest.TestCase):
    def _harness(self, root: Path, *, cache: bool = True, judge: bool = False) -> tuple[Path, Path]:
        codebase = root / "repo"
        harness = codebase / "flowsteps" / "flows" / "cache_demo_v1"
        milestone = {
            "id": "result_ready",
            "success": "The current result is acceptable.",
            "output_contract": "result_ready_v1",
            "output_schema": "schemas/candidate.json",
            "outputs": [
                {"id": "result", "name": "Result", "kind": "json", "cardinality": "one", "required": True}
            ],
            "handler": "handler.py",
            "input_schema": "schemas/input.json",
            "inputs": {"request": "user.request"},
            "intelligence": "none",
            "execution": {
                "candidate_executor": {
                    "ref": "handler.cache_demo_v1.result_ready@3.1.0"
                },
                "tool_bindings": [],
            },
            "on_tool_fail": "BLOCKED",
        }
        if cache:
            milestone["cache"] = {"reuse": "candidate", "ttl_seconds": 3600, "side_effects": "none"}
        if judge:
            milestone.update(
                {
                    "loop": "judge",
                    "worker": "result_ready_judge@1.0.0",
                    "judge_abi": "m8m_milestone_judge_v1",
                    "tools": [],
                    "receipt_schema": "schemas/receipt.json",
                    "max_attempts": 3,
                }
            )
            milestone["execution"]["judge"] = {
                "ref": "result_ready_judge@1.0.0"
            }
        flow = {
            "schema": "flowstep_flow_v4",
            "flow_id": "cache_demo_v1",
            "version": 1,
            "artifact_root": "artifacts",
            "milestones": [milestone],
        }
        harness.mkdir(parents=True)
        (harness / "flow.yaml").write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
        _write_json(harness / "schemas" / "input.json", OPEN_INPUT)
        _write_json(harness / "schemas" / "candidate.json", CANDIDATE)
        _write_json(
            harness / "schemas" / "receipt.json",
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
        (harness / "handler.py").write_text(
            "from pathlib import Path\n"
            "def run(input_data, draft=None, task=None, run_dir=None, **_):\n"
            "    codebase = Path(__file__).resolve().parents[3]\n"
            "    marker = codebase / 'handler-calls.txt'\n"
            "    count = int(marker.read_text()) + 1 if marker.exists() else 1\n"
            "    marker.write_text(str(count))\n"
            "    result = {'outputs': {'result': {'message': input_data['request']['message'], 'handler_call': count}}}\n"
            + "    return result\n",
            encoding="utf-8",
        )
        if judge:
            tool = codebase / "flowsteps" / "tools" / "result_ready_judge"
            tool.mkdir(parents=True)
            _write_json(tool / "input.schema.json", OPEN_INPUT)
            _write_json(
                tool / "output.schema.json",
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
            (tool / "tool.py").write_text(
                "from pathlib import Path\n"
                "def run(input_data, params=None, **_):\n"
                "    codebase = Path(__file__).resolve().parents[3]\n"
                "    marker = codebase / 'judge-calls.txt'\n"
                "    count = int(marker.read_text()) + 1 if marker.exists() else 1\n"
                "    marker.write_text(str(count))\n"
                "    handler_call = input_data['candidate']['outputs']['result']['handler_call']\n"
                "    reject = (codebase / 'reject-cache.txt').exists() and handler_call == 1\n"
                "    return {\n"
                "        'decision': 'RETRY' if reject else 'PASS',\n"
                "        'reasons': ['cache-reject' if reject else 'cache-pass'],\n"
                "        'blockers': ['stale candidate'] if reject else [],\n"
                "    }\n",
                encoding="utf-8",
            )
        return harness, codebase

    def test_short_workflow_without_declaration_never_touches_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root, cache=False)
            done = advance(harness, root / "run", request_path=_request(root), cache_mode="read-write")
            self.assertEqual(done["state"], "COMPLETE")
            self.assertFalse((codebase / "flowsteps" / "cache").exists())
            self.assertFalse(
                (root / "run" / "milestones" / "result_ready" / "work" / "cache-receipt.json").exists()
            )
            self.assertFalse(
                (root / "run" / "milestones" / "result_ready" / "work" / "cache-candidate").exists()
            )

    def test_declared_cache_stays_off_without_run_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root)
            done = advance(harness, root / "run", request_path=_request(root))
            self.assertEqual(done["state"], "COMPLETE")
            self.assertFalse((codebase / "flowsteps" / "cache").exists())
            receipt = read_json(root / "run" / "milestones" / "result_ready" / "work" / "cache-receipt.json")
            self.assertEqual(receipt["status"], "disabled")

    def test_exact_hit_imports_candidate_and_keeps_state_run_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root)
            first = advance(
                harness, root / "run-1", request_path=_request(root), cache_mode="read-write", cache_namespace="tenant-a"
            )
            second = advance(
                harness, root / "run-2", request_path=_request(root), cache_mode="read-write", cache_namespace="tenant-a"
            )
            self.assertEqual(first["state"], "COMPLETE")
            self.assertEqual(second["state"], "COMPLETE")
            self.assertEqual((codebase / "handler-calls.txt").read_text(), "1")
            receipt = read_json(root / "run-2" / "milestones" / "result_ready" / "work" / "cache-receipt.json")
            self.assertEqual(receipt["status"], "hit")
            manifest = load_chosen_output(root / "run-2", "result_ready")
            self.assertEqual(manifest["run_id"], "run-2")
            self.assertNotIn("cache_key_sha256", manifest)
            self.assertNotIn("cache_entry", manifest)
            self.assertEqual(resolve_chosen_output(root / "run-2", "result_ready", output_id="result")["handler_call"], 1)

    def test_namespace_and_input_changes_are_misses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root)
            advance(harness, root / "run-a", request_path=_request(root), cache_mode="read-write", cache_namespace="a")
            advance(harness, root / "run-b", request_path=_request(root), cache_mode="read-write", cache_namespace="b")
            advance(harness, root / "run-c", request_path=_request(root, "changed"), cache_mode="read-write", cache_namespace="a")
            self.assertEqual((codebase / "handler-calls.txt").read_text(), "3")

    def test_implementation_change_is_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root)
            advance(harness, root / "run-1", request_path=_request(root), cache_mode="read-write")
            handler = harness / "handler.py"
            handler.write_text(handler.read_text(encoding="utf-8") + "\n# implementation changed\n", encoding="utf-8")
            advance(harness, root / "run-2", request_path=_request(root), cache_mode="read-write")
            self.assertEqual((codebase / "handler-calls.txt").read_text(), "2")

    def test_implementation_identity_covers_contract_gem_tools_judge_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root)
            step = load_flow(harness)["steps"][0]
            baseline = milestone_implementation_fingerprint(harness, step)
            mutations = {
                "success": "A different success rule.",
                "output_contract": "different_v1",
                "worker": "different_judge@1.0.0",
                "judge_abi": "m8m_milestone_judge_v1",
                "model": "completion",
            }
            for field, value in mutations.items():
                changed = copy.deepcopy(step)
                changed[field] = value
                self.assertNotEqual(
                    milestone_implementation_fingerprint(harness, changed),
                    baseline,
                    field,
                )
            changed = copy.deepcopy(step)
            changed["outputs"][0]["name"] = "Different port"
            self.assertNotEqual(milestone_implementation_fingerprint(harness, changed), baseline)

            with_execution = copy.deepcopy(step)
            with_execution["execution"] = {
                "candidate_executor": {
                    "ref": "handler.cache_demo_v1.result_ready@3.1.0",
                    "profile": {
                        "ref": "profile.result_ready.candidate.v1",
                        "model_configuration": {"model": "codex", "reasoning": "low"},
                    },
                },
                "judge": {
                    "ref": "judge.result_ready@1.0.0",
                    "profile": {
                        "ref": "profile.result_ready.judge.v1",
                        "model_configuration": {"model": "codex", "reasoning": "medium"},
                    },
                },
            }
            execution_baseline = milestone_implementation_fingerprint(harness, with_execution)
            for path, value in (
                (("candidate_executor", "ref"), "handler.result_ready@2.0.0"),
                (("judge", "ref"), "judge.result_ready@2.0.0"),
                (("candidate_executor", "profile", "model_configuration", "model"), "other-model"),
            ):
                changed = copy.deepcopy(with_execution)
                target = changed["execution"]
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.assertNotEqual(
                    milestone_implementation_fingerprint(harness, changed),
                    execution_baseline,
                    path,
                )

            schema_path = harness / step["output_schema"]
            original_schema = schema_path.read_text(encoding="utf-8")
            schema_path.write_text(original_schema + "\n", encoding="utf-8")
            self.assertNotEqual(milestone_implementation_fingerprint(harness, step), baseline)
            schema_path.write_text(original_schema, encoding="utf-8")

            gem = harness / "references" / "result_ready.md"
            gem.parent.mkdir(parents=True)
            gem.write_text("first rule", encoding="utf-8")
            with_gem = copy.deepcopy(step)
            with_gem["gem"] = "references/result_ready.md"
            gem_baseline = milestone_implementation_fingerprint(harness, with_gem)
            gem.write_text("second rule", encoding="utf-8")
            self.assertNotEqual(milestone_implementation_fingerprint(harness, with_gem), gem_baseline)

            tool = codebase / "flowsteps" / "tools" / "pure_tool"
            tool.mkdir(parents=True)
            (tool / "tool.py").write_text("def run(input_data): return input_data\n", encoding="utf-8")
            with_tool = copy.deepcopy(step)
            with_tool["flowsteps"] = [{"id": "pure_step", "tool": "pure_tool@1.0.0"}]
            with_tool["tools"] = ["pure_step"]
            with_tool["execution"]["tool_bindings"] = [
                {"tool": "pure_step", "ref": "pure_tool@1.0.0"}
            ]
            tool_baseline = milestone_implementation_fingerprint(harness, with_tool)
            (tool / "tool.py").write_text("def run(input_data): return {'changed': True}\n", encoding="utf-8")
            self.assertNotEqual(milestone_implementation_fingerprint(harness, with_tool), tool_baseline)

    def test_file_input_bytes_participate_in_cache_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root)
            source = root / "source.bin"
            source.write_bytes(b"first")
            request = root / "request.json"
            _write_json(request, {"message": "same", "source": {"path": str(source)}})
            advance(harness, root / "run-1", request_path=request, cache_mode="read-write")
            source.write_bytes(b"second")
            advance(harness, root / "run-2", request_path=request, cache_mode="read-write")
            self.assertEqual((codebase / "handler-calls.txt").read_text(), "2")

    def test_write_mode_forces_fresh_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root)
            advance(harness, root / "run-1", request_path=_request(root), cache_mode="read-write")
            advance(harness, root / "run-2", request_path=_request(root), cache_mode="write")
            self.assertEqual((codebase / "handler-calls.txt").read_text(), "2")
            receipt = read_json(root / "run-2" / "milestones" / "result_ready" / "work" / "cache-receipt.json")
            self.assertEqual(receipt["lookup_status"], "bypassed")
            self.assertEqual(receipt["write_status"], "stored")

    def test_cached_file_is_copied_back_inside_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root)
            flow_path = harness / "flow.yaml"
            flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
            flow["milestones"][0]["outputs"][0]["kind"] = "file"
            flow_path.write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
            (harness / "handler.py").write_text(
                "from pathlib import Path\n"
                "def run(input_data, draft=None, task=None, run_dir=None, **_):\n"
                "    codebase = Path(__file__).resolve().parents[3]\n"
                "    marker = codebase / 'handler-calls.txt'\n"
                "    count = int(marker.read_text()) + 1 if marker.exists() else 1\n"
                "    marker.write_text(str(count))\n"
                "    generated = Path(run_dir) / 'generated.txt'\n"
                "    generated.write_text('cached bytes')\n"
                "    return {'outputs': {'result': {'path': str(generated)}}}\n",
                encoding="utf-8",
            )
            advance(harness, root / "run-1", request_path=_request(root), cache_mode="read-write")
            advance(harness, root / "run-2", request_path=_request(root), cache_mode="read-write")
            self.assertEqual((codebase / "handler-calls.txt").read_text(), "1")
            resolved = resolve_chosen_output(root / "run-2", "result_ready", output_id="result")
            copied = Path(resolved["path"])
            self.assertTrue(copied.is_file())
            self.assertIn((root / "run-2").resolve(), copied.resolve().parents)
            self.assertEqual(copied.read_text(), "cached bytes")

    def test_cache_hit_calls_current_judge(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root, judge=True)
            advance(harness, root / "run-1", request_path=_request(root), cache_mode="read-write")
            advance(harness, root / "run-2", request_path=_request(root), cache_mode="read-write")
            self.assertEqual((codebase / "handler-calls.txt").read_text(), "1")
            self.assertEqual((codebase / "judge-calls.txt").read_text(), "2")
            judge_receipt = read_json(root / "run-2" / "milestones" / "result_ready" / "out" / "judge-receipt.json")
            self.assertEqual(judge_receipt["decision"], "PASS")
            self.assertEqual(judge_receipt["reasons"], ["cache-pass"])

    def test_rejected_cache_falls_through_without_spending_handler_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root, judge=True)
            advance(harness, root / "run-1", request_path=_request(root), cache_mode="read-write")
            (codebase / "reject-cache.txt").write_text("1", encoding="utf-8")
            done = advance(harness, root / "run-2", request_path=_request(root), cache_mode="read-write")
            self.assertEqual(done["state"], "COMPLETE")
            self.assertEqual((codebase / "handler-calls.txt").read_text(), "2")
            receipt = read_json(root / "run-2" / "milestones" / "result_ready" / "work" / "cache-receipt.json")
            self.assertEqual(receipt["lookup_status"], "rejected")
            self.assertEqual(receipt["write_status"], "stored")

    def test_replace_bypasses_cache_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root)
            run = root / "run"
            advance(harness, run, request_path=_request(root), cache_mode="read-write")
            done = advance(harness, run, replace_milestone="result_ready")
            self.assertEqual(done["state"], "COMPLETE")
            self.assertEqual((codebase / "handler-calls.txt").read_text(), "2")
            receipt = read_json(run / "milestones" / "result_ready" / "work" / "cache-receipt.json")
            self.assertEqual(receipt["lookup_status"], "bypassed")

    def test_replace_bypasses_cache_for_transitive_downstream_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root)
            flow_path = harness / "flow.yaml"
            flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
            downstream = copy.deepcopy(flow["milestones"][0])
            downstream.update(
                {
                    "id": "final_ready",
                    "success": "The final result is acceptable.",
                    "output_contract": "final_ready_v1",
                    "handler": "final_handler.py",
                    "inputs": {
                        "source": {
                            "from": "result_ready.result_ready_v1",
                            "output": "result",
                        }
                    },
                }
            )
            downstream["execution"]["candidate_executor"]["ref"] = (
                "handler.cache_demo_v1.final_ready@3.1.0"
            )
            flow["milestones"].append(downstream)
            flow_path.write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
            (harness / "final_handler.py").write_text(
                "from pathlib import Path\n"
                "def run(input_data, draft=None, task=None, run_dir=None, **_):\n"
                "    codebase = Path(__file__).resolve().parents[3]\n"
                "    marker = codebase / 'final-handler-calls.txt'\n"
                "    count = int(marker.read_text()) + 1 if marker.exists() else 1\n"
                "    marker.write_text(str(count))\n"
                "    return {'outputs': {'result': {'source': input_data['source'], 'handler_call': count}}}\n",
                encoding="utf-8",
            )
            run = root / "run"
            advance(harness, run, request_path=_request(root), cache_mode="read-write")
            done = advance(harness, run, replace_milestone="result_ready")
            self.assertEqual(done["state"], "COMPLETE", done)
            self.assertEqual((codebase / "handler-calls.txt").read_text(), "2")
            self.assertEqual((codebase / "final-handler-calls.txt").read_text(), "2")
            for milestone in ("result_ready", "final_ready"):
                receipt = read_json(run / "milestones" / milestone / "work" / "cache-receipt.json")
                self.assertEqual(receipt["lookup_status"], "bypassed")

    def test_expired_and_corrupt_entries_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root)
            advance(harness, root / "run-1", request_path=_request(root), cache_mode="read-write")
            manifest_path = next((root / "cache").rglob("cache-entry.json"))
            entry = read_json(manifest_path)
            entry["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat().replace(
                "+00:00", "Z"
            )
            entry["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
            _write_json(manifest_path, entry)
            advance(harness, root / "run-2", request_path=_request(root), cache_mode="read-write")
            receipt = read_json(root / "run-2" / "milestones" / "result_ready" / "work" / "cache-receipt.json")
            self.assertEqual(receipt["lookup_status"], "expired")
            manifest_path = next((root / "cache").rglob("cache-entry.json"))
            cached = read_json(manifest_path)
            member = manifest_path.parent / cached["members"][0]["path"]
            member.unlink()
            advance(harness, root / "run-3", request_path=_request(root), cache_mode="read-write")
            receipt = read_json(root / "run-3" / "milestones" / "result_ready" / "work" / "cache-receipt.json")
            self.assertEqual(receipt["lookup_status"], "invalid")
            self.assertEqual((codebase / "handler-calls.txt").read_text(), "3")

    def test_duplicate_cache_members_are_invalid_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root)
            advance(harness, root / "run-1", request_path=_request(root), cache_mode="read-write")
            manifest_path = next((root / "cache").rglob("cache-entry.json"))
            entry = read_json(manifest_path)
            entry["members"].append(dict(entry["members"][0]))
            _write_json(manifest_path, entry)
            done = advance(harness, root / "run-2", request_path=_request(root), cache_mode="read-write")
            self.assertEqual(done["state"], "COMPLETE")
            receipt = read_json(
                root / "run-2" / "milestones" / "result_ready" / "work" / "cache-receipt.json"
            )
            self.assertEqual(receipt["lookup_status"], "invalid")
            self.assertEqual((codebase / "handler-calls.txt").read_text(), "2")

    def test_current_shorter_ttl_expires_an_older_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root)
            advance(harness, root / "run-1", request_path=_request(root), cache_mode="read-write")
            manifest_path = next((root / "cache").rglob("cache-entry.json"))
            entry = read_json(manifest_path)
            entry["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat().replace(
                "+00:00", "Z"
            )
            entry["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=3600)).isoformat().replace(
                "+00:00", "Z"
            )
            _write_json(manifest_path, entry)
            flow_path = harness / "flow.yaml"
            flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
            flow["milestones"][0]["cache"]["ttl_seconds"] = 1
            flow_path.write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
            done = advance(harness, root / "run-2", request_path=_request(root), cache_mode="read-write")
            self.assertEqual(done["state"], "COMPLETE")
            receipt = read_json(
                root / "run-2" / "milestones" / "result_ready" / "work" / "cache-receipt.json"
            )
            self.assertEqual(receipt["lookup_status"], "expired")
            self.assertEqual((codebase / "handler-calls.txt").read_text(), "2")

    def test_concurrent_writers_leave_one_complete_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root)
            (harness / "handler.py").write_text(
                "def run(input_data, draft=None, task=None, run_dir=None, **_):\n"
                "    return {'outputs': {'result': {'message': input_data['request']['message']}}}\n",
                encoding="utf-8",
            )
            runs = [root / "run-a", root / "run-b"]
            requests = [_request(root, "shared-a"), _request(root, "shared-b")]
            # Keep semantic input identical while using distinct request files.
            requests[1].write_text(requests[0].read_text(encoding="utf-8"), encoding="utf-8")
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        advance,
                        harness,
                        run,
                        request_path=request,
                        cache_mode="write",
                        cache_namespace="tenant-a",
                    )
                    for run, request in zip(runs, requests)
                ]
                results = [future.result() for future in futures]
            self.assertTrue(all(item["state"] == "COMPLETE" for item in results), results)
            manifests = list((root / "cache").rglob("cache-entry.json"))
            self.assertEqual(len(manifests), 1)
            entry = read_json(manifests[0])
            validate_against_schema(entry, candidate_cache_schema_path())
            for member in entry["members"]:
                self.assertTrue((manifests[0].parent / member["path"]).is_file())

    def test_cache_write_failure_does_not_change_milestone_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, _ = self._harness(root)
            with patch("candidate_cache.os.open", side_effect=OSError("cache lock denied")):
                done = advance(harness, root / "run", request_path=_request(root), cache_mode="read-write")
            self.assertEqual(done["state"], "COMPLETE", done)
            receipt = read_json(
                root / "run" / "milestones" / "result_ready" / "work" / "cache-receipt.json"
            )
            self.assertEqual(receipt["write_status"], "failed")
            self.assertIn("cache lock denied", receipt["warning"])

    def test_resume_completed_run_never_reads_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root)
            run = root / "run"
            advance(harness, run, request_path=_request(root), cache_mode="read-write")
            shutil.rmtree(root / "cache")
            done = advance(harness, run)
            self.assertEqual(done["state"], "COMPLETE")
            self.assertEqual((codebase / "handler-calls.txt").read_text(), "1")

    def test_unfinished_resume_does_not_repeat_lookup_when_local_candidate_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, _ = self._harness(root)
            advance(harness, root / "run-1", request_path=_request(root), cache_mode="read-write")
            flow = load_flow(harness)
            step = flow["steps"][0]
            run = root / "run-2"
            first = prepare_candidate_cache(
                harness,
                run,
                flow,
                step,
                {"request": {"message": "same"}},
                mode="read",
                namespace="local",
            )
            self.assertIsInstance(first["candidate"], dict)
            (run / "milestones" / "result_ready" / "work" / "cache-candidate" / "candidate.json").unlink()
            second = prepare_candidate_cache(
                harness,
                run,
                flow,
                step,
                {"request": {"message": "same"}},
                mode="read",
                namespace="local",
            )
            self.assertIsNone(second["candidate"])
            self.assertEqual(second["receipt"]["lookup_status"], "invalid")

    def test_prune_removes_expired_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, codebase = self._harness(root)
            advance(harness, root / "run", request_path=_request(root), cache_mode="read-write")
            flow = load_flow(harness)
            root_cache = cache_root(harness, flow, runtime_root=root)
            manifest_path = next(root_cache.rglob("cache-entry.json"))
            entry = read_json(manifest_path)
            validate_against_schema(entry, candidate_cache_schema_path())
            entry["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
            _write_json(manifest_path, entry)
            result = prune_expired(root_cache)
            self.assertEqual(result["removed"], 1)
            self.assertFalse(manifest_path.exists())

    def test_invalid_cache_contracts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, _ = self._harness(root)
            flow_path = harness / "flow.yaml"
            flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
            del flow["milestones"][0]["cache"]["ttl_seconds"]
            flow_path.write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
            with self.assertRaises(FlowError):
                load_flow(harness)
            flow["milestones"][0]["cache"]["ttl_seconds"] = 60
            flow["milestones"][0]["flowsteps"] = [
                {"id": "upload_asset", "tool": "upload_asset@1.0.0"}
            ]
            flow["milestones"][0]["tools"] = ["upload_asset"]
            flow["milestones"][0]["execution"]["tool_bindings"] = [
                {"tool": "upload_asset", "ref": "upload_asset@1.0.0"}
            ]
            flow_path.write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(FlowError, "external side effects"):
                load_flow(harness)

    def test_run_cache_configuration_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness, _ = self._harness(root)
            run = root / "run"
            advance(harness, run, request_path=_request(root), cache_mode="read", cache_namespace="tenant-a")
            with self.assertRaisesRegex(FlowError, "cache mode is frozen"):
                advance(harness, run, cache_mode="write")


if __name__ == "__main__":
    unittest.main()
