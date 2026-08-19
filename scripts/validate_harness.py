"""Reject any FlowStep that is not a real Python tool with input and output schemas."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, RefResolver

from flowstep_instruction import sync_statuses_from_errors
from flowstep_tools import infer_codebase, validate_library_tool
from flowstep_runtime import (
    FlowError,
    add_harness_location_args,
    find_flow_path,
    harness_dir_from_args,
    inspect_step_test,
    inspect_tool_source,
    is_stub_output_schema,
    lint_file_payload_schema,
    load_flow,
    load_tool,
    read_json,
    resolve_harness_dir,
    skill_rel,
)


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
        if step["model"] != "none":
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
        if handler_path.is_file():
            errors.extend(
                inspect_tool_source(
                    handler_path.read_text(encoding="utf-8"),
                    step_id=step_id,
                    model=step["model"],
                )
            )
        if flow.get("_v3"):
            codebase = infer_codebase(skill_dir)
            if codebase is None:
                errors.append(f"{step_id}: v3 flow must live at flowsteps/flows/<flow_id>")
            else:
                for tool_id in step.get("tools") or []:
                    errors.extend(validate_library_tool(codebase, tool_id))
        test_path = skill_dir / step["test"]
        if test_path.is_file():
            errors.extend(inspect_step_test(test_path.read_text(encoding="utf-8"), step_id=step_id))
        for schema_key in ("input_schema", "output_schema", "draft_schema"):
            if schema_key == "draft_schema" and step["model"] == "none":
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
            errors.extend(lint_file_payload_schema(schema, label=f"{step_id}.{schema_key}"))
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
