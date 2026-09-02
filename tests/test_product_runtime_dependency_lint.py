from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from flowstep_runtime import FlowError, sha256_file
from m8m_build_steps import (
    _assert_product_runtime_isolated,
    _install_declared_members,
)


class ProductRuntimeDependencyLintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.codebase = Path(self.temporary.name)
        self.harness = self.codebase / "flowsteps" / "flows" / "audit_v1"
        self.harness.mkdir(parents=True)
        self.tool_root = self.codebase / "flowsteps" / "tools" / "audit_tool"
        self.tool_root.mkdir(parents=True)
        self.generated = {
            "harness_dir": str(self.harness),
            "stage_codebase": str(self.codebase),
            "skill_name": "audit-skill",
        }
        self.flow = {
            "milestones": [
                {
                    "id": "ready",
                    "execution": {
                        "tool_bindings": [
                            {"tool": "audit", "ref": "audit_tool@1.0.0"}
                        ]
                    },
                }
            ]
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def audit(self) -> None:
        _assert_product_runtime_isolated(self.generated, self.flow)

    def test_runner_json_builder_path_is_rejected(self) -> None:
        (self.tool_root / "tool.py").write_text(
            "import json, subprocess\n"
            "from pathlib import Path\n"
            "runner = json.loads((Path(__file__).parent / 'runner.json').read_text())['runner']\n"
            "subprocess.run(['python', runner], check=False)\n",
            encoding="utf-8",
        )
        (self.tool_root / "runner.json").write_text(
            json.dumps(
                {
                    "runner": (
                        "C:/Users/operator/.codex/skills/"
                        "m8m-harness-builder/scripts/run_flow.py"
                    )
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(FlowError, "mutable M8M Builder"):
            self.audit()

    def test_extensionless_runtime_config_is_scanned(self) -> None:
        (self.tool_root / "tool.py").write_text("VALUE = 'safe'\n", encoding="utf-8")
        (self.tool_root / "runner.conf").write_text(
            "runner: C:/Users/operator/.claude/skills/"
            "m8m-harness-builder/scripts/run_goal.py\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(FlowError, "mutable M8M Builder"):
            self.audit()

    def test_powershell_constant_concatenation_is_rejected(self) -> None:
        (self.tool_root / "tool.py").write_text(
            "import subprocess\nsubprocess.run(['powershell', 'helper.ps1'])\n",
            encoding="utf-8",
        )
        (self.tool_root / "helper.ps1").write_text(
            "$name = 'm8m-' + 'harness-builder'\n"
            '& "$HOME/.codex/skills/$name/scripts/run_flow.py"\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(FlowError, "mutable M8M Builder"):
            self.audit()

    def test_python_deterministic_bytes_name_is_rejected(self) -> None:
        encoded = list(b"m8m-harness-builder")
        (self.tool_root / "tool.py").write_text(
            "import subprocess, sys\n"
            "from pathlib import Path\n"
            f"name = bytes({encoded!r}).decode()\n"
            "runner = Path.home() / '.codex' / 'skills' / name / 'scripts' / 'run_flow.py'\n"
            "subprocess.run([sys.executable, str(runner)], check=False)\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(FlowError, "mutable M8M Builder"):
            self.audit()

    def test_human_facing_builder_mention_is_allowed(self) -> None:
        (self.tool_root / "tool.py").write_text("VALUE = 'safe'\n", encoding="utf-8")
        (self.tool_root / "instructions.md").write_text(
            "m8m-harness-builder is build-time provenance only. "
            "This product uses scripts/m8m_run.py.\n",
            encoding="utf-8",
        )

        self.audit()

    def test_builder_schema_identifier_is_not_an_environment_binding(self) -> None:
        (self.tool_root / "tool.py").write_text("VALUE = 'safe'\n", encoding="utf-8")
        (self.tool_root / "schema.json").write_text(
            json.dumps({"$id": "m8m_builder_input_v1.schema.json"}),
            encoding="utf-8",
        )

        self.audit()

    def test_unrelated_planning_prose_is_outside_execution_closure(self) -> None:
        (self.tool_root / "tool.py").write_text("VALUE = 'safe'\n", encoding="utf-8")
        planning = self.harness / "planning" / "migration-notes.md"
        planning.parent.mkdir()
        planning.write_text(
            "Historical note: C:/Users/operator/.codex/skills/"
            "m8m-harness-builder/scripts/run_flow.py was the retired launcher.\n",
            encoding="utf-8",
        )

        self.audit()

    def test_subprocess_flowstep_is_rejected(self) -> None:
        (self.tool_root / "tool.py").write_text(
            "import subprocess\n"
            "def run(value):\n"
            "    return subprocess.run(['worker'], check=True)\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(FlowError, "closed FlowStep execution violation"):
            self.audit()

    def test_recursive_run_directory_discovery_is_rejected(self) -> None:
        (self.tool_root / "tool.py").write_text(
            "from pathlib import Path\n"
            "def run(run_dir):\n"
            "    return list(Path(run_dir).rglob('*'))\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(FlowError, "recursive filesystem discovery"):
            self.audit()

    def test_rg_execution_root_discovery_is_rejected_without_shell_execution(self) -> None:
        (self.tool_root / "tool.py").write_text(
            "COMMAND = r'rg --files C:\\\\NisanRuntime'\n"
            "def run():\n"
            "    return execute(COMMAND)\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(FlowError, "uses rg to discover"):
            self.audit()

    def test_bounded_source_tree_discovery_is_allowed(self) -> None:
        (self.tool_root / "tool.py").write_text(
            "from pathlib import Path\n"
            "def run(source_root):\n"
            "    return [path.name for path in Path(source_root).rglob('*.json')]\n",
            encoding="utf-8",
        )

        self.audit()

    def test_runtime_control_manifest_write_is_rejected(self) -> None:
        (self.tool_root / "tool.py").write_text(
            "from pathlib import Path\n"
            "def run(run_dir):\n"
            "    Path(run_dir, 'chosen-output.json').write_text('{}')\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(FlowError, "runtime-owned control artifact"):
            self.audit()

    def test_subprocess_in_declared_implementation_dependency_is_rejected(self) -> None:
        (self.tool_root / "tool.py").write_text("VALUE = 'safe'\n", encoding="utf-8")
        dependency = self.codebase / "product" / "worker.py"
        dependency.parent.mkdir(parents=True)
        dependency.write_text(
            "import subprocess\n"
            "def run():\n"
            "    return subprocess.run(['worker'])\n",
            encoding="utf-8",
        )
        self.flow["milestones"][0]["implementation_dependencies"] = [
            "product/worker.py"
        ]

        with self.assertRaisesRegex(FlowError, "closed FlowStep execution violation"):
            self.audit()

    def test_flow_level_runtime_control_writer_is_not_misclassified_as_flowstep(self) -> None:
        (self.tool_root / "tool.py").write_text("VALUE = 'safe'\n", encoding="utf-8")
        runtime = self.codebase / "runtime" / "engine.py"
        runtime.parent.mkdir(parents=True)
        runtime.write_text(
            "from pathlib import Path\n"
            "def commit(out_dir):\n"
            "    Path(out_dir, 'chosen-output.json').write_text('{}')\n",
            encoding="utf-8",
        )
        self.flow["implementation_dependencies"] = ["runtime/engine.py"]

        self.audit()

    def test_split_receipt_flowstep_is_rejected(self) -> None:
        (self.tool_root / "tool.py").write_text("VALUE = 'safe'\n", encoding="utf-8")
        self.flow["milestones"][0]["flowsteps"] = [
            {"id": "generate_receipt", "tool": "audit_tool@1.0.0"}
        ]

        with self.assertRaisesRegex(FlowError, "receipt-generation FlowSteps are forbidden"):
            self.audit()

    def test_only_finalize_may_follow_approval(self) -> None:
        (self.tool_root / "tool.py").write_text("VALUE = 'safe'\n", encoding="utf-8")
        self.flow["milestones"][0]["flowsteps"] = [
            {"id": "request_approval", "tool": "audit_tool@1.0.0"},
            {"id": "stability_check", "tool": "audit_tool@1.0.0"},
        ]

        with self.assertRaisesRegex(FlowError, "post-approval FlowSteps"):
            self.audit()

    def test_declared_finalize_may_follow_approval(self) -> None:
        (self.tool_root / "tool.py").write_text("VALUE = 'safe'\n", encoding="utf-8")
        self.flow["milestones"][0]["flowsteps"] = [
            {"id": "request_approval", "tool": "audit_tool@1.0.0"},
            {"id": "finalize", "tool": "audit_tool@1.0.0"},
        ]

        self.audit()

    def test_install_transaction_does_not_lint_unrelated_planning_prose(self) -> None:
        stage = self.codebase / "stage"
        relative = "flowsteps/flows/audit_v1/planning/migration-notes.md"
        source = stage.joinpath(*relative.split("/"))
        source.parent.mkdir(parents=True)
        source.write_text(
            "Historical note: C:/Users/operator/.codex/skills/"
            "m8m-harness-builder/scripts/run_flow.py was retired.\n",
            encoding="utf-8",
        )
        installed_codebase = self.codebase / "installed-codebase"
        target = self.codebase / "installed-skill"
        run_dir = self.codebase / "install-run"
        run_dir.mkdir()

        result = _install_declared_members(
            run_dir=run_dir,
            stage=stage,
            members=[
                {
                    "source": relative,
                    "digest": f"sha256:{sha256_file(source)}",
                    "destinations": [f"codebase/{relative}"],
                }
            ],
            codebase=installed_codebase,
            target=target,
            overwrite=True,
        )

        destination = installed_codebase.joinpath(*relative.split("/"))
        self.assertEqual(result["status"], "COMMITTED")
        self.assertEqual(destination.read_bytes(), source.read_bytes())


if __name__ == "__main__":
    unittest.main()
