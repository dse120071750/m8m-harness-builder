"""Scaffold a skill as a v2 FlowStep tree: flow YAML plus one tool package per step."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from flowstep_instruction import write_instruction
from flowstep_runtime import (
    FLOW_ID_RE,
    STEP_ID_RE,
    FlowError,
    assert_product_harness_location,
    find_flow_path,
    load_flow,
    resolve_harness_dir,
    step_class_hint,
)
from flowstep_tools import tools_root


BUILDER_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = BUILDER_ROOT / "templates"
DEFAULT_BUILDER = BUILDER_ROOT


def _render(template_name: str, mapping: dict[str, str]) -> str:
    text = (TEMPLATE_DIR / template_name).read_text(encoding="utf-8")
    for key, value in mapping.items():
        text = text.replace(f"__{key}__", value)
    return text


def _write_text(path: Path, content: str, *, overwrite: bool) -> bool:
    if path.exists() and not overwrite:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return True


def _step_yaml(step_id: str) -> dict[str, Any]:
    return {
        "id": step_id,
        "kind": "step",
        "class": "tool",
        "handler": f"steps/{step_id}/tool.py",
        "model": "none",
        "inputs": {"request": "user.request"} if step_id else {},
        "output_contract": f"{step_id}_v1",
        "input_schema": f"steps/{step_id}/input.schema.json",
        "output_schema": f"steps/{step_id}/output.schema.json",
        "params": {"step_budget_seconds": 300},
    }


def _dump_flow(flow: dict[str, Any]) -> str:
    lines = [
        f"schema: {flow['schema']}",
        f"flow_id: {flow['flow_id']}",
        f"version: {flow['version']}",
        f"max_run_seconds: {flow.get('max_run_seconds', 3600)}",
        f"artifact_root: {flow.get('artifact_root', 'artifacts')}",
        "steps:",
    ]
    for step in flow["steps"]:
        lines.append(f"  - id: {step['id']}")
        lines.append(f"    kind: {step.get('kind', 'step')}")
        lines.append(f"    class: {step.get('class', 'tool')}")
        lines.append(f"    handler: {step['handler']}")
        lines.append(f"    model: {step.get('model', 'none')}")
        if step.get("model", "none") != "none":
            lines.append(f"    model_justification: {json.dumps(step.get('model_justification') or '', ensure_ascii=False)}")
            lines.append(f"    draft_schema: {step.get('draft_schema', f'steps/{step['id']}/draft.schema.json')}")
        lines.append("    inputs:")
        for name, reference in step["inputs"].items():
            lines.append(f"      {name}: {reference}")
        lines.append(f"    output_contract: {step['output_contract']}")
        lines.append(f"    input_schema: {step['input_schema']}")
        lines.append(f"    output_schema: {step['output_schema']}")
        budget = (step.get("params") or {}).get("step_budget_seconds", 300)
        lines.append("    params:")
        lines.append(f"      step_budget_seconds: {budget}")
    lines.append("")
    return "\n".join(lines)


def _chain_inputs(steps: list[dict[str, Any]]) -> None:
    previous = None
    for step in steps:
        if previous is None:
            step["inputs"] = {"request": "user.request"}
        else:
            step["inputs"] = {previous["id"]: f"{previous['id']}.{previous['output_contract']}"}
        previous = step


def _input_schema(step_id: str, previous_id: str | None) -> str:
    if previous_id is None:
        return _render("step/input.schema.json", {"STEP_ID": step_id, "OUTPUT_CONTRACT": f"{step_id}_v1"})
    return json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{step_id}.input.schema.json",
            "type": "object",
            "additionalProperties": False,
            "required": [previous_id],
            "properties": {
                previous_id: {"$ref": f"../{previous_id}/output.schema.json"},
            },
        },
        indent=2,
    ) + "\n"


def _write_step_package(
    skill_dir: Path,
    step_id: str,
    *,
    previous_id: str | None,
    overwrite: bool,
) -> list[str]:
    written: list[str] = []
    mapping = {"STEP_ID": step_id, "OUTPUT_CONTRACT": f"{step_id}_v1"}
    targets = {
        skill_dir / "steps" / step_id / "tool.py": _render("step/tool.py", mapping),
        skill_dir / "steps" / step_id / "input.schema.json": _input_schema(step_id, previous_id),
        skill_dir / "steps" / step_id / "output.schema.json": _render("step/output.schema.json", mapping),
        skill_dir / "steps" / step_id / "tests" / "test_tool.py": _render("step/test_tool.py", mapping),
    }
    for path, content in targets.items():
        if _write_text(path, content, overwrite=overwrite):
            written.append(str(path))
    return written


def generate_tool(codebase: Path, tool_id: str, *, overwrite: bool = False) -> dict[str, Any]:
    if not STEP_ID_RE.match(tool_id):
        raise FlowError(f"invalid tool id: {tool_id}")
    root = Path(codebase).resolve()
    if root.as_posix().lower().find("/.codex/skills") >= 0 or "\\.codex\\skills" in str(root).lower():
        raise FlowError("--codebase must be the repo root, not .codex/skills")
    dest = tools_root(root) / tool_id
    dest.mkdir(parents=True, exist_ok=True)
    written = _write_step_package(dest, tool_id, previous_id=None, overwrite=overwrite)
    return {
        "schema": "flowstep_tool_generate_v3",
        "status": "PASS",
        "tool_id": tool_id,
        "tool_dir": str(dest),
        "written": written,
    }


def generate_v3_flow(
    codebase: Path,
    flow_id: str,
    milestones: list[str],
    *,
    tools: list[str],
    intelligence: list[str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    harness = resolve_harness_dir(codebase=codebase, flow_id=flow_id)
    assert_product_harness_location(harness)
    if not milestones:
        raise FlowError("pass at least one --milestone")
    if not tools:
        raise FlowError("a milestone flow requires --tools (the pre-made toolbox)")
    intel = set(intelligence or [])
    unknown = sorted(intel - set(milestones))
    if unknown:
        raise FlowError(f"--intelligence names unknown milestones: {unknown}")
    for mid in milestones:
        if not STEP_ID_RE.match(mid):
            raise FlowError(f"invalid milestone id: {mid}")
        if step_class_hint(mid) == "tool":
            raise FlowError(f"{mid}: use --tool for crop/fetch/hash; milestones are checkpoints")
    items = []
    for mid in milestones:
        item: dict[str, Any] = {
            "id": mid,
            "output_contract": f"{mid}_v1",
            "output_schema": f"schemas/{mid}_v1.json",
            "tools": list(tools),
            "intelligence": "completion" if mid in intel else "none",
            "handler": f"milestones/{mid}/assemble.py",
        }
        if mid in intel:
            item["model_justification"] = "judgment that is not a typed transform"
            item["draft_schema"] = f"milestones/{mid}/draft.schema.json"
        items.append(item)
    flow = {
        "schema": "flowstep_flow_v3",
        "flow_id": flow_id,
        "version": 1,
        "max_run_seconds": 3600,
        "artifact_root": "artifacts",
        "milestones": items,
    }
    created: list[str] = []
    flow_path = harness / "flow.yaml"
    if _write_text(flow_path, yaml_dump_v3(flow), overwrite=overwrite or not flow_path.exists()):
        created.append(str(flow_path))
    for item in items:
        mid = item["id"]
        schema_path = harness / "schemas" / f"{mid}_v1.json"
        if _write_text(schema_path, _render("step/output.schema.json", {"STEP_ID": mid}), overwrite=overwrite):
            created.append(str(schema_path))
        assemble = harness / "milestones" / mid / "assemble.py"
        if _write_text(assemble, _render("milestone/assemble.py", {"STEP_ID": mid}), overwrite=overwrite):
            created.append(str(assemble))
        if item["intelligence"] != "none":
            draft = harness / "milestones" / mid / "draft.schema.json"
            if _write_text(draft, _render("step/draft.schema.json", {"STEP_ID": mid}), overwrite=overwrite):
                created.append(str(draft))
    loaded = load_flow(harness, flow_path)
    instruction = write_instruction(harness, loaded)
    created.append(str(instruction))
    return {
        "schema": "flowstep_harness_generate_v3",
        "status": "PASS",
        "harness_dir": str(harness),
        "codebase": str(Path(codebase).resolve()),
        "flow_id": flow_id,
        "milestones": milestones,
        "tools": tools,
        "instruction_path": str(instruction),
        "written": created,
    }


def yaml_dump_v3(flow: dict[str, Any]) -> str:
    lines = [
        f"schema: {flow['schema']}",
        f"flow_id: {flow['flow_id']}",
        f"version: {flow['version']}",
        f"max_run_seconds: {flow['max_run_seconds']}",
        f"artifact_root: {flow['artifact_root']}",
        "milestones:",
    ]
    for item in flow["milestones"]:
        lines.append(f"  - id: {item['id']}")
        lines.append(f"    output_contract: {item['output_contract']}")
        lines.append(f"    output_schema: {item['output_schema']}")
        lines.append(f"    tools: [{', '.join(item['tools'])}]")
        lines.append(f"    intelligence: {item['intelligence']}")
        if item["intelligence"] != "none":
            lines.append(f"    model_justification: {json.dumps(item.get('model_justification') or '', ensure_ascii=False)}")
            lines.append(f"    draft_schema: {item['draft_schema']}")
        lines.append(f"    handler: {item['handler']}")
    lines.append("")
    return "\n".join(lines)


def generate_harness(
    skill_dir: Path | None = None,
    *,
    codebase: Path | None = None,
    flow_id: str | None,
    step_ids: list[str],
    skill_name: str | None = None,
    overwrite: bool = False,
    write_skill_md: bool = False,
    intelligence: list[str] | None = None,
) -> dict[str, Any]:
    skill_dir = resolve_harness_dir(codebase=codebase, flow_id=flow_id, skill_dir=skill_dir)
    assert_product_harness_location(skill_dir)
    skill_dir.mkdir(parents=True, exist_ok=True)
    name = skill_name or skill_dir.name
    created: list[str] = []
    flows_dir = skill_dir / "flows"
    existing_flow: Path | None = None
    if flows_dir.is_dir() and list(flows_dir.glob("*.yaml")) + list(flows_dir.glob("*.yml")):
        try:
            existing_flow = find_flow_path(skill_dir)
        except FlowError:
            existing_flow = None

    if existing_flow and not step_ids:
        flow = load_flow(skill_dir, existing_flow)
    else:
        if not flow_id:
            raise FlowError("--flow-id is required when creating a flow")
        if not FLOW_ID_RE.match(flow_id):
            raise FlowError(f"invalid flow_id: {flow_id}")
        if not step_ids:
            raise FlowError("pass at least one --step")
        for step_id in step_ids:
            if not STEP_ID_RE.match(step_id):
                raise FlowError(f"invalid step id: {step_id}")
        if len(step_ids) != len(set(step_ids)):
            raise FlowError("duplicate --step values")
        steps = [_step_yaml(step_id) for step_id in step_ids]
        _chain_inputs(steps)
        intelligence_ids = set(intelligence or [])
        unknown = sorted(intelligence_ids - set(step_ids))
        if unknown:
            raise FlowError(f"--intelligence names unknown steps: {unknown}")
        for step in steps:
            if step["id"] in intelligence_ids:
                step["class"] = "intelligence"
                step["model"] = "completion"
                step["model_justification"] = "judgment that is not a typed transform"
                step["draft_schema"] = f"steps/{step['id']}/draft.schema.json"
        flow = {
            "schema": "flowstep_flow_v2",
            "flow_id": flow_id,
            "version": 1,
            "max_run_seconds": 3600,
            "artifact_root": "artifacts",
            "steps": steps,
        }
        flow_path = skill_dir / "flows" / f"{flow_id}.yaml"
        if _write_text(flow_path, _dump_flow(flow), overwrite=overwrite or not flow_path.exists()):
            created.append(str(flow_path))
        flow["_flow_path"] = flow_path

    previous_id = None
    for step in flow["steps"]:
        written = _write_step_package(
            skill_dir,
            step["id"],
            previous_id=previous_id,
            overwrite=overwrite,
        )
        if step.get("class") == "intelligence":
            draft = skill_dir / "steps" / step["id"] / "draft.schema.json"
            if _write_text(draft, _render("step/draft.schema.json", {"STEP_ID": step["id"]}), overwrite=overwrite):
                written.append(str(draft))
        created.extend(written)
        previous_id = step["id"]

    mapping = {
        "SKILL_NAME": name,
        "FLOW_ID": str(flow["flow_id"]),
        "BUILDER_ROOT": str(DEFAULT_BUILDER),
    }
    if write_skill_md:
        skill_md = skill_dir / "SKILL.md"
        if _write_text(skill_md, _render("SKILL.md", mapping), overwrite=overwrite or not skill_md.exists()):
            created.append(str(skill_md))
    run_py = skill_dir / "scripts" / "run.py"
    if _write_text(run_py, _render("run.py", mapping), overwrite=overwrite or not run_py.exists()):
        created.append(str(run_py))

    flow_for_table = load_flow(skill_dir, flow.get("_flow_path") or find_flow_path(skill_dir))
    instruction = write_instruction(skill_dir, flow_for_table)
    created.append(str(instruction))

    return {
        "schema": "flowstep_harness_generate_v2",
        "status": "PASS",
        "skill_dir": str(skill_dir),
        "harness_dir": str(skill_dir),
        "codebase": str(Path(codebase).resolve()) if codebase else None,
        "flow_id": flow_for_table["flow_id"],
        "steps": [step["id"] for step in flow_for_table["steps"]],
        "instruction_path": str(instruction),
        "written": created,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codebase", type=Path, help="Repo root. Writes <codebase>/flowsteps/<flow_id>.")
    parser.add_argument("--skill-dir", type=Path, help="Harness dir for the builder fixture only.")
    parser.add_argument("--flow-id")
    parser.add_argument("--tool", dest="tool_id", help="Generate one toolbox function under flowsteps/tools/.")
    parser.add_argument("--step", action="append", default=[], dest="steps")
    parser.add_argument("--milestone", action="append", default=[], dest="milestones")
    parser.add_argument("--tools", help="Comma-separated toolbox ids for every milestone.")
    parser.add_argument("--intelligence", action="append", default=[], help="Milestone or step ids that may NEED_MODEL.")
    parser.add_argument("--skill-name")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--write-skill-md",
        action="store_true",
        help="Write product SKILL.md. Use only after validate_harness PASS.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.tool_id:
            if not args.codebase:
                raise FlowError("--tool requires --codebase")
            result = generate_tool(args.codebase, args.tool_id, overwrite=args.force)
        elif args.milestones:
            if not args.codebase or not args.flow_id:
                raise FlowError("--milestone requires --codebase and --flow-id")
            tool_ids = [part.strip() for part in (args.tools or "").split(",") if part.strip()]
            result = generate_v3_flow(
                args.codebase,
                args.flow_id,
                args.milestones,
                tools=tool_ids,
                intelligence=args.intelligence,
                overwrite=args.force,
            )
        else:
            result = generate_harness(
                args.skill_dir,
                codebase=args.codebase,
                flow_id=args.flow_id,
                step_ids=args.steps,
                skill_name=args.skill_name,
                overwrite=args.force,
                write_skill_md=args.write_skill_md,
                intelligence=args.intelligence,
            )
    except FlowError as exc:
        print(json.dumps({"status": "BLOCKED", "blockers": [str(exc)]}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
