from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  # puts scripts/ on sys.path

from flowstep_runtime import read_json
from m8m_factory import BUILDER_MILESTONES, run_factory
from session_layout import load_chosen_output, resolve_chosen_output
from source_bundle import serialize_source_bundle


ROOT = Path(__file__).resolve().parents[1]


class BuilderSelfDogfoodTests(unittest.TestCase):
    def _run_clean_copy(self, root: Path, name: str) -> tuple[dict, dict, bytes]:
        target = root / name / "m8m-harness-builder"
        shutil.copytree(
            ROOT,
            target,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.pyo"),
        )
        result = run_factory(
            target,
            target,
            overwrite=True,
            harness_root=root / name / "runtime",
        )
        self.assertEqual(result["status"], "PASS", result)
        run_dir = Path(result["run_dir"])
        for milestone_id in BUILDER_MILESTONES:
            with self.subTest(run=name, milestone=milestone_id):
                self.assertEqual(result["milestones"][milestone_id]["status"], "PASS")
                manifest = load_chosen_output(run_dir, milestone_id)
                self.assertEqual(manifest["status"], "chosen")
                self.assertTrue(manifest["members"])
                judge = read_json(
                    run_dir
                    / "milestones"
                    / milestone_id
                    / "out"
                    / "judge-receipt.json"
                )
                self.assertEqual(judge["schema"], "m8m.milestone_judge_receipt.v1")
                self.assertEqual(judge["milestone_id"], milestone_id)
                self.assertEqual(judge["decision"], "PASS")
                self.assertEqual(judge["attempt"], 1)
                self.assertEqual(judge["blockers"], [])
                self.assertEqual(
                    judge["judge_ref"], "m8m_structural_admission@1.0.0"
                )
                self.assertEqual(judge["max_attempts"], 1)
                self.assertNotIn("details", judge)
                self.assertTrue(judge["reasons"])
                artifacts = list((run_dir / "artifacts").glob(f"{milestone_id}.*.json"))
                self.assertEqual(len(artifacts), 1)
                envelope = read_json(artifacts[0])
                self.assertEqual(envelope["schema"], "flowstep_output_v3")
                self.assertEqual(envelope["status"], "PASS")
                self.assertEqual(envelope["data"], manifest)
                self.assertEqual(envelope["evidence"]["attempt"], judge["attempt"])
                declared_members = {
                    member_id
                    for output in manifest["outputs"]
                    for member_id in output["member_ids"]
                }
                self.assertEqual(
                    declared_members,
                    {member["id"] for member in manifest["members"]},
                )
                for member in manifest["members"]:
                    self.assertTrue((run_dir / member["path"]).is_file())

        bundle = resolve_chosen_output(
            run_dir,
            "flow_generated",
            output_id="workflow_source_bundle",
        )
        self.assertIsInstance(bundle, dict)
        self.assertEqual(bundle["local_validation"]["status"], "SOURCE_VALID")
        self.assertEqual(bundle["local_validation"]["findings_count"], 0)
        judge_requirements = [
            item
            for item in bundle["implementation_requirements"]
            if item["kind"] == "milestone_judge"
        ]
        self.assertEqual(judge_requirements, [])
        resource_root = Path(result["source_bundle_resource_root"])
        self.assertTrue(Path(result["source_bundle_path"]).is_file())
        for resource in bundle["resources"]:
            self.assertTrue(
                (resource_root / resource["source_path"]).is_file(),
                resource["source_path"],
            )
        installation = resolve_chosen_output(
            run_dir,
            "skill_shipped",
            output_id="installation_receipt",
        )
        self.assertEqual(installation["source_bundle_path"], result["source_bundle_path"])
        self.assertEqual(
            installation["source_bundle_resource_root"],
            result["source_bundle_resource_root"],
        )
        self.assertEqual(
            installation["source_bundle_digest"],
            result["source_bundle_digest"],
        )
        workflow_package = resolve_chosen_output(
            run_dir,
            "harness_validated",
            output_id="workflow_package",
        )
        self.assertIsInstance(workflow_package, dict)
        package_path = Path(workflow_package["path"])
        self.assertTrue(package_path.is_file())
        self.assertEqual(
            installation["workflow_package_path"],
            result["workflow_package_path"],
        )
        self.assertEqual(
            installation["workflow_package_sha256"],
            result["workflow_package_sha256"],
        )
        self.assertEqual(
            installation["workflow_package_byte_count"],
            result["workflow_package_byte_count"],
        )
        self.assertEqual(
            installation["workflow_package_media_type"],
            result["workflow_package_media_type"],
        )
        self.assertEqual(package_path.stat().st_size, result["workflow_package_byte_count"])
        self.assertEqual(
            bundle["workflow_contracts"]["terminal_bindings"],
            [
                {
                    "name": "installation_receipt",
                    "from": "skill_shipped.m8m_builder_installation_receipt_v2",
                    "output": "installation_receipt",
                }
            ],
        )
        execution = read_json(run_dir / "flow-execution-record.json")
        self.assertEqual(execution["cache"]["mode"], "off")
        canonical = (
            Path(
                resolve_chosen_output(
                    run_dir,
                    "flow_generated",
                    output_id="staged_harness",
                )["harness_dir"]
            )
            / "compiled"
            / "workflow"
            / "flow.canonical.json"
        ).read_bytes()
        return result, bundle, canonical

    def test_builder_runs_all_five_nodes_and_two_clean_compiles_are_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first_result, first_bundle, first_canonical = self._run_clean_copy(root, "first")
            second_result, second_bundle, second_canonical = self._run_clean_copy(root, "second")

            self.assertEqual(first_canonical, second_canonical)
            self.assertEqual(serialize_source_bundle(first_bundle), serialize_source_bundle(second_bundle))
            self.assertEqual(
                first_result["source_bundle_digest"],
                second_result["source_bundle_digest"],
            )


if __name__ == "__main__":
    unittest.main()
