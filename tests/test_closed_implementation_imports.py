from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import support  # noqa: F401

from flowstep_runtime import FlowError, implementation_lock, load_flow
from validate_harness import validate_harness


OPEN_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
}
OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["outputs"],
    "properties": {
        "outputs": {
            "type": "object",
            "additionalProperties": False,
            "required": ["result"],
            "properties": {"result": {"type": "object"}},
        }
    },
}


class ClosedImplementationImportTests(unittest.TestCase):
    def _harness(
        self,
        root: Path,
        handler_source: str,
        *,
        dependencies: list[str] | None = None,
        package_init: bool = False,
    ) -> tuple[Path, Path]:
        project = root / "project"
        harness = project / "flowsteps" / "flows" / "import_demo_v1"
        milestone = harness / "milestones" / "ready"
        (milestone / "tests").mkdir(parents=True)
        (harness / "schemas").mkdir(parents=True)
        (project / "shared").mkdir(parents=True)
        (project / "shared" / "ready.py").write_text(
            "VALUE = 'closed'\n", encoding="utf-8"
        )
        if package_init:
            (project / "shared" / "__init__.py").write_text(
                "PACKAGE = 'shared'\n", encoding="utf-8"
            )
        (milestone / "assemble.py").write_text(handler_source, encoding="utf-8")
        (milestone / "tests" / "test_assemble.py").write_text(
            "def test_handler_contract():\n    assert True\n", encoding="utf-8"
        )
        (harness / "schemas" / "input.json").write_text(
            json.dumps(OPEN_SCHEMA), encoding="utf-8"
        )
        (harness / "schemas" / "output.json").write_text(
            json.dumps(OUTPUT_SCHEMA), encoding="utf-8"
        )
        raw = {
            "schema": "flowstep_flow_v4",
            "flow_id": "import_demo_v1",
            "version": 1,
            "context_policy": "isolated",
            "milestones": [
                {
                    "id": "ready",
                    "success": "The closed implementation result is ready.",
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
                    "test": "milestones/ready/tests/test_assemble.py",
                    "input_schema": "schemas/input.json",
                    "implementation_dependencies": list(dependencies or []),
                    "inputs": {"request": "user.request"},
                    "flowsteps": [],
                    "tools": [],
                    "execution": {
                        "candidate_executor": {
                            "ref": "handler.import_demo_v1.ready@3.1.0"
                        },
                        "tool_bindings": [],
                    },
                    "intelligence": "none",
                    "on_tool_fail": "BLOCKED",
                }
            ],
        }
        (harness / "flow.yaml").write_text(
            yaml.safe_dump(raw, sort_keys=False), encoding="utf-8"
        )
        return harness, project

    @staticmethod
    def _normal_local_import_handler(*, write_marker: bool = False) -> str:
        marker = (
            "MARKER = Path(__file__).resolve().parents[5] / 'handler-executed.txt'\n"
            "MARKER.write_text('executed', encoding='utf-8')\n"
            if write_marker
            else ""
        )
        return (
            "import sys\n"
            "from pathlib import Path\n"
            f"{marker}"
            "PROJECT = Path(__file__).resolve().parents[5]\n"
            "sys.path.insert(0, str(PROJECT))\n"
            "from shared.ready import VALUE\n\n"
            "def run(input_data, **_):\n"
            "    return {'outputs': {'result': {'value': VALUE}}}\n"
        )

    def test_undeclared_repository_import_fails_before_handler_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness, project = self._harness(
                Path(temp),
                self._normal_local_import_handler(write_marker=True),
            )
            flow = load_flow(harness)

            with self.assertRaisesRegex(
                FlowError,
                r"(?s)implementation import closure is open:.*shared/ready\.py.*implementation_dependencies",
            ):
                implementation_lock(harness, flow)

            self.assertFalse((project / "handler-executed.txt").exists())

    def test_declared_repository_import_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness, _ = self._harness(
                Path(temp),
                self._normal_local_import_handler(),
                dependencies=["shared/ready.py"],
            )
            lock = implementation_lock(harness, load_flow(harness))

            self.assertIn("project:shared/ready.py", lock["files"])

    def test_repository_import_from_static_sys_path_root_must_be_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness, project = self._harness(
                Path(temp),
                "import sys\n"
                "from pathlib import Path\n"
                "PROJECT = Path(__file__).resolve().parents[5]\n"
                "SOURCE_ROOT = PROJECT / 'src'\n"
                "sys.path.insert(0, str(SOURCE_ROOT))\n"
                "from shared.ready import VALUE\n\n"
                "def run(input_data, **_):\n"
                "    return {'outputs': {'result': {'value': VALUE}}}\n",
            )
            source_root = project / "src" / "shared"
            source_root.mkdir(parents=True)
            (source_root / "ready.py").write_text("VALUE = 'src'\n", encoding="utf-8")
            (project / "shared" / "ready.py").unlink()

            with self.assertRaisesRegex(
                FlowError,
                r"src/shared/ready\.py.*implementation_dependencies",
            ):
                implementation_lock(harness, load_flow(harness))

            flow_path = harness / "flow.yaml"
            raw = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
            raw["milestones"][0]["implementation_dependencies"] = [
                "src/shared/ready.py"
            ]
            flow_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
            lock = implementation_lock(harness, load_flow(harness))
            self.assertIn("project:src/shared/ready.py", lock["files"])

    def test_absolute_import_uses_declared_root_not_handler_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness, project = self._harness(
                Path(temp),
                "from compiler_support import VALUE\n\n"
                "def run(input_data, **_):\n"
                "    return {'outputs': {'result': {'value': VALUE}}}\n",
                dependencies=["runtime/compiler_support.py"],
            )
            runtime = project / "runtime"
            runtime.mkdir()
            (runtime / "compiler_support.py").write_text(
                "VALUE = 'declared'\n", encoding="utf-8"
            )
            sibling = harness / "milestones" / "ready" / "compiler_support.py"
            sibling.write_text("VALUE = 'staged-copy'\n", encoding="utf-8")

            lock = implementation_lock(harness, load_flow(harness))

            self.assertIn("project:runtime/compiler_support.py", lock["files"])
            self.assertNotIn(
                "skill:milestones/ready/compiler_support.py",
                lock["files"],
            )

    def test_imported_package_initializer_must_also_be_declared(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness, _ = self._harness(
                Path(temp),
                self._normal_local_import_handler(),
                dependencies=["shared/ready.py"],
                package_init=True,
            )
            with self.assertRaisesRegex(
                FlowError,
                r"shared/__init__\.py.*implementation_dependencies",
            ):
                implementation_lock(harness, load_flow(harness))

            flow_path = harness / "flow.yaml"
            raw = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
            raw["milestones"][0]["implementation_dependencies"].append(
                "shared/__init__.py"
            )
            flow_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
            lock = implementation_lock(harness, load_flow(harness))
            self.assertIn("project:shared/__init__.py", lock["files"])

    def test_standard_library_and_installed_package_imports_remain_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness, _ = self._harness(
                Path(temp),
                "import json\n"
                "import yaml\n\n"
                "def run(input_data, **_):\n"
                "    return {'outputs': {'result': {'json': json.dumps(input_data), 'yaml': yaml.safe_dump(input_data)}}}\n",
            )

            lock = implementation_lock(harness, load_flow(harness))

            self.assertIn(
                "skill:milestones/ready/assemble.py",
                lock["files"],
            )

    def test_nonliteral_dynamic_module_import_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness, _ = self._harness(
                Path(temp),
                "import importlib\n"
                "MODULE = '.'.join(['shared', 'ready'])\n"
                "READY = importlib.import_module(MODULE)\n\n"
                "def run(input_data, **_):\n"
                "    return {'outputs': {'result': {'value': READY.VALUE}}}\n",
                dependencies=["shared/ready.py"],
            )

            with self.assertRaisesRegex(
                FlowError,
                r"dynamic import target.*not statically provable",
            ):
                implementation_lock(harness, load_flow(harness))

    def test_literal_dynamic_file_import_must_name_a_frozen_file(self) -> None:
        relative = "../../../../../shared/ready.py"
        handler = (
            "import importlib.util\n"
            f"SPEC = importlib.util.spec_from_file_location('ready_dynamic', {relative!r})\n\n"
            "def run(input_data, **_):\n"
            "    return {'outputs': {'result': {'spec': SPEC.name}}}\n"
        )
        with tempfile.TemporaryDirectory() as temp:
            harness, _ = self._harness(Path(temp), handler)
            with self.assertRaisesRegex(
                FlowError,
                r"dynamic execution.*shared/ready\.py.*implementation_dependencies",
            ):
                implementation_lock(harness, load_flow(harness))

            flow_path = harness / "flow.yaml"
            raw = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
            raw["milestones"][0]["implementation_dependencies"] = [
                "shared/ready.py"
            ]
            flow_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
            lock = implementation_lock(harness, load_flow(harness))
            self.assertIn("project:shared/ready.py", lock["files"])

    def test_validation_never_imports_a_handler_with_an_open_closure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness, project = self._harness(
                Path(temp),
                self._normal_local_import_handler(write_marker=True),
            )

            with patch("validate_harness.verify_harness_runtime"):
                with self.assertRaisesRegex(
                    FlowError,
                    r"(?s)implementation import closure is open:.*shared/ready\.py",
                ):
                    validate_harness(harness, update_instruction=False)

            self.assertFalse((project / "handler-executed.txt").exists())


if __name__ == "__main__":
    unittest.main()
