from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import support  # noqa: F401  # puts scripts/ on sys.path
import m8m_build_steps

from flowstep_runtime import FlowError
from m8m_build_steps import (
    _assert_staged_install_manifest,
    _install_declared_members,
    _staged_install_members,
)
from flowstep_runtime import read_json
from source_bundle import digest_json


def _manifest_fixture(root: Path) -> tuple[Path, dict[str, object]]:
    stage = root / "stage"
    flow_id = "sample_v1"
    skill_name = "sample-skill"
    flow = stage / "flowsteps" / "flows" / flow_id
    skill = stage / ".agents" / "skills" / skill_name
    flow.mkdir(parents=True)
    skill.mkdir(parents=True)
    (flow / "flow.yaml").write_text("schema: flowstep_flow_v4\n", encoding="utf-8")
    (skill / "SKILL.md").write_text("# sample\n", encoding="utf-8")
    members = _staged_install_members(stage, flow_id, skill_name)
    return stage, {
        "stage_codebase": str(stage),
        "flow_id": flow_id,
        "skill_name": skill_name,
        "staged_members": members,
        "staged_members_digest": digest_json(members),
    }


def _transaction_fixture(
    root: Path,
) -> tuple[Path, Path, Path, list[dict[str, object]]]:
    stage = root / "stage"
    flow = stage / "flowsteps" / "flows" / "sample_v1"
    tool = stage / "flowsteps" / "tools" / "sample_tool"
    agent = stage / ".agents" / "skills" / "sample-skill"
    claude = stage / ".claude" / "skills" / "sample-skill"
    for path, text in (
        (flow / "flow.yaml", "schema: flowstep_flow_v4\nversion: new\n"),
        (
            flow / "launch.py",
            'M8M_CODEBASE_DISPATCH_ABI = "m8m_codebase_dispatch_v2"\nVERSION = "hardened"\n',
        ),
        (flow / "planning" / "m8m-flowchart.md", "# New flowchart\n"),
        (tool / "tool.py", "VERSION = 'new'\n"),
        (stage / "flowsteps" / "lib" / "runtime_support.py", "VERSION = 'new'\n"),
        (agent / "SKILL.md", "# New agent skill\n"),
        (
            agent / "scripts" / "m8m_run.py",
            'M8M_SKILL_POINTER_ABI = "m8m_skill_pointer_v2"\nVERSION = "hardened"\n',
        ),
        (claude / "SKILL.md", "# New Claude skill\n"),
        (
            claude / "scripts" / "m8m_run.py",
            'M8M_SKILL_POINTER_ABI = "m8m_skill_pointer_v2"\nVERSION = "hardened"\n',
        ),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return (
        stage,
        root / "codebase",
        root / "target",
        _staged_install_members(stage, "sample_v1", "sample-skill"),
    )


def _seed_old_install(codebase: Path, target: Path) -> None:
    for path, text in (
        (codebase / "flowsteps" / "flows" / "sample_v1" / "flow.yaml", "OLD FLOW\n"),
        (
            codebase / "flowsteps" / "flows" / "sample_v1" / "launch.py",
            'M8M_CODEBASE_DISPATCH_ABI = "m8m_codebase_dispatch_v1"\nVERSION = "old"\n',
        ),
        (codebase / "flowsteps" / "flows" / "sample_v1" / "keep.txt", "keep\n"),
        (
            codebase
            / "flowsteps"
            / "flows"
            / "sample_v1"
            / "runtime"
            / "releases"
            / ("a" * 64)
            / "runtime-manifest.json",
            "{}\n",
        ),
        (codebase / "flowsteps" / "tools" / "sample_tool" / "tool.py", "OLD TOOL\n"),
        (codebase / "flowsteps" / "lib" / "unrelated.py", "PRESERVED = True\n"),
        (codebase / ".agents" / "skills" / "sample-skill" / "SKILL.md", "OLD AGENT\n"),
        (
            codebase / ".agents" / "skills" / "sample-skill" / "scripts" / "m8m_run.py",
            'M8M_SKILL_POINTER_ABI = "m8m_skill_pointer_v1"\nVERSION = "old"\n',
        ),
        (codebase / ".claude" / "skills" / "sample-skill" / "SKILL.md", "OLD CLAUDE\n"),
        (
            codebase / ".claude" / "skills" / "sample-skill" / "scripts" / "m8m_run.py",
            'M8M_SKILL_POINTER_ABI = "m8m_skill_pointer_v1"\nVERSION = "old"\n',
        ),
        (target / "planning" / "m8m-flowchart.md", "OLD CHART\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class BuilderInstallIntegrityTests(unittest.TestCase):
    def test_install_preparation_does_not_treat_authoring_metadata_as_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage, codebase, target, _ = _transaction_fixture(root)
            metadata = stage / "flowsteps" / "flows" / "sample_v1" / "agents" / "openai.yaml"
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text(
                "interface:\n"
                "  default_prompt: Use $m8m-harness-builder to compile this source.\n"
                "canvas:\n"
                "  implementation_dependencies:\n"
                "    - scripts/run_flow.py\n",
                encoding="utf-8",
            )
            guide = stage / "flowsteps" / "flows" / "sample_v1" / "references" / "guide.md"
            guide.parent.mkdir(parents=True, exist_ok=True)
            guide.write_text(
                "The retired m8m-harness-builder/scripts/run_flow.py path must never be used.\n",
                encoding="utf-8",
            )
            members = _staged_install_members(stage, "sample_v1", "sample-skill")

            result = _install_declared_members(
                run_dir=root / "run",
                stage=stage,
                members=members,
                codebase=codebase,
                target=target,
                overwrite=True,
            )

            self.assertEqual(result["status"], "COMMITTED")
            installed = (
                codebase
                / "flowsteps"
                / "flows"
                / "sample_v1"
                / "agents"
                / "openai.yaml"
            )
            self.assertEqual(installed.read_bytes(), metadata.read_bytes())
            installed_guide = (
                codebase
                / "flowsteps"
                / "flows"
                / "sample_v1"
                / "references"
                / "guide.md"
            )
            self.assertEqual(installed_guide.read_bytes(), guide.read_bytes())

    def test_install_preparation_rejects_builder_dependent_tool_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage, codebase, target, _ = _transaction_fixture(root)
            poisoned = stage / "flowsteps" / "tools" / "sample_tool" / "helper.py"
            poisoned.write_text(
                "from pathlib import Path\n"
                "BUILDER = Path.home() / '.codex' / 'skills' / 'm8m-harness-builder'\n",
                encoding="utf-8",
            )
            members = _staged_install_members(stage, "sample_v1", "sample-skill")

            with self.assertRaisesRegex(FlowError, "mutable M8M Builder"):
                _install_declared_members(
                    run_dir=root / "run",
                    stage=stage,
                    members=members,
                    codebase=codebase,
                    target=target,
                    overwrite=True,
                )

            self.assertFalse(
                (codebase / "flowsteps" / "tools" / "sample_tool" / "helper.py").exists()
            )
            self.assertFalse(any((root / "run").rglob("install-journal.json")))

    def test_installed_v2_bootstrap_cannot_be_rewritten_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage, codebase, target, members = _transaction_fixture(root)
            _seed_old_install(codebase, target)
            installed = codebase / "flowsteps" / "flows" / "sample_v1" / "launch.py"
            installed.write_text(
                'M8M_CODEBASE_DISPATCH_ABI = "m8m_codebase_dispatch_v2"\n'
                'VERSION = "different-v2"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(FlowError, "v2 bootstrap ABI is immutable"):
                _install_declared_members(
                    run_dir=root / "run",
                    stage=stage,
                    members=members,
                    codebase=codebase,
                    target=target,
                    overwrite=True,
                )

    def test_explicit_overwrite_transactionally_upgrades_bootstrap_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage, codebase, target, members = _transaction_fixture(root)
            _seed_old_install(codebase, target)

            result = _install_declared_members(
                run_dir=root / "run",
                stage=stage,
                members=members,
                codebase=codebase,
                target=target,
                overwrite=True,
            )

            self.assertEqual(result["status"], "COMMITTED")
            self.assertIn(
                'VERSION = "hardened"',
                (codebase / "flowsteps" / "flows" / "sample_v1" / "launch.py").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertIn(
                'VERSION = "hardened"',
                (
                    codebase
                    / ".agents"
                    / "skills"
                    / "sample-skill"
                    / "scripts"
                    / "m8m_run.py"
                ).read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (codebase / "flowsteps" / "lib" / "runtime_support.py").read_text(
                    encoding="utf-8"
                ),
                "VERSION = 'new'\n",
            )
            self.assertEqual(
                (codebase / "flowsteps" / "lib" / "unrelated.py").read_text(
                    encoding="utf-8"
                ),
                "PRESERVED = True\n",
            )

    def test_mutated_or_injected_stage_file_invalidates_chosen_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage, generated = _manifest_fixture(root)
            (stage / "flowsteps" / "flows" / "sample_v1" / "flow.yaml").write_text(
                "schema: flowstep_flow_v4\n# changed\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FlowError, "changed after source selection"):
                _assert_staged_install_manifest(generated)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage, generated = _manifest_fixture(root)
            injected = stage / "flowsteps" / "tools" / "undeclared_extra" / "secret.txt"
            injected.parent.mkdir(parents=True)
            injected.write_text("not declared", encoding="utf-8")
            with self.assertRaisesRegex(FlowError, "changed after source selection"):
                _assert_staged_install_manifest(generated)

    def test_stale_destination_blocks_before_any_install_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage, codebase, target, members = _transaction_fixture(root)
            stale = codebase / ".agents" / "skills" / "sample-skill" / "SKILL.md"
            stale.parent.mkdir(parents=True)
            stale.write_text("STALE INSTALL\n", encoding="utf-8")

            with self.assertRaisesRegex(FlowError, "local install conflict"):
                _install_declared_members(
                    run_dir=root / "run",
                    stage=stage,
                    members=members,
                    codebase=codebase,
                    target=target,
                    overwrite=False,
                )

            self.assertEqual(stale.read_text(encoding="utf-8"), "STALE INSTALL\n")
            self.assertFalse((codebase / "flowsteps").exists())
            self.assertFalse(any((root / "run").rglob("install-journal.json")))

    def test_preparation_crash_preserves_old_roots_and_clean_retry_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage, codebase, target, members = _transaction_fixture(root)
            _seed_old_install(codebase, target)
            real_copy = m8m_build_steps.shutil.copy2
            copied_to_prepared = 0

            def interrupt_preparation(source, destination, *args, **kwargs):
                nonlocal copied_to_prepared
                result = real_copy(source, destination, *args, **kwargs)
                if ".prepared" in str(destination):
                    copied_to_prepared += 1
                    if copied_to_prepared == 1:
                        raise KeyboardInterrupt("simulated preparation crash")
                return result

            with patch.object(
                m8m_build_steps.shutil,
                "copy2",
                side_effect=interrupt_preparation,
            ):
                with self.assertRaisesRegex(KeyboardInterrupt, "preparation crash"):
                    _install_declared_members(
                        run_dir=root / "run",
                        stage=stage,
                        members=members,
                        codebase=codebase,
                        target=target,
                        overwrite=True,
                    )

            self.assertEqual(
                (codebase / "flowsteps" / "flows" / "sample_v1" / "flow.yaml").read_text(),
                "OLD FLOW\n",
            )
            self.assertEqual(
                (target / "planning" / "m8m-flowchart.md").read_text(),
                "OLD CHART\n",
            )
            self.assertFalse(any((root / "run").rglob("install-journal.json")))

            result = _install_declared_members(
                run_dir=root / "run",
                stage=stage,
                members=members,
                codebase=codebase,
                target=target,
                overwrite=True,
            )

            self.assertEqual(result["status"], "COMMITTED")
            self.assertEqual(
                (codebase / "flowsteps" / "flows" / "sample_v1" / "flow.yaml").read_text(),
                "schema: flowstep_flow_v4\nversion: new\n",
            )
            self.assertFalse(
                (codebase / "flowsteps" / "flows" / "sample_v1" / "keep.txt").exists()
            )
            self.assertTrue(
                (
                    codebase
                    / "flowsteps"
                    / "flows"
                    / "sample_v1"
                    / "runtime"
                    / "releases"
                    / ("a" * 64)
                    / "runtime-manifest.json"
                ).is_file()
            )

    def test_prepared_promotion_crash_recovers_forward_and_committed_rerun_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage, codebase, target, members = _transaction_fixture(root)
            _seed_old_install(codebase, target)
            real_replace = m8m_build_steps.os.replace
            promoted = 0

            def promote_then_interrupt(source, destination):
                nonlocal promoted
                result = real_replace(source, destination)
                if str(source).endswith(".prepared"):
                    promoted += 1
                    if promoted == 1:
                        raise KeyboardInterrupt("simulated promotion crash")
                return result

            with patch.object(
                m8m_build_steps.os,
                "replace",
                side_effect=promote_then_interrupt,
            ):
                with self.assertRaisesRegex(KeyboardInterrupt, "promotion crash"):
                    _install_declared_members(
                        run_dir=root / "run",
                        stage=stage,
                        members=members,
                        codebase=codebase,
                        target=target,
                        overwrite=True,
                    )

            journals = list((root / "run").rglob("install-journal.json"))
            self.assertEqual(len(journals), 1)
            self.assertEqual(read_json(journals[0])["status"], "PREPARED")
            for managed in (
                codebase / "flowsteps" / "flows" / "sample_v1",
                codebase / "flowsteps" / "tools" / "sample_tool",
                codebase / ".agents" / "skills" / "sample-skill",
                codebase / ".claude" / "skills" / "sample-skill",
                target / "planning",
            ):
                self.assertTrue(managed.is_dir())

            recovered = _install_declared_members(
                run_dir=root / "run",
                stage=stage,
                members=members,
                codebase=codebase,
                target=target,
                overwrite=True,
            )
            committed_at = recovered["committed_at"]
            self.assertEqual(recovered["status"], "COMMITTED")
            self.assertEqual(read_json(journals[0])["status"], "COMMITTED")
            self.assertEqual(
                (target / "planning" / "m8m-flowchart.md").read_text(),
                "# New flowchart\n",
            )

            with patch.object(
                m8m_build_steps.os,
                "replace",
                side_effect=AssertionError("committed rerun must not promote again"),
            ):
                repeated = _install_declared_members(
                    run_dir=root / "run",
                    stage=stage,
                    members=members,
                    codebase=codebase,
                    target=target,
                    overwrite=True,
                )
            self.assertEqual(repeated["committed_at"], committed_at)
            self.assertEqual(repeated["installed_files"], recovered["installed_files"])
            self.assertFalse(any(path.name.endswith(".backup") for path in root.rglob("*")))

    def test_crash_between_old_backup_and_new_root_has_deterministic_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage, codebase, target, members = _transaction_fixture(root)
            _seed_old_install(codebase, target)
            real_replace = m8m_build_steps.os.replace
            interrupted = False

            def backup_then_interrupt(source, destination):
                nonlocal interrupted
                result = real_replace(source, destination)
                if not interrupted and str(destination).endswith(".backup"):
                    interrupted = True
                    raise KeyboardInterrupt("simulated crash after old-root backup")
                return result

            with patch.object(
                m8m_build_steps.os,
                "replace",
                side_effect=backup_then_interrupt,
            ):
                with self.assertRaisesRegex(KeyboardInterrupt, "old-root backup"):
                    _install_declared_members(
                        run_dir=root / "run",
                        stage=stage,
                        members=members,
                        codebase=codebase,
                        target=target,
                        overwrite=True,
                    )

            journal_path = next((root / "run").rglob("install-journal.json"))
            journal = read_json(journal_path)
            self.assertEqual(journal["status"], "PREPARED")
            interrupted_row = next(
                row for row in journal["roots"] if Path(row["backup"]).exists()
            )
            self.assertFalse(Path(interrupted_row["destination"]).exists())
            self.assertTrue(Path(interrupted_row["prepared"]).is_dir())

            recovered = _install_declared_members(
                run_dir=root / "run",
                stage=stage,
                members=members,
                codebase=codebase,
                target=target,
                overwrite=True,
            )

            self.assertEqual(recovered["status"], "COMMITTED")
            self.assertEqual(
                (codebase / ".agents" / "skills" / "sample-skill" / "SKILL.md").read_text(),
                "# New agent skill\n",
            )

    def test_handled_promotion_failure_rolls_every_managed_root_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage, codebase, target, members = _transaction_fixture(root)
            _seed_old_install(codebase, target)
            real_replace = m8m_build_steps.os.replace
            promotions = 0

            def fail_second_promotion(source, destination):
                nonlocal promotions
                if str(source).endswith(".prepared"):
                    promotions += 1
                    if promotions == 2:
                        raise OSError("simulated handled promotion failure")
                return real_replace(source, destination)

            with patch.object(
                m8m_build_steps.os,
                "replace",
                side_effect=fail_second_promotion,
            ):
                with self.assertRaisesRegex(FlowError, "was rolled back"):
                    _install_declared_members(
                        run_dir=root / "run",
                        stage=stage,
                        members=members,
                        codebase=codebase,
                        target=target,
                        overwrite=True,
                    )

            self.assertEqual(
                (codebase / "flowsteps" / "flows" / "sample_v1" / "flow.yaml").read_text(),
                "OLD FLOW\n",
            )
            self.assertEqual(
                (codebase / "flowsteps" / "tools" / "sample_tool" / "tool.py").read_text(),
                "OLD TOOL\n",
            )
            self.assertEqual(
                (target / "planning" / "m8m-flowchart.md").read_text(),
                "OLD CHART\n",
            )
            self.assertFalse(any((root / "run").rglob("install-journal.json")))


class DeclaredDependencyInstallTests(unittest.TestCase):
    def fixture(self, root):
        stage, codebase, target, _ = _transaction_fixture(root)
        dependencies = {
            "flowsteps/sources/sample_source/__init__.py",
            "flowsteps/sources/sample_source/nested/helper.py",
            "business/operations/contracts/__init__.py",
            "business/operations/contracts/rules.json",
        }
        for relative in dependencies:
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n" if path.suffix == ".json" else "VERSION = 'new'\n")
        members = _staged_install_members(stage, "sample_v1", "sample-skill")
        return dict(run_dir=root / "run", stage=stage, members=members,
                    codebase=codebase, target=target, overwrite=True,
                    implementation_dependencies=dependencies)

    def snapshot(self, root):
        return {path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*") if path.is_file()}

    def test_declared_packages_preserve_unlisted_siblings_and_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = self.fixture(root)
            codebase = args["codebase"]
            sibling = codebase / "flowsteps/sources/sample_source/unrelated.txt"
            sibling.parent.mkdir(parents=True, exist_ok=True)
            sibling.write_bytes(b"untouched")
            outside = codebase / "business/operations/other_module/keep.txt"
            outside.parent.mkdir(parents=True, exist_ok=True)
            outside.write_bytes(b"outside managed roots")
            result = _install_declared_members(**args)
            self.assertEqual(result["status"], "COMMITTED")
            for relative in args["implementation_dependencies"]:
                self.assertEqual((codebase / relative).read_bytes(), (args["stage"] / relative).read_bytes())
            self.assertEqual(sibling.read_bytes(), b"untouched")
            self.assertEqual(outside.read_bytes(), b"outside managed roots")
            roots = {Path(row["destination"]).relative_to(codebase).as_posix()
                     for row in result["roots"] if Path(row["destination"]).is_relative_to(codebase)}
            self.assertIn("flowsteps/sources/sample_source", roots)
            self.assertNotIn("flowsteps/sources/sample_source/nested", roots)
            self.assertNotIn("business", roots)
            before = self.snapshot(codebase)
            self.assertEqual(_install_declared_members(**args)["status"], "COMMITTED")
            self.assertEqual(self.snapshot(codebase), before)

    def test_unknown_redirected_top_level_and_overlapping_paths_reject_before_writes(self):
        for scenario in ("undeclared", "redirect", "top_level", "overlap", "missing"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                args = self.fixture(root)
                dependencies = args["implementation_dependencies"]
                if scenario == "undeclared":
                    dependencies.remove("flowsteps/sources/sample_source/__init__.py")
                elif scenario == "redirect":
                    member = next(row for row in args["members"] if row["source"] == "business/operations/contracts/__init__.py")
                    member["destinations"] = ["codebase/business/operations/contracts/rules.json"]
                elif scenario in ("top_level", "overlap"):
                    relative = "root.py" if scenario == "top_level" else "flowsteps/__init__.py"
                    (args["stage"] / relative).write_text("VALUE = 1\n")
                    dependencies.add(relative)
                    args["members"] = _staged_install_members(args["stage"], "sample_v1", "sample-skill")
                else:
                    dependencies.add("missing/package.py")
                with self.assertRaises(FlowError):
                    _install_declared_members(**args)
                self.assertFalse(args["codebase"].exists())
                self.assertFalse(args["target"].exists())
                self.assertFalse(args["run_dir"].exists())

    def test_declared_dependency_isolation_and_stage_drift_reject_before_preparation(self):
        for poison in (False, True):
            with self.subTest(poison=poison), tempfile.TemporaryDirectory() as temp:
                args = self.fixture(Path(temp))
                source = args["stage"] / "flowsteps/sources/sample_source/__init__.py"
                source.write_text("from pathlib import Path\nBUILDER = Path.home() / '.codex' / 'skills' / 'm8m-harness-builder'\n")
                if poison:
                    args["members"] = _staged_install_members(args["stage"], "sample_v1", "sample-skill")
                expected = "mutable M8M Builder" if poison else "no longer matches"
                with self.assertRaisesRegex(FlowError, expected):
                    _install_declared_members(**args)
                self.assertFalse(args["codebase"].exists())
                self.assertFalse(args["run_dir"].exists())

    def test_fallback_failure_rolls_back_existing_and_fallback_roots(self):
        with tempfile.TemporaryDirectory() as temp:
            args = self.fixture(Path(temp))
            _seed_old_install(args["codebase"], args["target"])
            old = args["codebase"] / "business/operations/contracts/__init__.py"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"OLD DEPENDENCY\n")
            before = self.snapshot(args["codebase"])
            target_before = self.snapshot(args["target"])
            real_replace = m8m_build_steps.os.replace

            def fail_after_fallback(source, destination):
                if str(source).endswith(".prepared") and Path(destination).name == "sample_source":
                    raise OSError("failed after earlier fallback promotion")
                return real_replace(source, destination)

            with patch.object(m8m_build_steps.os, "replace", side_effect=fail_after_fallback):
                with self.assertRaisesRegex(FlowError, "rolled back"):
                    _install_declared_members(**args)
            self.assertEqual(self.snapshot(args["codebase"]), before)
            self.assertEqual(self.snapshot(args["target"]), target_before)
            self.assertFalse(any(args["run_dir"].rglob("install-journal.json")))

    def test_interrupted_fallback_promotion_resumes_same_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            args = self.fixture(Path(temp))
            real_replace = m8m_build_steps.os.replace

            def interrupt_fallback(source, destination):
                if str(source).endswith(".prepared") and Path(destination).name == "sample_source":
                    raise KeyboardInterrupt("fallback interruption")
                return real_replace(source, destination)

            with patch.object(m8m_build_steps.os, "replace", side_effect=interrupt_fallback):
                with self.assertRaisesRegex(KeyboardInterrupt, "fallback interruption"):
                    _install_declared_members(**args)
            journal = next(args["run_dir"].rglob("install-journal.json"))
            self.assertEqual(read_json(journal)["status"], "PREPARED")
            result = _install_declared_members(**args)
            self.assertEqual(result["status"], "COMMITTED")
            self.assertEqual(result["install_key"], read_json(journal)["install_key"])

    def test_fallback_destination_drift_and_link_ambiguity_reject(self):
        for scenario in ("drift", "link"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp:
                args = self.fixture(Path(temp))
                dependency = args["codebase"] / "business/operations/contracts/__init__.py"
                if scenario == "link":
                    real_check = m8m_build_steps._is_unsafe_link
                    def link_check(path):
                        return path == dependency.parent or real_check(path)
                    with patch.object(m8m_build_steps, "_is_unsafe_link", side_effect=link_check):
                        with self.assertRaisesRegex(FlowError, "unsafe link"):
                            _install_declared_members(**args)
                    self.assertFalse(args["run_dir"].exists())
                else:
                    _install_declared_members(**args)
                    dependency.write_bytes(b"changed after commit")
                    with self.assertRaisesRegex(FlowError, "read-back verification"):
                        _install_declared_members(**args)
                    self.assertEqual(dependency.read_bytes(), b"changed after commit")


if __name__ == "__main__":
    unittest.main()
