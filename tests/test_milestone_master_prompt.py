from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import support  # noqa: F401
from gem_text import OUTLINE_START, OUTLINE_END, read_gem_section, read_master_prompt
from run_flow import advance
from session_layout import resolve_chosen_output
from teaching_contracts import write_milestone_gems
from test_run_isolation import _scaffold, _request
from test_model_recovery_capsule import _two_phase_scaffold


PROMPT = (
    "MASTER PROMPT — WATERFRONT SETUP\n\n"
    "You are the automotive photographer. Use the car reference for vehicle identity only.\n\n"
    "## Complete task\n\n"
    + "Preserve the visible vehicle build, its wheels, paint, stance, and proportions.\n" * 180
    + "\n## Environment\n\nDry waterfront roadside at night, pale concrete barrier, warm road lamps.\n\n"
    "## Output\n\nGenerate one complete front three-quarter car image without a person.\n"
    "This is the master continuity image, not img1."
)


def add_prompt(harness: Path, text: str = PROMPT) -> Path:
    path = harness / "references/source_ready.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    flow_path = harness / "flow.yaml"
    flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
    flow["milestones"][0]["gem"] = "references/source_ready.md"
    flow_path.write_text(yaml.safe_dump(flow), encoding="utf-8")
    return path


def read_request(run, action):
    return json.loads((run / action["model_request_path"]).read_text(encoding="utf-8"))


def test_prompt_writer_preserves_complete_authored_prompt(tmp_path):
    paths = write_milestone_gems(tmp_path, [{"id": "source_ready", "master_prompt": PROMPT}])
    assert len(paths) == 1
    generated = Path(paths[0]).read_text(encoding="utf-8")
    assert generated.startswith(PROMPT)
    assert read_master_prompt(Path(paths[0])) == generated.strip()
    # Existing domain text is preserved on ordinary regeneration.
    assert write_milestone_gems(tmp_path, [{"id": "source_ready", "master_prompt": "replacement"}]) == []
    assert Path(paths[0]).read_text(encoding="utf-8") == generated


def test_writer_projects_bindings_and_outputs_without_replacing_domain_prompt(tmp_path):
    spec = {
        "id": "milestone03", "master_prompt": PROMPT, "output_contract": "series_v1",
        "flowsteps": [
            {"id": "read_inputs", "tool": "input_adapter"},
            {"id": "generate_images", "tool": "image_adapter"},
        ],
        "execution": {"tool_bindings": [
            {"tool": "read_inputs", "ref": "input_adapter@1.0.0"},
            {"tool": "generate_images", "ref": "image_adapter@2.0.0"},
        ]},
        "observer": {"title": "生成六張成品", "actions": [
            {"flowstep_id": "read_inputs", "title": "Read the original inputs", "summary": "Keep the original master image."},
            {"flowstep_id": "generate_images", "title": "Generate six images", "summary": "Execute img1 through img6 sequentially."},
        ]},
        "outputs": [
            {"id": "images", "name": "Ordered final images", "kind": "image", "cardinality": "many", "required": True},
            {"id": "caption", "name": "Caption", "kind": "json", "cardinality": "one", "required": False},
        ],
    }
    [path] = write_milestone_gems(tmp_path, [spec])
    path = Path(path)
    first = path.read_text(encoding="utf-8")
    assert first.startswith(PROMPT)
    assert first.count(PROMPT) == 1
    assert "input_adapter@1.0.0" in read_gem_section(first, "read_inputs")
    assert "image_adapter@2.0.0" in read_gem_section(first, "generate_images")
    assert "Keep the original master image." in read_gem_section(first, "read_inputs")
    assert first.index("FlowStep 1:") < first.index("FlowStep 2:") < first.index("## Named outputs")
    for port in spec["outputs"]:
        assert f"`{port['id']}`: {port['name']}" in first
        assert f"milestone03.{port['id']}" in first
    # Ordinary regeneration updates only the projected outline, retaining notes
    # both before and after it, even when another master_prompt is supplied.
    suffix = "\n## Author note\nDo not move the car.\n"
    path.write_text(first + suffix, encoding="utf-8")
    spec["master_prompt"] = "Do not overwrite the authored prompt."
    spec["execution"]["tool_bindings"][1]["ref"] = "image_adapter@3.0.0"
    write_milestone_gems(tmp_path, [spec])
    updated = path.read_text(encoding="utf-8")
    assert updated.startswith(PROMPT)
    assert updated.endswith(suffix)
    assert "image_adapter@2.0.0" not in updated
    assert "image_adapter@3.0.0" in read_gem_section(updated, "generate_images")
    assert updated.count(OUTLINE_START) == updated.count(OUTLINE_END) == 1
    assert write_milestone_gems(tmp_path, [spec]) == []


def test_deterministic_handler_receives_master_prompt_at_entry(tmp_path):
    _, harness = _scaffold(tmp_path)
    add_prompt(harness)
    (harness / "source.py").write_text(
        "def run(input_data, task=None, **kwargs):\n"
        "    prompt = task['master_prompt']\n"
        "    return {'outputs': {'result': {'prompt': prompt, 'source': task['master_prompt_path']}}}\n",
        encoding="utf-8",
    )
    run = tmp_path / "run"
    action = advance(harness, run, request_path=_request(tmp_path / "request.json"))
    assert action["state"] == "COMPLETE", action
    assert resolve_chosen_output(run, "source_ready", output_id="result") == {
        "prompt": PROMPT, "source": "references/source_ready.md",
    }


@pytest.mark.parametrize("failure", [False, True])
def test_full_prompt_leads_normal_and_recovery_requests_without_flowstep_sections(tmp_path, failure):
    _, harness = _scaffold(tmp_path, model=True)
    prompt_path = add_prompt(harness)
    if failure:
        (harness / "source.py").write_text(
            "def run(input_data, **kwargs):\n    raise RuntimeError('provider temporarily unavailable')\n",
            encoding="utf-8",
        )
    run = tmp_path / "run"
    action = advance(harness, run, request_path=_request(tmp_path / "request.json"))
    assert action["state"] == "ACTION_REQUIRED", action
    request = read_request(run, action)
    assert request["instruction"].startswith(PROMPT)
    assert request["instruction"].count(PROMPT) == 1
    assert request["gem_path"] == "references/source_ready.md"
    task = json.loads((run / action["task_path"]).read_text(encoding="utf-8"))
    assert task["master_prompt"] == PROMPT
    capsule = json.loads((run / action["context_capsule_path"]).read_text(encoding="utf-8"))
    assert str(prompt_path) in capsule["allowed_files"]
    before = (run / action["model_request_path"]).read_bytes()
    resumed = advance(harness, run)
    assert resumed["state"] == "ACTION_REQUIRED", resumed
    assert (run / resumed["model_request_path"]).read_bytes() == before


def test_same_master_prompt_leads_each_model_continuation(tmp_path):
    harness = _two_phase_scaffold(tmp_path)
    add_prompt(harness)
    run = tmp_path / "run"
    first = advance(harness, run, request_path=_request(tmp_path / "request.json"))
    assert first["state"] == "ACTION_REQUIRED", first
    assert read_request(run, first)["instruction"].startswith(PROMPT)
    draft = run / first["draft_path"]
    draft.write_text(json.dumps({"value": "first"}), encoding="utf-8")
    second = advance(harness, run, draft_path=draft)
    assert second["state"] == "ACTION_REQUIRED", second
    instruction = read_request(run, second)["instruction"]
    assert instruction.startswith(PROMPT)
    assert instruction.count(PROMPT) == 1
    assert "phase b" in instruction
    assert "phase a" not in instruction
