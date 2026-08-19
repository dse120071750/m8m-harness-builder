"""Schema-only if/else and foreach. No expressions. No intelligence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, RefResolver

from flowstep_runtime import FlowError, read_json, sha256_file
from flowstep_tools import infer_codebase, run_library_tool


CONTROL_PREFIXES = ("if_", "loop_", "switch_", "when_", "else_")
ELSE_BLOCKED = "BLOCKED"


def is_control_name(step_id: str) -> bool:
    lowered = str(step_id or "").lower()
    return any(lowered.startswith(prefix) for prefix in CONTROL_PREFIXES)


def schema_accepts(instance: Any, schema: dict[str, Any] | Path) -> bool:
    if isinstance(schema, Path):
        path = schema
        schema = read_json(path)
        resolver = RefResolver(base_uri=path.resolve().as_uri(), referrer=schema)
    else:
        resolver = RefResolver.from_schema(schema)
    return Draft202012Validator(schema, resolver=resolver).is_valid(instance)


def schema_property_names(schema: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    if not isinstance(schema, dict):
        return names
    props = schema.get("properties")
    if isinstance(props, dict):
        names.update(props)
    for key in ("allOf", "anyOf", "oneOf"):
        for child in schema.get(key) or []:
            if isinstance(child, dict):
                names |= schema_property_names(child)
    return names


def schema_required_names(schema: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    if not isinstance(schema, dict):
        return names
    for item in schema.get("required") or []:
        names.add(str(item))
    for key in ("allOf", "anyOf", "oneOf"):
        for child in schema.get(key) or []:
            if isinstance(child, dict):
                names |= schema_required_names(child)
    props = schema.get("properties")
    if isinstance(props, dict):
        names |= set(props)
    return names


def lookup_path(data: Any, path: str) -> Any:
    current = data
    for part in str(path).split("."):
        if not isinstance(current, dict) or part not in current:
            raise FlowError(f"foreach path not present: {path}")
        current = current[part]
    return current


def resolve_next(
    step: dict[str, Any],
    data: dict[str, Any],
    skill_dir: Path,
) -> tuple[str, dict[str, Any] | None]:
    edges = step.get("next") or []
    if not edges:
        return "", None
    for edge in edges:
        when = skill_dir / str(edge["when"])
        if schema_accepts(data, when):
            return str(edge["then"]), {
                "gate_id": Path(str(edge["when"])).stem,
                "gate_schema": str(edge["when"]).replace("\\", "/"),
                "gate_sha256": sha256_file(when),
                "then": edge["then"],
            }
    else_to = step.get("else") or ELSE_BLOCKED
    return str(else_to), {
        "gate_id": "else",
        "gate_schema": None,
        "then": else_to,
    }


def run_foreach(
    skill_dir: Path,
    step: dict[str, Any],
    input_data: dict[str, Any],
) -> dict[str, Any]:
    spec = step.get("foreach") or {}
    path = str(spec.get("path") or "")
    if not path:
        raise FlowError(f"{step['id']}: foreach.path is required")
    max_items = spec.get("max_items")
    if not isinstance(max_items, int) or max_items < 1:
        raise FlowError(f"{step['id']}: foreach.max_items must be a positive integer")
    item_schema_rel = spec.get("item_schema")
    if not item_schema_rel:
        raise FlowError(f"{step['id']}: foreach.item_schema is required")
    item_schema_path = skill_dir / str(item_schema_rel)
    tools = spec.get("tools") or []
    if not isinstance(tools, list) or not tools:
        raise FlowError(f"{step['id']}: foreach.tools must be a non-empty tool list")
    collect = str(spec.get("collect") or path)
    source = input_data
    if isinstance(input_data, dict) and len(input_data) == 1:
        only = next(iter(input_data.values()))
        if isinstance(only, dict) and path.split(".")[0] in only:
            source = only
    items = lookup_path(source, path)
    if not isinstance(items, list):
        raise FlowError(f"{step['id']}: foreach.{path} is not an array")
    if len(items) > max_items:
        raise FlowError(f"{step['id']}: foreach {path} length {len(items)} exceeds max_items {max_items}")
    codebase = infer_codebase(skill_dir)
    if codebase is None:
        raise FlowError(f"{step['id']}: foreach requires a project toolbox at flowsteps/tools")
    collected: list[Any] = []
    for index, item in enumerate(items):
        if not schema_accepts(item, item_schema_path):
            raise FlowError(f"{step['id']}: foreach item {index} failed {item_schema_rel}")
        current = item
        for tool_id in tools:
            if not isinstance(current, dict):
                raise FlowError(f"{step['id']}: foreach tool {tool_id} requires an object item")
            current = run_library_tool(codebase, str(tool_id), current)
        collected.append(current)
    return {collect: collected, "item_count": len(collected)}
