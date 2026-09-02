from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

import support  # noqa: F401

from flowstep_runtime import FlowError
from m8m_build_steps import _assert_frozen_skill_source, _audit, _generate, _toolbox
from test_skill_source import SkillFixture


class BuilderNativeSnapshotTests(unittest.TestCase):
    def _fixture(self, root: Path) -> SkillFixture:
        fixture = SkillFixture(root)
        handlers = root / "handlers"
        handlers.mkdir()
        for name in ("source", "finish"):
            (handlers / f"{name}.py").write_text(
                "def run(input_data, **_):\n"
                "    return {'outputs': {'result': dict(input_data)}}\n",
                encoding="utf-8",
            )
        return fixture

    @staticmethod
    def _input(skill: Path, codebase: Path) -> dict:
        return {
            "request": {
                "target": str(skill),
                "codebase": str(codebase),
                "flow_id": None,
                "skill_name": None,
                "overwrite": False,
            }
        }

    def test_audit_freezes_one_source_tree_and_ignores_later_target_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / "skill"
            self._fixture(skill)
            run_dir = root / "run"
            run_dir.mkdir()

            result = _audit(self._input(skill, root / "repo"), run_dir)
            audit = result["outputs"]["result"]
            snapshot = Path(audit["source_snapshot"])
            frozen_gem = snapshot / "references" / "source.md"
            frozen_text = frozen_gem.read_text(encoding="utf-8")

            (skill / "references" / "source.md").write_text(
                frozen_text + "\nA later, unchosen edit.\n",
                encoding="utf-8",
            )
            _, source, definition = _assert_frozen_skill_source(audit, run_dir)

            self.assertEqual(source, audit["skill_source"])
            self.assertEqual(definition, audit["compiled_flow"])
            self.assertEqual(frozen_gem.read_text(encoding="utf-8"), frozen_text)

    def test_mutating_the_frozen_snapshot_requires_replacing_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / "skill"
            self._fixture(skill)
            run_dir = root / "run"
            run_dir.mkdir()
            audit = _audit(self._input(skill, root / "repo"), run_dir)["outputs"]["result"]

            snapshot_gem = Path(audit["source_snapshot"]) / "references" / "source.md"
            snapshot_gem.write_text("changed after chosen audit\n", encoding="utf-8")

            with self.assertRaisesRegex(FlowError, "replace audit_complete"):
                _assert_frozen_skill_source(audit, run_dir)

    def test_declared_native_canvas_never_falls_back_to_legacy_import(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / "skill"
            fixture = self._fixture(skill)
            fixture.openai["canvas"]["entry"] = "missing"
            fixture.write()
            run_dir = root / "run"
            run_dir.mkdir()

            with self.assertRaisesRegex(FlowError, "declared skill-native source is invalid"):
                _audit(self._input(skill, root / "repo"), run_dir)

    def test_generation_uses_the_frozen_definition_for_both_output_ports(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / "skill"
            self._fixture(skill)
            run_dir = root / "run"
            run_dir.mkdir()
            input_data = self._input(skill, root / "repo")
            audit = _audit(input_data, run_dir)["outputs"]["result"]
            toolbox = _toolbox({**input_data, "source_audit": audit}, run_dir)["outputs"][
                "result"
            ]

            outputs = _generate(
                {
                    **input_data,
                    "source_audit": audit,
                    "toolbox_manifest": toolbox,
                },
                run_dir,
            )["outputs"]
            bundle = outputs["workflow_source_bundle"]
            staged = outputs["staged_harness"]
            canonical = json.loads(Path(staged["source_bundle_path"]).read_text(encoding="utf-8"))
            staged_definition = json.loads(
                (
                    Path(staged["harness_dir"])
                    / "compiled"
                    / "workflow"
                    / "flow.canonical.json"
                ).read_text(encoding="utf-8")
            )

            self.assertEqual(bundle, canonical)
            self.assertEqual(bundle["definition"], staged_definition)
            self.assertEqual(
                staged["source_bundle_digest"],
                bundle["source_bundle_proof"]["source_bundle_digest"],
            )
            self.assertNotIn("compiled_source_bundle", staged)


if __name__ == "__main__":
    unittest.main()
