"""Reject any FlowStep that is not a real Python tool with input and output schemas."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, RefResolver

from flowstep_instruction import sync_statuses_from_errors
from flowstep_tools import infer_codebase
from schema_gate import is_control_name, schema_property_names, schema_required_names
from flowstep_runtime import (
    FlowError,
    add_harness_location_args,
    find_flow_path,
    harness_dir_from_args,
    inspect_step_test,
    inspect_tool_source,
    is_passthrough_schema,
    is_stub_output_schema,
    lint_file_payload_schema,
    load_flow,
    load_tool,
    read_json,
    resolve_harness_dir,
    skill_rel,
)


def _previous_output_schema(skill_dir: Path, flow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any] | None:
    by_id = {item["id"]: item for item in flow["steps"]}
    refs: list[str] = []
    if step.get("join"):
        refs = list(step["join"])
    else:
        for reference in (step.get("inputs") or {}).values():
            if isinstance(reference, str) and "." in reference and reference != "user.request":
                refs.append(reference.split(".", 1)[0])
    names: set[str] = set()
    schema: dict[str, Any] | None = None
    for mid in refs:
        source = by_id.get(mid)
        if not source:
            continue
        path = skill_dir / source["output_schema"]
        if not path.is_file():
            continue
        schema = read_json(path)
        names |= schema_property_names(schema)
    if schema is None:
        return None
    schema = dict(schema)
    schema["_names"] = names
    return schema


def _validate_control(
    skill_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    index: int,
    declared: set[str],
    errors: list[str],
) -> None:
    step_id = step["id"]
    fail = step.get("on_tool_fail") or "need_model"
    if fail not in {"BLOCKED", "need_model"}:
        errors.append(f"{step_id}: on_tool_fail must be BLOCKED or need_model")
    attempts = step.get("max_model_attempts")
    if attempts is not None and (not isinstance(attempts, int) or attempts < 1):
        errors.append(f"{step_id}: max_model_attempts must be a positive integer")
    loop = str(step.get("loop") or "none")
    if step.get("next") or step.get("else") or step.get("join"):
        errors.append(f"{step_id}: exclusive next.when/else/join is removed; use branch after the milestone")
    branch = step.get("branch") if isinstance(step.get("branch"), dict) else None
    if branch:
        worker = branch.get("worker") or step.get("worker")
        if not worker:
            errors.append(f"{step_id}: branch requires a repo worker tool")
        paths = branch.get("paths") or []
        if not isinstance(paths, list) or len(paths) < 2:
            errors.append(f"{step_id}: branch requires at least two paths")
        path_ids = [str(item.get("id") or "") for item in paths if isinstance(item, dict)]
        if len(set(path_ids)) != len(path_ids):
            errors.append(f"{step_id}: branch path ids must be unique")
        default = str(branch.get("default") or "")
        if default and default not in path_ids:
            errors.append(f"{step_id}: branch.default {default} is not a path")
        join = str(branch.get("join") or "")
        if join and join not in declared:
            errors.append(f"{step_id}: branch.join {join} is not a milestone")
        for item in paths:
            if not isinstance(item, dict):
                continue
            then = str(item.get("then") or item.get("id") or "")
            if then and then not in declared:
                errors.append(f"{step_id}: branch path {item.get('id')} then {then} is not a milestone")
        receipt = skill_dir / str(branch.get("receipt_schema") or step.get("receipt_schema") or "")
        if not receipt.is_file():
            errors.append(f"{step_id}: missing branch receipt_schema")
        else:
            try:
                schema = read_json(receipt)
            except FlowError as exc:
                errors.append(str(exc))
            else:
                names = schema_required_names(schema)
                props = schema.get("properties") or {}
                if "ok" not in names and "ok" not in props:
                    errors.append(f"{step_id}: branch receipt schema must require ok")
                if "branch" not in names and "branch" not in props:
                    errors.append(f"{step_id}: branch receipt schema must require branch")
    on_path = str(step.get("on_path") or "")
    if on_path:
        known_paths: set[str] = set()
        for other in flow["steps"]:
            spec = other.get("branch") if isinstance(other.get("branch"), dict) else {}
            for item in spec.get("paths") or []:
                if isinstance(item, dict) and item.get("id"):
                    known_paths.add(str(item["id"]))
        if known_paths and on_path not in known_paths:
            errors.append(f"{step_id}: on_path {on_path} is not a declared branch path")
    if loop in {"for", "judge"} and not step.get("worker"):
        errors.append(f"{step_id}: loop={loop} requires a repo worker tool")
    if loop == "for":
        ledger = step.get("ledger") or {}
        if not isinstance(ledger, dict) or not ledger.get("path"):
            errors.append(f"{step_id}: loop=for requires ledger.path")
        if not isinstance(ledger.get("max_items"), int) or ledger.get("max_items", 0) < 1:
            errors.append(f"{step_id}: ledger.max_items must be a positive integer")
        item_schema = skill_dir / str(ledger.get("item_schema") or "")
        if not item_schema.is_file():
            errors.append(f"{step_id}: missing ledger.item_schema {ledger.get('item_schema')}")
        prev = _previous_output_schema(skill_dir, flow, step)
        if prev:
            props = prev.get("properties") or {}
            root = str(ledger.get("path") or "").split(".", 1)[0]
            field = props.get(root)
            if not isinstance(field, dict) or field.get("type") != "array":
                errors.append(f"{step_id}: ledger.path {ledger.get('path')} is not an array on the previous output schema")
            else:
                schema_max = field.get("maxItems")
                if schema_max is None:
                    errors.append(f"{step_id}: previous output {root} must declare maxItems")
                elif isinstance(ledger.get("max_items"), int) and ledger["max_items"] > int(schema_max):
                    errors.append(
                        f"{step_id}: ledger.max_items {ledger['max_items']} exceeds schema maxItems {schema_max}"
                    )
    if loop in {"for", "judge"}:
        receipt = skill_dir / str(step.get("receipt_schema") or "")
        if not receipt.is_file():
            errors.append(f"{step_id}: missing receipt_schema {step.get('receipt_schema')}")
        else:
            try:
                schema = read_json(receipt)
            except FlowError as exc:
                errors.append(str(exc))
            else:
                names = schema_required_names(schema)
                if "ok" not in names and "ok" not in (schema.get("properties") or {}):
                    errors.append(f"{step_id}: receipt schema must require ok")


def validate_harness(
    skill_dir: Path | None = None,
    flow_arg: str | None = None,
    *,
    codebase: Path | None = None,
    flow_id: str | None = None,
) -> dict[str, Any]:
    skill_dir = resolve_harness_dir(codebase=codebase, flow_id=flow_id, skill_dir=skill_dir)
    flow = load_flow(skill_dir, find_flow_path(skill_dir, flow_arg) if flow_arg else None)
    errors: list[str] = []
    seen_contracts: dict[str, str] = {}
    declared = {step["id"] for step in flow["steps"]}
    for index, step in enumerate(flow["steps"]):
        step_id = step["id"]
        required_files = ["handler", "input_schema", "output_schema", "test"]
        if step["model"] != "none" or step.get("on_tool_fail") == "need_model":
            if (skill_dir / step.get("draft_schema", "")).is_file() or step.get("draft_schema"):
                required_files.append("draft_schema")
        for key in required_files:
            try:
                path = skill_rel(skill_dir, step[key])
            except FlowError as exc:
                errors.append(str(exc))
                continue
            if not path.is_file():
                errors.append(f"{step_id}: missing {key} at {path}")
        try:
            load_tool(skill_dir, step)
        except FlowError as exc:
            errors.append(f"{step_id}: {exc}")
        handler_path = skill_dir / step["handler"]
        if flow.get("_v3"):
            codebase = infer_codebase(skill_dir)
            if codebase is None:
                errors.append(f"{step_id}: v3 flow must live at flowsteps/flows/<flow_id>")
        elif handler_path.is_file():
            errors.extend(
                inspect_tool_source(
                    handler_path.read_text(encoding="utf-8"),
                    step_id=step_id,
                    model=step["model"],
                )
            )
        test_path = skill_dir / step["test"]
        if test_path.is_file() and not flow.get("_v3"):
            errors.extend(inspect_step_test(test_path.read_text(encoding="utf-8"), step_id=step_id))
        for schema_key in ("input_schema", "output_schema", "draft_schema"):
            if schema_key == "draft_schema" and not step.get("draft_schema"):
                continue
            path = skill_dir / step[schema_key]
            if not path.is_file():
                continue
            try:
                schema = read_json(path)
            except FlowError as exc:
                errors.append(str(exc))
                continue
            if schema.get("type") != "object":
                errors.append(f"{step_id}: {schema_key} must be a JSON object schema")
            try:
                Draft202012Validator(
                    schema, resolver=RefResolver(base_uri=path.resolve().as_uri(), referrer=schema)
                )
            except Exception as exc:  # noqa: BLE001 - surface any unusable schema
                errors.append(f"{step_id}: {schema_key} is not a usable JSON Schema: {exc}")
            if schema_key == "output_schema" and is_stub_output_schema(schema):
                errors.append(f"{step_id}: output schema is still the generated {{ok: boolean}} stub")
            if schema_key == "output_schema" and flow.get("_v3") and is_passthrough_schema(schema):
                errors.append(f"{step_id}: milestone output is not a required asset (closed schema with required fields)")
            errors.extend(lint_file_payload_schema(schema, label=f"{step_id}.{schema_key}"))
        if is_control_name(step_id) and not flow.get("_v3"):
            errors.append(f"{step_id}: if/loop/switch names are notes; for/judge are milestones")
        _validate_control(skill_dir, flow, step, index, declared, errors)
        contract = step["output_contract"]
        owner = seen_contracts.get(contract)
        if owner and owner != step_id:
            errors.append(f"output_contract {contract} is used by both {owner} and {step_id}")
        seen_contracts[contract] = step_id
        for name, reference in step["inputs"].items():
            if reference == "user.request":
                continue
            if not isinstance(reference, str) or "." not in reference:
                errors.append(f"{step_id}.inputs.{name} must be user.request or <step>.<contract>")
                continue
            source_id, contract_name = reference.split(".", 1)
            if source_id not in declared:
                errors.append(f"{step_id}.inputs.{name} references unknown step {source_id}")
                continue
            source_index = next(i for i, item in enumerate(flow["steps"]) if item["id"] == source_id)
            if source_index >= index:
                errors.append(f"{step_id}.inputs.{name} must reference an earlier step")
            source = flow["steps"][source_index]
            if source["output_contract"] != contract_name:
                errors.append(
                    f"{step_id}.inputs.{name} expected {source['output_contract']}, got {contract_name}"
                )
    instruction = sync_statuses_from_errors(skill_dir, flow, errors)
    if errors:
        raise FlowError("harness invalid:\n- " + "\n- ".join(errors))
    return {
        "schema": "flowstep_harness_validation_v2",
        "status": "PASS",
        "skill": skill_dir.name,
        "flow_id": flow["flow_id"],
        "steps": [step["id"] for step in flow["steps"]],
        "instruction_path": str(instruction),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_harness_location_args(parser)
    parser.add_argument("--flow")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = validate_harness(
            harness_dir_from_args(args),
            args.flow,
        )
    except FlowError as exc:
        print(json.dumps({"status": "BLOCKED", "blockers": [str(exc)]}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
