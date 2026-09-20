from __future__ import annotations

import ast
import importlib.util
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


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
os.sys.path.insert(0, str(SCRIPTS))

from runtime_release import (  # noqa: E402
    RUNTIME_LOCK_FILENAME,
    RUNTIME_SCRIPT_FILES,
    RuntimeReleaseError,
    _is_unsafe_link,
    bind_runtime_to_run,
    resolve_runtime_manifest,
    stage_runtime_release,
    verify_runtime_release,
)
from flowstep_runtime import FlowError  # noqa: E402
from m8m_build_steps import _assert_product_runtime_isolated  # noqa: E402


def _write_resumable_flow(harness: Path) -> None:
    schema_dir = harness / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    open_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": True,
    }
    candidate_schema = {
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
    for path, value in (
        (schema_dir / "input.json", open_schema),
        (schema_dir / "draft.json", open_schema),
        (schema_dir / "candidate.json", candidate_schema),
    ):
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    (harness / "source.py").write_text(
        "def run(input_data, draft=None, **_):\n"
        "    if not draft:\n"
        "        return {'_flowstep': 'NEED_MODEL', 'model': 'completion', "
        "'model_request': {'instruction': 'produce the isolated draft'}}\n"
        "    return {'outputs': {'result': draft}}\n",
        encoding="utf-8",
    )
    flow = {
        "schema": "flowstep_flow_v4",
        "flow_id": "sample_v1",
        "version": 1,
        "context_policy": "isolated",
        "artifact_root": "artifacts",
        "milestones": [
            {
                "id": "source_ready",
                "success": "The isolated source draft is accepted.",
                "output_contract": "source_ready_v1",
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
                "handler": "source.py",
                "input_schema": "schemas/input.json",
                "draft_schema": "schemas/draft.json",
                "inputs": {"request": "user.request"},
                "intelligence": "completion",
                "model_justification": "A fresh isolated worker supplies the draft.",
                "on_tool_fail": "need_model",
                "execution": {
                    "candidate_executor": {
                        "ref": "handler.sample_v1.source_ready@3.1.0",
                        "profile": {
                            "ref": "agent_profile.sample_v1.source_ready.candidate.v1",
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
                        },
                    },
                    "tool_bindings": [],
                },
            }
        ],
    }
    (harness / "flow.yaml").write_text(
        yaml.safe_dump(flow, sort_keys=False), encoding="utf-8"
    )


class RuntimeReleaseIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.codebase = Path(cls.temporary.name) / "codebase with spaces"
        cls.harness = cls.codebase / "flowsteps" / "flows" / "sample_v1"
        cls.product_roots = [
            cls.codebase / ".agents" / "skills" / "sample",
            cls.codebase / ".claude" / "skills" / "sample",
        ]
        cls.release = stage_runtime_release(
            builder_root=ROOT,
            harness=cls.harness,
            product_roots=cls.product_roots,
            flow_id="sample_v1",
            codebase=cls.codebase,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_generated_launcher_never_falls_back_to_shared_builder(self) -> None:
        launcher = (ROOT / "templates" / "run.py").read_text(encoding="utf-8")
        self.assertNotIn("M8M_BUILDER", launcher)
        self.assertNotIn("FLOWSTEP_BUILDER", launcher)
        self.assertNotIn("M8M_CODEBASE_LAUNCHER", launcher)
        self.assertNotIn("m8m-harness-builder", launcher)
        self.assertIn("__CODEBASE_LAUNCHER__", launcher)
        self.assertIn("__CODEBASE_LAUNCHER_SHA256__", launcher)
        self.assertNotIn("runtime/releases", launcher)
        self.assertIn("subprocess.run", launcher)
        self.assertIn('M8M_SKILL_POINTER_ABI = "m8m_skill_pointer_v2"', launcher)

        handler = (ROOT / "templates" / "milestone" / "assemble.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("M8M_BUILDER", handler)
        self.assertNotIn("FLOWSTEP_BUILDER", handler)
        self.assertNotIn("m8m-harness-builder", handler)
        self.assertIn("from flowstep_tools import run_library_tool", handler)

    def test_runtime_source_imports_are_inside_the_release_closure(self) -> None:
        local_modules = {path.stem for path in (ROOT / "scripts").glob("*.py")}
        released = {Path(name).stem for name in RUNTIME_SCRIPT_FILES}
        missing: set[str] = set()
        for name in RUNTIME_SCRIPT_FILES:
            tree = ast.parse((ROOT / "scripts" / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module.split(".", 1)[0]
                    if module in local_modules and module not in released:
                        missing.add(module)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        module = alias.name.split(".", 1)[0]
                        if module in local_modules and module not in released:
                            missing.add(module)
        self.assertEqual(missing, set())

    def test_codebase_dispatcher_verifies_then_spawns_a_fresh_runtime(self) -> None:
        launcher = (ROOT / "templates" / "codebase-launcher.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('M8M_CODEBASE_DISPATCH_ABI = "m8m_codebase_dispatch_v2"', launcher)
        self.assertIn("_runtime_lock(require_run_lock=require_run_lock)", launcher)
        self.assertIn("subprocess.run", launcher)
        self.assertIn('if not sys.flags.isolated:', launcher)
        self.assertIn('"-I",', launcher)
        self.assertIn("isolated_runtime_bootstrap", launcher)
        self.assertIn('getattr(path, "is_junction", None)', launcher)
        self.assertNotIn("from runtime_release import", launcher)
        self.assertIn("does not accept harness location overrides", launcher)

    def test_windows_junctions_are_treated_as_unsafe_runtime_links(self) -> None:
        candidate = self.codebase / "runtime-junction"
        with mock.patch.object(Path, "is_junction", return_value=True, create=True):
            self.assertTrue(_is_unsafe_link(candidate))

    def test_bootstraps_reexec_before_sibling_stdlib_shadow_can_load(self) -> None:
        sentinel = self.codebase / "shadow-imported.txt"
        shadow_source = (
            f"open({str(sentinel)!r}, 'w', encoding='utf-8').write('loaded')\n"
            "raise RuntimeError('sibling shadow imported')\n"
        )
        pointer = self.product_roots[0] / "scripts" / "m8m_run.py"
        launcher = Path(self.release["codebase_launcher_path"])
        for program in (pointer, launcher):
            for name in ("__future__", "hashlib", "pathlib", "subprocess", "json"):
                with self.subTest(program=program.name, shadow=name):
                    shadow = program.parent / f"{name}.py"
                    try:
                        shadow.write_text(shadow_source, encoding="utf-8")
                        completed = subprocess.run(
                            [sys.executable, "-B", str(program), "--run-mode", "resume"],
                            cwd=self.codebase,
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=45,
                        )
                        self.assertNotEqual(completed.returncode, 0)
                        self.assertFalse(sentinel.exists(), completed.stderr)
                        self.assertIn("Pinned M8M resume requires the exact", completed.stderr)
                    finally:
                        shadow.unlink(missing_ok=True)
                        sentinel.unlink(missing_ok=True)

    def test_generated_isolation_prefixes_preserve_exact_argument_tokens(self) -> None:
        # Observe the exact generated prefix at the isolation boundary. The
        # appended recorder is a bootstrap diagnostic, not an M8M runtime.
        probe_root = self.codebase / "argument probes with spaces"
        probe_root.mkdir()
        pointer = self.product_roots[0] / "scripts" / "m8m_run.py"
        launcher = Path(self.release["codebase_launcher_path"])
        arguments = [
            "--request", "request with spaces.json", "--run-dir", "same run directory",
            "--draft", "literal $(never-run); draft.json", "", 'embedded"quote',
            "trailing\\", 'backslashes\\\\"and quote', "single\\slash",
            "two\\\\slashes", "資料 café.json",
        ]
        for name, generated in (("pointer", pointer), ("launcher", launcher)):
            with self.subTest(bootstrap=name):
                prefix = generated.read_text(encoding="utf-8").split("import hashlib", 1)[0]
                probe = probe_root / f"{name}.py"
                capture = probe_root / f"{name}.json"
                probe.write_text(
                    prefix + "import json\n"
                    + f"open({str(capture)!r}, 'w', encoding='utf-8').write("
                    + "json.dumps({'argv':sys.argv[1:],'isolated':sys.flags.isolated,"
                    + "'no_bytecode':sys.dont_write_bytecode}))\n"
                    + "raise SystemExit(17)\n",
                    encoding="utf-8",
                )
                completed = subprocess.run(
                    [sys.executable, "-B", str(probe), *arguments],
                    cwd=probe_root, capture_output=True, text=True, check=False, timeout=45,
                )
                self.assertEqual(completed.returncode, 17, completed.stderr)
                self.assertEqual(
                    json.loads(capture.read_text(encoding="utf-8")),
                    {"argv": arguments, "isolated": 1, "no_bytecode": True},
                )

    def test_codebase_dispatcher_rejects_missing_resume_pin_before_runtime(self) -> None:
        launcher = Path(self.release["codebase_launcher_path"])
        missing_run = self.codebase / "missing-run"
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(launcher),
                "--run-mode",
                "resume",
                "--run-dir",
                str(missing_run),
            ],
            cwd=self.codebase,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("missing the exact run-local runtime lock", completed.stderr)

    def test_direct_generated_launcher_preserves_request_and_draft_paths(self) -> None:
        _write_resumable_flow(self.harness)
        with tempfile.TemporaryDirectory(dir=self.codebase) as temporary:
            execution = Path(temporary) / "direct execution with spaces"
            execution.mkdir()
            run_dir = execution / "runs" / "run with spaces"
            request = execution / "request with spaces.json"
            request.write_text('{"message":"exact request"}\n', encoding="utf-8")
            launcher = Path(self.release["codebase_launcher_path"])
            common = [sys.executable, "-B", str(launcher), "--run-dir", str(run_dir),
                      "--harness-root", str(execution)]
            started = subprocess.run(
                [*common, "--request", str(request)], cwd=execution,
                capture_output=True, text=True, check=False, timeout=60,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            self.assertEqual(json.loads(started.stdout)["state"], "ACTION_REQUIRED")
            self.assertEqual(json.loads((run_dir / "request.json").read_text(encoding="utf-8")),
                             {"message": "exact request"})
            self.assertTrue((run_dir / RUNTIME_LOCK_FILENAME).is_file())
            draft = execution / "draft with spaces.json"
            draft.write_text('{"message":"accepted draft"}\n', encoding="utf-8")
            resumed = subprocess.run(
                [*common, "--run-mode", "resume", "--draft", str(draft),
                 "--draft-for", "source_ready"], cwd=execution,
                capture_output=True, text=True, check=False, timeout=60,
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual(json.loads(resumed.stdout)["state"], "COMPLETE")

    def test_stage_produces_self_contained_verified_release(self) -> None:
        release = self.release
        self.assertRegex(release["runtime_id"], r"^[0-9a-f]{64}$")
        self.assertTrue(Path(release["archive_path"]).is_file())
        self.assertTrue(Path(release["manifest_path"]).is_file())
        self.assertEqual(
            Path(release["manifest_path"]).parent.parent.parent,
            self.harness / "runtime",
        )
        self.assertTrue(Path(release["codebase_launcher_path"]).is_file())
        self.assertEqual(
            verify_runtime_release(Path(release["manifest_path"]))["payload_digest"],
            release["payload_digest"],
        )
        self.assertRegex(release["python_abi"], r"^[A-Za-z0-9_.-]+$")
        self.assertTrue(release["dependencies"])
        for root in self.product_roots:
            self.assertTrue((root / "scripts" / "m8m_run.py").is_file())
            self.assertFalse((root / "runtime").exists())
            self.assertNotIn(
                str(ROOT).casefold(),
                (root / "scripts" / "m8m_run.py").read_text(encoding="utf-8").casefold(),
            )

    def test_product_pointer_rejects_mutated_codebase_launcher(self) -> None:
        launcher = Path(self.release["codebase_launcher_path"])
        original = launcher.read_bytes()
        launcher.write_bytes(original + b"\n# unauthorized mutation\n")
        try:
            pointer = self.product_roots[0] / "scripts" / "m8m_run.py"
            completed = subprocess.run(
                [sys.executable, "-B", str(pointer)],
                cwd=self.codebase,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("failed its pinned digest check", completed.stderr)
        finally:
            launcher.write_bytes(original)

    def test_shared_builder_cannot_execute_a_built_harness(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.codebase) as temp:
            with mock.patch.dict(os.environ, {"M8M_RUNTIME_MANIFEST": ""}):
                with self.assertRaisesRegex(RuntimeReleaseError, "codebase-owned"):
                    bind_runtime_to_run(self.harness, Path(temp), run_mode="fresh")
            with mock.patch.dict(
                os.environ,
                {"M8M_RUNTIME_MANIFEST": str(self.release["manifest_path"])},
            ):
                with self.assertRaisesRegex(
                    RuntimeReleaseError, "actually executing"
                ):
                    bind_runtime_to_run(self.harness, Path(temp), run_mode="fresh")

    def test_release_rejects_undeclared_runtime_files(self) -> None:
        manifest = Path(self.release["manifest_path"])
        intruder = manifest.parent / "scripts" / "undeclared.py"
        intruder.write_text("raise RuntimeError('not declared')\n", encoding="utf-8")
        try:
            with self.assertRaisesRegex(RuntimeReleaseError, "not a closed directory"):
                verify_runtime_release(manifest)
        finally:
            intruder.unlink()

    def test_bound_tool_and_implementation_dependencies_cannot_reach_builder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codebase = Path(temp)
            harness = codebase / "flowsteps" / "flows" / "audit_v1"
            harness.mkdir(parents=True)
            generated = {
                "harness_dir": str(harness),
                "stage_codebase": str(codebase),
                "skill_name": "audit-skill",
            }
            tool_source = (
                codebase / "flowsteps" / "tools" / "danger_tool" / "helper.py"
            )
            tool_source.parent.mkdir(parents=True)
            tool_source.write_text(
                "from pathlib import Path\n"
                "BUILDER = Path.home() / '.codex' / 'skills' / 'm8m-harness-builder'\n",
                encoding="utf-8",
            )
            tool_flow = {
                "milestones": [
                    {
                        "id": "ready",
                        "execution": {
                            "tool_bindings": [
                                {"tool": "danger", "ref": "danger_tool@1.0.0"}
                            ]
                        },
                    }
                ]
            }
            with self.assertRaisesRegex(FlowError, "mutable M8M Builder"):
                _assert_product_runtime_isolated(generated, tool_flow)

            tool_source.write_text(
                "from pathlib import Path\n"
                "import subprocess\n"
                "name = 'm8m-' + 'harness-builder'\n"
                "runner = Path.home() / '.codex' / 'skills' / name / 'scripts' / 'run_flow.py'\n"
                "subprocess.run(['python', str(runner)], check=False)\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FlowError, "mutable M8M Builder"):
                _assert_product_runtime_isolated(generated, tool_flow)

            tool_source.write_text(
                "from pathlib import Path\n"
                "import subprocess\n"
                "name = '-'.join(['m8m', 'harness', 'builder'])\n"
                "runner = Path('{}/.codex/skills/{}/scripts/run_flow.py'.format(Path.home(), name))\n"
                "subprocess.run(['python', str(runner)], check=False)\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FlowError, "mutable M8M Builder"):
                _assert_product_runtime_isolated(generated, tool_flow)

            helper = tool_source.with_suffix(".ps1")
            tool_source.write_text(
                "import subprocess\nsubprocess.run(['powershell', 'helper.ps1'])\n",
                encoding="utf-8",
            )
            helper.write_text(
                "& $HOME/.codex/skills/m8m-harness-builder/scripts/run_flow.py\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FlowError, "mutable M8M Builder"):
                _assert_product_runtime_isolated(generated, tool_flow)
            helper.unlink()

            tool_source.write_text("VALUE = 'safe'\n", encoding="utf-8")
            dependency = codebase / "flowsteps" / "lib" / "m8m_runtime.py"
            dependency.parent.mkdir(parents=True)
            dependency.write_text(
                "from pathlib import Path\n"
                "BUILDER = Path.home() / '.codex' / 'skills' / 'm8m-harness-builder'\n",
                encoding="utf-8",
            )
            dependency_flow = {
                "implementation_dependencies": ["flowsteps/lib/m8m_runtime.py"],
                "milestones": [],
            }
            with self.assertRaisesRegex(FlowError, "mutable M8M Builder"):
                _assert_product_runtime_isolated(generated, dependency_flow)

            dependency.write_text("VALUE = 'safe'\n", encoding="utf-8")
            unrelated = codebase / "unrelated.py"
            unrelated.write_text(
                "from pathlib import Path\n"
                "BUILDER = Path.home() / '.codex' / 'skills' / 'm8m-harness-builder'\n",
                encoding="utf-8",
            )
            _assert_product_runtime_isolated(generated, dependency_flow)

    def test_resume_uses_run_pinned_release_not_new_active_release(self) -> None:
        first = self.release
        _write_resumable_flow(self.harness)
        with tempfile.TemporaryDirectory() as temp:
            execution_root = Path(temp) / "execution with spaces"
            execution_root.mkdir()
            run_dir = execution_root / "runs" / "run with spaces"
            request = execution_root / "request with spaces.json"
            request.write_text('{"message": "hello"}\n', encoding="utf-8")
            pointer = self.product_roots[0] / "scripts" / "m8m_run.py"
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
                cwd=execution_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            self.assertEqual(json.loads(started.stdout)["state"], "ACTION_REQUIRED")
            self.assertTrue((run_dir / RUNTIME_LOCK_FILENAME).is_file())

            # Simulate a later Builder/runtime release without mutating the old one.
            release_root = self.harness / "runtime" / "releases"
            second_id = "f" * 64 if first["runtime_id"] != "f" * 64 else "e" * 64
            second_dir = release_root / second_id
            second_dir.mkdir()
            second_manifest = second_dir / "runtime-manifest.json"
            second_manifest.write_text("{}\n", encoding="utf-8")
            active_path = self.harness / "runtime" / "active.json"
            original_active = active_path.read_text(encoding="utf-8")
            active = json.loads(original_active)
            active["runtime_id"] = second_id
            active["payload_digest"] = f"sha256:{second_id}"
            try:
                active_path.write_text(
                    json.dumps(active, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                resolved = resolve_runtime_manifest(
                    self.harness, ["--run-dir", str(run_dir)]
                )
                self.assertEqual(resolved.parent.name, first["runtime_id"])
                draft = execution_root / "draft with spaces.json"
                draft.write_text('{"message": "accepted"}\n', encoding="utf-8")
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
                        "--draft",
                        str(draft),
                    ],
                    cwd=execution_root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertEqual(json.loads(resumed.stdout)["state"], "COMPLETE")
            finally:
                active_path.write_text(original_active, encoding="utf-8")
                shutil.rmtree(second_dir)

    def test_wrong_runtime_fails_before_resume(self) -> None:
        release = self.release
        with tempfile.TemporaryDirectory(dir=self.codebase) as temp:
            run_dir = Path(temp)
            manifest = Path(release["manifest_path"])
            module_path = manifest.parent / "scripts" / "runtime_release.py"
            spec = importlib.util.spec_from_file_location(
                "m8m_test_pinned_runtime_release", module_path
            )
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            pinned_runtime = importlib.util.module_from_spec(spec)
            with mock.patch.object(sys, "dont_write_bytecode", True):
                spec.loader.exec_module(pinned_runtime)
            with mock.patch.dict(os.environ, {"M8M_RUNTIME_MANIFEST": str(manifest)}):
                pinned_runtime.bind_runtime_to_run(
                    self.harness, run_dir, run_mode="fresh"
                )

            lock_path = run_dir / RUNTIME_LOCK_FILENAME
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            lock["runtime_id"] = "0" * 64
            lock_path.write_text(json.dumps(lock, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"M8M_RUNTIME_MANIFEST": str(manifest)}):
                with self.assertRaisesRegex(
                    pinned_runtime.RuntimeReleaseError, "pinned M8M runtime"
                ):
                    pinned_runtime.bind_runtime_to_run(
                        self.harness, run_dir, run_mode="resume"
                    )


if __name__ == "__main__":
    unittest.main()
