"""Generate a v4 milestone flow with named admitted, optionally judged outputs."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

from flowstep_instruction import write_instruction
from flowstep_runtime import (
    FLOW_ID_RE,
    STEP_ID_RE,
    FlowError,
    assert_product_harness_location,
    find_flow_path,
    harness_output_schema,
    is_under_home_skills,
    local_tool_package_name,
    load_flow,
    normalize_flowsteps,
    resolve_harness_dir,
    runtime_package_name,
    step_class_hint,
)
from flowstep_tools import tools_root, validate_library_tool
from m8m_flowchart import flowchart_path
from teaching_contracts import copy_teaching_contracts, write_milestone_gems
from toolbox_plan import build_toolbox_plan, existing_toolbox_ids
from tool_vs_intelligence import from_audit as classification_from_audit
from tool_vs_intelligence import from_flow as classification_from_flow
from tool_vs_intelligence import render_markdown as render_classification_markdown


BUILDER_ROOT = Path(os.path.abspath(str(Path(__file__).parent.parent)))
TEMPLATE_DIR = BUILDER_ROOT / "templates"
DEFAULT_BUILDER = BUILDER_ROOT
SEEDS_DIR = BUILDER_ROOT / "seeds"
BUILD_REQUIRED_MARKER = "BUILD_REQUIRED"


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


def _write_package(
    dest: Path,
    step_id: str,
    *,
    previous_id: str | None,
    overwrite: bool,
) -> list[str]:
    written: list[str] = []
    mapping = {"STEP_ID": step_id, "OUTPUT_CONTRACT": f"{step_id}_v1"}
    targets = {
        dest / "tool.py": _render("step/tool.py", mapping),
        dest / "input.schema.json": _input_schema(step_id, previous_id),
        dest / "output.schema.json": _render("step/output.schema.json", mapping),
        dest / "tests" / "test_tool.py": _render("step/test_tool.py", mapping),
    }
    for path, content in targets.items():
        if _write_text(path, content, overwrite=overwrite):
            written.append(str(path))
    return written


def _write_step_package(
    skill_dir: Path,
    step_id: str,
    *,
    previous_id: str | None,
    overwrite: bool,
) -> list[str]:
    return _write_package(
        skill_dir / "steps" / step_id,
        step_id,
        previous_id=previous_id,
        overwrite=overwrite,
    )


def _write_judge_package(dest: Path, tool_id: str, *, overwrite: bool) -> list[str]:
    written: list[str] = []
    mapping = {"STEP_ID": tool_id}
    targets = {
        dest / "tool.py": _render("milestone/judge_tool.py", mapping),
        dest / "input.schema.json": _render("milestone/judge_input.schema.json", mapping),
        dest / "output.schema.json": _render("milestone/judge_output.schema.json", mapping),
        dest / "tests" / "test_tool.py": _render("milestone/judge_test_tool.py", mapping),
    }
    for path, content in targets.items():
        if _write_text(path, content, overwrite=overwrite):
            written.append(str(path))
    return written


def seed_path(tool_id: str) -> Path | None:
    path = SEEDS_DIR / tool_id
    if (path / "tool.py").is_file():
        return path
    return None


def _copy_seed(codebase: Path, tool_id: str, *, overwrite: bool) -> list[str]:
    src = seed_path(tool_id)
    if src is None:
        raise FlowError(f"no seed for {tool_id}")
    dest = tools_root(codebase) / tool_id
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for path in src.rglob("*"):
        if path.is_dir() or path.name == "__pycache__" or path.suffix == ".pyc":
            continue
        target = dest / path.relative_to(src)
        if target.exists() and not overwrite:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        written.append(str(target))
    return written


def generate_tool(codebase: Path, tool_id: str, *, overwrite: bool = False) -> dict[str, Any]:
    if not STEP_ID_RE.match(tool_id):
        raise FlowError(f"invalid tool id: {tool_id}")
    root = Path(os.path.abspath(str(codebase)))
    if is_under_home_skills(root):
        raise FlowError("--codebase must be the repo root, not ~/.codex/skills or ~/.claude/skills")
    dest = tools_root(root) / tool_id
    had_implementation = dest.is_dir() and any(dest.iterdir())
    dest.mkdir(parents=True, exist_ok=True)
    if seed_path(tool_id) is not None:
        written = _copy_seed(root, tool_id, overwrite=overwrite)
        return {
            "schema": "flowstep_tool_generate_v3",
            "status": "PASS",
            "tool_id": tool_id,
            "tool_dir": str(dest),
            "seeded": True,
            "origin": "existing",
            "runnable": True,
            "non_runnable": False,
            "written": written,
        }
    marker = dest / BUILD_REQUIRED_MARKER
    if had_implementation and not marker.is_file():
        try:
            existing_blockers = validate_library_tool(root, tool_id)
        except Exception as exc:  # noqa: BLE001 - report malformed local work as a build blocker
            existing_blockers = [f"{tool_id}: tool validation failed: {exc}"]
        if not existing_blockers:
            return {
                "schema": "flowstep_tool_generate_v3",
                "status": "PASS",
                "tool_id": tool_id,
                "tool_dir": str(dest),
                "seeded": False,
                "origin": "local-implementation",
                "runnable": True,
                "non_runnable": False,
                "blockers": [],
                "note": "preserved an existing validated local implementation",
                "written": [],
            }
    marker_written: list[str] = []
    if overwrite or not had_implementation:
        if _write_text(
            marker,
            "BUILD_REQUIRED: implement and test this generated tool, then remove this marker.\n",
            overwrite=True,
        ):
            marker_written.append(str(marker))
    if tool_id.endswith("_judge"):
        written = _write_judge_package(dest, tool_id, overwrite=overwrite)
        note = "generated judge scaffold; implement its milestone-specific approval rule"
    else:
        written = _write_package(dest, tool_id, previous_id=None, overwrite=overwrite)
        note = "generated tool scaffold; implement its public contract and tests"
    written.extend(marker_written)
    blockers: list[str] = []
    if marker.is_file():
        blockers.append(f"{tool_id}: {BUILD_REQUIRED_MARKER} marker is present")
    try:
        blockers.extend(validate_library_tool(root, tool_id))
    except Exception as exc:  # noqa: BLE001 - malformed scaffolds are build blockers
        blockers.append(f"{tool_id}: tool validation failed: {exc}")
    runnable = not blockers
    return {
        "schema": "flowstep_tool_generate_v3",
        "status": "PASS" if runnable else "BUILD_REQUIRED",
        "tool_id": tool_id,
        "tool_dir": str(dest),
        "seeded": False,
        "origin": "local-implementation" if runnable else "generate-new",
        "runnable": runnable,
        "non_runnable": not runnable,
        "blockers": blockers,
        "note": note,
        "written": written,
    }


PASSTHROUGH_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
}


def _output_declarations(spec: dict[str, Any], *, milestone_id: str, kind: str) -> list[dict[str, Any]]:
    declared = spec.get("outputs")
    if isinstance(declared, list) and declared:
        outputs = []
        for item in declared:
            if not isinstance(item, dict):
                raise FlowError(f"{milestone_id}.outputs must contain mappings")
            outputs.append(
                {
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("name") or ""),
                    "kind": str(item.get("kind") or kind),
                    "cardinality": str(item.get("cardinality") or "one"),
                    "required": bool(item.get("required", True)),
                }
            )
        return outputs
    raise FlowError(
        f"{milestone_id}: named outputs are BUILD_REQUIRED; the builder will not invent a result port"
    )


def _milestone_authoring_gaps(
    milestones: list[str],
    milestone_specs: list[dict[str, Any]] | None,
) -> dict[str, list[str]]:
    """Find expectation authority that cannot be inferred by a generator."""

    by_id = {
        str(item.get("id") or ""): item
        for item in (milestone_specs or [])
        if isinstance(item, dict)
    }
    gaps: dict[str, list[str]] = {}
    for milestone_id in milestones:
        spec = by_id.get(milestone_id)
        missing: list[str] = []
        if spec is None:
            missing.extend(
                [
                    "success",
                    "output_contract",
                    "output_schema",
                    "outputs",
                    "execution.candidate_executor",
                ]
            )
        else:
            if not str(spec.get("success") or "").strip():
                missing.append("success")
            if not str(spec.get("output_contract") or "").strip():
                missing.append("output_contract")
            if not isinstance(spec.get("output_schema_object"), dict):
                missing.append("output_schema")
            if not isinstance(spec.get("outputs"), list) or not spec.get("outputs"):
                missing.append("outputs")
            execution = spec.get("execution")
            candidate_executor = (
                execution.get("candidate_executor")
                if isinstance(execution, dict)
                else None
            )
            if not isinstance(candidate_executor, dict) or not str(
                candidate_executor.get("ref") or ""
            ).strip():
                missing.append("execution.candidate_executor")
            flowsteps = [
                item
                for item in spec.get("flowsteps") or []
                if isinstance(item, dict)
            ]
            bindings = (
                execution.get("tool_bindings")
                if isinstance(execution, dict)
                else None
            )
            binding_map = {
                str(item.get("tool") or ""): str(item.get("ref") or "")
                for item in bindings or []
                if isinstance(item, dict)
            }
            expected_ids = [str(item.get("id") or "") for item in flowsteps]
            if list(binding_map) != expected_ids or any(
                binding_map.get(str(item.get("id") or ""))
                != str(item.get("tool") or "")
                for item in flowsteps
            ):
                missing.append("execution.tool_bindings")
            else:
                try:
                    for item in flowsteps:
                        local_tool_package_name(
                            str(item.get("tool") or ""),
                            label=f"{milestone_id}.{item.get('id')}.tool",
                        )
                except FlowError:
                    missing.append("versioned_flowstep_tools")
            control = (
                spec.get("branch")
                if isinstance(spec.get("branch"), dict)
                else spec.get("cycle")
                if isinstance(spec.get("cycle"), dict)
                else None
            )
            if isinstance(control, dict):
                control_worker = str(control.get("worker") or "").strip()
                if (
                    not control_worker
                    or control_worker not in expected_ids
                    or not binding_map.get(control_worker)
                ):
                    missing.append("execution.control_worker_binding")
            if str(spec.get("loop") or "none") == "judge":
                judge = execution.get("judge") if isinstance(execution, dict) else None
                if not isinstance(judge, dict) or not str(judge.get("ref") or "").strip():
                    missing.append("execution.judge")
                for field in ("worker", "judge_abi", "receipt_schema", "max_attempts"):
                    if not spec.get(field):
                        missing.append(field)
            if spec.get("_judge_inferred"):
                missing.append("authored_judge_authority")
            if spec.get("_expectation_authored") is False:
                missing.append("authored_expectation_authority")
        if missing:
            gaps[milestone_id] = list(dict.fromkeys(missing))
    return gaps


def _candidate_output_schema(
    payload_schema: dict[str, Any],
    *,
    milestone_id: str,
    outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in payload_schema.items()
        if key not in {"$schema", "$id"}
    }
    properties: dict[str, Any] = {}
    required: list[str] = []
    for index, output in enumerate(outputs):
        output_id = str(output["id"])
        if output.get("required"):
            required.append(output_id)
        if output.get("cardinality") == "many":
            properties[output_id] = {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "required": ["id", "name"],
                    "properties": {
                        "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                        "name": {"type": "string", "minLength": 1},
                    },
                },
            }
        elif index == 0:
            properties[output_id] = payload
        else:
            properties[output_id] = {}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{milestone_id}.output.schema.json",
        "type": "object",
        "additionalProperties": False,
        "required": ["outputs"],
        "properties": {
            "outputs": {
                "type": "object",
                "additionalProperties": False,
                "required": required,
                "properties": properties,
            }
        },
    }


def _write_json(path: Path, value: Any, *, overwrite: bool) -> bool:
    return _write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n", overwrite=overwrite)


def generate_v4_flow(
    codebase: Path,
    flow_id: str,
    milestones: list[str],
    *,
    tools: list[str],
    intelligence: list[str] | None = None,
    overwrite: bool = False,
    milestone_specs: list[dict[str, Any]] | None = None,
    toolbox_plan: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    harness = resolve_harness_dir(codebase=codebase, flow_id=flow_id)
    assert_product_harness_location(harness)
    prior_toolbox = existing_toolbox_ids(Path(codebase))
    if milestone_specs:
        milestones = [str(item["id"]) for item in milestone_specs]
    if not milestones:
        raise FlowError("pass at least one --milestone")
    intel = set(intelligence or [])
    unknown = sorted(intel - set(milestones))
    if unknown:
        raise FlowError(f"--intelligence names unknown milestones: {unknown}")
    authoring_gaps = _milestone_authoring_gaps(milestones, milestone_specs)
    if authoring_gaps:
        notes = [
            f"{milestone_id}: BUILD_REQUIRED missing " + ", ".join(fields)
            for milestone_id, fields in authoring_gaps.items()
        ]
        notes.append(
            "No canonical flow.yaml was emitted because success, contract, schema, and named outputs are authored authority."
        )
        return {
            "schema": "flowstep_harness_generate_v4",
            "status": "BUILD_REQUIRED",
            "runnable": False,
            "non_runnable": True,
            "build_required_tools": [],
            "build_required_milestones": authoring_gaps,
            "harness_dir": str(harness),
            "codebase": str(Path(os.path.abspath(str(codebase)))),
            "flow_id": flow_id,
            "milestones": milestones,
            "tools": sorted(set(tools or [])),
            "written": [],
            "notes": notes,
        }
    notes: list[str] = []
    tool_generation: dict[str, dict[str, Any]] = {}
    for mid in milestones:
        if not STEP_ID_RE.match(mid):
            raise FlowError(f"invalid milestone id: {mid}")
        if any(mid.startswith(prefix) for prefix in ("if_", "loop_", "switch_", "when_", "else_")):
            notes.append(f"{mid}: name looks like control (if/loop); still drawn as a checkpoint")
        if step_class_hint(mid) == "tool":
            notes.append(f"{mid}: name looks like a tool; still drawn — consider it a FlowStep under a checkpoint")
    listed_tools: set[str] = set(tools or [])
    for spec in milestone_specs or []:
        execution = spec.get("execution") if isinstance(spec.get("execution"), dict) else {}
        for binding in execution.get("tool_bindings") or []:
            if not isinstance(binding, dict) or not binding.get("ref"):
                continue
            try:
                listed_tools.add(
                    local_tool_package_name(
                        str(binding["ref"]),
                        label=(
                            f"{spec.get('id')}.execution.tool_bindings"
                            f"[{binding.get('tool')}].ref"
                        ),
                    )
                )
            except FlowError:
                continue
        if spec.get("worker"):
            worker = str(spec["worker"])
            listed_tools.add(
                runtime_package_name(worker, label=f"{spec.get('id')}.worker")
                if spec.get("judge_abi")
                else worker
            )
        if spec.get("branch"):
            listed_tools.add(str((spec.get("branch") or {}).get("worker") or spec.get("worker") or "branch_receipt"))
        if spec.get("cycle"):
            listed_tools.add(str((spec.get("cycle") or {}).get("worker") or spec.get("worker") or "cycle_receipt"))
    for tool_id in sorted(listed_tools):
        tool_result = generate_tool(codebase, tool_id, overwrite=overwrite)
        tool_generation[tool_id] = tool_result
        if tool_result.get("status") != "PASS":
            notes.append(f"{tool_id}: BUILD_REQUIRED non-runnable scaffold")
    spec_by_id = {str(item["id"]): item for item in (milestone_specs or [])}
    items = []
    previous = None
    for index, mid in enumerate(milestones):
        spec = spec_by_id.get(mid) or {}
        flowsteps, step_tools = normalize_flowsteps(
            flowsteps=spec.get("flowsteps"),
            tools=spec.get("tools") or tools,
        )
        is_last = index == len(milestones) - 1
        declared_outputs = spec.get("outputs") if isinstance(spec.get("outputs"), list) else []
        declared_kind = ""
        if declared_outputs and isinstance(declared_outputs[0], dict):
            declared_kind = str(declared_outputs[0].get("kind") or "")
        declared_asset = spec.get("asset") if isinstance(spec.get("asset"), dict) else {}
        requested_kind = declared_kind or str(declared_asset.get("kind") or "")
        payload_schema, asset_kind = harness_output_schema(
            spec.get("output_schema_object") if isinstance(spec.get("output_schema_object"), dict) else None,
            step_id=mid,
            kind=requested_kind or None,
        )
        output_declarations = _output_declarations(
            spec,
            milestone_id=mid,
            kind=asset_kind,
        )
        output_obj = _candidate_output_schema(
            payload_schema,
            milestone_id=mid,
            outputs=output_declarations,
        )
        intel_value = spec.get("intelligence") or ("completion" if mid in intel else "none")
        on_tool_fail = spec.get("on_tool_fail") or "need_model"
        item: dict[str, Any] = {
            "id": mid,
            "output_contract": spec["output_contract"],
            "output_schema": f"schemas/{mid}_v1.json",
            "input_schema": f"milestones/{mid}/input.schema.json",
            "flowsteps": flowsteps,
            "tools": step_tools,
            "intelligence": intel_value,
            "on_tool_fail": on_tool_fail,
            "handler": f"milestones/{mid}/assemble.py",
            "test": f"milestones/{mid}/tests/test_assemble.py",
            "outputs": output_declarations,
            "draft_schema": f"milestones/{mid}/draft.schema.json",
            "_output_schema_object": output_obj,
            "_input_schema_object": spec.get("input_schema_object"),
            "_is_last": is_last,
            "_asset_kind": asset_kind,
        }
        if isinstance(spec.get("execution"), dict):
            item["execution"] = json.loads(
                json.dumps(spec["execution"], ensure_ascii=False, allow_nan=False)
            )
        if isinstance(spec.get("cache"), dict):
            item["cache"] = dict(spec["cache"])
        if spec.get("inputs"):
            item["inputs"] = spec["inputs"]
        elif previous is None:
            item["inputs"] = {"request": "user.request"}
        else:
            source = previous
            on_path = str(spec.get("on_path") or "")
            if on_path:
                same_path = [prior for prior in items if str(prior.get("on_path") or "") == on_path]
                if same_path:
                    source = same_path[-1]
                else:
                    branch_origins = [
                        prior
                        for prior in items
                        if any(
                            isinstance(path, dict) and str(path.get("id") or "") == on_path
                            for path in ((prior.get("branch") or {}).get("paths") or [])
                        )
                    ]
                    if branch_origins:
                        source = branch_origins[-1]
            else:
                join_origins = [
                    prior
                    for prior in items
                    if str((prior.get("branch") or {}).get("join") or "") == mid
                ]
                if join_origins:
                    source = join_origins[-1]
            item["inputs"] = {
                source["id"]: f"{source['id']}.{source['output_contract']}"
            }
        if intel_value != "none":
            item["model_justification"] = spec.get("model_justification") or "judgment that is not a typed transform"
            item["draft_schema"] = f"milestones/{mid}/draft.schema.json"
        if spec.get("max_model_attempts"):
            item["max_model_attempts"] = spec["max_model_attempts"]
        loop = str(spec.get("loop") or "none")
        if loop in {"for", "judge"}:
            item["loop"] = loop
            item["worker"] = spec.get("worker") or ("ledger_receipt" if loop == "for" else "")
            item["receipt_schema"] = spec.get("receipt_schema") or f"schemas/{mid}_receipt_v1.json"
            if loop == "judge":
                item["judge_abi"] = spec["judge_abi"]
            if spec.get("max_attempts"):
                item["max_attempts"] = spec["max_attempts"]
        if spec.get("ledger") or spec.get("foreach"):
            ledger = dict(spec.get("ledger") or spec.get("foreach") or {})
            item["_item_schema_object"] = ledger.pop("item_schema_object", None)
            ledger.pop("tools", None)
            ledger.pop("collect", None)
            if loop == "none":
                item["loop"] = "for"
                item["worker"] = item.get("worker") or "ledger_receipt"
                item["receipt_schema"] = item.get("receipt_schema") or f"schemas/{mid}_receipt_v1.json"
            item["ledger"] = {
                "path": ledger.get("path") or "items",
                "item_schema": ledger.get("item_schema") or f"schemas/{mid}_item_v1.json",
                "max_items": ledger.get("max_items") or 8,
            }
        if spec.get("branch"):
            br = dict(spec["branch"]) if isinstance(spec["branch"], dict) else {}
            br.setdefault("worker", spec.get("worker") or "branch_receipt")
            br.setdefault("receipt_schema", spec.get("receipt_schema") or f"schemas/{mid}_branch_v1.json")
            item["branch"] = br
            item["worker"] = br["worker"]
            item["receipt_schema"] = br["receipt_schema"]
            if item["worker"] not in item["tools"]:
                item["tools"].append(item["worker"])
        if spec.get("on_path"):
            item["on_path"] = spec["on_path"]
        if spec.get("cycle"):
            cy = dict(spec["cycle"]) if isinstance(spec["cycle"], dict) else {}
            cy.setdefault("worker", spec.get("worker") or "cycle_receipt")
            cy.setdefault("receipt_schema", spec.get("receipt_schema") or f"schemas/{mid}_cycle_v1.json")
            item["cycle"] = cy
            item["worker"] = cy["worker"]
            item["receipt_schema"] = cy["receipt_schema"]
            if item["worker"] not in item["tools"]:
                item["tools"].append(item["worker"])
        if spec.get("on_cycle"):
            item["on_cycle"] = spec["on_cycle"]
        item["success"] = str(spec["success"]).strip()
        item["gem"] = str(spec.get("gem") or f"references/{mid}.md")
        worker = str(item.get("worker") or "")
        if worker and item.get("judge_abi"):
            if worker in item["tools"]:
                item["tools"].remove(worker)
            item["flowsteps"] = [
                row for row in item["flowsteps"] if str(row.get("tool") or "") != worker
            ]
        elif worker and item["tools"] and worker not in item["tools"]:
            item["tools"].append(worker)
        elif worker in {"hash_bind", "schema_validate"} and not item["tools"]:
            item.pop("worker", None)
        items.append(item)
        previous = item
    for item in items:
        worker = str(item.get("worker") or "")
        if worker:
            package_name = (
                runtime_package_name(worker, label=f"{item['id']}.worker")
                if item.get("judge_abi")
                else worker
            )
            tool_result = generate_tool(codebase, package_name, overwrite=overwrite)
            tool_generation[package_name] = tool_result
            if tool_result.get("status") != "PASS":
                notes.append(f"{worker}: BUILD_REQUIRED non-runnable scaffold")
    flow = {
        "schema": "flowstep_flow_v4",
        "flow_id": flow_id,
        "version": 1,
        "context_policy": "isolated",
        "max_run_seconds": 3600,
        "artifact_root": "artifacts",
        "milestones": items,
    }
    created: list[str] = []
    flow_path = harness / "flow.yaml"
    public_items = []
    for item in items:
        public = {key: value for key, value in item.items() if not key.startswith("_")}
        public_items.append(public)
    flow_public = dict(flow)
    flow_public["milestones"] = public_items
    if _write_text(flow_path, yaml_dump_v4(flow_public), overwrite=overwrite or not flow_path.exists()):
        created.append(str(flow_path))
    unpackaged_marker = harness / "BUILD_REQUIRED_RUNTIME"
    unpackaged_marker.write_text(
        "This piecemeal scaffold has no codebase-owned runtime release. "
        "Run the complete m8m-harness-builder workflow before execution.\n",
        encoding="utf-8",
        newline="\n",
    )
    created.append(str(unpackaged_marker))
    previous_id = None
    for item in items:
        mid = item["id"]
        output_obj = item["_output_schema_object"]
        output_obj.setdefault("$id", f"{mid}.output.schema.json")
        schema_path = harness / "schemas" / f"{mid}_v1.json"
        if _write_json(schema_path, output_obj, overwrite=overwrite):
            created.append(str(schema_path))
        # Bindings may name any upstream output, not just the preceding node.
        # Leave value types to an authored schema; a port can be a scalar or array.
        input_obj = item.get("_input_schema_object") or {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{mid}.input.schema.json",
            "type": "object",
            "additionalProperties": True,
            "required": list(item["inputs"]),
            "properties": {name: {} for name in item["inputs"]},
        }
        input_path = harness / "milestones" / mid / "input.schema.json"
        if _write_json(input_path, input_obj, overwrite=overwrite):
            created.append(str(input_path))
        mapping = {
            "STEP_ID": mid,
            "TOOLS_JSON": json.dumps(item["tools"]),
            "FLOWSTEPS_JSON": json.dumps(item.get("flowsteps") or []),
            "TOOL_BINDINGS_JSON": json.dumps(
                (item.get("execution") or {}).get("tool_bindings") or []
            ),
            "INTELLIGENCE": item["intelligence"],
            "IS_LAST": "True" if item["_is_last"] else "False",
            "ASSET_KIND": item.get("_asset_kind") or "file",
            "OUTPUTS_JSON": json.dumps(item.get("outputs") or []),
            "WORKER": item.get("worker") or "",
            "CONTROL_KIND": (
                "branch"
                if item.get("branch")
                else "cycle"
                if item.get("cycle")
                else ""
            ),
            "LOOP": item.get("loop") or "none",
        }
        assemble = harness / "milestones" / mid / "assemble.py"
        if _write_text(assemble, _render("milestone/assemble.py", mapping), overwrite=overwrite):
            created.append(str(assemble))
        test_path = harness / "milestones" / mid / "tests" / "test_assemble.py"
        if _write_text(test_path, _render("milestone/test_assemble.py", mapping), overwrite=overwrite):
            created.append(str(test_path))
        draft = harness / "milestones" / mid / "draft.schema.json"
        open_draft = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{mid}.draft.schema.json",
            "type": "object",
            "additionalProperties": True,
        }
        if _write_json(draft, open_draft, overwrite=overwrite):
            created.append(str(draft))
        if item.get("ledger") and isinstance(item.get("_item_schema_object"), dict):
            item_schema_path = harness / str(item["ledger"]["item_schema"])
            if _write_json(item_schema_path, item["_item_schema_object"], overwrite=overwrite):
                created.append(str(item_schema_path))
        if item.get("loop") in {"for", "judge"}:
            receipt_path = harness / str(item.get("receipt_schema") or f"schemas/{mid}_receipt_v1.json")
            if item.get("loop") == "judge":
                receipt_obj = {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": f"{mid}.judge-result.schema.json",
                    "type": "object",
                    "additionalProperties": False,
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
            else:
                receipt_obj = {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": f"{mid}.receipt.schema.json",
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["ok"],
                    "properties": {
                        "ok": {"type": "boolean"},
                        "remaining": {"type": "integer", "minimum": 0},
                        "done": {"type": "integer", "minimum": 0},
                        "code": {"type": "string"},
                        "attempt": {"type": "integer", "minimum": 1},
                        "item_id": {"type": "string"},
                    },
                }
            if _write_json(receipt_path, receipt_obj, overwrite=overwrite):
                created.append(str(receipt_path))
        if item.get("branch"):
            spec = item["branch"] if isinstance(item["branch"], dict) else {}
            paths = []
            for raw in spec.get("paths") or []:
                if isinstance(raw, dict) and raw.get("id"):
                    paths.append(str(raw["id"]))
                elif isinstance(raw, str) and raw:
                    paths.append(raw)
            receipt_path = harness / str(item.get("receipt_schema") or spec.get("receipt_schema") or f"schemas/{mid}_branch_v1.json")
            receipt_obj = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"{mid}.branch.schema.json",
                "type": "object",
                "additionalProperties": False,
                "required": ["ok", "branch"],
                "properties": {
                    "ok": {"type": "boolean"},
                    "branch": {"type": "string", "enum": paths or ["direct"]},
                    "skipped": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
            }
            if _write_json(receipt_path, receipt_obj, overwrite=overwrite):
                created.append(str(receipt_path))
        if item.get("cycle"):
            spec = item["cycle"] if isinstance(item["cycle"], dict) else {}
            receipt_path = harness / str(item.get("receipt_schema") or spec.get("receipt_schema") or f"schemas/{mid}_cycle_v1.json")
            receipt_obj = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"{mid}.cycle.schema.json",
                "type": "object",
                "additionalProperties": False,
                "required": ["ok", "cycle"],
                "properties": {
                    "ok": {"type": "boolean"},
                    "cycle": {"enum": ["pass", "fail"]},
                    "row": {"type": "string"},
                    "reason": {"type": "string"},
                },
            }
            if _write_json(receipt_path, receipt_obj, overwrite=overwrite):
                created.append(str(receipt_path))
        previous_id = mid
    gem_items = [
        {**item, "master_prompt": spec_by_id.get(item["id"], {}).get("master_prompt"),
         "observer": spec_by_id.get(item["id"], {}).get("observer") or {}}
        for item in public_items
    ]
    gems = write_milestone_gems(harness, gem_items, overwrite=overwrite)
    created.extend(gems)
    loaded = load_flow(harness, flow_path)
    plan = toolbox_plan or build_toolbox_plan(
        loaded.get("steps") or items,
        existing_ids=prior_toolbox,
    )
    plan_path = harness / "planning" / "toolbox-plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    instruction = write_instruction(harness, loaded, toolbox_plan=plan, source="generate")
    created.append(str(instruction))
    chart = flowchart_path(harness)
    created.append(str(chart))
    jpg = chart.with_suffix(".jpg")
    if jpg.is_file():
        created.append(str(jpg))
    table = classification_from_flow(loaded)
    table_path = harness / "planning" / "tool-vs-intelligence.json"
    table_path.parent.mkdir(parents=True, exist_ok=True)
    table_path.write_text(json.dumps(table, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    created.append(str(table_path))
    build_required_tools = sorted(
        tool_id
        for tool_id, result in tool_generation.items()
        if result.get("status") != "PASS" or result.get("non_runnable")
    )
    build_required_handlers = []
    for item in items:
        handler_path = harness / str(item["handler"])
        source = handler_path.read_text(encoding="utf-8") if handler_path.is_file() else ""
        if (
            'M8M_BUILD_STATUS = "BUILD_REQUIRED"' in source
            or "M8M_BUILD_STATUS = 'BUILD_REQUIRED'" in source
            or "M8M_RUNNABLE = False" in source
        ):
            build_required_handlers.append(str(item["id"]))
    # Piecemeal generation never packages the codebase-owned runtime release.
    # Even a fully implemented tool/handler scaffold is therefore not an
    # executable product harness until the canonical five-milestone Builder
    # workflow validates and installs it.
    build_required_runtime = True
    build_required = bool(
        build_required_runtime or build_required_tools or build_required_handlers
    )
    return {
        "schema": "flowstep_harness_generate_v4",
        "status": "BUILD_REQUIRED" if build_required else "PASS",
        "runnable": not build_required,
        "non_runnable": build_required,
        "build_required_tools": build_required_tools,
        "build_required_handlers": build_required_handlers,
        "build_required_runtime": build_required_runtime,
        "harness_dir": str(harness),
        "codebase": str(Path(os.path.abspath(str(codebase)))),
        "flow_id": flow_id,
        "milestones": milestones,
        "tools": sorted({tool for item in items for tool in item["tools"]}),
        "instruction_path": str(instruction),
        "flowchart_path": str(chart),
        "flowchart_jpg": str(jpg),
        "tool_vs_intelligence": table,
        "tool_vs_intelligence_path": str(table_path),
        "tool_generation": [tool_generation[key] for key in sorted(tool_generation)],
        "written": created,
        "notes": [
            *notes,
            "Piecemeal generation has no codebase-owned runtime release; complete the canonical Builder workflow before execution.",
            *(
                [
                    "Generated candidate handlers are BUILD_REQUIRED/non-runnable until each "
                    "milestone-specific implementation returns explicit named outputs."
                ]
                if build_required_handlers
                else []
            ),
        ],
    }


# Python import compatibility only. This function now always emits v4 YAML.
generate_v3_flow = generate_v4_flow


def load_audit_report(path: Path) -> dict[str, Any]:
    path = Path(os.path.abspath(str(path)))
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    sibling = path.with_name("flowstep-audit.json") if path.name.endswith(".md") else path.with_suffix(".json")
    if sibling.is_file():
        return json.loads(sibling.read_text(encoding="utf-8"))
    raise FlowError(f"audit JSON not found next to {path}; run audit_harness.py first")


def write_product_skill(
    codebase: Path,
    skill_name: str,
    flow_id: str,
    *,
    overwrite: bool = False,
    classification: dict[str, Any] | None = None,
) -> str:
    root = Path(os.path.abspath(str(codebase)))
    table = render_classification_markdown(classification or {"rows": []})
    mapping = {
        "SKILL_NAME": skill_name,
        "FLOW_ID": flow_id,
        "BUILDER_ROOT": str(DEFAULT_BUILDER).replace("\\", "/"),
        "CLASSIFICATION_TABLE": table,
    }
    text = _render("product-SKILL.md", mapping)
    dests = [
        root / ".agents" / "skills" / skill_name / "SKILL.md",
        root / ".claude" / "skills" / skill_name / "SKILL.md",
    ]
    primary = dests[0]
    for dest in dests:
        _write_text(dest, text, overwrite=overwrite or not dest.exists())
    return str(primary)


def _copy_missing_control_schemas(harness: Path, audit: dict[str, Any]) -> None:
    sources: list[Path] = []
    grade = audit.get("grade") if isinstance(audit.get("grade"), dict) else {}
    for raw in (audit.get("target"), grade.get("target"), grade.get("flow_path")):
        if not raw:
            continue
        path = Path(str(raw))
        if path.is_file():
            path = path.parent
        if path.is_dir():
            sources.append(path)
    rels: list[str] = []
    for item in audit.get("proposed_milestones") or []:
        for edge in item.get("next") or []:
            if isinstance(edge, dict) and edge.get("when"):
                rels.append(str(edge["when"]))
        fe = item.get("foreach") or {}
        if fe.get("item_schema"):
            rels.append(str(fe["item_schema"]))
    for rel in rels:
        dest = harness / rel
        if dest.is_file():
            continue
        for src_root in sources:
            cand = src_root / rel
            if cand.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cand, dest)
                break


def generate_from_audit(
    codebase: Path,
    audit: dict[str, Any],
    *,
    flow_id: str | None = None,
    skill_name: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    skill = audit.get("audited_skill") if isinstance(audit.get("audited_skill"), dict) else {}
    name = skill_name or str(skill.get("name") or "product-skill")
    proposed = audit.get("proposed_milestones") or []
    if not proposed:
        raise FlowError("audit has no proposed_milestones")
    raw_flow_id = flow_id or (audit.get("grade") or {}).get("flow_id") or f"{name.replace('-', '_')}_v1"
    raw_flow_id = str(raw_flow_id).lower().replace("-", "_")
    if not FLOW_ID_RE.match(raw_flow_id):
        raw_flow_id = "product_v1"
    tool_ids: list[str] = []
    for row in audit.get("python_standardization") or []:
        if row.get("tool_id"):
            tool_ids.append(str(row["tool_id"]))
    for item in proposed:
        bindings = (
            (item.get("execution") or {}).get("tool_bindings") or []
            if isinstance(item.get("execution"), dict)
            else []
        )
        if bindings:
            for binding in bindings:
                if isinstance(binding, dict) and binding.get("ref"):
                    tool_ids.append(
                        local_tool_package_name(
                            str(binding["ref"]),
                            label=(
                                f"{item.get('id')}.execution.tool_bindings"
                                f"[{binding.get('tool')}].ref"
                            ),
                        )
                    )
        else:
            for tool_id in item.get("tools") or []:
                tool_ids.append(str(tool_id))
        if item.get("worker") and not item.get("judge_abi"):
            tool_ids.append(str(item["worker"]))
        if isinstance(item.get("branch"), dict) and item["branch"].get("worker"):
            tool_ids.append(str(item["branch"]["worker"]))
    unique_tools: list[str] = []
    for tool_id in tool_ids:
        if tool_id and tool_id not in unique_tools:
            unique_tools.append(tool_id)
    toolbox: list[dict[str, Any]] = []
    specs = []
    for item in proposed:
        tools = [str(tool_id) for tool_id in (item.get("tools") or []) if tool_id]
        spec = {
            "id": item["id"],
            "flowsteps": item.get("flowsteps"),
            "tools": tools,
            "intelligence": item.get("intelligence") or "none",
            "output_contract": item.get("output_contract") or f"{item['id']}_v1",
            "output_schema_object": item.get("output_schema"),
            "input_schema_object": item.get("input_schema"),
            "outputs": item.get("outputs") if isinstance(item.get("outputs"), list) else None,
            "inputs": item.get("inputs"),
            "model_justification": item.get("model_justification"),
            "loop": item.get("loop"),
            "ledger": item.get("ledger"),
            "worker": item.get("worker"),
            "judge_abi": item.get("judge_abi"),
            "receipt_schema": item.get("receipt_schema"),
            "max_attempts": item.get("max_attempts"),
            "foreach": item.get("foreach"),
            "branch": item.get("branch"),
            "on_path": item.get("on_path"),
            "cycle": item.get("cycle"),
            "on_cycle": item.get("on_cycle"),
            "on_tool_fail": item.get("on_tool_fail"),
            "success": item.get("success"),
            "_expectation_authored": (audit.get("grade") or {}).get("flow_schema") == "flowstep_flow_v4",
            "_judge_inferred": bool(item.get("_judge_inferred")),
            "gem": item.get("gem"),
            "master_prompt": item.get("master_prompt"),
            "max_model_attempts": item.get("max_model_attempts"),
            "cache": item.get("cache") if isinstance(item.get("cache"), dict) else None,
            "execution": item.get("execution") if isinstance(item.get("execution"), dict) else None,
            "_gate_schemas": {
                str(edge["when"]): edge["schema"]
                for edge in (item.get("next") or [])
                if isinstance(edge, dict) and edge.get("schema")
            },
        }
        specs.append(spec)
    result = generate_v4_flow(
        codebase,
        raw_flow_id,
        [item["id"] for item in specs],
        tools=unique_tools,
        overwrite=overwrite,
        milestone_specs=specs,
        toolbox_plan=audit.get("toolbox_plan")
        or build_toolbox_plan(proposed, audit.get("python_standardization") or []),
    )
    if result.get("build_required_milestones"):
        result["toolbox"] = []
        result["skill_name"] = name
        return result
    toolbox = [
        generate_tool(codebase, tool_id, overwrite=overwrite)
        for tool_id in unique_tools
    ]
    _copy_missing_control_schemas(Path(result["harness_dir"]), audit)
    copied_teaching = copy_teaching_contracts(
        Path(result["harness_dir"]), audit, overwrite=overwrite
    )
    if copied_teaching:
        write_instruction(
            Path(result["harness_dir"]),
            load_flow(Path(result["harness_dir"])),
            toolbox_plan=audit.get("toolbox_plan"),
            source="generate",
        )
        result.setdefault("written", []).extend(copied_teaching)
        result["teaching_contracts"] = copied_teaching
    table = audit.get("tool_vs_intelligence") or classification_from_audit(audit)
    table["flow_id"] = raw_flow_id
    table_path = Path(result["harness_dir"]) / "planning" / "tool-vs-intelligence.json"
    table_path.parent.mkdir(parents=True, exist_ok=True)
    table_path.write_text(json.dumps(table, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    product = write_product_skill(
        codebase, name, raw_flow_id, overwrite=overwrite, classification=table
    )
    result["product_skill"] = product
    result["toolbox"] = toolbox
    result["skill_name"] = name
    result["tool_vs_intelligence"] = table
    result["tool_vs_intelligence_path"] = str(table_path)
    build_required = sorted(
        {
            *(
                str(item["tool_id"])
                for item in toolbox
                if item.get("status") != "PASS" or item.get("non_runnable")
            ),
            *(str(item) for item in result.get("build_required_tools") or []),
        }
    )
    build_required_handlers = sorted(
        str(item) for item in result.get("build_required_handlers") or []
    )
    has_build_required = bool(build_required or build_required_handlers)
    result["status"] = "BUILD_REQUIRED" if has_build_required else "PASS"
    result["runnable"] = not has_build_required
    result["non_runnable"] = has_build_required
    result["build_required_tools"] = build_required
    if build_required:
        result.setdefault("notes", [])
        result["notes"].append(
            "BUILD_REQUIRED non-runnable tools (implement before validation): "
            + ", ".join(build_required)
        )
    return result


def yaml_dump_v4(flow: dict[str, Any]) -> str:
    lines = [
        f"schema: {flow['schema']}",
        f"flow_id: {flow['flow_id']}",
        f"version: {flow['version']}",
        f"context_policy: {flow.get('context_policy', 'isolated')}",
        f"max_run_seconds: {flow['max_run_seconds']}",
        f"artifact_root: {flow['artifact_root']}",
        "milestones:",
    ]
    for item in flow["milestones"]:
        lines.append(f"  - id: {item['id']}")
        lines.append(f"    output_contract: {item['output_contract']}")
        lines.append(f"    output_schema: {item['output_schema']}")
        if item.get("input_schema"):
            lines.append(f"    input_schema: {item['input_schema']}")
        lines.append("    outputs:")
        for output in item.get("outputs") or []:
            lines.append(f"      - id: {output['id']}")
            lines.append(f"        name: {json.dumps(str(output['name']), ensure_ascii=False)}")
            lines.append(f"        kind: {output['kind']}")
            lines.append(f"        cardinality: {output['cardinality']}")
            lines.append(f"        required: {'true' if output.get('required') else 'false'}")
        if item.get("success"):
            lines.append(f"    success: {json.dumps(str(item['success']), ensure_ascii=False)}")
        if item.get("gem"):
            lines.append(f"    gem: {item['gem']}")
        if isinstance(item.get("execution"), dict):
            lines.append("    execution:")
            execution_yaml = yaml.safe_dump(
                item["execution"],
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            ).rstrip()
            lines.extend(f"      {line}" for line in execution_yaml.splitlines())
        if isinstance(item.get("cache"), dict):
            cache = item["cache"]
            lines.append("    cache:")
            lines.append(f"      reuse: {cache.get('reuse')}")
            lines.append(f"      ttl_seconds: {cache.get('ttl_seconds')}")
            lines.append(f"      side_effects: {cache.get('side_effects')}")
        if (
            item.get("worker")
            and item.get("loop") not in {"for", "judge"}
            and not item.get("branch")
            and not item.get("cycle")
        ):
            lines.append(f"    worker: {item['worker']}")
        flowsteps = item.get("flowsteps") or []
        if flowsteps:
            lines.append("    flowsteps:")
            for fs in flowsteps:
                fid = fs.get("id") or fs.get("tool") or "step"
                lines.append(f"      - id: {fid}")
                if fs.get("tool"):
                    lines.append(f"        tool: {fs['tool']}")
        if item.get("tools"):
            lines.append(f"    tools: [{', '.join(item['tools'])}]")
        lines.append(f"    intelligence: {item['intelligence']}")
        if item.get("on_tool_fail"):
            lines.append(f"    on_tool_fail: {item['on_tool_fail']}")
        if item.get("max_model_attempts"):
            lines.append(f"    max_model_attempts: {item['max_model_attempts']}")
        if item["intelligence"] != "none":
            lines.append(f"    model_justification: {json.dumps(item.get('model_justification') or '', ensure_ascii=False)}")
        if item.get("draft_schema"):
            lines.append(f"    draft_schema: {item['draft_schema']}")
        lines.append(f"    handler: {item['handler']}")
        lines.append("    inputs:")
        for input_name, binding in (item.get("inputs") or {}).items():
            if isinstance(binding, str):
                lines.append(f"      {input_name}: {binding}")
            elif isinstance(binding, dict):
                lines.append(f"      {input_name}:")
                lines.append(f"        from: {binding.get('from')}")
                if binding.get("output"):
                    lines.append(f"        output: {binding.get('output')}")
                if binding.get("member"):
                    lines.append(f"        member: {binding.get('member')}")
        if item.get("loop") in {"for", "judge"}:
            lines.append(f"    loop: {item['loop']}")
            if item.get("worker"):
                lines.append(f"    worker: {item['worker']}")
            if item.get("judge_abi"):
                lines.append(f"    judge_abi: {item['judge_abi']}")
            if item.get("receipt_schema"):
                lines.append(f"    receipt_schema: {item['receipt_schema']}")
            if item.get("max_attempts"):
                lines.append(f"    max_attempts: {item['max_attempts']}")
        if item.get("ledger"):
            fe = item["ledger"]
            lines.append("    ledger:")
            lines.append(f"      path: {fe['path']}")
            lines.append(f"      item_schema: {fe['item_schema']}")
            lines.append(f"      max_items: {fe['max_items']}")
        if item.get("on_path"):
            lines.append(f"    on_path: {item['on_path']}")
        if item.get("branch") and isinstance(item["branch"], dict):
            br = item["branch"]
            lines.append("    branch:")
            if br.get("worker"):
                lines.append(f"      worker: {br.get('worker')}")
            if br.get("default"):
                lines.append(f"      default: {br.get('default')}")
            if br.get("join"):
                lines.append(f"      join: {br.get('join')}")
            if br.get("receipt_schema"):
                lines.append(f"      receipt_schema: {br.get('receipt_schema')}")
            if br.get("paths"):
                lines.append("      paths:")
                for path in br["paths"]:
                    if isinstance(path, dict):
                        lines.append(f"        - id: {path.get('id')}")
                        if path.get("then"):
                            lines.append(f"          then: {path.get('then')}")
                    else:
                        lines.append(f"        - {path}")
        if item.get("on_cycle"):
            lines.append(f"    on_cycle: {item['on_cycle']}")
        if item.get("cycle") and isinstance(item["cycle"], dict):
            cy = item["cycle"]
            lines.append("    cycle:")
            for key in ("id", "worker", "ledger", "start", "join", "receipt_schema", "pass"):
                if cy.get(key):
                    value = cy[key]
                    if key == "pass":
                        lines.append(f"      pass: {json.dumps(str(value), ensure_ascii=False)}")
                    else:
                        lines.append(f"      {key}: {value}")
            if cy.get("max_rounds"):
                lines.append(f"      max_rounds: {cy.get('max_rounds')}")
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
    raise FlowError(
        "legacy v2 harness generation was removed; regenerate with "
        "m8m-harness-builder 3.1 using --from-audit or --milestone"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codebase", type=Path, help="Repo root. Writes <codebase>/flowsteps/<flow_id>.")
    parser.add_argument("--skill-dir", type=Path, help="Harness dir for the builder fixture only.")
    parser.add_argument("--flow-id")
    parser.add_argument("--from-audit", type=Path, help="flowstep-audit.json (or .md next to that JSON).")
    parser.add_argument("--tool", dest="tool_id", help="Install one toolbox function under flowsteps/tools/.")
    parser.add_argument("--step", action="append", default=[], dest="steps")
    parser.add_argument("--milestone", action="append", default=[], dest="milestones")
    parser.add_argument("--tools", help="Comma-separated toolbox ids when not using --from-audit.")
    parser.add_argument("--intelligence", action="append", default=[], help="Milestone ids that may NEED_MODEL.")
    parser.add_argument("--skill-name")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--write-skill-md",
        action="store_true",
        help="Write product SKILL.md under <repo>/.agents/skills/.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.from_audit:
            if not args.codebase:
                raise FlowError("--from-audit requires --codebase")
            result = generate_from_audit(
                args.codebase,
                load_audit_report(args.from_audit),
                flow_id=args.flow_id,
                skill_name=args.skill_name,
                overwrite=args.force,
            )
        elif args.tool_id:
            if not args.codebase:
                raise FlowError("--tool requires --codebase")
            result = generate_tool(args.codebase, args.tool_id, overwrite=args.force)
        elif args.milestones:
            if not args.codebase or not args.flow_id:
                raise FlowError("--milestone requires --codebase and --flow-id")
            tool_ids = [part.strip() for part in (args.tools or "").split(",") if part.strip()]
            result = generate_v4_flow(
                args.codebase,
                args.flow_id,
                args.milestones,
                tools=tool_ids,
                intelligence=args.intelligence,
                overwrite=args.force,
            )
            if args.write_skill_md:
                name = args.skill_name or args.flow_id
                result["product_skill"] = write_product_skill(
                    args.codebase, name, args.flow_id, overwrite=args.force
                )
        elif args.steps:
            raise FlowError(
                "v2 --step generation was removed; regenerate with "
                "m8m-harness-builder 3.1 using --from-audit or --milestone"
            )
        else:
            raise FlowError("pass --from-audit, --milestone, or --tool")
    except FlowError as exc:
        print(json.dumps({"status": "BLOCKED", "blockers": [str(exc)]}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
