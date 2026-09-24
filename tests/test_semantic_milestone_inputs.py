from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import support  # noqa: F401
from flowstep_runtime import FlowError, implementation_files, load_flow
from run_flow import advance
from session_layout import resolve_chosen_output
from test_run_isolation import _scaffold
from coordinate_workflow import prepare_workflow


@pytest.mark.parametrize("legacy", ["omitted", "missing", "rejecting", "malformed"])
@pytest.mark.parametrize("context", [
    "Use the blue car. Actually, keep its original paint. See attached notes.",
    ["first note", {"correction": "night", "unplanned_field": True}],
    {"notes": "unfinished brief", "references": [], "anything_else": [1, None]},
])
def test_semantic_context_reaches_model_and_downstream_without_input_schema(tmp_path, legacy, context):
    _, harness = _scaffold(tmp_path, two_steps=True, model=True)
    path = harness / "flow.yaml"
    definition = yaml.safe_load(path.read_text(encoding="utf-8"))
    for milestone in definition["milestones"]:
        if legacy == "omitted":
            milestone.pop("input_schema", None)
    path.write_text(yaml.safe_dump(definition), encoding="utf-8")
    old_schema = harness / "schemas/input.json"
    if legacy in {"omitted", "missing"}:
        old_schema.unlink()
    else:
        old_schema.write_text("not JSON" if legacy == "malformed" else
                              json.dumps({"type": "object", "required": ["never_supplied"]}), encoding="utf-8")
    (harness / "schemas/draft.json").write_text(json.dumps({
        "type": "array", "items": {"type": "object", "required": ["fact"],
                                     "properties": {"fact": {"type": "string"}}},
    }), encoding="utf-8")
    (harness / "source.py").write_text(
        "def run(input_data, draft=None, **kwargs):\n"
        "    if draft is None:\n"
        "        return {'_flowstep': 'NEED_MODEL', 'model': 'completion', 'model_request': {'instruction': 'Interpret the supplied context.'}}\n"
        "    return {'outputs': {'result': {'context': input_data['request'], 'facts': draft}}}\n",
        encoding="utf-8",
    )
    (harness / "result.py").write_text(
        "def run(input_data, **kwargs):\n    return {'outputs': {'result': input_data['source']}}\n",
        encoding="utf-8",
    )
    request = tmp_path / "request.json"
    request.write_text(json.dumps(context), encoding="utf-8")
    run = tmp_path / "run"
    action = advance(harness, run, request_path=request)
    assert action["state"] == "ACTION_REQUIRED", action
    task = json.loads((run / action["task_path"]).read_text(encoding="utf-8"))
    assert task["inputs"]["request"] == context
    draft = run / action["draft_path"]
    facts = [{"fact": "The scene is at night."}]
    draft.write_text(json.dumps(facts), encoding="utf-8")
    complete = advance(harness, run, draft_path=draft)
    assert complete["state"] == "COMPLETE", complete
    assert resolve_chosen_output(run, "result_ready", output_id="result") == {"context": context, "facts": facts}
    # Editing an unused old schema must not invalidate resume or regenerate work.
    if old_schema.exists():
        old_schema.write_text("changed unused legacy schema", encoding="utf-8")
    assert advance(harness, run)["state"] == "COMPLETE"
    flow = load_flow(harness)
    assert old_schema not in implementation_files(harness, flow)


def test_schema_free_input_does_not_relax_structured_output_admission(tmp_path):
    _, harness = _scaffold(tmp_path, invalid=True)
    path = harness / "flow.yaml"
    definition = yaml.safe_load(path.read_text(encoding="utf-8"))
    definition["milestones"][0].pop("input_schema")
    path.write_text(yaml.safe_dump(definition), encoding="utf-8")
    (harness / "schemas/input.json").unlink()
    request = tmp_path / "request.json"
    request.write_text(json.dumps("A rough request with no formal fields"), encoding="utf-8")
    action = advance(harness, tmp_path / "run", request_path=request)
    assert action["state"] == "BLOCKED", action
    assert not (tmp_path / "run/milestones/source_ready/out/chosen-output.json").exists()
    assert "output" in json.dumps(action).lower()


def test_native_coordination_validates_without_input_schema_files():
    from test_coordination_workflow import CoordinationTests

    case = CoordinationTests()
    case.setUp()
    try:
        for agent in case.fixture.agents.values():
            path = case.harness / agent.pop("input_schema")
            path.unlink()
        case.fixture.write()
        assert prepare_workflow(case.harness, case.repo)["status"] == "PASS"
        compiled = yaml.safe_load((case.harness / "flow.yaml").read_text(encoding="utf-8"))
        assert all("input_schema" not in row for row in compiled["milestones"])
        # Missing declared upstream outputs are still an error, not guessed context.
        case.fixture.agents["finish"]["inputs"]["source"]["output"] = "unknown_port"
        case.fixture.write()
        with pytest.raises(FlowError):
            prepare_workflow(case.harness, case.repo)
    finally:
        case.doCleanups()


@pytest.mark.parametrize("value", [[], [{"fact": "night"}, {"fact": "parked"}]])
def test_one_json_output_can_be_an_array(tmp_path, value):
    _, harness = _scaffold(tmp_path)
    output_path = harness / "schemas/candidate.json"
    schema = json.loads(output_path.read_text(encoding="utf-8"))
    schema["properties"]["outputs"]["properties"]["result"] = {
        "type": "array", "items": {"type": "object", "required": ["fact"],
                                     "properties": {"fact": {"type": "string"}}},
    }
    output_path.write_text(json.dumps(schema), encoding="utf-8")
    (harness / "source.py").write_text(
        f"def run(input_data, **kwargs):\n    return {{'outputs': {{'result': {value!r}}}}}\n", encoding="utf-8",
    )
    (harness / "schemas/input.json").unlink()
    request = tmp_path / "request.json"
    request.write_text(json.dumps("Extract what is available."), encoding="utf-8")
    run = tmp_path / "run"
    result = advance(harness, run, request_path=request)
    assert result["state"] == "COMPLETE", result
    assert resolve_chosen_output(run, "source_ready", output_id="result") == value
