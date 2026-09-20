from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import support  # noqa: F401

from m8m_build_steps import (
    BUILDER_ROOT,
    CONTRACT_BUNDLE_FILES,
    _agent_profile_requirements,
    _capability_requirements,
    _contract_bundle_lock,
    _contract_bundle_member_manifest,
    _implementation_requirements,
    _local_validation_summary,
)
from flowstep_runtime import FlowError
from source_bundle import digest_json


class BuilderRequirementTests(unittest.TestCase):
    def test_contract_bundle_binds_the_judge_receipt_contract(self) -> None:
        self.assertIn(
            "m8m_milestone_expectation_v1.schema.json",
            CONTRACT_BUNDLE_FILES,
        )
        self.assertIn(
            "m8m_milestone_judge_receipt_v1.schema.json",
            CONTRACT_BUNDLE_FILES,
        )
        self.assertIn(
            "m8m_milestone_judge_request_v1.schema.json",
            CONTRACT_BUNDLE_FILES,
        )
        self.assertEqual(
            _contract_bundle_lock()["id"],
            "m8m-harness-builder-contracts/3.1",
        )
        self.assertRegex(_contract_bundle_lock()["digest"], r"^sha256:[0-9a-f]{64}$")

    def test_contract_bundle_lock_binds_exact_member_bytes(self) -> None:
        manifest = _contract_bundle_member_manifest()
        self.assertEqual(manifest["schema"], "m8m.contract_bundle_members.v1")
        self.assertEqual(
            [item["name"] for item in manifest["members"]],
            list(CONTRACT_BUNDLE_FILES),
        )
        for item in manifest["members"]:
            payload = (BUILDER_ROOT / "contracts" / item["name"]).read_bytes()
            self.assertEqual(item["byte_count"], len(payload))
            self.assertEqual(
                item["digest"],
                "sha256:" + hashlib.sha256(payload).hexdigest(),
            )
        self.assertEqual(_contract_bundle_lock()["digest"], digest_json(manifest))

    def test_source_valid_summary_requires_the_current_full_validator(self) -> None:
        generated = {"status": "PASS", "build_required_tools": []}
        with patch("m8m_build_steps.validate_harness", return_value={"status": "PASS"}):
            self.assertEqual(
                _local_validation_summary(
                    Path("unused"),
                    generated,
                    ready=True,
                    requirement_groups=([], [], []),
                ),
                {"status": "SOURCE_VALID", "findings_count": 0},
            )

        with patch(
            "m8m_build_steps.validate_harness",
            side_effect=FlowError("harness invalid:\n- missing test\n- unsafe path"),
        ):
            self.assertEqual(
                _local_validation_summary(
                    Path("unused"),
                    generated,
                    ready=True,
                    requirement_groups=([], [], []),
                ),
                {"status": "BUILD_REQUIRED", "findings_count": 2},
            )

    def test_handler_and_shared_dependency_bytes_are_truthfully_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            harness = project / "flowsteps" / "flows" / "example_v1"
            handler = harness / "handlers" / "ready.py"
            dependency = project / "shared" / "policy.py"
            handler.parent.mkdir(parents=True)
            dependency.parent.mkdir(parents=True)
            handler.write_text("VALUE = 1\n", encoding="utf-8")
            dependency.write_text("POLICY = 1\n", encoding="utf-8")
            definition = {
                "flow_id": "example_v1",
                "implementation_dependencies": ["shared/policy.py"],
                "milestones": [
                    {
                        "id": "ready",
                        "handler": "handlers/ready.py",
                        "tools": [],
                    }
                ],
            }
            source = {
                "milestones": [
                    {
                        "agent_id": "ready",
                        "execution": {
                            "candidate_executor": {"ref": "handler.ready@1.0.0"},
                            "tool_bindings": [],
                        },
                    }
                ]
            }

            first = _implementation_requirements(harness, definition, source)
            self.assertTrue(all(item["build_state"] == "built" for item in first))
            handler_row = next(item for item in first if item["kind"] == "milestone_handler")
            dependency_row = next(item for item in first if item["kind"] == "shared_dependency")
            self.assertEqual(handler_row["byte_count"], handler.stat().st_size)
            self.assertEqual(dependency_row["byte_count"], dependency.stat().st_size)

            handler.write_text("VALUE = 2\n", encoding="utf-8")
            dependency.write_text("POLICY = 2\n", encoding="utf-8")
            second = _implementation_requirements(harness, definition, source)
            self.assertNotEqual(
                handler_row["digest"],
                next(item for item in second if item["kind"] == "milestone_handler")["digest"],
            )
            self.assertNotEqual(
                dependency_row["digest"],
                next(item for item in second if item["kind"] == "shared_dependency")["digest"],
            )

    def test_missing_implementation_is_build_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            harness = project / "flowsteps" / "flows" / "missing_v1"
            harness.mkdir(parents=True)
            definition = {
                "flow_id": "missing_v1",
                "implementation_dependencies": ["shared/missing.py"],
                "milestones": [
                    {"id": "ready", "handler": "handlers/missing.py", "tools": []}
                ],
            }
            requirements = _implementation_requirements(harness, definition)
            self.assertEqual(
                [item["build_state"] for item in requirements],
                ["BUILD_REQUIRED", "BUILD_REQUIRED"],
            )
            self.assertTrue(all("digest" not in item for item in requirements))

    def test_existing_handler_without_authored_executor_ref_is_build_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = Path(temp) / "flow"
            handler = harness / "handlers" / "ready.py"
            handler.parent.mkdir(parents=True)
            handler.write_text("VALUE = 1\n", encoding="utf-8")
            requirements = _implementation_requirements(
                harness,
                {
                    "flow_id": "truthful_v1",
                    "milestones": [
                        {"id": "ready", "handler": "handlers/ready.py", "tools": []}
                    ],
                },
            )
            self.assertEqual(requirements[0]["build_state"], "BUILD_REQUIRED")
            self.assertEqual(requirements[0]["ref"], "handler.truthful_v1.ready@source")
            self.assertNotIn("digest", requirements[0])

    def test_judge_requirement_never_synthesizes_a_missing_abi(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = Path(temp) / "flow"
            harness.mkdir(parents=True)
            requirements = _implementation_requirements(
                harness,
                {
                    "flow_id": "judge_truth_v1",
                    "milestones": [
                        {
                            "id": "ready",
                            "handler": "handlers/ready.py",
                            "tools": [],
                            "loop": "judge",
                            "worker": "ready_judge",
                        }
                    ],
                },
                {
                    "milestones": [
                        {
                            "agent_id": "ready",
                            "execution": {
                                "candidate_executor": {"ref": "handler.ready@1.0.0"},
                                "judge": {"ref": "judge.ready@1.0.0"},
                                "tool_bindings": [],
                            },
                        }
                    ]
                },
            )
            self.assertFalse(
                any(item["kind"] == "milestone_judge" for item in requirements)
            )

    def test_judge_requirement_matches_closed_source_bundle_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            harness = project / "flowsteps" / "flows" / "judge_shape_v1"
            handler = harness / "handlers" / "ready.py"
            judge_tool = project / "flowsteps" / "tools" / "ready_judge" / "tool.py"
            handler.parent.mkdir(parents=True)
            judge_tool.parent.mkdir(parents=True)
            handler.write_text("VALUE = 1\n", encoding="utf-8")
            judge_tool.write_text("def run(input_data, **kwargs):\n    return {}\n", encoding="utf-8")
            definition = {
                "flow_id": "judge_shape_v1",
                "milestones": [
                    {
                        "id": "ready",
                        "handler": "handlers/ready.py",
                        "tools": [],
                        "loop": "judge",
                        "worker": "ready_judge@1.0.0",
                        "judge_abi": "m8m_milestone_judge_v1",
                    }
                ],
            }
            source = {
                "milestones": [
                    {
                        "agent_id": "ready",
                        "execution": {
                            "candidate_executor": {"ref": "handler.ready@1.0.0"},
                            "judge": {"ref": "ready_judge@1.0.0"},
                            "tool_bindings": [],
                        },
                    }
                ]
            }

            with patch("m8m_build_steps.validate_library_tool", return_value=[]):
                requirements = _implementation_requirements(harness, definition, source)

            judge = next(item for item in requirements if item["kind"] == "milestone_judge")
            self.assertEqual(judge["build_state"], "built")
            self.assertEqual(judge["entrypoint"], "run")
            self.assertNotIn("local_name", judge)

    def test_shared_judge_requirement_is_emitted_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            harness = project / "flowsteps" / "flows" / "shared_judge_v1"
            judge_tool = project / "flowsteps" / "tools" / "shared_judge"
            judge_tool.mkdir(parents=True)
            (judge_tool / "tool.py").write_text(
                "def run(input_data, **kwargs):\n    return {}\n",
                encoding="utf-8",
            )
            milestones = []
            source_milestones = []
            for milestone_id in ("first", "second", "third"):
                handler = harness / "handlers" / f"{milestone_id}.py"
                handler.parent.mkdir(parents=True, exist_ok=True)
                handler.write_text("VALUE = 1\n", encoding="utf-8")
                milestones.append(
                    {
                        "id": milestone_id,
                        "handler": f"handlers/{milestone_id}.py",
                        "tools": [],
                        "loop": "judge",
                        "worker": "shared_judge@1.0.0",
                        "judge_abi": "m8m_milestone_judge_v1",
                    }
                )
                source_milestones.append(
                    {
                        "agent_id": milestone_id,
                        "execution": {
                            "candidate_executor": {
                                "ref": f"handler.{milestone_id}@1.0.0"
                            },
                            "judge": {"ref": "shared_judge@1.0.0"},
                            "tool_bindings": [],
                        },
                    }
                )

            with patch("m8m_build_steps.validate_library_tool", return_value=[]):
                requirements = _implementation_requirements(
                    harness,
                    {"flow_id": "shared_judge_v1", "milestones": milestones},
                    {"milestones": source_milestones},
                )

            judges = [
                item for item in requirements if item["kind"] == "milestone_judge"
            ]
            self.assertEqual(len(judges), 1)
            self.assertEqual(judges[0]["ref"], "shared_judge@1.0.0")
            self.assertNotIn("milestone_id", judges[0])

    def test_shared_judge_ref_rejects_conflicting_runtime_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            harness = project / "flowsteps" / "flows" / "conflict_v1"
            for name in ("first_judge", "second_judge"):
                tool = project / "flowsteps" / "tools" / name
                tool.mkdir(parents=True)
                (tool / "tool.py").write_text(
                    "def run(input_data, **kwargs):\n    return {}\n",
                    encoding="utf-8",
                )
            milestones = []
            source_milestones = []
            for milestone_id, worker in (
                ("first", "first_judge@1.0.0"),
                ("second", "second_judge@1.0.0"),
            ):
                handler = harness / "handlers" / f"{milestone_id}.py"
                handler.parent.mkdir(parents=True, exist_ok=True)
                handler.write_text("VALUE = 1\n", encoding="utf-8")
                milestones.append(
                    {
                        "id": milestone_id,
                        "handler": f"handlers/{milestone_id}.py",
                        "tools": [],
                        "loop": "judge",
                        "worker": worker,
                        "judge_abi": "m8m_milestone_judge_v1",
                    }
                )
                source_milestones.append(
                    {
                        "agent_id": milestone_id,
                        "execution": {
                            "candidate_executor": {
                                "ref": f"handler.{milestone_id}@1.0.0"
                            },
                            "judge": {"ref": "shared_judge@1.0.0"},
                            "tool_bindings": [],
                        },
                    }
                )

            with patch("m8m_build_steps.validate_library_tool", return_value=[]):
                with self.assertRaisesRegex(
                    FlowError,
                    "shared judge ref maps to conflicting runtime packages or ABIs",
                ):
                    _implementation_requirements(
                        harness,
                        {"flow_id": "conflict_v1", "milestones": milestones},
                        {"milestones": source_milestones},
                    )

    def test_missing_authored_ai_requirements_are_never_inferred_as_built(self) -> None:
        definition = {
            "milestones": [
                {
                    "id": "render",
                    "intelligence": "image",
                    "side_effects": "external",
                },
                {"id": "check", "intelligence": "judge", "side_effects": "none"},
            ]
        }
        self.assertEqual(
            _agent_profile_requirements(definition),
            [
                {
                    "ref": "agent_profile.render.candidate_executor@source",
                    "milestone_id": "render",
                    "role": "candidate_executor",
                    "build_state": "BUILD_REQUIRED",
                    "missing": ["authored execution declaration"],
                },
                {
                    "ref": "agent_profile.check.candidate_executor@source",
                    "milestone_id": "check",
                    "role": "candidate_executor",
                    "build_state": "BUILD_REQUIRED",
                    "missing": ["authored execution declaration"],
                },
                {
                    "ref": "agent_profile.check.ai_judge@source",
                    "milestone_id": "check",
                    "role": "ai_judge",
                    "build_state": "BUILD_REQUIRED",
                    "missing": ["authored judge profile"],
                },
            ],
        )
        self.assertEqual(
            _capability_requirements(definition),
            [
                {
                    "id": "unresolved.render.external_side_effect",
                    "ref": "capability.render.external_side_effect@0.0.0",
                    "milestone_id": "render",
                    "access": "execute",
                    "side_effects": "external",
                    "build_state": "BUILD_REQUIRED",
                    "missing": ["explicit external capability declaration"],
                }
            ],
        )

    def test_explicit_candidate_and_judge_profiles_bind_exact_closure(self) -> None:
        definition = {
            "milestones": [
                {
                    "id": "render",
                    "intelligence": "judge",
                    "loop": "judge",
                    "gem": "references/render.md",
                    "input_schema": "schemas/render_input.json",
                    "draft_schema": "schemas/render_draft.json",
                    "output_schema": "schemas/render_output.json",
                    "receipt_schema": "schemas/render_receipt.json",
                    "tools": ["render_image"],
                    "side_effects": "external",
                    "phase_journal": {
                        "path": "publication/phase-journal.json",
                        "operator_result_path": "publication/operator-result.json",
                        "resume": "query_exact_operation",
                    },
                }
            ]
        }
        profile = {
            "model_configuration": {"model": "codex", "reasoning": "high"},
            "token_budget": {"max_input_tokens": 4096, "max_output_tokens": 1024},
            "timeout_seconds": 120,
        }
        source = {
            "milestones": [
                {
                    "agent_id": "render",
                    "capabilities": [
                        {
                            "id": "media.render",
                            "ref": "capability.media.render@1.0.0",
                            "access": "execute",
                            "side_effects": "external",
                        }
                    ],
                    "phase_journal": definition["milestones"][0]["phase_journal"],
                    "execution": {
                        "candidate_executor": {
                            "ref": "executor.render@1.0.0",
                            "profile": {
                                "ref": "agent_profile.render.candidate.v1",
                                **profile,
                                "tools": ["render_image"],
                                "capabilities": ["media.render"],
                            },
                        },
                        "judge": {
                            "ref": "judge.render@1.0.0",
                            "profile": {
                                "ref": "agent_profile.render.judge.v1",
                                **profile,
                                "tools": [],
                                "capabilities": [],
                            },
                        },
                        "tool_bindings": [
                            {"tool": "render_image", "ref": "render_image@1.0.0"}
                        ],
                    },
                }
            ]
        }
        resources = [{"source_path": "references/render.md", "ref": "gem.render.v1"}] + [
            {
                "source_path": f"schemas/render_{kind}.json",
                "ref": f"schema.render_{kind}.v1",
            }
            for kind in ("input", "draft", "output", "receipt")
        ]
        implementations = [
            {"ref": "executor.render@1.0.0", "build_state": "built"},
            {"ref": "render_image@1.0.0", "build_state": "built"},
            {"ref": "judge.render@1.0.0", "build_state": "built"},
        ]
        capabilities = _capability_requirements(definition, source=source)
        self.assertEqual(capabilities[0]["build_state"], "built")
        rows = _agent_profile_requirements(
            definition,
            source=source,
            resources=resources,
            implementations=implementations,
            capabilities=capabilities,
        )
        self.assertEqual([item["role"] for item in rows], ["candidate_executor", "ai_judge"])
        self.assertTrue(all(item["build_state"] == "built" for item in rows))
        self.assertEqual(rows[0]["tool_refs"], ["render_image@1.0.0"])
        self.assertEqual(rows[0]["capability_ids"], ["media.render"])
        self.assertEqual(rows[0]["model_configuration"], profile["model_configuration"])
        self.assertEqual(rows[0]["token_budget"], profile["token_budget"])
        self.assertEqual(rows[0]["timeout_seconds"], profile["timeout_seconds"])
        self.assertEqual(rows[1]["tool_refs"], [])

        missing_implementation = _agent_profile_requirements(
            definition,
            source=source,
            resources=resources,
            implementations=[],
            capabilities=capabilities,
        )
        self.assertTrue(
            all(item["build_state"] == "BUILD_REQUIRED" for item in missing_implementation)
        )
        self.assertTrue(
            all(
                any(term.startswith("implementation:") for term in item["missing"])
                for item in missing_implementation
            )
        )

        blocked_implementations = copy.deepcopy(implementations)
        blocked_implementations[0]["build_state"] = "BUILD_REQUIRED"
        blocked_profiles = _agent_profile_requirements(
            definition,
            source=source,
            resources=resources,
            implementations=blocked_implementations,
            capabilities=capabilities,
        )
        self.assertEqual(blocked_profiles[0]["build_state"], "BUILD_REQUIRED")
        self.assertIn(
            "implementation:executor.render@1.0.0:BUILD_REQUIRED",
            blocked_profiles[0]["missing"],
        )

        blocked_capabilities = copy.deepcopy(capabilities)
        blocked_capabilities[0]["build_state"] = "BUILD_REQUIRED"
        blocked_capabilities[0]["missing"] = ["local source declaration incomplete"]
        capability_blocked_profiles = _agent_profile_requirements(
            definition,
            source=source,
            resources=resources,
            implementations=implementations,
            capabilities=blocked_capabilities,
        )
        self.assertEqual(
            capability_blocked_profiles[0]["build_state"], "BUILD_REQUIRED"
        )
        self.assertIn(
            "capability:render:media.render:BUILD_REQUIRED",
            capability_blocked_profiles[0]["missing"],
        )

    def test_deterministic_workflow_needs_no_profiles(self) -> None:
        self.assertEqual(
            _agent_profile_requirements(
                {"milestones": [{"id": "bind", "intelligence": "none"}]}
            ),
            [],
        )

    def test_deterministic_candidate_and_ai_judge_profiles_compile_independently(self) -> None:
        definition = {
            "milestones": [
                {
                    "id": "check",
                    "intelligence": "none",
                    "loop": "judge",
                    "gem": "references/check.md",
                    "input_schema": "schemas/check_input.json",
                    "output_schema": "schemas/check_output.json",
                    "receipt_schema": "schemas/check_receipt.json",
                    "tools": ["deterministic_candidate"],
                }
            ]
        }
        judge_profile = {
            "ref": "agent_profile.check.judge.v1",
            "model_configuration": {"model": "codex", "reasoning": "low"},
            "token_budget": {"max_input_tokens": 2048, "max_output_tokens": 512},
            "timeout_seconds": 60,
            "tools": [],
            "capabilities": [],
        }
        source = {
            "milestones": [
                {
                    "agent_id": "check",
                    "execution": {
                        "candidate_executor": {"ref": "handler.check@1.0.0"},
                        "judge": {
                            "ref": "judge.check@1.0.0",
                            "profile": judge_profile,
                        },
                        "tool_bindings": [
                            {
                                "tool": "deterministic_candidate",
                                "ref": "deterministic_candidate@1.0.0",
                            }
                        ],
                    },
                }
            ]
        }
        resources = [
            {"source_path": "references/check.md", "ref": "gem.check.v1"},
            {"source_path": "schemas/check_input.json", "ref": "schema.check.input.v1"},
            {"source_path": "schemas/check_output.json", "ref": "schema.check.output.v1"},
            {"source_path": "schemas/check_receipt.json", "ref": "schema.check.receipt.v1"},
        ]
        implementations = [
            {"ref": "handler.check@1.0.0", "build_state": "built"},
            {"ref": "judge.check@1.0.0", "build_state": "built"},
            {"ref": "deterministic_candidate@1.0.0", "build_state": "built"},
        ]
        rows = _agent_profile_requirements(
            definition,
            source=source,
            resources=resources,
            implementations=implementations,
            capabilities=[],
        )
        self.assertEqual([row["role"] for row in rows], ["ai_judge"])
        self.assertEqual(rows[0]["build_state"], "built")
        self.assertEqual(rows[0]["executor_ref"], "judge.check@1.0.0")


if __name__ == "__main__":
    unittest.main()
