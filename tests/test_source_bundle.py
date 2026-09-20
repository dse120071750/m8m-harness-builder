from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

import support  # noqa: F401  # puts scripts/ on sys.path
from jsonschema import Draft202012Validator, RefResolver
from jsonschema.exceptions import ValidationError

from source_bundle import (
    SourceBundleError,
    _validate_requirement_closure,
    canonical_json,
    compile_source_bundle as _compile_source_bundle,
    digest_json,
    serialize_source_bundle,
    verify_source_bundle,
)
from package_archive import WorkflowPackageError, build_workflow_package


def compile_source_bundle(**kwargs):
    """Most fixtures model explicit legacy-import wiring, not native sources."""

    kwargs.setdefault("allow_legacy_wiring_only", True)
    return _compile_source_bundle(**kwargs)


CONTRACT_LOCK = {
    "schema": "m8m.contract_bundle_lock.v1",
    "id": "m8m-contracts/2026-08-30",
    "digest": "sha256:" + "a" * 64,
}


def _definition(*, windows_paths: bool = False, ai: bool = False) -> dict:
    separator = "\\" if windows_paths else "/"
    milestone = {
        "outputs": [
            {
                "required": True,
                "cardinality": "one",
                "kind": "json",
                "name": "Audited source",
                "id": "audit",
            }
        ],
        "handler": f"milestones{separator}audit{separator}assemble.py",
        "output_schema": f"schemas{separator}audit.schema.json",
        "output_contract": "m8m.source_audit.v1",
        "success": "The source audit is complete.",
        "id": "source_audit",
    }
    if ai:
        milestone.update(
            {
                "gem": f"references{separator}source_audit.md",
                "input_schema": f"schemas{separator}audit_input.schema.json",
                "draft_schema": f"schemas{separator}audit_draft.schema.json",
                "intelligence": "completion",
                "tools": [],
            }
        )
    return {
        "milestones": [milestone],
        "context_policy": "isolated",
        "version": 1,
        "flow_id": "builder_source_compile_v1",
        "schema": "flowstep_flow_v4",
    }


def _resources(*, windows_paths: bool = False, reverse: bool = False) -> list[dict]:
    separator = "\\" if windows_paths else "/"
    rows = [
        {
            "media_type": "text/markdown",
            "source_path": f"references{separator}source_audit.md",
            "role": "milestone_gem",
            "kind": "gem",
            "ref": "gem.source_audit.v1",
        },
        {
            "ref": "schema.source_audit.v1",
            "kind": "json_schema",
            "role": "milestone_output_schema",
            "source_path": f"schemas{separator}audit.schema.json",
            "media_type": "application/schema+json",
        },
        {
            "ref": "schema.source_audit_input.v1",
            "kind": "json_schema",
            "role": "milestone_input_schema",
            "source_path": f"schemas{separator}audit_input.schema.json",
            "media_type": "application/schema+json",
        },
        {
            "ref": "schema.source_audit_draft.v1",
            "kind": "json_schema",
            "role": "milestone_draft_schema",
            "source_path": f"schemas{separator}audit_draft.schema.json",
            "media_type": "application/schema+json",
        },
        {
            "ref": "schema.workflow_request.v1",
            "kind": "json_schema",
            "role": "workflow_request_schema",
            "source_path": f"schemas{separator}workflow_request.schema.json",
            "media_type": "application/schema+json",
        },
        {
            "ref": "schema.workflow_configuration.v1",
            "kind": "json_schema",
            "role": "workflow_configuration_schema",
            "source_path": f"schemas{separator}workflow_configuration.schema.json",
            "media_type": "application/schema+json",
        },
        {
            "ref": "schema.workflow_result.v1",
            "kind": "json_schema",
            "role": "workflow_result_schema",
            "source_path": f"schemas{separator}workflow_result.schema.json",
            "media_type": "application/schema+json",
        },
    ]
    return list(reversed(rows)) if reverse else rows


def _implementations(*, reverse: bool = False, build_state: str = "built") -> list[dict]:
    rows = [
        {
            "ref": "handler.source_audit@3.0.0",
            "kind": "milestone_handler",
            "role": "candidate_executor",
            "milestone_id": "source_audit",
            "runtime_abi": "m8m_milestone_handler_v1",
            "entrypoint": "execute_candidate",
            "build_state": build_state,
            "digest": "sha256:" + "1" * 64,
            "byte_count": 128,
        },
        {
            "build_state": "built",
            "entrypoint": "judge",
            "runtime_abi": "m8m_milestone_judge_v1",
            "role": "milestone_judge",
            "milestone_id": "source_audit",
            "kind": "milestone_judge",
            "ref": "judge.source_audit@3.0.0",
            "digest": "sha256:" + "2" * 64,
            "byte_count": 96,
        },
    ]
    return list(reversed(rows)) if reverse else rows


def _profiles(*, reverse_keys: bool = False) -> list[dict]:
    row = {
        "ref": "agent_profile.builder_candidate.v1",
        "milestone_id": "source_audit",
        "role": "candidate_executor",
        "executor_ref": "handler.source_audit@3.0.0",
        "gem_ref": "gem.source_audit.v1",
        "schema_refs": {
            "input_schema": "schema.source_audit_input.v1",
            "draft_schema": "schema.source_audit_draft.v1",
            "output_schema": "schema.source_audit.v1",
        },
        "tool_refs": [],
        "capability_ids": ["filesystem.read.source", "filesystem.write.stage"],
        "model_configuration": {"model": "codex", "reasoning": "medium"},
        "token_budget": {"max_input_tokens": 8192, "max_output_tokens": 2048},
        "timeout_seconds": 300,
        "build_state": "built",
    }
    if reverse_keys:
        return [{key: row[key] for key in reversed(list(row))}]
    return [row]


def _capabilities(*, reverse: bool = False) -> list[dict]:
    rows = [
        {
            "id": "filesystem.read.source",
            "ref": "capability.filesystem.read.source@1.0.0",
            "milestone_id": "source_audit",
            "access": "read",
            "side_effects": "none",
            "build_state": "built",
        },
        {
            "id": "filesystem.write.stage",
            "ref": "capability.filesystem.write.stage@1.0.0",
            "milestone_id": "source_audit",
            "access": "write",
            "side_effects": "local",
            "build_state": "built",
        },
    ]
    return list(reversed(rows)) if reverse else rows


def _write_sources(root: Path, *, changed_gem: bool = False) -> None:
    (root / "references").mkdir(parents=True)
    (root / "schemas").mkdir()
    gem = "# Rule of success\n\nSource is valid."
    if changed_gem:
        gem += "\nChanged."
    (root / "references" / "source_audit.md").write_text(gem, encoding="utf-8")
    (root / "schemas" / "audit.schema.json").write_text(
        '{"additionalProperties":false,"type":"object"}', encoding="utf-8"
    )
    for name in ("audit_input.schema.json", "audit_draft.schema.json"):
        (root / "schemas" / name).write_text(
            '{"additionalProperties":false,"type":"object"}', encoding="utf-8"
        )
    for name in (
        "workflow_request.schema.json",
        "workflow_configuration.schema.json",
        "workflow_result.schema.json",
    ):
        (root / "schemas" / name).write_text(
            '{"additionalProperties":false,"type":"object"}', encoding="utf-8"
        )


def _workflow_contracts(definition: dict, *, include_root: bool = True) -> dict:
    milestone = definition["milestones"][0]
    contracts = {
        "milestones": [
            {
                "milestone_id": milestone["id"],
                "input_schema": milestone.get("input_schema", ""),
                "inputs": copy.deepcopy(milestone.get("inputs", {})),
                "output_contract": milestone["output_contract"],
                "output_schema": milestone["output_schema"],
                "outputs": copy.deepcopy(milestone["outputs"]),
            }
        ],
        "entry_milestones": ["source_audit"],
        "terminal_milestones": ["source_audit"],
    }
    if include_root:
        contracts = {
            "request_schema": "schemas/workflow_request.schema.json",
            "configuration_schema": "schemas/workflow_configuration.schema.json",
            "result_schema": "schemas/workflow_result.schema.json",
            "terminal_bindings": [
                {
                    "output": "audit",
                    "from": "source_audit.m8m.source_audit.v1",
                    "name": "audit_result",
                }
            ],
            **contracts,
        }
    return contracts


def _rehash_workflow_contracts(bundle: dict) -> None:
    digest = digest_json(bundle["workflow_contracts"])
    bundle["workflow_contracts_digest"] = digest
    bundle["source_bundle_proof"]["workflow_contracts_digest"] = digest
    bundle["source_bundle_proof"]["source_bundle_digest"] = digest_json(
        {
            key: value
            for key, value in bundle.items()
            if key != "source_bundle_proof"
        }
    )


def _rehash_resources(bundle: dict) -> None:
    digest = digest_json(bundle["resources"])
    bundle["resource_requirements_digest"] = digest
    bundle["source_bundle_proof"]["resource_requirements_digest"] = digest
    bundle["source_bundle_proof"]["source_bundle_digest"] = digest_json(
        {
            key: value
            for key, value in bundle.items()
            if key != "source_bundle_proof"
        }
    )


def _compile(
    root: Path,
    *,
    windows_paths: bool = False,
    reverse: bool = False,
    build_state: str = "built",
    require_ready: bool = False,
    validation: dict | None = None,
) -> dict:
    definition = _definition(windows_paths=windows_paths, ai=True)
    profiles = _profiles(reverse_keys=reverse)
    if build_state != "built":
        profiles = [
            {
                "ref": profiles[0]["ref"],
                "milestone_id": "source_audit",
                "role": "candidate_executor",
                "build_state": "BUILD_REQUIRED",
                "missing": ["implementation:handler.source_audit@3.0.0:BUILD_REQUIRED"],
            }
        ]
    return compile_source_bundle(
        source_root=root,
        contract_bundle=dict(CONTRACT_LOCK),
        definition=definition,
        workflow_contracts=_workflow_contracts(definition),
        resources=_resources(windows_paths=windows_paths, reverse=reverse),
        implementation_requirements=_implementations(
            reverse=reverse, build_state=build_state
        ),
        agent_profile_requirements=profiles,
        capability_requirements=_capabilities(reverse=reverse),
        observer={
            "milestones": [{"title": "Audit source", "id": "source_audit"}],
            "workflow": {"summary": "Compile source.", "title": "Builder compile"},
        },
        validation=validation,
        require_ready=require_ready,
    )


class DeterministicSourceBundleTests(unittest.TestCase):
    def test_one_exact_judge_implementation_can_serve_multiple_milestones(self) -> None:
        milestones = []
        for milestone_id in ("first", "second", "third"):
            milestones.append(
                {
                    "id": milestone_id,
                    "loop": "judge",
                    "worker": "shared_judge@1.0.0",
                    "judge_abi": "m8m_milestone_judge_v1",
                    "intelligence": "none",
                    "execution": {
                        "judge": {"ref": "shared_judge@1.0.0"},
                        "tool_bindings": [],
                    },
                }
            )
        shared_judge = {
            "ref": "shared_judge@1.0.0",
            "kind": "milestone_judge",
            "role": "milestone_judge",
            "runtime_abi": "m8m_milestone_judge_v1",
            "entrypoint": "run",
            "build_state": "built",
            "digest": "sha256:" + "2" * 64,
            "byte_count": 96,
        }

        _validate_requirement_closure(
            {"milestones": milestones},
            resources=[],
            implementations=[shared_judge],
            profiles=[],
            capabilities=[],
        )

    def test_validated_bundle_builds_one_deterministic_transport_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            bundle = _compile(root)

            first, first_manifest = build_workflow_package(
                bundle, resource_root=root
            )
            second, second_manifest = build_workflow_package(
                bundle, resource_root=root
            )

            self.assertEqual(first, second)
            self.assertEqual(first_manifest, second_manifest)
            self.assertEqual(
                first_manifest["source_bundle_digest"],
                bundle["source_bundle_proof"]["source_bundle_digest"],
            )
            with zipfile.ZipFile(BytesIO(first)) as archive:
                names = archive.namelist()
                self.assertEqual(names[:2], ["package-manifest.json", "source-bundle.json"])
                self.assertEqual(archive.read("source-bundle.json"), serialize_source_bundle(bundle))
                self.assertTrue(all(item.compress_type == zipfile.ZIP_STORED for item in archive.infolist()))
                self.assertEqual(
                    len(names),
                    2 + len({item["source_path"] for item in bundle["resources"]}),
                )

    def test_transport_packaging_rejects_resource_drift_after_compile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            bundle = _compile(root)
            resource_path = root / bundle["resources"][0]["source_path"]
            resource_path.write_bytes(resource_path.read_bytes() + b"drift")

            with self.assertRaisesRegex(WorkflowPackageError, "changed"):
                build_workflow_package(bundle, resource_root=root)

    def test_bundle_is_stable_across_roots_mtimes_key_order_and_path_separators(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            first_root = Path(temp) / "first" / "checkout"
            second_root = Path(temp) / "elsewhere" / "checkout"
            _write_sources(first_root)
            _write_sources(second_root)
            os.utime(first_root / "references" / "source_audit.md", (1_700_000_000, 1_700_000_000))
            os.utime(second_root / "references" / "source_audit.md", (1_800_000_000, 1_800_000_000))

            first = _compile(first_root)
            second = _compile(
                second_root,
                windows_paths=True,
                reverse=True,
                validation={
                    "status": "BUILD_REQUIRED",
                    "prose": "ignored advisory text",
                    "generated_at": "2099-01-01T00:00:00Z",
                    "local_path": str(second_root.resolve()),
                },
            )

            self.assertEqual(serialize_source_bundle(first), serialize_source_bundle(second))
            self.assertEqual(
                first["source_bundle_proof"]["source_bundle_digest"],
                second["source_bundle_proof"]["source_bundle_digest"],
            )
            encoded = serialize_source_bundle(first).decode("utf-8")
            self.assertNotIn(str(first_root.resolve()), encoded)
            self.assertNotIn(str(second_root.resolve()), encoded)
            self.assertNotIn("validation", first)
            self.assertNotIn("generated_at", encoded)
            self.assertEqual(
                first["local_validation"],
                {
                    "schema": "m8m.builder_validation_summary.v1",
                    "builder": "m8m-harness-builder/3.1",
                    "policy_version": "m8m-builder-validation/3",
                    "contract_bundle_digest": CONTRACT_LOCK["digest"],
                    "status": "BUILD_REQUIRED",
                    "findings_count": 0,
                    "advisory": True,
                },
            )
            self.assertEqual(
                first["source_bundle_proof"]["local_validation_digest"],
                first["local_validation_digest"],
            )

    def test_resource_bytes_change_component_and_whole_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            first_root = Path(temp) / "first"
            second_root = Path(temp) / "second"
            _write_sources(first_root)
            _write_sources(second_root, changed_gem=True)
            first = _compile(first_root)
            second = _compile(second_root)
            self.assertNotEqual(
                first["resource_requirements_digest"], second["resource_requirements_digest"]
            )
            self.assertNotEqual(
                first["source_bundle_proof"]["source_bundle_digest"],
                second["source_bundle_proof"]["source_bundle_digest"],
            )

    def test_text_resources_are_canonical_across_line_endings_and_unicode_forms(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            first_root = Path(temp) / "first"
            second_root = Path(temp) / "second"
            _write_sources(first_root)
            _write_sources(second_root)
            (first_root / "references" / "source_audit.md").write_bytes(
                "# Caf\u00e9\r\n\r\nSource is valid.\r".encode("utf-8")
            )
            (second_root / "references" / "source_audit.md").write_bytes(
                "# Cafe\u0301\n\nSource is valid.\n".encode("utf-8")
            )

            first = _compile(first_root)
            second = _compile(second_root)
            self.assertEqual(serialize_source_bundle(first), serialize_source_bundle(second))

    def test_binary_resources_remain_byte_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            first_root = Path(temp) / "first"
            second_root = Path(temp) / "second"
            _write_sources(first_root)
            _write_sources(second_root)
            (first_root / "assets").mkdir()
            (second_root / "assets").mkdir()
            (first_root / "assets" / "blob.bin").write_bytes(b"\x00\r\n\xff")
            (second_root / "assets" / "blob.bin").write_bytes(b"\x00\n\xff")
            binary = {
                "ref": "asset.binary.v1",
                "kind": "file",
                "role": "binary_fixture",
                "source_path": "assets/blob.bin",
                "media_type": "application/octet-stream",
            }

            first = compile_source_bundle(
                source_root=first_root,
                contract_bundle=CONTRACT_LOCK,
                definition=_definition(),
                resources=[*_resources(), binary],
            )
            second = compile_source_bundle(
                source_root=second_root,
                contract_bundle=CONTRACT_LOCK,
                definition=_definition(),
                resources=[*_resources(), binary],
            )
            self.assertNotEqual(
                first["resource_requirements_digest"], second["resource_requirements_digest"]
            )

    def test_proof_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            bundle = _compile(root)
            tampered = copy.deepcopy(bundle)
            tampered["observer"]["workflow"]["title"] = "Tampered"
            with self.assertRaisesRegex(SourceBundleError, "observer_digest mismatch"):
                verify_source_bundle(tampered)

            tampered = copy.deepcopy(bundle)
            tampered["source_bundle_proof"]["source_bundle_digest"] = "sha256:" + "b" * 64
            with self.assertRaisesRegex(SourceBundleError, "source_bundle_proof mismatch"):
                verify_source_bundle(tampered)

            tampered = copy.deepcopy(bundle)
            tampered["local_validation"]["findings_count"] = 1
            with self.assertRaisesRegex(SourceBundleError, "local_validation_digest mismatch"):
                verify_source_bundle(tampered)

    def test_canonical_json_rejects_non_json_and_non_finite_numbers(self) -> None:
        with self.assertRaisesRegex(SourceBundleError, "non-finite"):
            canonical_json({"temperature": float("nan")})
        with self.assertRaisesRegex(SourceBundleError, "non-JSON"):
            canonical_json({"not_json": {1, 2}})


class WorkflowContractBoundaryTests(unittest.TestCase):
    def test_wiring_only_contracts_require_explicit_legacy_import_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            definition = _definition()
            explicit = compile_source_bundle(
                source_root=root,
                contract_bundle=CONTRACT_LOCK,
                definition=definition,
                workflow_contracts=_workflow_contracts(
                    definition, include_root=False
                ),
                allow_legacy_wiring_only=True,
            )
            derived = compile_source_bundle(
                source_root=root,
                contract_bundle=CONTRACT_LOCK,
                definition=definition,
                allow_legacy_wiring_only=True,
            )
            expected = {
                "milestones": [
                    {
                        "milestone_id": "source_audit",
                        "input_schema": "",
                        "inputs": {},
                        "output_contract": "m8m.source_audit.v1",
                        "output_schema": "schemas/audit.schema.json",
                        "outputs": definition["milestones"][0]["outputs"],
                    }
                ],
                "entry_milestones": ["source_audit"],
                "terminal_milestones": ["source_audit"],
            }
            self.assertEqual(explicit["workflow_contracts"], expected)
            self.assertEqual(derived["workflow_contracts"], expected)
            self.assertEqual(verify_source_bundle(explicit), explicit["source_bundle_proof"])
            self.assertNotIn("request_schema", explicit["workflow_contracts"])

            with self.assertRaisesRegex(SourceBundleError, "explicit legacy-import staging"):
                _compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=definition,
                )

    def test_wiring_and_root_groups_are_closed_and_required_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            definition = _definition()
            canonical = _workflow_contracts(definition)

            for field in ("milestones", "entry_milestones", "terminal_milestones"):
                with self.subTest(missing=field):
                    malformed = copy.deepcopy(canonical)
                    malformed.pop(field)
                    with self.assertRaisesRegex(SourceBundleError, "missing fields"):
                        compile_source_bundle(
                            source_root=root,
                            contract_bundle=CONTRACT_LOCK,
                            definition=definition,
                            workflow_contracts=malformed,
                        )

            partial = _workflow_contracts(definition, include_root=False)
            partial["request_schema"] = "schemas/workflow_request.schema.json"
            with self.assertRaisesRegex(SourceBundleError, "root contract group is missing"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=definition,
                    workflow_contracts=partial,
                )

            unknown = copy.deepcopy(canonical)
            unknown["unexpected"] = True
            with self.assertRaisesRegex(SourceBundleError, "unknown fields"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=definition,
                    workflow_contracts=unknown,
                )

    def test_schema_refs_must_be_safe_relative_json_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            definition = _definition()
            cases = (
                "/absolute.json",
                "C:/absolute.json",
                "../outside.json",
                "schemas\\request.json",
                "schemas/*.json",
                "schemas/request.yaml",
                "./schemas/request.json",
                "schemas//request.json",
                "https://example.test/request.json",
            )
            for field in ("request_schema", "configuration_schema", "result_schema"):
                for value in cases:
                    with self.subTest(field=field, value=value):
                        malformed = _workflow_contracts(definition)
                        malformed[field] = value
                        with self.assertRaisesRegex(
                            SourceBundleError, "safe relative .json schema ref"
                        ):
                            compile_source_bundle(
                                source_root=root,
                                contract_bundle=CONTRACT_LOCK,
                                definition=definition,
                                workflow_contracts=malformed,
                            )

            malformed = _workflow_contracts(definition)
            malformed["milestones"][0]["output_schema"] = "../audit.json"
            with self.assertRaisesRegex(
                SourceBundleError, "safe relative .json schema ref"
            ):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=definition,
                    workflow_contracts=malformed,
                )

    def test_root_schema_refs_must_be_bound_to_included_schema_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            definition = _definition()
            with self.assertRaisesRegex(
                SourceBundleError, "not bound to an included JSON Schema resource"
            ):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=definition,
                    workflow_contracts=_workflow_contracts(definition),
                )

            bundle = _compile(root)
            bundle["resources"] = [
                row
                for row in bundle["resources"]
                if row["source_path"] != "schemas/workflow_request.schema.json"
            ]
            _rehash_resources(bundle)
            with self.assertRaisesRegex(
                SourceBundleError, "not bound to an included JSON Schema resource"
            ):
                verify_source_bundle(bundle)

    def test_milestone_rows_reuse_closed_flowstep_output_and_binding_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            definition = _definition()

            row_unknown = _workflow_contracts(definition)
            row_unknown["milestones"][0]["unexpected"] = True
            with self.assertRaisesRegex(SourceBundleError, "unknown fields"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=definition,
                    workflow_contracts=row_unknown,
                )

            output_unknown = _workflow_contracts(definition)
            output_unknown["milestones"][0]["outputs"][0]["unexpected"] = True
            with self.assertRaisesRegex(SourceBundleError, "flowstep_flow_v4 output"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=definition,
                    workflow_contracts=output_unknown,
                )

            binding_unknown = _workflow_contracts(definition)
            binding_unknown["milestones"][0]["inputs"] = {
                "request": {"from": "user.request", "unexpected": True}
            }
            with self.assertRaisesRegex(SourceBundleError, "flowstep_flow_v4 binding"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=definition,
                    workflow_contracts=binding_unknown,
                )

    def test_wiring_must_exactly_project_the_canonical_definition(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            definition = _definition()
            for field, value in (
                ("output_contract", "m8m.other.v1"),
                ("output_schema", "schemas/other.schema.json"),
            ):
                with self.subTest(field=field):
                    malformed = _workflow_contracts(definition)
                    malformed["milestones"][0][field] = value
                    with self.assertRaisesRegex(
                        SourceBundleError, "must exactly project definition"
                    ):
                        compile_source_bundle(
                            source_root=root,
                            contract_bundle=CONTRACT_LOCK,
                            definition=definition,
                            workflow_contracts=malformed,
                        )

            malformed = _workflow_contracts(definition)
            malformed["terminal_milestones"] = ["unknown"]
            with self.assertRaisesRegex(SourceBundleError, "unknown milestones"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=definition,
                    workflow_contracts=malformed,
                )

    def test_terminal_bindings_are_exact_and_logically_unique(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            definition = _definition()
            canonical = _workflow_contracts(definition)

            duplicate_name = copy.deepcopy(canonical)
            duplicate_name["terminal_bindings"].append(
                dict(duplicate_name["terminal_bindings"][0])
            )
            with self.assertRaisesRegex(SourceBundleError, "duplicate name"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=definition,
                    workflow_contracts=duplicate_name,
                )

            duplicate_output = copy.deepcopy(canonical)
            duplicate_output["terminal_bindings"].append(
                {
                    **duplicate_output["terminal_bindings"][0],
                    "name": "another_result",
                }
            )
            with self.assertRaisesRegex(SourceBundleError, "duplicate terminal output"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=definition,
                    workflow_contracts=duplicate_output,
                )

            for field, value, message in (
                ("from", "missing.m8m.source_audit.v1", "terminal milestone"),
                ("from", "source_audit.m8m.other.v1", "contract must match"),
                ("output", "missing", "is not declared"),
            ):
                with self.subTest(field=field, value=value):
                    malformed = copy.deepcopy(canonical)
                    malformed["terminal_bindings"][0][field] = value
                    with self.assertRaisesRegex(SourceBundleError, message):
                        compile_source_bundle(
                            source_root=root,
                            contract_bundle=CONTRACT_LOCK,
                            definition=definition,
                            workflow_contracts=malformed,
                        )

    def test_verifier_rejects_redigested_unknown_malformed_and_null_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            bundle = _compile(root)

            unknown = copy.deepcopy(bundle)
            unknown["workflow_contracts"]["milestones"][0]["unexpected"] = True
            _rehash_workflow_contracts(unknown)
            with self.assertRaisesRegex(SourceBundleError, "unknown fields"):
                verify_source_bundle(unknown)

            malformed = copy.deepcopy(bundle)
            malformed["workflow_contracts"]["terminal_bindings"][0]["name"] = "Bad"
            _rehash_workflow_contracts(malformed)
            with self.assertRaisesRegex(SourceBundleError, "must match"):
                verify_source_bundle(malformed)

            null_contracts = copy.deepcopy(bundle)
            null_contracts["workflow_contracts"] = None
            _rehash_workflow_contracts(null_contracts)
            with self.assertRaisesRegex(SourceBundleError, "must be an object"):
                verify_source_bundle(null_contracts)


class SourceBoundaryTests(unittest.TestCase):
    def test_definition_must_pass_the_complete_flowstep_v4_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            with self.assertRaisesRegex(SourceBundleError, "not valid flowstep_flow_v4"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition={"schema": "flowstep_flow_v4"},
                )

            bundle = _compile(root)
            invalid = {"schema": "flowstep_flow_v4"}
            bundle["definition"] = invalid
            bundle["definition_digest"] = digest_json(invalid)
            bundle["source_bundle_proof"]["definition_digest"] = bundle[
                "definition_digest"
            ]
            bundle["source_bundle_proof"]["source_bundle_digest"] = digest_json(
                {
                    key: value
                    for key, value in bundle.items()
                    if key != "source_bundle_proof"
                }
            )
            with self.assertRaisesRegex(SourceBundleError, "not valid flowstep_flow_v4"):
                verify_source_bundle(bundle)

    def test_requirement_identities_are_unique_and_shapes_are_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            duplicate = _implementations()
            conflicting = dict(duplicate[0])
            conflicting["build_state"] = "BUILD_REQUIRED"
            conflicting.pop("digest")
            duplicate.append(conflicting)
            with self.assertRaisesRegex(SourceBundleError, "duplicate logical identity"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=_definition(),
                    implementation_requirements=duplicate,
                )

            malformed = _implementations()
            malformed[0]["unexpected"] = True
            with self.assertRaisesRegex(SourceBundleError, "unknown fields"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=_definition(),
                    implementation_requirements=malformed,
                )

            wrong_abi = _implementations()
            wrong_abi[0]["runtime_abi"] = "m8m_flowstep_tool_v1"
            with self.assertRaisesRegex(SourceBundleError, "runtime_abi must be"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=_definition(),
                    implementation_requirements=wrong_abi,
                )

            missing_proof = _implementations()
            missing_proof[0].pop("digest")
            with self.assertRaisesRegex(SourceBundleError, "digest"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=_definition(),
                    implementation_requirements=missing_proof,
                )

            duplicate_capabilities = _capabilities()
            duplicate_capabilities.append(
                {
                    **duplicate_capabilities[0],
                    "access": "write",
                }
            )
            with self.assertRaisesRegex(SourceBundleError, "duplicate logical identity"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=_definition(),
                    capability_requirements=duplicate_capabilities,
                )

    def test_built_profile_requires_exact_execution_closure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            definition = _definition(ai=True)

            def compile_with(
                *,
                profiles: list[dict] | None = None,
                implementations: list[dict] | None = None,
                capabilities: list[dict] | None = None,
                resources: list[dict] | None = None,
            ) -> dict:
                return compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=definition,
                    resources=resources if resources is not None else _resources(),
                    implementation_requirements=(
                        implementations if implementations is not None else _implementations()
                    ),
                    agent_profile_requirements=(
                        profiles if profiles is not None else _profiles()
                    ),
                    capability_requirements=(
                        capabilities if capabilities is not None else _capabilities()
                    ),
                )

            self.assertEqual(compile_with()["schema"], "m8m.workflow_source_bundle.v1")
            cases = (
                (
                    "executor",
                    "executor_ref",
                    lambda row: row.pop("executor_ref"),
                ),
                ("Gem", "gem_ref", lambda row: row.pop("gem_ref")),
                ("schemas", "schema_refs", lambda row: row.pop("schema_refs")),
                ("model", "model_configuration", lambda row: row.pop("model_configuration")),
                ("budget", "token_budget", lambda row: row.pop("token_budget")),
                ("timeout", "timeout_seconds", lambda row: row.pop("timeout_seconds")),
            )
            for label, expected, mutate in cases:
                with self.subTest(label=label):
                    profiles = _profiles()
                    mutate(profiles[0])
                    with self.assertRaisesRegex(SourceBundleError, expected):
                        compile_with(profiles=profiles)

            wrong_gem = _profiles()
            wrong_gem[0]["gem_ref"] = "schema.source_audit.v1"
            with self.assertRaisesRegex(SourceBundleError, "exact milestone Gem"):
                compile_with(profiles=wrong_gem)

            wrong_schema = _profiles()
            wrong_schema[0]["schema_refs"]["input_schema"] = "schema.source_audit.v1"
            with self.assertRaisesRegex(SourceBundleError, "exact schema"):
                compile_with(profiles=wrong_schema)

            extra_schema = _profiles()
            extra_schema[0]["schema_refs"]["receipt_schema"] = "schema.source_audit.v1"
            with self.assertRaisesRegex(SourceBundleError, "exactly contain"):
                compile_with(profiles=extra_schema)

            wrong_capability = _profiles()
            wrong_capability[0]["capability_ids"] = ["filesystem.read.other"]
            with self.assertRaisesRegex(SourceBundleError, "unowned capability"):
                compile_with(profiles=wrong_capability)

            blocked_implementation = _implementations()
            blocked_implementation[0]["build_state"] = "BUILD_REQUIRED"
            blocked_implementation[0].pop("digest")
            blocked_implementation[0].pop("byte_count")
            with self.assertRaisesRegex(SourceBundleError, "executor_ref is BUILD_REQUIRED"):
                compile_with(implementations=blocked_implementation)

            blocked_capability = _capabilities()
            blocked_capability[0]["build_state"] = "BUILD_REQUIRED"
            blocked_capability[0]["missing"] = ["local declaration incomplete"]
            with self.assertRaisesRegex(SourceBundleError, "capability .* is BUILD_REQUIRED"):
                compile_with(capabilities=blocked_capability)

            tool_definition = copy.deepcopy(definition)
            tool_definition["milestones"][0].update(
                {
                    "tools": ["read_source"],
                    "flowsteps": [
                        {"id": "read_source", "tool": "source_reader@1.0.0"}
                    ],
                    "execution": {
                        "candidate_executor": {
                            "ref": "handler.source_audit@3.0.0"
                        },
                        "tool_bindings": [
                            {
                                "tool": "read_source",
                                "ref": "source_reader@1.0.0",
                            }
                        ],
                    },
                }
            )
            tool_implementations = _implementations() + [
                {
                    "ref": "source_reader@1.0.0",
                    "kind": "flowstep_tool",
                    "role": "candidate_executor",
                    "runtime_abi": "m8m_flowstep_tool_v1",
                    "entrypoint": "run",
                    "local_name": "source_reader",
                    "build_state": "built",
                    "digest": "sha256:" + "3" * 64,
                    "member_count": 1,
                }
            ]
            tool_profiles = _profiles()
            tool_profiles[0]["tool_refs"] = ["source_reader@1.0.0"]
            definition = tool_definition
            self.assertEqual(
                compile_with(
                    profiles=tool_profiles,
                    implementations=tool_implementations,
                )["schema"],
                "m8m.workflow_source_bundle.v1",
            )
            tool_profiles[0]["tool_refs"] = []
            with self.assertRaisesRegex(
                SourceBundleError,
                "ordered execution.tool_bindings refs",
            ):
                compile_with(
                    profiles=tool_profiles,
                    implementations=tool_implementations,
                )

    def test_capability_identity_is_milestone_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            definition = _definition()
            second = copy.deepcopy(definition["milestones"][0])
            second.update(
                {
                    "id": "finish",
                    "handler": "milestones/finish/assemble.py",
                    "output_contract": "m8m.finish.v1",
                    "inputs": {"source": "source_audit.m8m.source_audit.v1"},
                }
            )
            definition["milestones"].append(second)
            capabilities = [
                {
                    "id": "filesystem.read",
                    "ref": "capability.filesystem.read@1.0.0",
                    "milestone_id": milestone_id,
                    "access": "read",
                    "side_effects": "none",
                    "build_state": "built",
                }
                for milestone_id in ("source_audit", "finish")
            ]
            bundle = compile_source_bundle(
                source_root=root,
                contract_bundle=CONTRACT_LOCK,
                definition=definition,
                capability_requirements=capabilities,
            )
            self.assertEqual(len(bundle["capability_requirements"]), 2)

    def test_schema_registry_rejects_unsafe_or_undeclared_transitive_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            audit = root / "schemas" / "audit.schema.json"
            dependency = root / "schemas" / "common.schema.json"
            dependency.write_text(
                json.dumps(
                    {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "$defs": {"payload": {"type": "object"}},
                    }
                ),
                encoding="utf-8",
            )

            cases = (
                ("https://example.test/schema.json", "external JSON Schema refs"),
                ("missing.schema.json", "referenced JSON Schema is missing"),
            )
            for raw_ref, message in cases:
                with self.subTest(raw_ref=raw_ref):
                    audit.write_text(
                        json.dumps(
                            {
                                "$schema": "https://json-schema.org/draft/2020-12/schema",
                                "$ref": raw_ref,
                            }
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(SourceBundleError, message):
                        _compile(root)

            resources = _resources() + [
                {
                    "ref": "schema.common.v1",
                    "kind": "json_schema",
                    "role": "schema_dependency",
                    "source_path": "schemas/common.schema.json",
                    "media_type": "application/schema+json",
                }
            ]
            definition = _definition(ai=True)
            audit.write_text(
                json.dumps(
                    {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "$ref": "common.schema.json#/$defs/missing",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SourceBundleError, "unresolved JSON Schema fragment"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=definition,
                    workflow_contracts=_workflow_contracts(definition),
                    resources=resources,
                    implementation_requirements=_implementations(),
                    agent_profile_requirements=_profiles(),
                    capability_requirements=_capabilities(),
                )

            outside = root.parent / "outside.schema.json"
            outside.write_text(
                json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema"}),
                encoding="utf-8",
            )
            audit.write_text(
                json.dumps(
                    {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "$ref": "../../outside.schema.json",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SourceBundleError, "escapes source_root"):
                _compile(root)

            audit.write_text(
                json.dumps(
                    {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "$ref": "common.schema.json#/$defs/payload",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SourceBundleError, "undeclared dependency"):
                _compile(root)

            bundle = compile_source_bundle(
                source_root=root,
                contract_bundle=CONTRACT_LOCK,
                definition=definition,
                workflow_contracts=_workflow_contracts(definition),
                resources=resources,
                implementation_requirements=_implementations(),
                agent_profile_requirements=_profiles(),
                capability_requirements=_capabilities(),
            )
            self.assertIn("schema.common.v1", {row["ref"] for row in bundle["resources"]})

    def test_kind_specific_implementation_shape_matches_normative_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            invalid_tool = {
                "ref": "normalize@1.0.0",
                "kind": "flowstep_tool",
                "role": "candidate_executor",
                "runtime_abi": "m8m_flowstep_tool_v1",
                "entrypoint": "run",
                "milestone_id": "source_audit",
                "local_name": "normalize",
                "build_state": "built",
                "digest": "sha256:" + "3" * 64,
                "member_count": 1,
            }
            with self.assertRaisesRegex(SourceBundleError, "unknown fields: milestone_id"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=_definition(),
                    implementation_requirements=[invalid_tool],
                )

    def test_ready_compile_requires_nonvacuous_handler_tool_and_judge_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            ready = {"status": "SOURCE_VALID", "findings_count": 0}
            with self.assertRaisesRegex(SourceBundleError, "one exact milestone_handler"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=_definition(),
                    validation=ready,
                    require_ready=True,
                )

            tool_definition = _definition()
            tool_definition["milestones"][0].update(
                {
                    "tools": ["normalize"],
                    "flowsteps": [
                        {"id": "normalize", "tool": "normalize@1.0.0"}
                    ],
                    "execution": {
                        "candidate_executor": {
                            "ref": "handler.source_audit@3.0.0"
                        },
                        "tool_bindings": [
                            {"tool": "normalize", "ref": "normalize@1.0.0"}
                        ],
                    },
                }
            )
            with self.assertRaisesRegex(
                SourceBundleError,
                "one exact FlowStep implementation normalize@1.0.0",
            ):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=tool_definition,
                    implementation_requirements=[_implementations()[0]],
                    validation=ready,
                    require_ready=True,
                )

            judge_definition = _definition()
            judge_definition["milestones"][0].update(
                {
                    "loop": "judge",
                    "worker": "judge.source_audit@3.0.0",
                    "judge_abi": "m8m_milestone_judge_v1",
                    "execution": {
                        "candidate_executor": {
                            "ref": "handler.source_audit@3.0.0"
                        },
                        "judge": {"ref": "judge.source_audit@3.0.0"},
                        "tool_bindings": [],
                    },
                }
            )
            with self.assertRaisesRegex(SourceBundleError, "milestone_judge"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=judge_definition,
                    implementation_requirements=[_implementations()[0]],
                    validation=ready,
                    require_ready=True,
                )

    def test_ai_judge_requires_a_separate_exact_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            (root / "schemas" / "audit_receipt.schema.json").write_text(
                '{"additionalProperties":false,"type":"object"}', encoding="utf-8"
            )
            definition = _definition(ai=True)
            definition["milestones"][0].update(
                {
                    "intelligence": "judge",
                    "loop": "judge",
                    "worker": "judge.source_audit@3.0.0",
                    "judge_abi": "m8m_milestone_judge_v1",
                    "receipt_schema": "schemas/audit_receipt.schema.json",
                    "execution": {
                        "candidate_executor": {
                            "ref": "handler.source_audit@3.0.0"
                        },
                        "judge": {"ref": "judge.source_audit@3.0.0"},
                        "tool_bindings": [],
                    },
                }
            )
            resources = _resources() + [
                {
                    "ref": "schema.source_audit_receipt.v1",
                    "kind": "json_schema",
                    "role": "milestone_receipt_schema",
                    "source_path": "schemas/audit_receipt.schema.json",
                    "media_type": "application/schema+json",
                }
            ]
            judge_profile = {
                "ref": "agent_profile.builder_judge.v1",
                "milestone_id": "source_audit",
                "role": "ai_judge",
                "executor_ref": "judge.source_audit@3.0.0",
                "gem_ref": "gem.source_audit.v1",
                "schema_refs": {
                    "input_schema": "schema.source_audit_input.v1",
                    "output_schema": "schema.source_audit.v1",
                    "receipt_schema": "schema.source_audit_receipt.v1",
                },
                "tool_refs": [],
                "capability_ids": [],
                "model_configuration": {"model": "codex", "reasoning": "low"},
                "token_budget": {"max_input_tokens": 2048, "max_output_tokens": 512},
                "timeout_seconds": 60,
                "build_state": "built",
            }
            with self.assertRaisesRegex(SourceBundleError, "separate ai_judge"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=definition,
                    resources=resources,
                    implementation_requirements=_implementations(),
                    agent_profile_requirements=_profiles(),
                    capability_requirements=_capabilities(),
                )
            bundle = compile_source_bundle(
                source_root=root,
                contract_bundle=CONTRACT_LOCK,
                definition=definition,
                resources=resources,
                implementation_requirements=_implementations(),
                agent_profile_requirements=[*_profiles(), judge_profile],
                capability_requirements=_capabilities(),
            )
            self.assertEqual(
                {row["role"] for row in bundle["agent_profile_requirements"]},
                {"ai_judge", "candidate_executor"},
            )

    def test_ready_requires_every_requirement_group_to_be_built(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            profiles = _profiles()
            profiles[0]["build_state"] = "BUILD_REQUIRED"
            profiles[0]["missing"] = ["admitted profile"]
            with self.assertRaisesRegex(SourceBundleError, "build_state built"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=_definition(),
                    agent_profile_requirements=profiles,
                    validation={"status": "SOURCE_VALID", "findings_count": 0},
                    require_ready=True,
                )

            capabilities = _capabilities()
            capabilities[0]["build_state"] = "BUILD_REQUIRED"
            capabilities[0]["missing"] = ["capability binding"]
            with self.assertRaisesRegex(SourceBundleError, "build_state built"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=_definition(),
                    capability_requirements=capabilities,
                    validation={"status": "SOURCE_VALID", "findings_count": 0},
                    require_ready=True,
                )

    def test_identity_key_variants_are_rejected_before_shape_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            for forbidden in ("runId", "deploymentId", "authoring-session-id"):
                with self.subTest(forbidden=forbidden):
                    with self.assertRaisesRegex(SourceBundleError, "cloud/run identity"):
                        compile_source_bundle(
                            source_root=root,
                            contract_bundle=CONTRACT_LOCK,
                            definition=_definition(),
                            observer={forbidden: "not-source"},
                        )

    def test_secret_key_variants_are_rejected_before_shape_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            for forbidden in (
                "apiKey",
                "ACCESS-TOKEN",
                "authorization",
                "client_secret",
                "credentials",
                "password",
                "privateKey",
                "refreshToken",
                "secret",
            ):
                with self.subTest(forbidden=forbidden):
                    with self.assertRaisesRegex(SourceBundleError, "credential-bearing field"):
                        compile_source_bundle(
                            source_root=root,
                            contract_bundle=CONTRACT_LOCK,
                            definition=_definition(),
                            observer={"debug": {forbidden: "credential-value"}},
                        )

    def test_text_resources_reject_credential_patterns_without_blocking_placeholders(self) -> None:
        cases = {
            "private-key block": (
                "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----"
            ),
            "Bearer token": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
            "prefixed API token": "token = sk-proj-abcdefghijklmnopqrstuvwx",
            "GitHub token": "github_pat_abcdefghijklmnopqrstuvwxyz012345",
            "AWS access key": "AKIAABCDEFGHIJKLMNOP",
            "Google API key": "AIza" + "a" * 35,
            "Slack token": "xox" + "b-1234567890-abcdefghijklmnop",
            "Stripe secret key": "sk_" + "live_abcdefghijklmnopqrstuvwx",
        }
        for expected, content in cases.items():
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "source"
                _write_sources(root)
                (root / "references" / "source_audit.md").write_text(
                    content, encoding="utf-8"
                )
                with self.assertRaisesRegex(SourceBundleError, expected):
                    _compile(root)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            (root / "references" / "source_audit.md").write_text(
                "Document the `sk-` prefix and send `Bearer <token>` at runtime.",
                encoding="utf-8",
            )
            self.assertEqual(_compile(root)["schema"], "m8m.workflow_source_bundle.v1")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            (root / "references" / "source_audit.md").write_bytes(b"\xff")
            with self.assertRaisesRegex(SourceBundleError, "valid UTF-8"):
                _compile(root)

    def test_absolute_traversal_and_cloud_identity_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            absolute = _resources()
            absolute[0]["source_path"] = str(
                (root / "references" / "source_audit.md").resolve()
            )
            with self.assertRaisesRegex(SourceBundleError, "unsafe resource path"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=_definition(),
                    resources=absolute,
                )

            traversal = _resources()
            traversal[0]["source_path"] = "../outside.md"
            with self.assertRaisesRegex(SourceBundleError, "unsafe resource path"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=_definition(),
                    resources=traversal,
                )

            with self.assertRaisesRegex(SourceBundleError, "cloud/run identity"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=_definition(),
                    resources=_resources(),
                    implementation_requirements=[
                        {
                            "ref": "example@1",
                            "implementation_bundle_id": "implementation_bundle:cloud",
                        }
                    ],
                )

    def test_nested_absolute_path_and_timestamp_metadata_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            with self.assertRaisesRegex(SourceBundleError, "absolute or unsafe local path"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=_definition(),
                    resources=_resources(),
                    observer={"debug": {"source": str(root.resolve())}},
                )
            with self.assertRaisesRegex(SourceBundleError, "timestamp metadata"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=_definition(),
                    resources=_resources(),
                    capability_requirements=[
                        {"id": "filesystem.read", "generated_at": "2099-01-01"}
                    ],
                )

    def test_ready_assertion_rejects_build_required_and_nonpassing_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            staged = _compile(root, build_state="BUILD_REQUIRED")
            self.assertIn("BUILD_REQUIRED", json.dumps(staged))

            with self.assertRaisesRegex(SourceBundleError, "BUILD_REQUIRED"):
                _compile(
                    root,
                    build_state="BUILD_REQUIRED",
                    require_ready=True,
                    validation={"status": "SOURCE_VALID", "findings_count": 0},
                )
            with self.assertRaisesRegex(SourceBundleError, "SOURCE_VALID"):
                _compile(
                    root,
                    require_ready=True,
                    validation={"status": "BLOCKED", "findings_count": 1},
                )
            ready = _compile(
                root,
                require_ready=True,
                validation={"status": "SOURCE_VALID", "findings_count": 0},
            )
            self.assertEqual(ready["local_validation"]["status"], "SOURCE_VALID")
            self.assertEqual(ready["local_validation"]["findings_count"], 0)
            self.assertEqual(verify_source_bundle(ready), ready["source_bundle_proof"])

            with self.assertRaisesRegex(SourceBundleError, "validation.status"):
                _compile(root, validation={"status": "PASS", "findings_count": 0})
            with self.assertRaisesRegex(SourceBundleError, "findings_count"):
                _compile(
                    root,
                    validation={"status": "BUILD_REQUIRED", "findings_count": -1},
                )

    def test_resource_digest_and_count_assertions_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            resources = _resources()
            resources[0]["digest"] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(SourceBundleError, "resource digest mismatch"):
                compile_source_bundle(
                    source_root=root,
                    contract_bundle=CONTRACT_LOCK,
                    definition=_definition(),
                    resources=resources,
                )


class SourceBundleSchemaTests(unittest.TestCase):
    def test_contract_schemas_are_valid_and_accept_compiler_output(self) -> None:
        skill_root = Path(__file__).resolve().parents[1]
        contracts = skill_root / "contracts"
        names = (
            "flowstep_flow_v4.schema.json",
            "m8m_contract_bundle_lock_v1.schema.json",
            "m8m_workflow_source_bundle_proof_v1.schema.json",
            "m8m_workflow_source_bundle_v1.schema.json",
        )
        schemas = {
            name: json.loads((contracts / name).read_text(encoding="utf-8")) for name in names
        }
        for schema in schemas.values():
            Draft202012Validator.check_schema(schema)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            bundle = _compile(root)
            store: dict[str, dict] = {}
            for name, schema in schemas.items():
                store[name] = schema
                store[(contracts / name).as_uri()] = schema
            resolver = RefResolver(
                base_uri=contracts.as_uri() + "/",
                referrer=schemas["m8m_workflow_source_bundle_v1.schema.json"],
                store=store,
            )
            Draft202012Validator(
                schemas["m8m_workflow_source_bundle_v1.schema.json"], resolver=resolver
            ).validate(bundle)
            validator = Draft202012Validator(
                schemas["m8m_workflow_source_bundle_v1.schema.json"], resolver=resolver
            )
            malformed = copy.deepcopy(bundle)
            malformed["workflow_contracts"]["milestones"][0]["unexpected"] = True
            with self.assertRaises(ValidationError):
                validator.validate(malformed)
            partial = copy.deepcopy(bundle)
            partial["workflow_contracts"].pop("configuration_schema")
            with self.assertRaises(ValidationError):
                validator.validate(partial)
            Draft202012Validator(
                schemas["m8m_contract_bundle_lock_v1.schema.json"]
            ).validate(bundle["contract_bundle"])
            Draft202012Validator(
                schemas["m8m_workflow_source_bundle_proof_v1.schema.json"]
            ).validate(bundle["source_bundle_proof"])

    def test_unknown_top_level_field_is_rejected_by_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_sources(root)
            bundle = _compile(root)
            bundle["release_id"] = "release:not-allowed"
            with self.assertRaisesRegex(SourceBundleError, "unknown fields"):
                verify_source_bundle(bundle)


if __name__ == "__main__":
    unittest.main()
