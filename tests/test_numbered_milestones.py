from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml

import support  # noqa: F401
from audit_harness import audit_skill, number_new_milestones
from generate_harness import generate_v4_flow
from run_flow import advance
from session_layout import resolve_chosen_output
from test_run_isolation import _scaffold, _request


def test_numbering_remaps_dependencies_and_controls_without_changing_tool_contracts():
    original = [
        {"id": "setup", "inputs": {"request": "user.request"}, "output_contract": "setup_contract",
         "branch": {"join": "finish", "paths": [{"id": "direct", "then": "prompts"}]},
         "cycle": {"ledger": "setup", "start": "prompts", "join": "finish"}},
        {"id": "prompts", "inputs": {"image": {"from": "setup.setup_contract", "output": "master"}},
         "execution": {"candidate_executor": {"ref": "handler.existing.prompts@1.0.0"}}},
        {"id": "finish", "inputs": {"original": {"from": "setup.setup_contract", "output": "master"},
                                      "prompts": "prompts.prompt_contract"}},
    ]
    before = copy.deepcopy(original)
    numbered = number_new_milestones(original)
    assert original == before
    assert [item["id"] for item in numbered] == ["milestone01", "milestone02", "milestone03"]
    assert numbered[2]["inputs"] == {
        "original": {"from": "milestone01.setup_contract", "output": "master"},
        "prompts": "milestone02.prompt_contract",
    }
    assert numbered[0]["branch"] == {"join": "milestone03", "paths": [{"id": "direct", "then": "milestone02"}]}
    assert numbered[0]["cycle"] == {"ledger": "milestone01", "start": "milestone02", "join": "milestone03"}
    assert numbered[1]["execution"]["candidate_executor"]["ref"] == "handler.existing.milestone02@1.0.0"
    assert numbered[1]["gem"] == "references/milestone02.md"


def test_generated_bindings_follow_upstream_references_without_input_schemas(tmp_path):
    specs = []
    for index in range(1, 4):
        mid = f"milestone{index:02d}"
        specs.append({
            "id": mid, "success": "The requested value is available.",
            "output_contract": f"value_{index}",
            "output_schema_object": {"type": "object", "required": ["value"], "properties": {"value": {"type": "integer"}}},
            "outputs": [{"id": "result", "name": "Computed value", "kind": "json", "cardinality": "one", "required": True}],
            "flowsteps": [], "tools": [],
            "execution": {"candidate_executor": {"ref": f"handler.numbered.{mid}@3.1.0"}, "tool_bindings": []},
        })
    specs[2]["inputs"] = {
        "original": {"from": "milestone01.value_1", "output": "result"},
        "derived": {"from": "milestone02.value_2", "output": "result"},
    }
    result = generate_v4_flow(tmp_path / "repo", "numbered", [], tools=[], milestone_specs=specs)
    harness = Path(result["harness_dir"])
    steps = yaml.safe_load((harness / "flow.yaml").read_text(encoding="utf-8"))["milestones"]
    assert steps[1]["inputs"] == {"milestone01": "milestone01.value_1"}
    assert steps[2]["inputs"] == specs[2]["inputs"]
    assert all("input_schema" not in step for step in steps)
    assert not list((harness / "milestones").glob("*/input.schema.json"))
    prompt = (harness / "references/milestone03.md").read_text(encoding="utf-8")
    assert "from: milestone01.value_1" in prompt
    assert "from: milestone02.value_2" in prompt
    assert "from: milestone03.value_3" in prompt
    assert "output: result" in prompt


def test_third_milestone_uses_both_earlier_outputs_with_its_own_full_prompt_and_resumes(tmp_path):
    _, harness = _scaffold(tmp_path)
    flow_path = harness / "flow.yaml"
    flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
    template = flow["milestones"][0]
    steps = []
    expressions = ["{'number': 7}", "{'number': input_data['original']['number'] * 2}",
                   "{'original': input_data['original'], 'derived': input_data['derived'], 'prompt': task['master_prompt']}" ]
    for index, expression in enumerate(expressions, 1):
        mid = f"milestone{index:02d}"
        step = copy.deepcopy(template)
        step.update(id=mid, output_contract=f"value_{index}", handler=f"{mid}.py", gem=f"references/{mid}.md")
        step["execution"]["candidate_executor"]["ref"] = f"handler.isolation_v1.{mid}@3.1.0"
        if index > 1:
            step["inputs"] = {"original": {"from": "milestone01.value_1", "output": "result"}}
        if index == 3:
            step["inputs"]["derived"] = {"from": "milestone02.value_2", "output": "result"}
        prompt_path = harness / step["gem"]
        prompt_path.parent.mkdir(exist_ok=True)
        prompt_path.write_text(f"MASTER PROMPT — {mid}\n\nUse the declared inputs to produce the complete requested value.", encoding="utf-8")
        (harness / step["handler"]).write_text(
            f"def run(input_data, task=None, **kwargs):\n    return {{'outputs': {{'result': {expression}}}}}\n", encoding="utf-8")
        steps.append(step)
    flow["milestones"] = steps
    flow_path.write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
    run = tmp_path / "run"
    action = advance(harness, run, request_path=_request(tmp_path / "request.json"))
    assert action["state"] == "COMPLETE", action
    assert resolve_chosen_output(run, "milestone03", output_id="result") == {
        "original": {"number": 7}, "derived": {"number": 14},
        "prompt": (harness / "references/milestone03.md").read_text(encoding="utf-8"),
    }
    assert advance(harness, run)["state"] == "COMPLETE"
    # Auditing an existing numbered flow must preserve its nodes, even when all
    # three are deterministic; their names must not cause them to be collapsed.
    report = audit_skill(harness)
    assert [item["id"] for item in report["proposed_milestones"]] == [step["id"] for step in steps]
