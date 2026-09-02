from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import support  # noqa: F401

from flowstep_runtime import (
    FlowError,
    assert_implementation_lock,
    implementation_lock,
    load_flow,
)
from run_flow import (
    _changed_implementation_owners,
    _compatible_additive_flow_metadata,
    _has_frozen_external_recovery,
    _recommended_continue_root,
)


OPEN_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
}


class ImplementationDependencyTests(unittest.TestCase):
    def test_additive_safety_metadata_is_compatible_for_explicit_adoption(self) -> None:
        old = {"schema": "flowstep_flow_v4", "version": 8, "max_run_seconds": 60}
        current = {
            **old,
            "context_policy": "isolated",
            "implementation_dependencies": ["flowsteps/lib/m8m.py"],
        }
        self.assertTrue(_compatible_additive_flow_metadata(old, current))

    def test_runtime_budget_can_only_increase_during_explicit_adoption(self) -> None:
        old = {"schema": "flowstep_flow_v4", "version": 8, "max_run_seconds": 60}
        self.assertTrue(
            _compatible_additive_flow_metadata(old, {**old, "max_run_seconds": 120})
        )
        self.assertFalse(
            _compatible_additive_flow_metadata(old, {**old, "max_run_seconds": 30})
        )
        self.assertFalse(
            _compatible_additive_flow_metadata(old, {key: value for key, value in old.items() if key != "max_run_seconds"})
        )

    def test_existing_or_unknown_global_changes_remain_incompatible(self) -> None:
        old = {"schema": "flowstep_flow_v4", "version": 8, "max_run_seconds": 60}
        self.assertFalse(
            _compatible_additive_flow_metadata(old, {**old, "context_policy": "shared"})
        )
        self.assertFalse(
            _compatible_additive_flow_metadata(old, {**old, "new_runtime_switch": True})
        )
        self.assertFalse(
            _compatible_additive_flow_metadata(
                old,
                {**old, "implementation_dependencies": ["flowsteps/lib/m8m.py", "flowsteps/lib/m8m.py"]},
            )
        )

    def _flow(self, root: Path) -> Path:
        project = root / "project"
        harness = project / "flowsteps" / "flows" / "dependency_demo_v1"
        (harness / "milestones" / "ready").mkdir(parents=True)
        (harness / "schemas").mkdir(parents=True)
        (project / "flowsteps" / "lib").mkdir(parents=True)
        (project / "shared").mkdir(parents=True)
        (harness / "milestones" / "ready" / "assemble.py").write_text(
            "def run(input_data, **_):\n    return {'outputs': {'result': input_data}}\n",
            encoding="utf-8",
        )
        for name in ("input.json", "output.json"):
            (harness / "schemas" / name).write_text(json.dumps(OPEN_SCHEMA), encoding="utf-8")
        (project / "flowsteps" / "lib" / "shared.py").write_text("VALUE = 1\n", encoding="utf-8")
        (project / "shared" / "ready.py").write_text("VALUE = 1\n", encoding="utf-8")
        raw = {
            "schema": "flowstep_flow_v4",
            "flow_id": "dependency_demo_v1",
            "version": 1,
            "implementation_dependencies": ["flowsteps/lib/shared.py"],
            "milestones": [
                {
                    "id": "ready",
                    "success": "The output is ready.",
                    "output_contract": "ready_v1",
                    "output_schema": "schemas/output.json",
                    "outputs": [
                        {
                            "id": "result",
                            "name": "Result",
                            "kind": "json",
                            "cardinality": "one",
                            "required": True,
                        }
                    ],
                    "handler": "milestones/ready/assemble.py",
                    "input_schema": "schemas/input.json",
                    "implementation_dependencies": ["shared/ready.py"],
                    "inputs": {"request": "user.request"},
                    "execution": {
                        "candidate_executor": {
                            "ref": "handler.dependency_demo_v1.ready@3.1.0"
                        },
                        "tool_bindings": [],
                    },
                    "intelligence": "none",
                    "on_tool_fail": "BLOCKED",
                }
            ],
        }
        (harness / "flow.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        return harness

    def test_declared_shared_files_are_locked_and_resume_detects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = self._flow(Path(temp))
            flow = load_flow(harness)
            lock = implementation_lock(harness, flow)
            self.assertIn("project:flowsteps/lib/shared.py", lock["files"])
            self.assertIn("project:shared/ready.py", lock["files"])

            run_dir = Path(temp) / "run"
            run_dir.mkdir()
            (run_dir / "implementation-lock.json").write_text(json.dumps(lock), encoding="utf-8")
            with patch.dict(
                os.environ,
                {"M8M_IMPLEMENTATION_VERIFICATION_TTL_SECONDS": "0"},
            ):
                assert_implementation_lock(run_dir, harness, flow)

            (Path(temp) / "project" / "flowsteps" / "lib" / "shared.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            with patch.dict(
                os.environ,
                {"M8M_IMPLEMENTATION_VERIFICATION_TTL_SECONDS": "0"},
            ):
                with self.assertRaisesRegex(FlowError, "implementation drift detected"):
                    assert_implementation_lock(run_dir, harness, flow)

    def test_bound_tool_package_helpers_are_part_of_the_run_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = self._flow(Path(temp))
            project = harness.parents[2]
            tool_root = project / "flowsteps" / "tools" / "bound_tool"
            tool_root.mkdir(parents=True)
            (tool_root / "tool.py").write_text(
                "from helper import VALUE\n\ndef run(input_data, **_):\n    return {'value': VALUE}\n",
                encoding="utf-8",
            )
            helper = tool_root / "helper.py"
            helper.write_text("VALUE = 1\n", encoding="utf-8")
            test_helper = tool_root / "tests" / "runtime_hook.py"
            test_helper.parent.mkdir()
            test_helper.write_text("HOOK = 1\n", encoding="utf-8")
            for name in ("input.schema.json", "output.schema.json"):
                (tool_root / name).write_text(json.dumps(OPEN_SCHEMA), encoding="utf-8")

            flow_path = harness / "flow.yaml"
            raw = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
            milestone = raw["milestones"][0]
            milestone["tools"] = ["bound_action"]
            milestone["flowsteps"] = [
                {"id": "bound_action", "tool": "bound_tool@1.0.0"}
            ]
            milestone["execution"]["tool_bindings"] = [
                {"tool": "bound_action", "ref": "bound_tool@1.0.0"}
            ]
            flow_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

            flow = load_flow(harness)
            lock = implementation_lock(harness, flow)
            self.assertIn("project:flowsteps/tools/bound_tool/helper.py", lock["files"])
            self.assertIn(
                "project:flowsteps/tools/bound_tool/tests/runtime_hook.py",
                lock["files"],
            )

            run_dir = Path(temp) / "run"
            run_dir.mkdir()
            (run_dir / "implementation-lock.json").write_text(
                json.dumps(lock), encoding="utf-8"
            )
            helper.write_text("VALUE = 2\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"M8M_IMPLEMENTATION_VERIFICATION_TTL_SECONDS": "0"},
            ):
                with self.assertRaisesRegex(FlowError, "implementation drift detected"):
                    assert_implementation_lock(run_dir, harness, flow)

    def test_recent_verification_receipt_never_hides_live_handler_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = self._flow(Path(temp))
            flow = load_flow(harness)
            lock = implementation_lock(harness, flow)
            run_dir = Path(temp) / "run"
            run_dir.mkdir()
            (run_dir / "implementation-lock.json").write_text(
                json.dumps(lock), encoding="utf-8"
            )

            assert_implementation_lock(run_dir, harness, flow)
            verification_path = run_dir / "implementation-verification.json"
            verification = json.loads(verification_path.read_text(encoding="utf-8"))
            verification["verified_at_epoch"] = time.time()
            verification["ttl_seconds"] = 3600
            verification_path.write_text(json.dumps(verification), encoding="utf-8")
            (harness / "milestones" / "ready" / "assemble.py").write_text(
                "def run(input_data, **_):\n    return {'outputs': {'result': {'changed': True}}}\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"M8M_IMPLEMENTATION_VERIFICATION_TTL_SECONDS": "3600"},
            ):
                with self.assertRaisesRegex(FlowError, "implementation drift detected"):
                    assert_implementation_lock(run_dir, harness, flow)

    def test_new_tool_package_member_is_implementation_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = self._flow(Path(temp))
            project = harness.parents[2]
            tool_root = project / "flowsteps" / "tools" / "bound_tool"
            tool_root.mkdir(parents=True)
            for name, content in (
                ("tool.py", "def run(input_data, **_): return input_data\n"),
                ("input.schema.json", json.dumps(OPEN_SCHEMA)),
                ("output.schema.json", json.dumps(OPEN_SCHEMA)),
            ):
                (tool_root / name).write_text(content, encoding="utf-8")
            flow_path = harness / "flow.yaml"
            raw = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
            milestone = raw["milestones"][0]
            milestone["tools"] = ["bound_action"]
            milestone["flowsteps"] = [
                {"id": "bound_action", "tool": "bound_tool@1.0.0"}
            ]
            milestone["execution"]["tool_bindings"] = [
                {"tool": "bound_action", "ref": "bound_tool@1.0.0"}
            ]
            flow_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

            flow = load_flow(harness)
            lock = implementation_lock(harness, flow)
            run_dir = Path(temp) / "run"
            run_dir.mkdir()
            (run_dir / "implementation-lock.json").write_text(
                json.dumps(lock), encoding="utf-8"
            )
            assert_implementation_lock(run_dir, harness, flow)
            plugins = tool_root / "plugins"
            plugins.mkdir()
            (plugins / "injected.py").write_text("ENABLED = True\n", encoding="utf-8")
            with self.assertRaisesRegex(FlowError, "implementation drift detected"):
                assert_implementation_lock(run_dir, harness, flow)

    def test_dependency_cannot_escape_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = self._flow(Path(temp))
            flow_path = harness / "flow.yaml"
            raw = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
            raw["implementation_dependencies"] = ["../outside.py"]
            flow_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(FlowError, "implementation_dependencies"):
                load_flow(harness)

    def test_implementation_dependency_rejects_an_intermediate_junction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = self._flow(Path(temp))
            flow = load_flow(harness)
            project = harness.parents[2]
            unsafe = project / "shared"
            original = Path.is_junction

            def fake_is_junction(path: Path) -> bool:
                return path == unsafe or original(path)

            with patch.object(Path, "is_junction", autospec=True, side_effect=fake_is_junction):
                with self.assertRaisesRegex(FlowError, "unsafe link"):
                    implementation_lock(harness, flow)

    def test_frozen_external_operation_allows_recovery_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            journal = run_dir / "publication" / "phase-journal.json"
            journal.parent.mkdir(parents=True)
            journal.write_text(
                json.dumps(
                    {
                        "schema": "m8m_external_phase_journal_v1",
                        "milestone_id": "write_verified",
                        "operation_id": "operation_one",
                        "plan_sha256": "a" * 64,
                        "phases": [{"phase": "plan_frozen"}],
                    }
                ),
                encoding="utf-8",
            )
            flow = {
                "steps": [
                    {
                        "id": "write_verified",
                        "side_effects": "external",
                        "phase_journal": {
                            "path": "publication/phase-journal.json"
                        },
                    }
                ]
            }

            self.assertTrue(
                _has_frozen_external_recovery(
                    run_dir,
                    flow,
                    "write_verified",
                )
            )

    def test_recommended_continue_root_uses_dependency_graph_not_sort_order(self) -> None:
        flow = {
            "steps": [
                {"id": "request_ready", "inputs": {"request": "user.request"}},
                {
                    "id": "z_branch",
                    "inputs": {"intake": "request_ready.ready_v1"},
                },
                {
                    "id": "a_branch",
                    "inputs": {"intake": "request_ready.ready_v1"},
                },
                {
                    "id": "write_verified",
                    "inputs": {
                        "left": "z_branch.branch_v1",
                        "right": "a_branch.branch_v1",
                    },
                },
            ]
        }
        self.assertEqual(
            _recommended_continue_root(
                flow,
                {"request_ready", "z_branch", "a_branch", "write_verified"},
            ),
            "request_ready",
        )

    def test_strict_judge_package_is_owned_by_its_milestone_during_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            skill_dir = project / "flowsteps" / "flows" / "judge_demo_v1"
            skill_dir.mkdir(parents=True)
            flow_path = skill_dir / "flow.yaml"
            flow_path.write_text("schema: flowstep_flow_v4\n", encoding="utf-8")
            old_raw = {
                "milestones": [
                    {
                        "id": "quality_ready",
                        "tools": [],
                        "flowsteps": [],
                    }
                ]
            }
            current_raw = {
                "milestones": [
                    {
                        "id": "quality_ready",
                        "tools": [],
                        "flowsteps": [],
                        "loop": "judge",
                        "worker": "quality_ready_judge@3.1.0",
                        "judge_abi": "m8m_milestone_judge_v1",
                    }
                ]
            }
            label = "project:flowsteps/tools/quality_ready_judge/tool.py"
            changed, unowned = _changed_implementation_owners(
                skill_dir,
                {"_flow_path": str(flow_path)},
                old_raw,
                current_raw,
                {"files": {}},
                {"files": {label: "a" * 64}},
            )

            self.assertEqual(unowned, [])
            self.assertEqual(changed, {"quality_ready"})


if __name__ == "__main__":
    unittest.main()
