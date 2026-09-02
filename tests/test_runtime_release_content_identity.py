from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import support  # noqa: F401

import runtime_release
from flowstep_runtime import validate_against_schema
from runtime_release import (
    RUNTIME_CONTRACT_FILES,
    RUNTIME_LOCK_FILENAME,
    RUNTIME_SCRIPT_FILES,
    RuntimeReleaseError,
    _canonical_bytes,
    _copy_release,
    _digest_bytes,
    _manifest_from_sources,
    _runtime_identity,
    _runtime_lock,
    resolve_runtime_manifest,
    stage_runtime_release,
)


ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _write_complete_flow(harness: Path) -> None:
    open_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": True,
    }
    output_schema = {
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
    _write_json(harness / "schemas" / "input.json", open_schema)
    _write_json(harness / "schemas" / "output.json", output_schema)
    (harness / "handler.py").write_text(
        "def run(input_data, draft=None, **_):\n"
        "    return {'outputs': {'result': {'message': input_data['request']['message']}}}\n",
        encoding="utf-8",
    )
    flow = {
        "schema": "flowstep_flow_v4",
        "flow_id": "identity_demo",
        "version": 1,
        "context_policy": "isolated",
        "artifact_root": "artifacts",
        "milestones": [
            {
                "id": "result_ready",
                "success": "The deterministic result is ready.",
                "output_contract": "result_ready_v1",
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
                "handler": "handler.py",
                "input_schema": "schemas/input.json",
                "inputs": {"request": "user.request"},
                "intelligence": "none",
                "loop": "none",
                "flowsteps": [],
                "tools": [],
                "on_tool_fail": "BLOCKED",
                "execution": {
                    "candidate_executor": {
                        "ref": "handler.identity_demo.result_ready@3.1.0"
                    },
                    "tool_bindings": [],
                },
            }
        ],
    }
    (harness / "flow.yaml").write_text(
        yaml.safe_dump(flow, sort_keys=False), encoding="utf-8"
    )


class RuntimeReleaseContentIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.builder_temporary = tempfile.TemporaryDirectory()
        cls.builder_root = Path(cls.builder_temporary.name)
        for folder, names in (
            ("scripts", RUNTIME_SCRIPT_FILES),
            ("contracts", RUNTIME_CONTRACT_FILES),
        ):
            for name in names:
                destination = cls.builder_root / folder / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / folder / name, destination)
        for name in ("codebase-launcher.py", "run.py"):
            destination = cls.builder_root / "templates" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "templates" / name, destination)
        cls.manifest, cls.sources = _manifest_from_sources(cls.builder_root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.builder_temporary.cleanup()

    def test_every_execution_identity_field_is_content_addressed(self) -> None:
        identity = _runtime_identity(self.manifest)
        self.assertEqual(
            set(identity),
            {
                "schema",
                "runtime_name",
                "runtime_version",
                "cli_abi",
                "entrypoint",
                "python_abi",
                "python_executable_digest",
                "dependencies",
                "members",
            },
        )
        baseline = _digest_bytes(_canonical_bytes(identity))
        self.assertEqual(self.manifest["payload_digest"], baseline)
        self.assertEqual(self.manifest["runtime_id"], baseline.removeprefix("sha256:"))
        mutations = {
            "schema": "m8m.runtime_release.future",
            "runtime_name": "different-runtime",
            "runtime_version": "9.9.9",
            "cli_abi": "different-cli",
            "entrypoint": "scripts/run_goal.py",
            "python_abi": "different-abi",
            "python_executable_digest": "sha256:" + "f" * 64,
        }
        for field, value in mutations.items():
            changed = copy.deepcopy(self.manifest)
            changed[field] = value
            self.assertNotEqual(
                _digest_bytes(_canonical_bytes(_runtime_identity(changed))),
                baseline,
                field,
            )

        for field in ("dependencies", "members"):
            changed = copy.deepcopy(self.manifest)
            changed[field][0]["digest" if field == "members" else "files_digest"] = (
                "sha256:" + "e" * 64
            )
            self.assertNotEqual(
                _digest_bytes(_canonical_bytes(_runtime_identity(changed))),
                baseline,
                field,
            )
        dependency_names = {item["name"].replace("_", "-").lower() for item in self.manifest["dependencies"]}
        self.assertIn("typing-extensions", dependency_names)
        self.assertTrue(
            all(item["files_digest"].startswith("sha256:") for item in self.manifest["dependencies"])
        )
        self.assertTrue(
            all(
                any(member["path"].startswith(item["vendor_path"] + "/") for member in self.manifest["members"])
                for item in self.manifest["dependencies"]
            )
        )
        validate_against_schema(
            self.manifest, ROOT / "contracts" / "m8m_runtime_release_v1.schema.json"
        )
        validate_against_schema(
            _runtime_lock(self.manifest),
            ROOT / "contracts" / "m8m_runtime_lock_v1.schema.json",
        )

    def test_generated_bytecode_caches_are_not_release_members(self) -> None:
        paths = {str(item["path"]) for item in self.manifest["members"]}
        self.assertFalse(
            any("/__pycache__/" in f"/{path}/" for path in paths)
        )
        self.assertFalse(any(path.endswith((".pyc", ".pyo")) for path in paths))

    def test_existing_release_directory_is_compare_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            release_dir = Path(temp) / self.manifest["runtime_id"]
            first = _copy_release(release_dir, self.manifest, self.sources)
            first_manifest_bytes = first.read_bytes()
            second = _copy_release(release_dir, self.manifest, self.sources)
            self.assertEqual(second.read_bytes(), first_manifest_bytes)
            member = release_dir / "scripts" / "run_flow.py"
            member.write_bytes(member.read_bytes() + b"\n# tampered\n")
            tampered = member.read_bytes()
            with self.assertRaisesRegex(RuntimeReleaseError, "different bytes"):
                _copy_release(release_dir, self.manifest, self.sources)
            self.assertEqual(member.read_bytes(), tampered)

    def test_metadata_upgrade_keeps_old_run_pin_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codebase = root / "repo"
            harness = codebase / "flowsteps" / "flows" / "identity_demo"
            product = codebase / ".agents" / "skills" / "identity-demo"
            _write_complete_flow(harness)
            release_a = stage_runtime_release(
                builder_root=self.builder_root,
                harness=harness,
                product_roots=[product],
                flow_id="identity_demo",
                codebase=codebase,
            )
            release_a_again = stage_runtime_release(
                builder_root=self.builder_root,
                harness=harness,
                product_roots=[product],
                flow_id="identity_demo",
                codebase=codebase,
            )
            self.assertEqual(release_a_again["runtime_id"], release_a["runtime_id"])
            request = root / "request.json"
            _write_json(request, {"message": "hello"})
            execution_root = root / "execution"
            run_dir = execution_root / "runs" / "run-a"
            pointer = product / "scripts" / "m8m_run.py"
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            started = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(pointer),
                    "--run-dir",
                    str(run_dir),
                    "--harness-root",
                    str(execution_root),
                    "--request",
                    str(request),
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            self.assertEqual(json.loads(started.stdout)["state"], "COMPLETE")
            run_lock = json.loads(
                (run_dir / RUNTIME_LOCK_FILENAME).read_text(encoding="utf-8")
            )
            self.assertEqual(run_lock["runtime_id"], release_a["runtime_id"])
            manifest_a = Path(release_a["manifest_path"])
            manifest_a_bytes = manifest_a.read_bytes()

            with mock.patch.object(runtime_release, "RUNTIME_VERSION", "1.0.1"):
                release_b = stage_runtime_release(
                    builder_root=self.builder_root,
                    harness=harness,
                    product_roots=[product],
                    flow_id="identity_demo",
                    codebase=codebase,
                )
            self.assertNotEqual(release_a["runtime_id"], release_b["runtime_id"])
            self.assertNotEqual(
                Path(release_a["manifest_path"]).parent,
                Path(release_b["manifest_path"]).parent,
            )
            self.assertEqual(manifest_a.read_bytes(), manifest_a_bytes)
            self.assertEqual(
                resolve_runtime_manifest(harness, ["--run-dir", str(run_dir)]),
                manifest_a,
            )
            resumed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(pointer),
                    "--run-mode",
                    "resume",
                    "--run-dir",
                    str(run_dir),
                    "--harness-root",
                    str(execution_root),
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual(json.loads(resumed.stdout)["state"], "COMPLETE")
            launcher = (harness / "launch.py").read_text(encoding="utf-8")
            self.assertIn('"-S"', launcher)
            self.assertNotIn("importlib.metadata", launcher)


if __name__ == "__main__":
    unittest.main()
