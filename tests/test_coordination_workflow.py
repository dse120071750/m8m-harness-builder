from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from support import SCRIPTS  # noqa: F401
from test_skill_source import SkillFixture, _candidate_schema
from coordinate_workflow import prepare_workflow
from flowstep_runtime import FlowError
from runtime_release import RuntimeReleaseError, bind_runtime_to_run
from session_layout import resolve_chosen_output
from validate_harness import validate_harness
import run_flow
import run_m8m


def write(path: Path, value: str | dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if isinstance(value, dict) else value, encoding="utf-8")


class CoordinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.harness = self.repo / "flowsteps" / "flows" / "example_flow"
        self.fixture = SkillFixture(self.harness)
        value_schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "additionalProperties": False,
                        "required": ["text"], "properties": {"text": {"type": "string"}}}
        write(self.harness / "schemas/workflow_request.schema.json", value_schema)
        write(self.repo / "bin/existing.py",
              "import json, sys\ndata = json.load(sys.stdin)\n"
              "print(json.dumps({'text': data['text'].upper()}))\n")
        self.fixture.openai["canvas"]["implementation_dependencies"] = ["bin/existing.py"]
        self.fixture.agents["finish"]["inputs"] = {
            "source": {"from": "source.source_v1", "output": "result"}}
        for name in ("source", "finish"):
            agent = self.fixture.agents[name]
            agent["input_schema"] = f"schemas/{name}.input.json"
            agent["test"] = f"handlers/test_{name}.py"
            write(self.harness / agent["test"], "def test_contract():\n    assert True\n")
            input_key = "request" if name == "source" else "source"
            write(self.harness / agent["input_schema"], {
                "type": "object", "additionalProperties": False, "required": [input_key],
                "properties": {input_key: value_schema}})
            write(self.harness / agent["output_schema"], _candidate_schema(value_schema))
            write(self.harness / agent["handler"],
                  "from pathlib import Path\nfrom flowstep_tools import run_library_tool\n"
                  "def run(input_data, **kwargs):\n"
                  f"    result = run_library_tool(Path(__file__).parents[4], '{name}_tool', input_data['{input_key}'])\n"
                  "    return {'outputs': {'result': result}}\n")
            tool = self.repo / "flowsteps/tools" / f"{name}_tool"
            write(tool / "input.schema.json", value_schema)
            write(tool / "output.schema.json", value_schema)
            write(tool / "tests/test_tool.py", "def test_contract():\n    assert True\n")
            if name == "source":
                write(tool / "tool.py",
                      "import json, subprocess, sys\nfrom pathlib import Path\n"
                      "def run(input_data, **kwargs):\n"
                      "    repo = Path(__file__).parents[3]\n"
                      "    reply = subprocess.run([sys.executable, str(repo / 'bin/existing.py')],\n"
                      "        input=json.dumps(input_data), text=True, capture_output=True,\n"
                      "        cwd=repo, shell=False, timeout=10, check=True)\n"
                      "    return json.loads(reply.stdout)\n")
            else:
                write(tool / "tool.py", "def run(input_data, **kwargs):\n"
                      "    return {'text': input_data['text'] + '!'}\n")
        self.fixture.write()

    def test_edit_run_and_resume_use_existing_cli_without_packaging(self) -> None:
        existing = (self.repo / "bin/existing.py").read_bytes()
        with mock.patch("runtime_release.stage_runtime_release", side_effect=AssertionError("packaging")), \
             mock.patch("package_archive.write_workflow_package", side_effect=AssertionError("archive")):
            result = prepare_workflow(self.harness, self.repo)
            self.assertEqual(result["validation_scope"], "workflow")
            self.fixture.agents["source"]["success"] = "The existing CLI returns uppercase text."
            self.fixture.write()
            # An ordinary edit leaves a stale generated snapshot and must recompile it.
            prepare_workflow(self.harness, self.repo)
        definition = yaml.safe_load((self.harness / "flow.yaml").read_text(encoding="utf-8"))
        self.assertEqual(definition["milestones"][0]["success"], self.fixture.agents["source"]["success"])
        write(self.root / "request.json", {"text": "hello"})
        args = ["--execution-mode", "coordination", "--harness-dir", str(self.harness),
                "--harness-root", str(self.root / "execution"),
                "--run-dir", str(self.root / "execution/runs/one")]
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("subprocess.run", wraps=subprocess.run) as command:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = run_flow.main([*args, "--request", str(self.root / "request.json")])
            self.assertEqual(code, 0, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["state"], "COMPLETE")
            self.assertEqual(command.call_count, 1)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = run_flow.main([*args, "--run-mode", "resume"])
            self.assertEqual(code, 0, output.getvalue())
            self.assertEqual(command.call_count, 1, "resume must not repeat the existing CLI")
        self.assertEqual(resolve_chosen_output(self.root / "execution/runs/one", "finish", output_id="result"),
                         {"text": "HELLO!"})
        self.assertEqual((self.repo / "bin/existing.py").read_bytes(), existing)
        self.assertFalse((self.harness / "runtime").exists())
        self.assertFalse(list(self.repo.rglob("*.m8mpkg")))
        self.assertFalse(list(self.repo.rglob("*.jpg")))

    def test_coordination_updates_documents_from_validated_bindings(self) -> None:
        original = {name: (self.harness / agent["gem"]).read_text(encoding="utf-8")
                    for name, agent in self.fixture.agents.items()}
        result = prepare_workflow(self.harness, self.repo)
        self.assertEqual(len(result["updated_milestone_documents"]), len(original))
        documents = {}
        for name, agent in self.fixture.agents.items():
            text = (self.harness / agent["gem"]).read_text(encoding="utf-8")
            self.assertTrue(text.startswith(original[name]))
            for binding in agent["execution"]["tool_bindings"]:
                self.assertIn(binding["ref"], text)
            for port in agent["outputs"]:
                self.assertIn(f"`{port['id']}`:", text)
            documents[name] = text
        second = prepare_workflow(self.harness, self.repo)
        self.assertEqual(second["updated_milestone_documents"], [])
        write(self.harness / "handlers/source.py", "M8M_RUNNABLE = False\ndef run(data, **kwargs):\n    return {}\n")
        with self.assertRaises(FlowError):
            prepare_workflow(self.harness, self.repo)
        for name, agent in self.fixture.agents.items():
            self.assertEqual((self.harness / agent["gem"]).read_text(encoding="utf-8"), documents[name])

    def test_failed_compile_preserves_snapshot(self) -> None:
        prepare_workflow(self.harness, self.repo)
        before = (self.harness / "flow.yaml").read_bytes()
        self.fixture.agents["finish"]["inputs"] = {"source": "missing.missing_v1"}
        self.fixture.write()
        with self.assertRaises(FlowError):
            prepare_workflow(self.harness, self.repo)
        self.assertEqual((self.harness / "flow.yaml").read_bytes(), before)
        self.assertEqual(list(self.harness.glob(".flow-*.yaml")), [])

    def test_file_milestones_compile_complete_and_resume_with_plain_paths(self) -> None:
        source_schema = {"$schema": "https://json-schema.org/draft/2020-12/schema",
                         "type": "object", "additionalProperties": False,
                         "required": ["source_path"],
                         "properties": {"source_path": {"type": "string", "minLength": 1}}}
        path_schema = {"type": "object", "additionalProperties": False,
                       "required": ["path"],
                       "properties": {"path": {"type": "string", "minLength": 1}}}
        source = self.root / "source.txt"
        source.write_text("file delivered", encoding="utf-8")
        write(self.harness / "schemas/workflow_request.schema.json", source_schema)
        self.fixture.agents["source"]["outputs"][0]["kind"] = "file"
        write(self.harness / self.fixture.agents["source"]["input_schema"], {
            "type": "object", "required": ["request"], "properties": {"request": source_schema}})
        write(self.harness / self.fixture.agents["source"]["output_schema"], _candidate_schema(path_schema))
        write(self.repo / "flowsteps/tools/source_tool/input.schema.json", source_schema)
        write(self.repo / "flowsteps/tools/source_tool/output.schema.json", path_schema)
        write(self.repo / "flowsteps/tools/source_tool/tool.py",
              "def run(input_data, **kwargs):\n    return {'path': input_data['source_path']}\n")
        write(self.harness / self.fixture.agents["source"]["handler"],
              "from pathlib import Path\nimport shutil\nfrom flowstep_tools import run_library_tool\n"
              "def run(input_data, run_dir=None, **kwargs):\n"
              "    result = run_library_tool(Path(__file__).parents[4], 'source_tool', input_data['request'])\n"
              "    target = Path(run_dir) / 'work' / 'source.txt'\n"
              "    target.parent.mkdir(parents=True, exist_ok=True)\n"
              "    shutil.copy2(result['path'], target)\n"
              "    return {'outputs': {'result': {'path': str(target)}}}\n")
        member_schema = {"type": "object", "required": ["path"],
                         "properties": {"path": {"type": "string", "minLength": 1}}}
        write(self.harness / self.fixture.agents["finish"]["input_schema"], {
            "type": "object", "required": ["source"], "properties": {"source": member_schema}})
        write(self.repo / "flowsteps/tools/finish_tool/input.schema.json", member_schema)
        write(self.repo / "flowsteps/tools/finish_tool/tool.py",
              "from pathlib import Path\ndef run(input_data, **kwargs):\n"
              "    return {'text': Path(input_data['path']).read_text(encoding='utf-8')}\n")
        self.fixture.write()
        prepare_workflow(self.harness, self.repo)
        write(self.root / "request.json", {"source_path": str(source)})
        run_dir = self.root / "execution/runs/plain"
        args = ["--execution-mode", "coordination", "--harness-dir", str(self.harness),
                "--harness-root", str(self.root / "execution"), "--run-dir", str(run_dir)]
        with mock.patch("session_layout._sha256", side_effect=AssertionError("unrequested file hash")):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = run_flow.main([*args, "--request", str(self.root / "request.json")])
            self.assertEqual(code, 0, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["state"], "COMPLETE")
            source.unlink()  # Resume consumes the copied chosen output.
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = run_flow.main([*args, "--run-mode", "resume"])
            self.assertEqual(code, 0, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["state"], "COMPLETE")
        self.assertEqual(resolve_chosen_output(run_dir, "finish", output_id="result"),
                         {"text": "file delivered"})

    def test_existing_v4_flow_does_not_require_native_conversion(self) -> None:
        prepare_workflow(self.harness, self.repo)
        snapshot = self.harness / "flow.yaml"
        before = snapshot.read_bytes()
        (self.harness / "agents/openai.yaml").rename(self.harness / "agents/openai.saved")
        result = prepare_workflow(self.harness, self.repo)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(snapshot.read_bytes(), before)

    def test_handler_validation_failure_preserves_snapshot_and_cleans_temporary(self) -> None:
        prepare_workflow(self.harness, self.repo)
        snapshot = self.harness / "flow.yaml"
        before = snapshot.read_bytes()
        write(self.harness / "handlers/source.py", "M8M_RUNNABLE = False\ndef run(data, **kwargs):\n    return {}\n")
        with self.assertRaisesRegex(FlowError, "non-runnable"):
            prepare_workflow(self.harness, self.repo)
        self.assertEqual(snapshot.read_bytes(), before)
        self.assertEqual(list(self.harness.glob(".flow-*.yaml")), [])

    def test_workflow_scope_preserves_package_gate_and_tool_blockers(self) -> None:
        prepare_workflow(self.harness, self.repo)
        validate_harness(self.harness, scope="workflow", update_instruction=False)
        with self.assertRaises(FlowError):
            validate_harness(self.harness, scope="package", update_instruction=False)
        write(self.repo / "flowsteps/tools/source_tool/BUILD_REQUIRED", "unfinished")
        with self.assertRaisesRegex(FlowError, "BUILD_REQUIRED"):
            prepare_workflow(self.harness, self.repo)

    def test_cli_defaults_to_coordination_and_keeps_explicit_package_path(self) -> None:
        args = ["--target", str(self.harness), "--codebase", str(self.repo)]
        with mock.patch.object(run_m8m, "run_factory", side_effect=AssertionError("packaging")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run_m8m.main(args), 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "coordinate")
        with mock.patch.object(run_m8m, "run_factory", return_value={"status": "PASS"}) as packaged, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_m8m.main([*args, "--mode", "package"]), 0)
        packaged.assert_called_once()

    def test_coordination_cannot_bypass_existing_workflow_or_run_pin(self) -> None:
        run = self.root / "run"
        for pinned in (self.harness, run):
            lock = pinned / "m8m-runtime-lock.json"
            write(lock, {})
            try:
                with self.assertRaisesRegex(RuntimeReleaseError, "cannot bypass"):
                    bind_runtime_to_run(self.harness, run, run_mode="fresh", execution_mode="coordination")
            finally:
                lock.unlink()


if __name__ == "__main__":
    unittest.main()
