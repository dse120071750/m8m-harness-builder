from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from support import SCRIPTS, SKILL_ROOT  # noqa: F401

from skill_source import (  # noqa: E402
    SkillSourceError,
    canonical_json_bytes,
    compile_skill_source,
    compile_skill_source_bytes,
    load_skill_source,
)
from flowstep_runtime import load_flow  # noqa: E402


FLOW_SCHEMA = json.loads(
    (SKILL_ROOT / "contracts" / "flowstep_flow_v4.schema.json").read_text(encoding="utf-8")
)


def _candidate_schema(port_schema: dict | None = None) -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["outputs"],
        "properties": {
            "outputs": {
                "type": "object",
                "additionalProperties": False,
                "required": ["result"],
                "properties": {"result": port_schema or {"type": "object"}},
            }
        },
    }


def _judge_result_schema(*, closed: bool = True) -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        **({"additionalProperties": False} if closed else {}),
        "required": ["decision", "reasons", "blockers"],
        "properties": {
            "decision": {"enum": ["PASS", "RETRY", "BLOCKED"]},
            "reasons": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {"type": "string", "minLength": 1, "maxLength": 512},
            },
            "blockers": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string", "minLength": 1, "maxLength": 512},
            },
        },
    }


class SkillFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "agents").mkdir(parents=True)
        (root / "references").mkdir()
        (root / "schemas").mkdir()
        (root / "SKILL.md").write_text(
            "---\n"
            "name: example-skill\n"
            "description: A deterministic example M8M skill.\n"
            "metadata:\n"
            "  version: '3.1'\n"
            "---\n\n"
            "# Example skill\n\n"
            "Invoke `$example-skill` through `agents/openai.yaml`; the canvas points "
            "to each exact `references/<milestone>.md` Gem.\n",
            encoding="utf-8",
        )
        for name in ("source", "finish"):
            schema = _candidate_schema()
            (root / "schemas" / f"{name}.schema.json").write_text(
                json.dumps(schema), encoding="utf-8"
            )
            (root / "references" / f"{name}.md").write_text(
                f"# {name.title()}\n\n"
                f"## `{name}_step`\n\nProduce the bounded candidate.\n",
                encoding="utf-8",
            )

        self.openai = {
            "interface": {
                "display_name": "Example M8M",
                "short_description": "Compile two explicit milestones.",
                "default_prompt": "Use $example-skill with an explicit request.",
            },
            "policy": {"allow_implicit_invocation": False},
            "canvas": {
                "schema": "m8m_skill_canvas_v1",
                "flow_id": "example_flow",
                "version": 3,
                "context_policy": "isolated",
                "entry": "source",
                "milestones": ["source", "finish"],
                "graph": [
                    {"from": "source", "to": ["finish"]},
                    {"from": "finish", "to": []},
                ],
                "terminal_states": {"success": ["finish"], "blocked": "BLOCKED"},
                "observer": {
                    "title": "Example workflow",
                    "summary": "Create a source candidate, then finish it.",
                },
            },
        }
        self.agents = {
            "source": self._agent("source", "source_v1"),
            "finish": self._agent("finish", "finish_v1"),
        }
        self.agents["source"]["inputs"] = {"request": "user.request"}
        self.agents["finish"]["inputs"] = {"source": "source.source_v1"}
        workflow_schemas = {
            "request": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
            "configuration": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
            "result": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["final_result"],
                "properties": {"final_result": {"type": "object"}},
            },
        }
        for name, workflow_schema in workflow_schemas.items():
            (root / "schemas" / f"workflow_{name}.schema.json").write_text(
                json.dumps(workflow_schema), encoding="utf-8"
            )
        self.openai["canvas"]["workflow_contracts"] = {
            "request_schema": "schemas/workflow_request.schema.json",
            "configuration_schema": "schemas/workflow_configuration.schema.json",
            "result_schema": "schemas/workflow_result.schema.json",
            "terminal_bindings": [
                {
                    "name": "final_result",
                    "from": "finish.finish_v1",
                    "output": "result",
                }
            ],
        }
        self.write()

    @staticmethod
    def _agent(agent_id: str, contract: str) -> dict:
        return {
            "schema": "m8m_milestone_agent_v1",
            "agent_id": agent_id,
            "role": "milestone",
            "success": f"{agent_id} output is contract-valid.",
            "output_contract": contract,
            "output_schema": f"schemas/{agent_id}.schema.json",
            "outputs": [
                {
                    "id": "result",
                    "name": f"{agent_id.title()} result",
                    "kind": "json",
                    "cardinality": "one",
                    "required": True,
                }
            ],
            "handler": f"handlers/{agent_id}.py",
            "flowsteps": [
                {"id": f"{agent_id}_step", "tool": f"{agent_id}_tool"}
            ],
            "intelligence": "none",
            "execution": {
                "candidate_executor": {
                    "ref": f"handler.example_flow.{agent_id}@3.1.0"
                },
                "tool_bindings": [
                    {
                        "tool": f"{agent_id}_step",
                        "ref": f"{agent_id}_tool@1.0.0",
                    }
                ],
            },
            "on_tool_fail": "BLOCKED",
            "loop": "none",
            "gem": f"references/{agent_id}.md",
            "read_paths": [f"references/{agent_id}.md"],
            "observer": {
                "title": f"Human {agent_id}",
                "summary": f"Explain the {agent_id} checkpoint.",
                "actions": [
                    {
                        "flowstep_id": f"{agent_id}_step",
                        "title": f"Do {agent_id}",
                        "summary": "Produce one bounded result.",
                    }
                ],
                "outputs": [
                    {
                        "output_id": "result",
                        "title": f"{agent_id.title()} result",
                        "summary": "One required settings record.",
                    }
                ],
            },
        }

    def write(self) -> None:
        (self.root / "agents" / "openai.yaml").write_text(
            yaml.safe_dump(self.openai, sort_keys=False), encoding="utf-8"
        )
        for agent_id, agent in self.agents.items():
            (self.root / "agents" / f"{agent_id}.yaml").write_text(
                yaml.safe_dump(agent, sort_keys=False), encoding="utf-8"
            )


class SkillSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "skill"
        self.fixture = SkillFixture(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _configure_ai_source(self) -> None:
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
        }
        for name in ("source_input", "source_draft"):
            (self.root / "schemas" / f"{name}.schema.json").write_text(
                json.dumps(schema), encoding="utf-8"
            )
        (self.root / "schemas" / "source_receipt.schema.json").write_text(
            json.dumps(_judge_result_schema()),
            encoding="utf-8",
        )
        agent = self.fixture.agents["source"]
        agent.update(
            {
                "intelligence": "judge",
                "input_schema": "schemas/source_input.schema.json",
                "draft_schema": "schemas/source_draft.schema.json",
                "receipt_schema": "schemas/source_receipt.schema.json",
                "loop": "judge",
                "worker": "source_judge@1.0.0",
                "judge_abi": "m8m_milestone_judge_v1",
                "execution": {
                    "candidate_executor": {
                        "ref": "handler.example_flow.source@3.1.0",
                        "profile": {
                            "ref": "agent_profile.source.candidate.v1",
                            "model_configuration": {
                                "model": "codex",
                                "reasoning": "medium",
                            },
                            "token_budget": {
                                "max_input_tokens": 4096,
                                "max_output_tokens": 1024,
                            },
                            "timeout_seconds": 120,
                            "tools": ["source_step"],
                            "capabilities": [],
                        },
                    },
                    "judge": {
                        "ref": "source_judge@1.0.0",
                        "profile": {
                            "ref": "agent_profile.source.judge.v1",
                            "model_configuration": {
                                "model": "codex",
                                "reasoning": "low",
                            },
                            "token_budget": {
                                "max_input_tokens": 2048,
                                "max_output_tokens": 512,
                            },
                            "timeout_seconds": 60,
                            "tools": [],
                            "capabilities": [],
                        },
                    },
                    "tool_bindings": [
                        {"tool": "source_step", "ref": "source_tool@1.0.0"}
                    ],
                },
            }
        )
        self.fixture.write()

    def test_load_preserves_human_source_without_absolute_identity(self) -> None:
        source = load_skill_source(self.root)
        self.assertEqual(source["schema"], "m8m_skill_source_v1")
        self.assertEqual(source["canvas"]["observer"]["title"], "Example workflow")
        self.assertEqual(
            [item["observer"]["title"] for item in source["milestones"]],
            ["Human source", "Human finish"],
        )
        self.assertIn(
            {"owner": "source", "id": "gem", "kind": "gem", "path": "references/source.md"},
            source["resources"],
        )
        self.assertNotIn(str(self.root), json.dumps(source))

    def test_compile_is_flowstep_v4_compatible_and_strips_authoring_fields(self) -> None:
        flow = compile_skill_source(self.root)
        Draft202012Validator(FLOW_SCHEMA).validate(flow)
        self.assertEqual(flow["schema"], "flowstep_flow_v4")
        self.assertEqual(flow["flow_id"], "example_flow")
        self.assertEqual([row["id"] for row in flow["milestones"]], ["source", "finish"])
        self.assertNotIn("next", flow["milestones"][0])
        self.assertNotIn("join", flow["milestones"][1])
        self.assertEqual(flow["milestones"][0]["tools"], ["source_step"])
        for milestone in flow["milestones"]:
            self.assertNotIn("observer", milestone)
            self.assertNotIn("read_paths", milestone)
            self.assertNotIn("rules", milestone)
            self.assertNotIn("agent_id", milestone)

    def test_explicit_ai_execution_compiles_without_losing_exact_refs(self) -> None:
        self._configure_ai_source()
        source = load_skill_source(self.root)
        self.assertIn("execution", source["milestones"][0])
        flow = compile_skill_source(self.root)
        milestone = flow["milestones"][0]
        self.assertEqual(milestone["execution"], source["milestones"][0]["execution"])
        self.assertEqual(
            milestone["worker"],
            milestone["execution"]["judge"]["ref"],
        )
        self.assertNotIn("capabilities", milestone)
        self.assertEqual(milestone["intelligence"], "judge")

    def test_judge_worker_is_the_exact_native_runtime_ref(self) -> None:
        self._configure_ai_source()
        self.fixture.agents["source"]["worker"] = "other_judge@1.0.0"
        self.fixture.write()
        with self.assertRaisesRegex(
            SkillSourceError,
            "worker must exactly equal execution.judge.ref",
        ):
            load_skill_source(self.root)

    def test_candidate_executor_ref_is_the_exact_builder_handler_slot(self) -> None:
        self.fixture.agents["source"]["execution"]["candidate_executor"]["ref"] = (
            "handler.other_flow.source@3.1.0"
        )
        self.fixture.write()
        with self.assertRaisesRegex(
            SkillSourceError,
            "candidate_executor.ref must be "
            "handler.example_flow.source@3.1.0; "
            "regenerate with m8m-harness-builder 3.1",
        ):
            load_skill_source(self.root)

    def test_separate_judge_requires_strict_abi_and_closed_receipt_schema(self) -> None:
        self._configure_ai_source()
        agent = self.fixture.agents["source"]
        agent.pop("judge_abi")
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "judge_abi"):
            load_skill_source(self.root)

        agent["judge_abi"] = "m8m_milestone_judge_v1"
        (self.root / "schemas" / "source_receipt.schema.json").write_text(
            json.dumps(_judge_result_schema(closed=False)),
            encoding="utf-8",
        )
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "result schema must be closed"):
            load_skill_source(self.root)

    def test_deterministic_candidate_can_bind_an_independent_ai_judge(self) -> None:
        (self.root / "schemas" / "source_input.schema.json").write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                }
            ),
            encoding="utf-8",
        )
        (self.root / "schemas" / "source_receipt.schema.json").write_text(
            json.dumps(_judge_result_schema()),
            encoding="utf-8",
        )
        agent = self.fixture.agents["source"]
        agent.update(
            {
                "intelligence": "none",
                "input_schema": "schemas/source_input.schema.json",
                "receipt_schema": "schemas/source_receipt.schema.json",
                "loop": "judge",
                "worker": "source_judge@1.0.0",
                "judge_abi": "m8m_milestone_judge_v1",
            }
        )
        agent["execution"]["judge"] = {
            "ref": "source_judge@1.0.0",
            "profile": {
                "ref": "agent_profile.source.judge.v1",
                "model_configuration": {"model": "codex", "reasoning": "low"},
                "token_budget": {
                    "max_input_tokens": 2048,
                    "max_output_tokens": 512,
                },
                "timeout_seconds": 60,
                "tools": [],
                "capabilities": [],
            },
        }
        self.fixture.write()

        source = load_skill_source(self.root)
        authored = source["milestones"][0]
        self.assertNotIn("profile", authored["execution"]["candidate_executor"])
        self.assertIn("profile", authored["execution"]["judge"])
        compiled = compile_skill_source(self.root)["milestones"][0]
        self.assertEqual(compiled["intelligence"], "none")
        self.assertEqual(compiled["judge_abi"], "m8m_milestone_judge_v1")
        self.assertEqual(compiled["execution"], authored["execution"])

    def test_ai_execution_requires_every_exact_profile_term(self) -> None:
        self._configure_ai_source()
        baseline = copy.deepcopy(self.fixture.agents["source"])
        cases = (
            ("candidate executor", lambda agent: agent["execution"].pop("candidate_executor")),
            ("judge ref", lambda agent: agent["execution"].pop("judge")),
            (
                "candidate profile",
                lambda agent: agent["execution"]["candidate_executor"].pop("profile"),
            ),
            (
                "token budget",
                lambda agent: agent["execution"]["candidate_executor"]["profile"].pop(
                    "token_budget"
                ),
            ),
            (
                "timeout",
                lambda agent: agent["execution"]["candidate_executor"]["profile"].pop(
                    "timeout_seconds"
                ),
            ),
            (
                "tool binding",
                lambda agent: agent["execution"].pop("tool_bindings"),
            ),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                self.fixture.agents["source"] = copy.deepcopy(baseline)
                mutate(self.fixture.agents["source"])
                self.fixture.write()
                with self.assertRaises(SkillSourceError):
                    load_skill_source(self.root)

    def test_deterministic_execution_and_tool_bindings_are_explicit_and_exact(self) -> None:
        baseline = copy.deepcopy(self.fixture.agents["source"])
        self.fixture.agents["source"].pop("execution")
        self.fixture.write()
        with self.assertRaises(SkillSourceError):
            load_skill_source(self.root)

        self.fixture.agents["source"] = copy.deepcopy(baseline)
        self.fixture.agents["source"]["execution"]["tool_bindings"] = []
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "exactly cover FlowStep ids"):
            load_skill_source(self.root)

    def test_compulsory_milestone_requires_one_required_output(self) -> None:
        self.fixture.agents["source"]["outputs"][0]["required"] = False
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "does not contain"):
            load_skill_source(self.root)

    def test_expectation_fields_reject_whitespace_only_values(self) -> None:
        mutations = {
            "success": lambda agent: agent.__setitem__("success", "   "),
            "output_contract": lambda agent: agent.__setitem__("output_contract", "   "),
            "output_schema": lambda agent: agent.__setitem__("output_schema", "   "),
            "output.id": lambda agent: agent["outputs"][0].__setitem__("id", "   "),
            "output.name": lambda agent: agent["outputs"][0].__setitem__("name", "   "),
        }
        baseline = copy.deepcopy(self.fixture.agents["source"])
        for label, mutate in mutations.items():
            with self.subTest(field=label):
                self.fixture.agents["source"] = copy.deepcopy(baseline)
                mutate(self.fixture.agents["source"])
                self.fixture.write()
                with self.assertRaises(SkillSourceError):
                    load_skill_source(self.root)
        self.fixture.agents["source"] = baseline
        self.fixture.write()

    def test_ai_candidate_and_judge_need_no_milestone_input_schema(self) -> None:
        self._configure_ai_source()
        agent = self.fixture.agents["source"]
        schema_path = self.root / agent.pop("input_schema")
        schema_path.unlink()
        self.fixture.write()
        compiled = compile_skill_source(self.root)
        self.assertNotIn("input_schema", compiled["milestones"][0])
        # Old declarations are readable metadata, even if the file is gone.
        agent["input_schema"] = "schemas/retired_input.json"
        self.fixture.write()
        source = load_skill_source(self.root)
        self.assertFalse(any(row["id"] == "input_schema" for row in source["resources"]))

    def test_output_schema_ports_exactly_match_declarations(self) -> None:
        path = self.root / "schemas" / "source.schema.json"
        baseline = json.loads(path.read_text(encoding="utf-8"))

        cases = []
        unknown = copy.deepcopy(baseline)
        unknown["properties"]["outputs"]["properties"]["extra"] = {"type": "object"}
        cases.append(("unknown", unknown, "ports differ"))
        missing = copy.deepcopy(baseline)
        missing["properties"]["outputs"]["properties"].pop("result")
        cases.append(("missing", missing, "ports differ"))
        required = copy.deepcopy(baseline)
        required["properties"]["outputs"]["required"] = []
        cases.append(("required", required, "exactly match required"))

        for label, schema, message in cases:
            with self.subTest(label=label):
                path.write_text(json.dumps(schema), encoding="utf-8")
                with self.assertRaisesRegex(SkillSourceError, message):
                    load_skill_source(self.root)
        path.write_text(json.dumps(baseline), encoding="utf-8")

    def test_output_schema_envelope_is_closed_at_both_levels(self) -> None:
        path = self.root / "schemas" / "source.schema.json"
        baseline = json.loads(path.read_text(encoding="utf-8"))
        cases = []
        open_root = copy.deepcopy(baseline)
        open_root.pop("additionalProperties")
        cases.append(("root", open_root, "root must set additionalProperties to false"))
        open_outputs = copy.deepcopy(baseline)
        open_outputs["properties"]["outputs"].pop("additionalProperties")
        cases.append(
            (
                "outputs",
                open_outputs,
                "outputs object must set additionalProperties to false",
            )
        )
        for label, schema, message in cases:
            with self.subTest(level=label):
                path.write_text(json.dumps(schema), encoding="utf-8")
                with self.assertRaisesRegex(SkillSourceError, message):
                    load_skill_source(self.root)
        path.write_text(json.dumps(baseline), encoding="utf-8")

    def test_output_schema_cardinality_matches_array_shape(self) -> None:
        path = self.root / "schemas" / "source.schema.json"
        baseline = json.loads(path.read_text(encoding="utf-8"))

        self.fixture.agents["source"]["outputs"][0]["cardinality"] = "many"
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "cardinality many requires an array"):
            load_skill_source(self.root)

        self.fixture.agents["source"]["outputs"][0]["cardinality"] = "one"
        array_schema = copy.deepcopy(baseline)
        array_schema["properties"]["outputs"]["properties"]["result"] = {
            "type": "array",
            "items": {"type": "object"},
        }
        path.write_text(json.dumps(array_schema), encoding="utf-8")
        self.fixture.write()
        # A JSON array is one JSON document, not necessarily a set of members.
        load_skill_source(self.root)
        self.fixture.agents["source"]["outputs"][0]["kind"] = "image"
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "cardinality one cannot use an array"):
            load_skill_source(self.root)

    def test_ai_profiles_exactly_bind_declared_capabilities(self) -> None:
        self._configure_ai_source()
        agent = self.fixture.agents["source"]
        agent["capabilities"] = [
            {
                "id": "content.read",
                "ref": "capability.content.read@1.0.0",
                "access": "read",
                "side_effects": "none",
            }
        ]
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "exactly bind declared capabilities"):
            load_skill_source(self.root)

        agent["execution"]["candidate_executor"]["profile"]["capabilities"] = [
            "content.read"
        ]
        self.fixture.write()
        self.assertEqual(load_skill_source(self.root)["milestones"][0]["agent_id"], "source")

    def test_external_side_effect_requires_explicit_external_capability(self) -> None:
        self._configure_ai_source()
        agent = self.fixture.agents["source"]
        agent["side_effects"] = "external"
        agent["phase_journal"] = {
            "path": "publication/phase-journal.json",
            "operator_result_path": "publication/operator-result.json",
            "resume": "query_exact_operation",
        }
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "explicit external capability"):
            load_skill_source(self.root)

    def test_capability_requires_versioned_ref_and_external_semantics_are_bidirectional(self) -> None:
        agent = self.fixture.agents["source"]
        agent["capabilities"] = [
            {
                "id": "publication.write",
                "access": "write",
                "side_effects": "external",
            }
        ]
        self.fixture.write()
        with self.assertRaises(SkillSourceError):
            load_skill_source(self.root)

        agent["capabilities"][0]["ref"] = "capability.publication.write@1.0.0"
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "requires side_effects external"):
            load_skill_source(self.root)

    def test_schema_dependencies_are_transitively_declared_and_unsafe_refs_fail_closed(self) -> None:
        source_schema = self.root / "schemas" / "source.schema.json"
        shared_schema = self.root / "schemas" / "shared.schema.json"
        shared_schema.write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$defs": {
                        "payload": {
                            "type": "object",
                            "additionalProperties": False,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        source_schema.write_text(
            json.dumps(_candidate_schema({"$ref": "shared.schema.json#/$defs/payload"})),
            encoding="utf-8",
        )
        source = load_skill_source(self.root)
        dependency = next(
            item
            for item in source["resources"]
            if item["path"] == "schemas/shared.schema.json"
        )
        self.assertEqual(dependency["kind"], "schema")

        for raw_ref, message in (
            ("https://example.test/schema.json", "external JSON Schema refs"),
            ("missing.schema.json", "referenced JSON Schema is missing"),
            ("shared.schema.json#/$defs/missing", "unresolved JSON Schema fragment"),
        ):
            with self.subTest(raw_ref=raw_ref):
                source_schema.write_text(
                    json.dumps(_candidate_schema({"$ref": raw_ref})),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(SkillSourceError, message):
                    load_skill_source(self.root)

        outside = self.root.parent / "outside.schema.json"
        outside.write_text(
            json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema"}),
            encoding="utf-8",
        )
        source_schema.write_text(
            json.dumps(_candidate_schema({"$ref": "../../outside.schema.json"})),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SkillSourceError, "escapes the skill root"):
            load_skill_source(self.root)

    def _declare_workflow_contracts(self) -> None:
        schemas = {
            "request": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["prompt"],
                "properties": {"prompt": {"type": "string", "minLength": 1}},
            },
            "configuration": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "properties": {"locale": {"type": "string"}},
            },
            "result": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["final_result"],
                "properties": {"final_result": {"type": "object"}},
            },
        }
        for name, schema in schemas.items():
            (self.root / "schemas" / f"workflow_{name}.schema.json").write_text(
                json.dumps(schema), encoding="utf-8"
            )
        self.fixture.openai["canvas"]["workflow_contracts"] = {
            "request_schema": "schemas/workflow_request.schema.json",
            "configuration_schema": "schemas/workflow_configuration.schema.json",
            "result_schema": "schemas/workflow_result.schema.json",
            "terminal_bindings": [
                {
                    "name": "final_result",
                    "from": "finish.finish_v1",
                    "output": "result",
                }
            ],
        }
        self.fixture.write()

    def test_required_workflow_contracts_are_validated_and_stay_authoring_only(self) -> None:
        self._declare_workflow_contracts()
        source = load_skill_source(self.root)
        self.assertEqual(
            source["canvas"]["workflow_contracts"]["terminal_bindings"],
            [
                {
                    "name": "final_result",
                    "from": "finish.finish_v1",
                    "output": "result",
                }
            ],
        )
        for field in ("request_schema", "configuration_schema", "result_schema"):
            self.assertIn(
                {
                    "owner": "workflow",
                    "id": field,
                    "kind": "schema",
                    "path": f"schemas/workflow_{field.removesuffix('_schema')}.schema.json",
                },
                source["resources"],
            )
        compiled = compile_skill_source(self.root)
        self.assertNotIn("workflow_contracts", compiled)

    def test_native_source_rejects_missing_workflow_contracts(self) -> None:
        self.fixture.openai["canvas"].pop("workflow_contracts")
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "workflow_contracts"):
            load_skill_source(self.root)

    def test_workflow_contract_schemas_must_be_safe_draft_2020_12_resources(self) -> None:
        self._declare_workflow_contracts()
        request_path = self.root / "schemas" / "workflow_request.schema.json"
        request_path.write_text(
            json.dumps({"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SkillSourceError, "must declare Draft 2020-12"):
            load_skill_source(self.root)

        request_path.write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "unknown",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SkillSourceError, "invalid JSON Schema"):
            load_skill_source(self.root)

        self.fixture.openai["canvas"]["workflow_contracts"]["request_schema"] = "../outside.json"
        self.fixture.write()
        with self.assertRaises(SkillSourceError):
            load_skill_source(self.root)

    def test_terminal_bindings_must_name_exact_terminal_contract_and_port(self) -> None:
        self._declare_workflow_contracts()
        binding = self.fixture.openai["canvas"]["workflow_contracts"]["terminal_bindings"][0]
        for field, value, message in (
            ("from", "source.source_v1", "not a declared success terminal"),
            ("from", "finish.wrong_v1", "contract does not match"),
            ("output", "missing", "unknown output port"),
        ):
            with self.subTest(field=field, value=value):
                binding.update(
                    {"from": "finish.finish_v1", "output": "result", field: value}
                )
                self.fixture.write()
                with self.assertRaisesRegex(SkillSourceError, message):
                    load_skill_source(self.root)

    def test_workflow_contract_roots_are_closed_and_result_names_are_exact(self) -> None:
        self._declare_workflow_contracts()
        configuration_path = self.root / "schemas" / "workflow_configuration.schema.json"
        configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
        configuration["additionalProperties"] = True
        configuration_path.write_text(json.dumps(configuration), encoding="utf-8")
        with self.assertRaisesRegex(SkillSourceError, "must be a closed object schema"):
            load_skill_source(self.root)

        configuration["additionalProperties"] = False
        configuration_path.write_text(json.dumps(configuration), encoding="utf-8")
        result_path = self.root / "schemas" / "workflow_result.schema.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["properties"] = {"wrong_name": {"type": "object"}}
        result["required"] = ["wrong_name"]
        result_path.write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaisesRegex(SkillSourceError, "properties must exactly match"):
            load_skill_source(self.root)

    def test_terminal_binding_names_and_source_ports_are_unique(self) -> None:
        self._declare_workflow_contracts()
        bindings = self.fixture.openai["canvas"]["workflow_contracts"]["terminal_bindings"]
        bindings.append(dict(bindings[0]))
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "duplicate name"):
            load_skill_source(self.root)
        bindings[1]["name"] = "also_final"
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "duplicate terminal output"):
            load_skill_source(self.root)

    def test_compile_bytes_are_deterministic_across_roots(self) -> None:
        second_root = Path(self.temp.name) / "copy"
        SkillFixture(second_root)
        first = compile_skill_source_bytes(self.root)
        second = compile_skill_source_bytes(second_root)
        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b"\n"))
        self.assertEqual(first, canonical_json_bytes(compile_skill_source(self.root)))

    def test_native_branch_graph_is_accepted_without_legacy_next_or_join(self) -> None:
        schema = _candidate_schema()
        for agent_id in ("left", "right"):
            (self.root / "schemas" / f"{agent_id}.schema.json").write_text(
                json.dumps(schema), encoding="utf-8"
            )
            (self.root / "references" / f"{agent_id}.md").write_text(
                f"# {agent_id.title()}\n\n"
                f"## `{agent_id}_step`\n\nProduce it.\n",
                encoding="utf-8",
            )
            self.fixture.agents[agent_id] = SkillFixture._agent(agent_id, f"{agent_id}_v1")
            self.fixture.agents[agent_id]["inputs"] = {"source": "source.source_v1"}
            self.fixture.agents[agent_id]["on_path"] = agent_id
        self.fixture.agents["source"]["flowsteps"].append(
            {"id": "source_branch_control", "tool": "branch_receipt"}
        )
        self.fixture.agents["source"]["execution"]["tool_bindings"].append(
            {
                "tool": "source_branch_control",
                "ref": "branch_receipt@1.0.0",
            }
        )
        self.fixture.agents["source"]["observer"]["actions"].append(
            {
                "flowstep_id": "source_branch_control",
                "title": "Choose the admitted path",
                "summary": "Apply branch control after candidate admission.",
            }
        )
        with (self.root / "references" / "source.md").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(
                "\n## `source_branch_control`\n\n"
                "Choose a path from the admitted source output.\n"
            )
        self.fixture.agents["source"]["branch"] = {
            "worker": "source_branch_control",
            "default": "left",
            "join": "finish",
            "paths": [
                {"id": "left", "then": "left"},
                {"id": "right", "then": "right"},
            ],
        }
        self.fixture.agents["finish"]["inputs"] = {
            "left": "left.left_v1",
            "right": "right.right_v1",
        }
        canvas = self.fixture.openai["canvas"]
        canvas["milestones"] = ["source", "left", "right", "finish"]
        canvas["graph"] = [
            {"from": "source", "to": ["left", "right"]},
            {"from": "left", "to": ["finish"]},
            {"from": "right", "to": ["finish"]},
            {"from": "finish", "to": []},
        ]
        self.fixture.write()
        flow = compile_skill_source(self.root)
        source = next(row for row in flow["milestones"] if row["id"] == "source")
        finish = next(row for row in flow["milestones"] if row["id"] == "finish")
        self.assertEqual(source["branch"]["join"], "finish")
        self.assertNotIn("next", source)
        self.assertNotIn("join", finish)
        compiled_path = self.root / "compiled-branch.yaml"
        compiled_path.write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
        normalized = load_flow(self.root, compiled_path)
        normalized_source = next(row for row in normalized["steps"] if row["id"] == "source")
        self.assertEqual(normalized_source["branch"]["join"], "finish")

    def test_branch_graph_must_match_declared_paths_and_join(self) -> None:
        schema = _candidate_schema()
        for agent_id in ("left", "right"):
            (self.root / "schemas" / f"{agent_id}.schema.json").write_text(
                json.dumps(schema), encoding="utf-8"
            )
            (self.root / "references" / f"{agent_id}.md").write_text(
                f"# {agent_id.title()}\n\n"
                f"## `{agent_id}_step`\n\nProduce it.\n",
                encoding="utf-8",
            )
            self.fixture.agents[agent_id] = SkillFixture._agent(agent_id, f"{agent_id}_v1")
            self.fixture.agents[agent_id]["inputs"] = {"source": "source.source_v1"}
            self.fixture.agents[agent_id]["on_path"] = agent_id
        self.fixture.agents["source"]["branch"] = {
            "worker": "source_tool",
            "default": "left",
            "join": "finish",
            "paths": [
                {"id": "left", "then": "left"},
                {"id": "right", "then": "right"},
            ],
        }
        canvas = self.fixture.openai["canvas"]
        canvas["milestones"] = ["source", "left", "right", "finish"]
        canvas["graph"] = [
            {"from": "source", "to": ["right", "left"]},
            {"from": "left", "to": ["finish"]},
            {"from": "right", "to": ["finish"]},
            {"from": "finish", "to": []},
        ]
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "does not match native v4"):
            load_skill_source(self.root)

    def test_native_cycle_graph_round_trips_deterministically(self) -> None:
        schema = _candidate_schema()
        for agent_id in ("bound", "rendered"):
            (self.root / "schemas" / f"{agent_id}.schema.json").write_text(
                json.dumps(schema), encoding="utf-8"
            )
            (self.root / "references" / f"{agent_id}.md").write_text(
                f"# {agent_id.title()}\n\n"
                f"## `{agent_id}_step`\n\nProduce it.\n",
                encoding="utf-8",
            )
            self.fixture.agents[agent_id] = SkillFixture._agent(agent_id, f"{agent_id}_v1")
            self.fixture.agents[agent_id]["on_cycle"] = "pages"
        self.fixture.agents["bound"]["inputs"] = {"ledger": "source.source_v1"}
        self.fixture.agents["rendered"]["inputs"] = {"bound": "bound.bound_v1"}
        self.fixture.agents["rendered"]["flowsteps"].append(
            {"id": "rendered_cycle_control", "tool": "cycle_receipt"}
        )
        self.fixture.agents["rendered"]["execution"]["tool_bindings"].append(
            {
                "tool": "rendered_cycle_control",
                "ref": "cycle_receipt@1.0.0",
            }
        )
        self.fixture.agents["rendered"]["observer"]["actions"].append(
            {
                "flowstep_id": "rendered_cycle_control",
                "title": "Advance the admitted row",
                "summary": "Apply cycle control after candidate admission.",
            }
        )
        with (self.root / "references" / "rendered.md").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(
                "\n## `rendered_cycle_control`\n\n"
                "Choose pass or fail from the admitted rendered output.\n"
            )
        self.fixture.agents["rendered"]["cycle"] = {
            "id": "pages",
            "worker": "rendered_cycle_control",
            "ledger": "source",
            "start": "bound",
            "join": "finish",
            "max_rounds": 10,
            "pass": "The current row is complete.",
        }
        self.fixture.agents["finish"]["inputs"] = {"rendered": "rendered.rendered_v1"}
        canvas = self.fixture.openai["canvas"]
        canvas["milestones"] = ["source", "bound", "rendered", "finish"]
        canvas["graph"] = [
            {"from": "source", "to": ["bound"]},
            {"from": "bound", "to": ["rendered"]},
            {"from": "rendered", "to": ["bound", "finish"]},
            {"from": "finish", "to": []},
        ]
        self.fixture.write()
        first = compile_skill_source_bytes(self.root)
        second = compile_skill_source_bytes(self.root)
        self.assertEqual(first, second)
        flow = json.loads(first)
        rendered = next(row for row in flow["milestones"] if row["id"] == "rendered")
        self.assertEqual(rendered["cycle"]["start"], "bound")
        self.assertEqual(rendered["on_cycle"], "pages")
        self.assertNotIn("next", rendered)
        compiled_path = self.root / "compiled-cycle.yaml"
        compiled_path.write_bytes(first)
        normalized = load_flow(self.root, compiled_path)
        normalized_rendered = next(row for row in normalized["steps"] if row["id"] == "rendered")
        self.assertEqual(normalized_rendered["cycle"]["start"], "bound")
        self.assertEqual(normalized_rendered["cycle"]["join"], "finish")

    def test_missing_milestone_agent_is_rejected(self) -> None:
        (self.root / "agents" / "finish.yaml").unlink()
        with self.assertRaisesRegex(SkillSourceError, "does not exist"):
            load_skill_source(self.root)

    def test_undeclared_agent_is_rejected(self) -> None:
        (self.root / "agents" / "fat_worker.yaml").write_text("rules: []\n", encoding="utf-8")
        with self.assertRaisesRegex(SkillSourceError, "undeclared agent file"):
            load_skill_source(self.root)

    def test_standalone_judge_agent_is_rejected_until_it_has_a_typed_contract(self) -> None:
        judge = self.root / "agents" / "source_judge.yaml"
        judge.write_text("agent_id: source_judge\nrole: judge\n", encoding="utf-8")
        with self.assertRaisesRegex(SkillSourceError, "standalone judge agent files are not accepted"):
            load_skill_source(self.root)

    def test_missing_resource_is_rejected(self) -> None:
        self.fixture.agents["source"]["read_paths"].append("references/missing.md")
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "does not exist"):
            load_skill_source(self.root)

    def test_outside_root_and_absolute_resource_refs_are_rejected(self) -> None:
        for bad in ("../outside.md", "C:/outside.md", "/tmp/outside.md", "references/**"):
            with self.subTest(path=bad):
                self.fixture.agents["source"]["read_paths"] = ["references/source.md", bad]
                self.fixture.write()
                with self.assertRaises(SkillSourceError):
                    load_skill_source(self.root)

    def test_outside_root_symlink_is_rejected_when_supported(self) -> None:
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        link = self.root / "references" / "linked.md"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")
        self.fixture.agents["source"]["read_paths"].append("references/linked.md")
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "symbolic links"):
            load_skill_source(self.root)

    def test_duplicate_graph_row_is_rejected(self) -> None:
        self.fixture.openai["canvas"]["graph"].append({"from": "source", "to": ["finish"]})
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "duplicate rows"):
            load_skill_source(self.root)

    def test_disconnected_or_wrong_entry_graph_is_rejected(self) -> None:
        self.fixture.openai["canvas"]["graph"][0]["to"] = []
        self.fixture.openai["canvas"]["terminal_states"]["success"] = ["source", "finish"]
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "exactly the declared entry root"):
            load_skill_source(self.root)

    def test_undeclared_backward_edge_is_rejected_instead_of_inferred(self) -> None:
        self.fixture.openai["canvas"]["entry"] = "finish"
        self.fixture.openai["canvas"]["graph"] = [
            {"from": "source", "to": []},
            {"from": "finish", "to": ["source"]},
        ]
        self.fixture.openai["canvas"]["terminal_states"]["success"] = ["source"]
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "does not match native v4"):
            load_skill_source(self.root)

    def test_terminal_states_must_equal_graph_sinks(self) -> None:
        self.fixture.openai["canvas"]["terminal_states"]["success"] = ["source"]
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "sink milestones"):
            load_skill_source(self.root)

    def test_binding_must_match_upstream_contract(self) -> None:
        self.fixture.agents["finish"]["inputs"]["source"] = "source.wrong_v1"
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "contract does not match"):
            load_skill_source(self.root)

    def test_binding_cannot_read_downstream(self) -> None:
        self.fixture.agents["source"]["inputs"]["bad"] = "finish.finish_v1"
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "not upstream"):
            load_skill_source(self.root)

    def test_master_prompt_needs_no_flowstep_headings_but_keeps_milestone_path(self) -> None:
        (self.root / "references" / "source.md").write_text(
            "MASTER PROMPT — SOURCE\n\nYou prepare the source from the bound request.\n"
            "Return the complete source result in its declared format.\n", encoding="utf-8"
        )
        load_skill_source(self.root)
        self.fixture.agents["source"]["gem"] = "references/finish.md"
        self.fixture.agents["source"]["read_paths"] = ["references/finish.md"]
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "exact path"):
            load_skill_source(self.root)

    def test_master_prompt_cannot_be_empty(self) -> None:
        (self.root / "references" / "source.md").write_text("\n  \n", encoding="utf-8")
        with self.assertRaisesRegex(SkillSourceError, "master prompt must not be empty"):
            load_skill_source(self.root)

    def test_gem_success_is_an_exact_projection_of_authored_success(self) -> None:
        path = self.root / "references" / "source.md"
        path.write_text(
            "# Source\n\n## Rule of success\n\nA different semantic goal.\n\n"
            "## `source_step`\n\nProduce the bounded candidate.\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SkillSourceError, "one expectation authority"):
            load_skill_source(self.root)

    def test_observer_action_and_output_refs_are_closed(self) -> None:
        self.fixture.agents["source"]["observer"]["actions"][0]["flowstep_id"] = "wrong"
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "observer actions"):
            load_skill_source(self.root)
        self.fixture.agents["source"]["observer"]["actions"][0]["flowstep_id"] = "source_step"
        self.fixture.agents["source"]["observer"]["outputs"][0]["output_id"] = "wrong"
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "observer outputs"):
            load_skill_source(self.root)

    def test_duplicate_output_and_flowstep_ids_are_rejected(self) -> None:
        self.fixture.agents["source"]["outputs"].append(
            dict(self.fixture.agents["source"]["outputs"][0])
        )
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "output ids"):
            load_skill_source(self.root)
        self.fixture.agents["source"]["outputs"].pop()
        self.fixture.agents["source"]["flowsteps"].append(
            dict(self.fixture.agents["source"]["flowsteps"][0])
        )
        self.fixture.agents["source"]["observer"]["actions"].append(
            dict(self.fixture.agents["source"]["observer"]["actions"][0])
        )
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "FlowStep ids"):
            load_skill_source(self.root)

    def test_tools_must_be_derived_in_flowstep_order(self) -> None:
        self.fixture.agents["source"]["tools"] = ["other_tool"]
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "first-use FlowStep id order"):
            load_skill_source(self.root)

    def test_duplicate_yaml_keys_and_aliases_are_rejected(self) -> None:
        path = self.root / "agents" / "source.yaml"
        original = path.read_text(encoding="utf-8")
        path.write_text(original + "success: duplicate\n", encoding="utf-8")
        with self.assertRaisesRegex(SkillSourceError, "duplicate YAML key"):
            load_skill_source(self.root)
        path.write_text(original.replace("success:", "success: &same") + "alias_probe: *same\n", encoding="utf-8")
        with self.assertRaisesRegex(SkillSourceError, "aliases are not allowed"):
            load_skill_source(self.root)

    def test_malformed_json_resource_is_rejected(self) -> None:
        (self.root / "schemas" / "source.schema.json").write_text(
            '{"type":"object","type":"array"}', encoding="utf-8"
        )
        with self.assertRaisesRegex(SkillSourceError, "duplicate JSON key"):
            load_skill_source(self.root)

    def test_unknown_source_fields_fail_closed(self) -> None:
        self.fixture.openai["canvas"]["cloud_release_id"] = "release-123"
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "Additional properties"):
            load_skill_source(self.root)

    def test_skill_body_is_a_bounded_pointer_not_a_recipe(self) -> None:
        path = self.root / "SKILL.md"
        frontmatter = path.read_text(encoding="utf-8").split("---", 2)[:2]

        def write_body(body: str) -> None:
            path.write_text("---".join([*frontmatter, f"\n{body}"]), encoding="utf-8")

        write_body(
            "# Example skill\n\nInvoke `$example-skill` through `agents/openai.yaml`; "
            "milestone instructions live in `references/<milestone>.md`.\n"
        )
        self.assertEqual(load_skill_source(self.root)["skill"]["name"], "example-skill")

        for label, body in (
            (
                "recipe list",
                "# Example skill\n\nInvoke through `agents/openai.yaml` and "
                "`references/<milestone>.md`.\n\n- First produce the product.\n",
            ),
            (
                "oversized",
                "# Example skill\n\nInvoke through `agents/openai.yaml` and "
                "`references/<milestone>.md`. " + ("x" * 2100),
            ),
        ):
            with self.subTest(label=label):
                write_body(body)
                with self.assertRaisesRegex(SkillSourceError, "SKILL.md body"):
                    load_skill_source(self.root)

    def test_milestone_rules_are_canonical_harness_only_directives(self) -> None:
        self.fixture.agents["source"]["rules"] = [
            "Read only the declared read_paths. Do not set ok, branch, or cycle.",
            "Write only the declared write_paths.",
        ]
        self.fixture.write()
        self.assertEqual(load_skill_source(self.root)["milestones"][0]["agent_id"], "source")

        self.fixture.agents["source"]["rules"] = [
            "Write the complete product recipe, then publish it."
        ]
        self.fixture.write()
        with self.assertRaisesRegex(SkillSourceError, "harness-only directive"):
            load_skill_source(self.root)

    def test_generated_flow_snapshot_must_match_canonical_compile(self) -> None:
        flow_path = self.root / "flow.yaml"
        compiled = compile_skill_source(self.root)
        flow_path.write_text(
            yaml.safe_dump(compiled, sort_keys=False), encoding="utf-8"
        )
        self.assertEqual(load_skill_source(self.root)["canvas"]["flow_id"], "example_flow")

        drifted = copy.deepcopy(compiled)
        drifted["version"] += 1
        flow_path.write_text(
            yaml.safe_dump(drifted, sort_keys=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(SkillSourceError, "flow.yaml differs"):
            load_skill_source(self.root)

    def test_compile_is_read_only(self) -> None:
        before = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        compile_skill_source(self.root)
        after = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
