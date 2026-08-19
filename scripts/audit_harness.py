"""Read-only audit worker: grade a skill and write a separation-plan markdown."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

from flowstep_runtime import (
    FLOW_SCHEMA,
    FLOW_SCHEMA_V3,
    INTEL_ID_HINTS,
    MILESTONE_SUFFIXES,
    TOOL_ID_HINTS,
    FlowError,
    add_harness_location_args,
    harness_dir_from_args,
    is_stub_output_schema,
    load_yaml,
    read_json,
    step_class_hint,
    utc_now,
)
from flowstep_tools import infer_codebase, tools_root, validate_library_tool
from m8m_flowchart import write_flowchart
from tool_vs_intelligence import from_audit as classification_from_audit
from tool_vs_intelligence import render_markdown as render_classification_markdown


AUDIT_SCHEMA = "flowstep_skill_audit_v1"
DEFAULT_REPORT = Path("planning/flowstep-audit.md")
DEFAULT_REPORT_JSON = Path("planning/flowstep-audit.json")
SCRIPT_CALL_RE = re.compile(r"scripts[/\\]([a-z][a-z0-9_]*)\.py")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*", re.S)
LINKED_FLOW_RE = re.compile(
    r"((?:[A-Za-z]:)?[\\/][^\s`\"']+?[\\/]flowsteps[\\/]flows[\\/][a-z][a-z0-9_]*)"
)
TOOLBOX_RE = re.compile(r"flowsteps[/\\]tools[/\\]([a-z][a-z0-9_]*)")
MILESTONE_LINE_RE = re.compile(r"^\d+\.\s+`([a-z][a-z0-9_]*)`")
DRIVER_STEMS = {
    "audit_harness",
    "emit_step",
    "flowstep_instruction",
    "flowstep_runtime",
    "flowstep_tools",
    "generate_harness",
    "run",
    "run_flow",
    "run_flow_sequence",
    "m8m_flowchart",
    "schema_gate",
    "self_test",
    "validate_harness",
    "validate_run",
}
ACTION_HINTS = TOOL_ID_HINTS + (
    "ingest",
    "load",
    "bind",
    "validate",
    "verify",
    "check",
    "bootstrap",
    "resolve",
    "materialize",
    "compile",
    "aggregate",
    "finalize",
    "release",
)
INTEL_HINTS = INTEL_ID_HINTS + ("label", "caption", "plan", "select")
ENVELOPE_CONTRACTS = {"flowstep_output_v2", "flow_sequence_action_v2", "file_ref_v2"}
BUCKETS = (
    ("source_ready", ("fetch", "ingest", "load", "query", "resolve", "bootstrap", "normalize", "source"), "none"),
    ("plan_frozen", ("plan", "select", "choose", "label", "draft", "candidate", "describe"), "completion"),
    ("assets_bound", ("crop", "hash", "bind", "generate", "resize", "letterbox", "image"), "none"),
    ("cards_rendered", ("render", "compile", "composite", "layout", "video", "presentation"), "none"),
    ("captions_frozen", ("caption",), "completion"),
    ("release_packaged", ("package", "release", "register", "upload", "commit", "verify", "io"), "none"),
)
CONTRACT_MILESTONE = (
    (re.compile(r"source|capture|bootstrap|knowledge"), "source_ready"),
    (re.compile(r"plan|carousel|candidate"), "plan_frozen"),
    (re.compile(r"prompt"), "prompts_frozen"),
    (re.compile(r"asset|scene|detail"), "assets_bound"),
    (re.compile(r"render|card|presentation"), "cards_rendered"),
    (re.compile(r"caption"), "captions_frozen"),
    (re.compile(r"package|handoff|operator_result|release"), "release_packaged"),
)


def _find_any_flow(root: Path) -> Path | None:
    direct = root / "flow.yaml"
    if direct.is_file():
        return direct
    flows = root / "flows"
    if flows.is_dir():
        matches = sorted(flows.glob("*.yaml")) + sorted(flows.glob("*.yml"))
        if matches:
            return matches[0]
    return None


def _step_rows(raw: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(raw.get("milestones"), list):
        return [item for item in raw["milestones"] if isinstance(item, dict)]
    if isinstance(raw.get("steps"), list):
        return [item for item in raw["steps"] if isinstance(item, dict)]
    return []


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _tokens(name: str) -> set[str]:
    return {part for part in str(name or "").lower().replace("-", "_").split("_") if part}


def _has_milestone_suffix(step_id: str) -> bool:
    lowered = step_id.lower()
    return any(lowered.endswith(suffix) for suffix in MILESTONE_SUFFIXES)


def _action_name(name: str) -> bool:
    if _has_milestone_suffix(name):
        return False
    if step_class_hint(name) == "tool":
        return True
    return bool(_tokens(name) & set(ACTION_HINTS))


def _intelligence_name(name: str) -> bool:
    if _has_milestone_suffix(name):
        return False
    if step_class_hint(name) == "intelligence":
        return True
    return bool(_tokens(name) & set(INTEL_HINTS))


def _rename_milestone(step_id: str, intelligence: str) -> str:
    if _has_milestone_suffix(step_id):
        return step_id
    if intelligence == "judge" or "judge" in _tokens(step_id):
        return f"{step_id}_decided"
    if intelligence != "none" or _intelligence_name(step_id):
        return f"{step_id}_frozen"
    return "source_ready"


def _schema_payload(schema: dict[str, Any]) -> dict[str, Any]:
    for item in schema.get("allOf") or []:
        if not isinstance(item, dict):
            continue
        then = item.get("then") if isinstance(item.get("then"), dict) else {}
        data = (then.get("properties") or {}).get("data")
        if isinstance(data, dict) and (data.get("properties") or data.get("required")):
            return data
        nested = (item.get("properties") or {}).get("data")
        if isinstance(nested, dict) and (nested.get("properties") or nested.get("required")):
            return nested
    data = (schema.get("properties") or {}).get("data")
    if isinstance(data, dict) and (data.get("properties") or data.get("required")):
        return data
    return schema


def _prop_brief(prop: Any) -> dict[str, Any]:
    if not isinstance(prop, dict):
        return {"type": "object"}
    if "enum" in prop:
        return {"enum": list(prop["enum"])}
    if "const" in prop:
        return {"const": prop["const"]}
    if "$ref" in prop:
        return {"$ref": prop["$ref"]}
    if "type" in prop:
        brief: dict[str, Any] = {"type": prop["type"]}
        if "minLength" in prop:
            brief["minLength"] = prop["minLength"]
        if "pattern" in prop:
            brief["pattern"] = prop["pattern"]
        if "items" in prop:
            brief["items"] = _prop_brief(prop["items"])
        if "minItems" in prop:
            brief["minItems"] = prop["minItems"]
        if "maxItems" in prop:
            brief["maxItems"] = prop["maxItems"]
        return brief
    return {"type": "object"}


def summarize_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"required": [], "properties": {}, "stub": True}
    payload = _schema_payload(schema)
    properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
    required = list(payload.get("required") or [])
    return {
        "required": required,
        "properties": {key: _prop_brief(value) for key, value in properties.items()},
        "additionalProperties": payload.get("additionalProperties", False),
        "stub": is_stub_output_schema(payload) or is_stub_output_schema(schema),
    }


def load_schema_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = read_json(path)
    except (FlowError, OSError, TypeError):
        return None
    return raw if isinstance(raw, dict) else None


def proposed_schema_object(
    *,
    step_id: str,
    kind: str,
    summary: dict[str, Any] | None = None,
    inputs: dict[str, str] | None = None,
) -> dict[str, Any]:
    if summary and summary.get("properties"):
        schema: dict[str, Any] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{step_id}.{kind}.schema.json",
            "type": "object",
            "additionalProperties": bool(summary.get("additionalProperties", False)),
            "required": list(summary.get("required") or []),
            "properties": summary.get("properties") or {},
        }
        return schema
    if kind == "input":
        properties = {
            name: {"$ref": f"{name}.output.schema.json"} if ref != "user.request" else {"type": "object"}
            for name, ref in (inputs or {"request": "user.request"}).items()
        }
        required = list(properties)
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{step_id}.input.schema.json",
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": properties,
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{step_id}.output.schema.json",
        "type": "object",
        "additionalProperties": False,
        "required": [f"{step_id}_sha256"],
        "properties": {
            f"{step_id}_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
    }


def _frontmatter(skill_md: Path) -> dict[str, str]:
    if not skill_md.is_file():
        return {"name": skill_md.parent.name, "description": "", "body": ""}
    text = skill_md.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {"name": skill_md.parent.name, "description": "", "body": text}
    raw = yaml_map(match.group(1))
    return {
        "name": str(raw.get("name") or skill_md.parent.name),
        "description": str(raw.get("description") or "").strip(),
        "body": text[match.end() :].strip(),
    }


def yaml_map(text: str) -> dict[str, Any]:
    try:
        raw = load_yaml_text(text)
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def load_yaml_text(text: str) -> Any:
    import yaml

    return yaml.safe_load(text)


def _public_functions(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    names = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            names.append(node.name)
    return names


def _classify_script(stem: str) -> str:
    if stem in DRIVER_STEMS:
        return "driver"
    if _action_name(stem) or step_class_hint(stem) == "tool":
        return "tool"
    if _intelligence_name(stem):
        return "intelligence"
    return "unknown"


def _linked_flow(body: str) -> Path | None:
    for match in LINKED_FLOW_RE.finditer(body or ""):
        path = Path(match.group(1).rstrip("\\/"))
        if (path / "flow.yaml").is_file() or path.is_dir():
            return path
    return None


def _markdown_milestones(body: str) -> list[str]:
    found: list[str] = []
    for line in (body or "").splitlines():
        match = MILESTONE_LINE_RE.match(line.strip())
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return found


def inventory_target(root: Path) -> dict[str, Any]:
    root = root.resolve()
    meta = _frontmatter(root / "SKILL.md")
    scripts: list[dict[str, Any]] = []
    scripts_dir = root / "scripts"
    if scripts_dir.is_dir():
        for path in sorted(scripts_dir.glob("*.py")):
            stem = path.stem
            kind = _classify_script(stem)
            scripts.append(
                {
                    "id": stem,
                    "path": _rel(root, path),
                    "class": kind,
                    "functions": _public_functions(path),
                    "standardize": kind in {"tool", "intelligence", "unknown"},
                }
            )
    agents: list[dict[str, Any]] = []
    agents_dir = root / "agents"
    if agents_dir.is_dir():
        for path in sorted(list(agents_dir.glob("*.yaml")) + list(agents_dir.glob("*.yml"))):
            raw = load_yaml(path) if path.is_file() else {}
            raw = raw if isinstance(raw, dict) else {}
            interface = raw.get("interface") if isinstance(raw.get("interface"), dict) else {}
            agents.append(
                {
                    "id": str(raw.get("agent_id") or path.stem),
                    "path": _rel(root, path),
                    "role": str(raw.get("role") or interface.get("display_name") or ""),
                }
            )
    contracts: list[dict[str, Any]] = []
    for folder in (root / "contracts", root / "schemas"):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.schema.json")):
            schema = load_schema_file(path)
            contract_id = path.name.replace(".schema.json", "")
            if contract_id in ENVELOPE_CONTRACTS:
                continue
            contracts.append(
                {
                    "id": contract_id,
                    "path": _rel(root, path),
                    "summary": summarize_schema(schema),
                }
            )
    mentioned = sorted(set(SCRIPT_CALL_RE.findall(meta.get("body") or "")))
    workers: list[dict[str, Any]] = []
    refs_dir = root / "references"
    if refs_dir.is_dir():
        for path in sorted(refs_dir.glob("*worker*.md")):
            workers.append({"id": path.stem, "path": _rel(root, path), "class": "worker_doc"})
    mentioned_tools = sorted(set(TOOLBOX_RE.findall(meta.get("body") or "")))
    linked = _linked_flow(meta.get("body") or "")
    return {
        "name": meta["name"],
        "description": meta["description"],
        "path": str(root),
        "scripts": scripts,
        "agents": agents,
        "contracts": contracts,
        "mentioned_scripts": mentioned,
        "mentioned_tools": mentioned_tools,
        "worker_docs": workers,
        "linked_flow": str(linked) if linked else None,
        "markdown_milestones": _markdown_milestones(meta.get("body") or ""),
        "references": sorted(path.name for path in refs_dir.glob("*.md")) if refs_dir.is_dir() else [],
    }


def audit_harness(root: Path) -> dict[str, Any]:
    root = root.resolve()
    flow_path = _find_any_flow(root)
    findings: list[dict[str, str]] = []
    if flow_path is None:
        return {
            "schema": "flowstep_audit_v1",
            "status": "BLOCKED",
            "target": str(root),
            "flow_schema": None,
            "verdict": "NO_FLOW",
            "findings": [{"severity": "P0", "id": "flow", "note": "no flow.yaml or flows/*.yaml"}],
            "steps": [],
            "p0_count": 1,
            "p1_count": 0,
            "milestone_count": 0,
            "location": "codex_skill" if "/.codex/skills/" in root.as_posix().lower() else "codebase",
        }

    raw = load_yaml(flow_path)
    if not isinstance(raw, dict):
        raise FlowError(f"flow must be a mapping: {flow_path}")
    flow_schema = raw.get("schema")
    steps = _step_rows(raw)
    location = "codex_skill" if "/.codex/skills/" in root.as_posix().lower() else "codebase"
    if infer_codebase(root) is not None:
        location = "flowsteps_flow"

    if flow_schema == "flowstep_flow_v1":
        findings.append(
            {
                "severity": "P0",
                "id": "schema",
                "note": "v1 worker/in_process harness; not a milestone+toolbox flow",
            }
        )
    elif flow_schema == FLOW_SCHEMA:
        findings.append(
            {
                "severity": "P1",
                "id": "schema",
                "note": "v2 one-step-one-script; upgrade to v3 milestones that call flowsteps/tools",
            }
        )
    elif flow_schema != FLOW_SCHEMA_V3:
        findings.append({"severity": "P0", "id": "schema", "note": f"unknown schema {flow_schema}"})

    if raw.get("persistent_worker") or raw.get("max_subagent_roles"):
        findings.append(
            {
                "severity": "P0",
                "id": "worker",
                "note": "persistent worker / subagent roles — the worker is the runtime, not the toolbox",
            }
        )
    if int(raw.get("max_run_repair_cycles") or 0) > 0:
        findings.append({"severity": "P1", "id": "repair", "note": "repair loops are forbidden"})

    if location == "codex_skill" and flow_schema == FLOW_SCHEMA_V3:
        findings.append(
            {
                "severity": "P0",
                "id": "location",
                "note": "v3 product flow must live under <repo>/flowsteps/flows/<id>, not .codex/skills",
            }
        )

    instruction = root / "planning" / "flowstep-instruction.md"
    if not instruction.is_file():
        findings.append(
            {
                "severity": "P1",
                "id": "instruction",
                "note": "missing planning/flowstep-instruction.md",
            }
        )

    codebase = infer_codebase(root)
    known_tools = set()
    if codebase is not None and tools_root(codebase).is_dir():
        known_tools = {path.name for path in tools_root(codebase).iterdir() if path.is_dir()}

    step_reports: list[dict[str, Any]] = []
    for item in steps:
        step_id = str(item.get("id") or "")
        mode = (item.get("params") or {}).get("execution_mode") or item.get("execution_mode")
        tools = item.get("tools") if isinstance(item.get("tools"), list) else []
        intel = item.get("intelligence")
        hint = step_class_hint(step_id) if step_id else None
        inputs = item.get("inputs") if isinstance(item.get("inputs"), dict) else {}
        row = {
            "id": step_id,
            "kind": item.get("kind"),
            "class": item.get("class"),
            "mode": mode,
            "handler": item.get("handler"),
            "output_contract": item.get("output_contract"),
            "input_schema": item.get("input_schema"),
            "output_schema": item.get("output_schema"),
            "tools": tools,
            "intelligence": intel if intel is not None else item.get("model", "none"),
            "model": item.get("model"),
            "inputs": inputs,
            "hint": hint,
            "next": item.get("next"),
            "else": item.get("else"),
            "foreach": item.get("foreach"),
            "issues": [],
        }
        if item.get("next"):
            row["next"] = item["next"]
            row["else"] = item.get("else") or "BLOCKED"
        if item.get("foreach"):
            row["foreach"] = item["foreach"]
        if item.get("join"):
            row["join"] = item["join"]
        if hint == "tool":
            row["issues"].append("name is a toolbox action; this should be a tool, not a milestone/step")
            findings.append({"severity": "P0", "id": step_id, "note": row["issues"][-1]})
        if mode == "in_process":
            row["issues"].append("in_process worker step — intelligence without a declared toolbox")
            findings.append({"severity": "P0", "id": step_id, "note": row["issues"][-1]})
        if mode == "local" and not tools:
            runner = (item.get("params") or {}).get("runner") or (item.get("params") or {}).get("handler")
            if runner:
                row["issues"].append(f"local handler {runner} is flow-private; promote reusable work to flowsteps/tools")
                findings.append({"severity": "P1", "id": step_id, "note": row["issues"][-1]})
        if flow_schema == FLOW_SCHEMA_V3:
            if not tools:
                row["issues"].append("milestone has no toolbox")
                findings.append({"severity": "P0", "id": step_id, "note": row["issues"][-1]})
            for tool_id in tools:
                if tool_id not in known_tools:
                    row["issues"].append(f"missing toolbox tool: {tool_id}")
                    findings.append({"severity": "P0", "id": step_id, "note": row["issues"][-1]})
                elif codebase is not None:
                    for err in validate_library_tool(codebase, str(tool_id)):
                        row["issues"].append(err)
                        findings.append({"severity": "P1", "id": step_id, "note": err})
            if intel not in {None, "none"} and not item.get("model_justification"):
                row["issues"].append("intelligence requires model_justification")
                findings.append({"severity": "P0", "id": step_id, "note": row["issues"][-1]})
            if any(step_id.lower().startswith(prefix) for prefix in ("if_", "loop_", "switch_", "when_", "else_")):
                row["issues"].append("if/loop/switch are schema gates, not milestones")
                findings.append({"severity": "P0", "id": step_id, "note": row["issues"][-1]})
            if item.get("next") and not item.get("else"):
                row["issues"].append("next requires else (milestone id or BLOCKED)")
                findings.append({"severity": "P0", "id": step_id, "note": row["issues"][-1]})
            if item.get("foreach") and intel not in {None, "none"}:
                row["issues"].append("foreach is schema control; intelligence cannot own the loop")
                findings.append({"severity": "P0", "id": step_id, "note": row["issues"][-1]})
        if not item.get("output_contract"):
            row["issues"].append("no output_contract")
            findings.append({"severity": "P0", "id": step_id, "note": row["issues"][-1]})
        step_reports.append(row)

    p0 = [item for item in findings if item["severity"] == "P0"]
    status = "PASS" if not p0 else "FINDINGS"
    if flow_schema == FLOW_SCHEMA_V3 and not p0:
        status = "PASS"
    elif flow_schema != FLOW_SCHEMA_V3:
        status = "FINDINGS"

    return {
        "schema": "flowstep_audit_v1",
        "status": status,
        "target": str(root),
        "flow_path": str(flow_path),
        "flow_id": raw.get("flow_id"),
        "flow_schema": flow_schema,
        "location": location,
        "milestone_count": len(steps),
        "steps": step_reports,
        "findings": findings,
        "p0_count": len(p0),
        "p1_count": len(findings) - len(p0),
        "verdict": (
            "MILESTONE_TOOLBOX"
            if flow_schema == FLOW_SCHEMA_V3 and not p0
            else "NEEDS_UPGRADE"
        ),
    }


def _schema_from_step(root: Path, relative: str | None) -> dict[str, Any] | None:
    if not relative:
        return None
    return load_schema_file(root / relative)


def _is_action_step(step: dict[str, Any]) -> bool:
    step_id = str(step.get("id") or "")
    if _has_milestone_suffix(step_id):
        return False
    if step.get("hint") == "tool" or _action_name(step_id):
        return True
    if str(step.get("class") or "") == "tool" and not _has_milestone_suffix(step_id):
        return True
    return False


def _intel_value(step: dict[str, Any]) -> str:
    intel = step.get("intelligence")
    if intel in {"completion", "image", "judge"}:
        return str(intel)
    model = step.get("model")
    if model in {"completion", "image", "judge"}:
        return str(model)
    if str(step.get("class") or "") == "intelligence" or _intelligence_name(str(step.get("id") or "")):
        return "completion"
    return "none"


def _attach_schemas(root: Path, milestone: dict[str, Any]) -> dict[str, Any]:
    input_raw = _schema_from_step(root, milestone.get("input_schema_path"))
    output_raw = _schema_from_step(root, milestone.get("output_schema_path"))
    input_summary = summarize_schema(input_raw) if input_raw else None
    output_summary = summarize_schema(output_raw) if output_raw else None
    milestone["input_schema"] = proposed_schema_object(
        step_id=milestone["id"],
        kind="input",
        summary=input_summary,
        inputs=milestone.get("inputs") or {"request": "user.request"},
    )
    milestone["output_schema"] = proposed_schema_object(
        step_id=milestone["id"],
        kind="output",
        summary=output_summary,
    )
    return milestone


def _python_tool_row(
    *,
    current: str,
    tool_id: str,
    source: str,
    reason: str,
    already: bool = False,
) -> dict[str, str]:
    return {
        "current": current,
        "tool_id": tool_id,
        "destination": f"flowsteps/tools/{tool_id}/",
        "source": source,
        "action": "already_python" if already else "standardize_to_python",
        "reason": reason,
    }


DEFAULT_INTEL_TOOLS = ("hash_bind", "schema_validate")


def _inputs_point_at_milestones(inputs: dict[str, str], known: set[str]) -> bool:
    if not inputs:
        return False
    for name, ref in inputs.items():
        if ref == "user.request":
            continue
        upstream = str(ref).split(".", 1)[0]
        if upstream not in known and name not in known:
            return False
    return True


def _rechain_milestones(root: Path, milestones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    known = {item["id"] for item in milestones}
    previous = None
    for item in milestones:
        current = item.get("inputs") if isinstance(item.get("inputs"), dict) else {}
        if previous is None:
            item["inputs"] = current or {"request": "user.request"}
        elif not _inputs_point_at_milestones(current, known):
            item["inputs"] = {previous["id"]: f"{previous['id']}.{previous['output_contract']}"}
            item["input_schema_path"] = None
            item["input_schema"] = proposed_schema_object(
                step_id=item["id"],
                kind="input",
                inputs=item["inputs"],
            )
        previous = item
    return milestones


def _enum_values(prop: Any) -> list[str]:
    if not isinstance(prop, dict):
        return []
    if "enum" in prop and isinstance(prop["enum"], list):
        return [str(item) for item in prop["enum"] if item is not None and not isinstance(item, (dict, list))]
    if "const" in prop and prop["const"] is not None and not isinstance(prop["const"], (dict, list)):
        return [str(prop["const"])]
    return []


def _match_branch(candidate_ids: list[str], value: str) -> str | None:
    token = str(value).lower().replace("-", "_")
    matches = [mid for mid in candidate_ids if token in _tokens(mid)]
    return matches[0] if matches else None


def _schema_properties(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties")
    return props if isinstance(props, dict) else {}


def infer_schema_control(milestones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Emit next/foreach from JSON Schema only. Never from a model verdict."""
    ids = [str(item["id"]) for item in milestones]
    by_id = {item["id"]: item for item in milestones}
    for index, item in enumerate(milestones):
        later = ids[index + 1 :]
        if not item.get("next"):
            props = _schema_properties(item.get("output_schema"))
            for field, prop in props.items():
                values = _enum_values(prop)
                if len(values) < 2:
                    continue
                edges = []
                for value in values:
                    then = _match_branch(later, value)
                    if not then:
                        edges = []
                        break
                    gate_rel = f"schemas/gates/{field}_{value}.schema.json"
                    edges.append(
                        {
                            "when": gate_rel,
                            "then": then,
                            "schema": {
                                "$schema": "https://json-schema.org/draft/2020-12/schema",
                                "$id": f"{field}_{value}.gate.schema.json",
                                "type": "object",
                                "additionalProperties": True,
                                "required": [field],
                                "properties": {field: {"const": value}},
                            },
                        }
                    )
                if len(edges) >= 2 and len({edge["then"] for edge in edges}) == len(edges):
                    item["next"] = edges
                    item["else"] = "BLOCKED"
                    branch_ids = [edge["then"] for edge in edges]
                    last_branch = max(ids.index(mid) for mid in branch_ids)
                    for mid in ids[last_branch + 1 :]:
                        if mid not in branch_ids and not by_id[mid].get("join"):
                            by_id[mid]["join"] = branch_ids
                            break
                    break
        if item.get("foreach") or item.get("intelligence") not in {None, "none"}:
            continue
        if index == 0:
            continue
        prev_props = _schema_properties(milestones[index - 1].get("output_schema"))
        tokens = _tokens(item["id"])
        for path, prop in prev_props.items():
            if not isinstance(prop, dict) or prop.get("type") != "array":
                continue
            if prop.get("maxItems") is None:
                continue
            if path not in tokens and path.rstrip("s") not in tokens:
                continue
            items_schema = prop.get("items") if isinstance(prop.get("items"), dict) else {"type": "object"}
            stem = path.rstrip("s") or path
            item["foreach"] = {
                "path": path,
                "item_schema": f"schemas/{stem}_item_v1.json",
                "tools": list(item.get("tools") or ["hash_bind"]),
                "max_items": int(prop["maxItems"]),
                "collect": path,
                "item_schema_object": items_schema,
            }
            break
    return milestones


def control_table(milestones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in milestones:
        if item.get("next"):
            rows.append(
                {
                    "milestone": item["id"],
                    "kind": "gate",
                    "criterion": "json_schema",
                    "else": item.get("else") or "BLOCKED",
                    "edges": [
                        {"when": edge.get("when"), "then": edge.get("then")}
                        for edge in item["next"]
                    ],
                }
            )
        if item.get("foreach"):
            fe = item["foreach"]
            rows.append(
                {
                    "milestone": item["id"],
                    "kind": "foreach",
                    "criterion": "json_schema",
                    "path": fe.get("path"),
                    "max_items": fe.get("max_items"),
                    "item_schema": fe.get("item_schema"),
                    "tools": list(fe.get("tools") or []),
                }
            )
    return rows


def _ensure_intel_toolbox(
    milestones: list[dict[str, Any]],
    python_tools: list[dict[str, str]],
) -> list[dict[str, str]]:
    for item in milestones:
        if item.get("intelligence") in {None, "none"}:
            continue
        if item.get("tools"):
            continue
        item["tools"] = list(DEFAULT_INTEL_TOOLS)
        for tool_id in DEFAULT_INTEL_TOOLS:
            python_tools.append(
                _python_tool_row(
                    current=tool_id,
                    tool_id=tool_id,
                    source=f"suggested:{item['id']}",
                    reason="intelligence milestone still needs a typed toolbox (hash/schema), not a free-form worker",
                )
            )
    return _unique_tools(python_tools)


def propose_from_flow(root: Path, grade: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    milestones: list[dict[str, Any]] = []
    python_tools: list[dict[str, str]] = []
    pending: list[dict[str, Any]] = []
    previous_id: str | None = None

    def flush_pending(into: dict[str, Any]) -> None:
        tools = list(into.get("tools") or [])
        for item in pending:
            tool_id = str(item.get("id") or "tool")
            if tool_id not in tools:
                tools.append(tool_id)
            python_tools.append(
                _python_tool_row(
                    current=tool_id,
                    tool_id=tool_id,
                    source=str(item.get("handler") or item.get("id")),
                    reason="action-named unit; toolbox function, not a milestone",
                )
            )
        into["tools"] = tools
        pending.clear()

    def emit_source_from_pending() -> None:
        nonlocal previous_id
        if not pending:
            return
        last = pending[-1]
        first = pending[0]
        row = {
            "id": "source_ready",
            "from_step": last.get("id"),
            "intelligence": "none",
            "tools": [],
            "output_contract": last.get("output_contract") or "source_ready_v1",
            "inputs": first.get("inputs") or {"request": "user.request"},
            "input_schema_path": first.get("input_schema"),
            "output_schema_path": last.get("output_schema"),
            "inspects": "bound source / first typed payload",
        }
        flush_pending(row)
        milestones.append(_attach_schemas(root, row))
        previous_id = "source_ready"

    for step in grade.get("steps") or []:
        if _is_action_step(step):
            pending.append(step)
            continue
        if pending and not milestones:
            emit_source_from_pending()
        intel = _intel_value(step)
        milestone_id = _rename_milestone(str(step["id"]), intel)
        declared_tools = [str(item) for item in (step.get("tools") or [])]
        for tool_id in declared_tools:
            python_tools.append(
                _python_tool_row(
                    current=tool_id,
                    tool_id=tool_id,
                    source=f"flow:{step['id']}.tools",
                    reason="already listed on the milestone; keep as standardized Python toolbox",
                    already=True,
                )
            )
        row = {
            "id": milestone_id,
            "from_step": step["id"],
            "intelligence": intel,
            "tools": declared_tools,
            "output_contract": step.get("output_contract") or f"{milestone_id}_v1",
            "inputs": step.get("inputs")
            or ({"request": "user.request"} if previous_id is None else {previous_id: f"{previous_id}_v1"}),
            "input_schema_path": step.get("input_schema"),
            "output_schema_path": step.get("output_schema"),
            "inspects": f"PASS payload `{step.get('output_contract') or milestone_id}`",
        }
        if step.get("next"):
            row["next"] = step["next"]
            row["else"] = step.get("else") or "BLOCKED"
        if step.get("foreach"):
            row["foreach"] = step["foreach"]
        if step.get("join"):
            row["join"] = step["join"]
        flush_pending(row)
        if previous_id is None:
            row["inputs"] = step.get("inputs") or {"request": "user.request"}
        elif not step.get("inputs") and milestones:
            prev = milestones[-1]
            row["inputs"] = {prev["id"]: f"{prev['id']}.{prev['output_contract']}"}
        milestones.append(_attach_schemas(root, row))
        previous_id = milestone_id

    if pending and milestones:
        flush_pending(milestones[-1])
    elif pending:
        last = pending[-1]
        row = {
            "id": "source_ready",
            "from_step": last.get("id"),
            "intelligence": "none",
            "tools": [],
            "output_contract": last.get("output_contract") or "source_ready_v1",
            "inputs": {"request": "user.request"},
            "input_schema_path": pending[0].get("input_schema"),
            "output_schema_path": last.get("output_schema"),
            "inspects": "bound source / first typed payload",
        }
        flush_pending(row)
        milestones.append(_attach_schemas(root, row))
    return _rechain_milestones(root, milestones), _unique_tools(python_tools)


def _unique_tools(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for row in rows:
        key = row["tool_id"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _contract_milestone_id(contract_id: str) -> str:
    lowered = contract_id.lower()
    for pattern, milestone_id in CONTRACT_MILESTONE:
        if pattern.search(lowered):
            return milestone_id
    return contract_id.rstrip("0123456789").rstrip("_v") or contract_id


def _bucket_for(name: str) -> str | None:
    tokens = _tokens(name)
    for milestone_id, hints, _intel in BUCKETS:
        if tokens & set(hints):
            return milestone_id
    return None


def propose_from_inventory(root: Path, inventory: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    python_tools: list[dict[str, str]] = []
    for script in inventory.get("scripts") or []:
        if not script.get("standardize"):
            continue
        reason = (
            "intelligence wrapper still needs a typed Python tool.py"
            if script["class"] == "intelligence"
            else "skill-private script should become a reusable toolbox function"
        )
        python_tools.append(
            _python_tool_row(
                current=script["path"],
                tool_id=script["id"],
                source=script["path"],
                reason=reason,
            )
        )

    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def ensure(milestone_id: str, *, intelligence: str, contract: str) -> dict[str, Any]:
        if milestone_id not in grouped:
            grouped[milestone_id] = {
                "id": milestone_id,
                "from_step": None,
                "intelligence": intelligence,
                "tools": [],
                "output_contract": contract,
                "inputs": {},
                "input_schema_path": None,
                "output_schema_path": None,
                "inspects": f"PASS payload `{contract}`",
            }
            order.append(milestone_id)
        return grouped[milestone_id]

    for contract in inventory.get("contracts") or []:
        milestone_id = _contract_milestone_id(contract["id"])
        intel = "none"
        for bid, _hints, bucket_intel in BUCKETS:
            if bid == milestone_id:
                intel = bucket_intel
        row = ensure(milestone_id, intelligence=intel, contract=contract["id"])
        row["output_contract"] = contract["id"]
        row["output_schema_path"] = contract["path"]

    for tool_id in inventory.get("mentioned_tools") or []:
        python_tools.append(
            _python_tool_row(
                current=tool_id,
                tool_id=tool_id,
                source="SKILL.md toolbox mention",
                reason="already named as a repo toolbox function",
                already=True,
            )
        )

    for milestone_id in inventory.get("markdown_milestones") or []:
        intel = "none"
        for bid, _hints, bucket_intel in BUCKETS:
            if bid == milestone_id:
                intel = bucket_intel
        if _intelligence_name(milestone_id):
            intel = "completion"
        ensure(milestone_id, intelligence=intel, contract=f"{milestone_id}_v1")

    if not grouped:
        for script in inventory.get("scripts") or []:
            if not script.get("standardize"):
                continue
            milestone_id = _bucket_for(script["id"]) or "work_bound"
            intel = "completion" if script["class"] == "intelligence" else "none"
            if milestone_id == "work_bound":
                intel = "none"
            row = ensure(milestone_id, intelligence=intel, contract=f"{milestone_id}_v1")
            if script["id"] not in row["tools"]:
                row["tools"].append(script["id"])
    else:
        for script in inventory.get("scripts") or []:
            if not script.get("standardize"):
                continue
            milestone_id = _bucket_for(script["id"])
            if milestone_id:
                intel = next(item[2] for item in BUCKETS if item[0] == milestone_id)
                row = ensure(milestone_id, intelligence=intel, contract=f"{milestone_id}_v1")
                if script["id"] not in row["tools"]:
                    row["tools"].append(script["id"])
            elif order and script["id"] not in grouped[order[0]]["tools"]:
                grouped[order[0]]["tools"].append(script["id"])

    for agent in inventory.get("agents") or []:
        agent_id = str(agent.get("id") or "")
        if agent_id in {"openai"}:
            continue
        milestone_id = _bucket_for(agent_id)
        if milestone_id and milestone_id in grouped and grouped[milestone_id]["intelligence"] == "none":
            if any(token in agent_id for token in ("judge", "review")):
                grouped[milestone_id]["intelligence"] = "judge"
            elif any(token in agent_id for token in ("prompt", "caption", "plan", "worker")):
                grouped[milestone_id]["intelligence"] = "completion"

    milestones: list[dict[str, Any]] = []
    previous = None
    for milestone_id in order:
        row = grouped[milestone_id]
        if previous is None:
            row["inputs"] = {"request": "user.request"}
        else:
            row["inputs"] = {previous["id"]: f"{previous['id']}.{previous['output_contract']}"}
        milestones.append(_attach_schemas(root, row))
        previous = row
    milestones = _rechain_milestones(root, milestones)
    if not milestones:
        row = {
            "id": "source_ready",
            "from_step": None,
            "intelligence": "none",
            "tools": [item["tool_id"] for item in python_tools],
            "output_contract": "source_ready_v1",
            "inputs": {"request": "user.request"},
            "input_schema_path": None,
            "output_schema_path": None,
            "inspects": "first typed payload a human would check",
        }
        milestones.append(_attach_schemas(root, row))
    return milestones, _unique_tools(python_tools)


def goal_text(inventory: dict[str, Any], grade: dict[str, Any], milestones: list[dict[str, Any]]) -> str:
    name = inventory.get("name") or Path(grade["target"]).name
    last = milestones[-1]["output_contract"] if milestones else "the final payload"
    split = " → ".join(f"`{item['id']}`" for item in milestones) or "`source_ready`"
    if grade.get("verdict") == "NO_FLOW":
        shape = "This skill has no FlowStep YAML; work lives in markdown, workers, and scripts."
    elif grade.get("flow_schema") == "flowstep_flow_v1":
        shape = "Current shape is a v1 in-process worker graph (n8n-style actions / persistent workers)."
    elif grade.get("flow_schema") == FLOW_SCHEMA:
        shape = "Current shape is v2 one-script-per-step. Fold action scripts into milestone toolboxes."
    elif grade.get("flow_schema") == FLOW_SCHEMA_V3:
        shape = "Current shape is v3 milestones. Keep the checkpoints; finish Python toolbox coverage."
    else:
        shape = "Current shape is not a milestone + toolbox harness."
    n_tools = len({item["id"] for item in inventory.get("scripts") or [] if item.get("standardize")})
    return (
        f"Separate `{name}` so each human-inspectable outcome is one milestone "
        f"({split}). {shape} Promote reusable current units "
        f"({n_tools} skill script(s) plus any action-named steps) to standardized "
        f"Python tools under `flowsteps/tools/<tool_id>/` with typed input/output "
        f"schemas. Intelligence may exist only *inside* a milestone and may only "
        f"call that milestone's toolbox. Final payload: `{last}`."
    )


def audit_skill(root: Path) -> dict[str, Any]:
    root = root.resolve()
    inventory = inventory_target(root)
    grade_root = Path(inventory["linked_flow"]).resolve() if inventory.get("linked_flow") else root
    grade = audit_harness(grade_root)
    if grade.get("steps"):
        milestones, python_tools = propose_from_flow(root, grade)
        if grade.get("flow_schema") != FLOW_SCHEMA_V3:
            extra_ms, extra_tools = propose_from_inventory(root, inventory)
            known = {item["id"] for item in milestones}
            for item in extra_ms:
                if item["id"] not in known:
                    milestones.append(item)
            python_tools = _unique_tools(python_tools + extra_tools)
        else:
            extra_tools = [
                _python_tool_row(
                    current=script["path"],
                    tool_id=script["id"],
                    source=script["path"],
                    reason="skill-private script still outside the repo toolbox",
                )
                for script in inventory.get("scripts") or []
                if script.get("standardize")
            ]
            python_tools = _unique_tools(python_tools + extra_tools)
    else:
        milestones, python_tools = propose_from_inventory(root, inventory)
    python_tools = _ensure_intel_toolbox(milestones, python_tools)
    milestones = _rechain_milestones(root, milestones)
    milestones = infer_schema_control(milestones)

    current_tools = []
    for script in inventory.get("scripts") or []:
        current_tools.append(
            {
                "id": script["id"],
                "kind": "script",
                "class": script["class"],
                "path": script["path"],
                "functions": script.get("functions") or [],
            }
        )
    for agent in inventory.get("agents") or []:
        current_tools.append(
            {
                "id": agent["id"],
                "kind": "agent",
                "class": "intelligence" if agent["id"] != "openai" else "interface",
                "path": agent["path"],
                "functions": [],
            }
        )
    for worker in inventory.get("worker_docs") or []:
        current_tools.append(
            {
                "id": worker["id"],
                "kind": "worker_doc",
                "class": "intelligence",
                "path": worker["path"],
                "functions": [],
            }
        )
    for tool_id in inventory.get("mentioned_tools") or []:
        current_tools.append(
            {
                "id": tool_id,
                "kind": "declared_toolbox",
                "class": "tool",
                "path": f"flowsteps/tools/{tool_id}/",
                "functions": ["run"],
            }
        )
    for step in grade.get("steps") or []:
        for tool_id in step.get("tools") or []:
            current_tools.append(
                {
                    "id": tool_id,
                    "kind": "declared_toolbox",
                    "class": "tool",
                    "path": f"flowsteps/tools/{tool_id}/",
                    "functions": ["run"],
                }
            )
        if step.get("handler"):
            current_tools.append(
                {
                    "id": str(step["id"]),
                    "kind": "flow_handler",
                    "class": step.get("class") or "tool",
                    "path": str(step["handler"]),
                    "functions": [],
                }
            )

    return {
        "schema": AUDIT_SCHEMA,
        "status": grade["status"] if grade.get("verdict") != "NO_FLOW" else "FINDINGS",
        "verdict": grade.get("verdict") or "NEEDS_UPGRADE",
        "target": str(root),
        "audited_at": utc_now(),
        "audited_skill": {
            "name": inventory["name"],
            "description": inventory["description"],
            "path": inventory["path"],
            "linked_flow": inventory.get("linked_flow"),
            "references": inventory.get("references") or [],
        },
        "grade": grade,
        "goal": goal_text(inventory, grade, milestones),
        "current_tools": current_tools,
        "proposed_milestones": milestones,
        "control": control_table(milestones),
        "python_standardization": python_tools,
        "tool_vs_intelligence": classification_from_audit(
            {
                "audited_skill": {"name": inventory["name"]},
                "grade": grade,
                "proposed_milestones": milestones,
                "python_standardization": python_tools,
            }
        ),
        "contracts": inventory.get("contracts") or [],
        "agents": inventory.get("agents") or [],
        "scripts": inventory.get("scripts") or [],
    }


def _json_fence(value: Any) -> str:
    return "```json\n" + json.dumps(value, indent=2, ensure_ascii=False) + "\n```"


def render_audit_markdown(report: dict[str, Any]) -> str:
    if report.get("schema") != AUDIT_SCHEMA:
        # legacy grade-only report
        lines = [
            f"# FlowStep audit: {report.get('flow_id') or report['target']}",
            "",
            f"- status: {report['status']}",
            f"- verdict: {report['verdict']}",
            f"- flow_schema: {report.get('flow_schema')}",
            f"- location: {report.get('location')}",
            f"- P0: {report.get('p0_count')}  P1: {report.get('p1_count')}",
            "",
            "## Findings",
            "",
        ]
        if not report.get("findings"):
            lines.append("None. This harness matches milestone + toolbox doctrine.")
        for item in report.get("findings") or []:
            lines.append(f"- **{item['severity']}** `{item['id']}`: {item['note']}")
        lines.extend(["", "## Units", ""])
        for step in report.get("steps") or []:
            issues = "; ".join(step.get("issues") or []) or "ok"
            lines.append(f"- `{step['id']}` mode={step.get('mode')} tools={step.get('tools')} — {issues}")
        lines.append("")
        return "\n".join(lines)

    skill = report["audited_skill"]
    grade = report.get("grade") or {}
    lines = [
        f"# FlowStep skill audit: {skill['name']}",
        "",
        f"- audited_skill: `{skill['name']}`",
        f"- path: `{skill['path']}`",
        f"- linked_flow: `{report.get('grade', {}).get('target') or skill['path']}`",
        f"- audited_at: {report.get('audited_at')}",
        f"- current_flow_schema: `{grade.get('flow_schema')}`",
        f"- flow_id: `{grade.get('flow_id')}`",
        f"- location: `{grade.get('location')}`",
        f"- verdict: `{report['verdict']}`",
        f"- status: `{report['status']}`",
        f"- P0: {grade.get('p0_count', 0)}  P1: {grade.get('p1_count', 0)}",
        "",
        "## Audited skill",
        "",
        skill.get("description") or "(no SKILL.md description)",
        "",
        "## Goal",
        "",
        report["goal"],
        "",
        "## Tool vs intelligence",
        "",
        "Schema: `tool_vs_intelligence_table_v1`. Classify before generate.",
        "",
        render_classification_markdown(report.get("tool_vs_intelligence") or {"rows": []}),
        "",
        "## Current tools",
        "",
        "| Unit | Kind | Class | Path |",
        "| --- | --- | --- | --- |",
    ]
    if not report.get("current_tools"):
        lines.append("| (none found) | | | |")
    for item in report.get("current_tools") or []:
        lines.append(
            f"| `{item['id']}` | {item['kind']} | `{item['class']}` | `{item['path']}` |"
        )
    lines.extend(
        [
            "",
            "## Proposed milestone split",
            "",
            "The M8M flowchart (gates and foreach) is `planning/m8m-flowchart.md`.",
            "",
            "| # | Milestone | Intelligence | Python tools | Output contract | Human inspects |",
            "| ---: | --- | --- | --- | --- | --- |",
        ]
    )
    for index, item in enumerate(report.get("proposed_milestones") or [], start=1):
        tools = ", ".join(f"`{tool}`" for tool in item.get("tools") or []) or "none"
        lines.append(
            f"| {index} | `{item['id']}` | `{item.get('intelligence') or 'none'}` | {tools} | "
            f"`{item['output_contract']}` | {item.get('inspects') or ''} |"
        )
    lines.extend(
        [
            "",
            "## Tools to standardize to Python",
            "",
            "| Current unit | Proposed tool_id | Destination | Action | Why |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    if not report.get("python_standardization"):
        lines.append("| (none) | | | | |")
    for item in report.get("python_standardization") or []:
        lines.append(
            f"| `{item['current']}` | `{item['tool_id']}` | `{item['destination']}` | "
            f"`{item['action']}` | {item['reason']} |"
        )
    lines.extend(
        [
            "",
            "## Schema control",
            "",
            "If/else and loop are JSON Schema predicates (`schema_validate`), never semantic approval.",
            "",
        ]
    )
    control = report.get("control") or []
    if not control:
        lines.append("None. Linear chain; no enum/array gate on the proposed output schemas.")
    else:
        lines.extend(
            [
                "| Milestone | Kind | Criterion | Detail |",
                "| --- | --- | --- | --- |",
            ]
        )
        for item in control:
            if item.get("kind") == "gate":
                detail = "; ".join(
                    f"{edge.get('when')} → `{edge.get('then')}`" for edge in item.get("edges") or []
                )
                detail = f"{detail}; else `{item.get('else')}`"
            else:
                detail = (
                    f"path `{item.get('path')}` max_items={item.get('max_items')} "
                    f"item_schema `{item.get('item_schema')}`"
                )
            lines.append(
                f"| `{item.get('milestone')}` | `{item.get('kind')}` | `{item.get('criterion')}` | {detail} |"
            )
    lines.extend(["", "## FlowStep input and output schemas", ""])
    for index, item in enumerate(report.get("proposed_milestones") or [], start=1):
        inputs = ", ".join(f"{name}={ref}" for name, ref in (item.get("inputs") or {}).items()) or "request=user.request"
        lines.extend(
            [
                f"### {index}. `{item['id']}`",
                "",
                f"- intelligence: `{item.get('intelligence') or 'none'}`",
                f"- toolbox: {', '.join(f'`{tool}`' for tool in item.get('tools') or []) or 'none'}",
                f"- inputs: {inputs}",
                f"- output_contract: `{item['output_contract']}`",
                "",
                "**Input schema**",
                "",
                _json_fence(item.get("input_schema") or {}),
                "",
                "**Output schema**",
                "",
                _json_fence(item.get("output_schema") or {}),
                "",
            ]
        )
    lines.extend(["## Current harness grade", ""])
    findings = grade.get("findings") or []
    if not findings:
        lines.append("None. Existing flow already matches milestone + toolbox doctrine.")
    for item in findings:
        lines.append(f"- **{item['severity']}** `{item['id']}`: {item['note']}")
    lines.append("")
    lines.append("This file is an audit. It does not rewrite the target skill.")
    lines.append("")
    return "\n".join(lines)


def default_report_path(root: Path) -> Path:
    return root / DEFAULT_REPORT


def write_audit_markdown(report: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_audit_markdown(report), encoding="utf-8", newline="\n")
    skill = report.get("audited_skill") if isinstance(report.get("audited_skill"), dict) else {}
    write_flowchart(
        path.parent.parent,
        report.get("proposed_milestones") or [],
        title=str(skill.get("name") or path.parent.parent.name),
        flow_id=str((report.get("grade") or {}).get("flow_id") or ""),
        source="audit",
    )
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_harness_location_args(parser)
    parser.add_argument("--target", type=Path, help="Skill or flow directory to audit (read-only besides the report).")
    parser.add_argument(
        "--write-report",
        type=Path,
        help="Markdown report path. Default: <target>/planning/flowstep-audit.md",
    )
    parser.add_argument("--no-write", action="store_true", help="Print JSON only; do not write the markdown report.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.target:
            root = args.target.resolve()
        else:
            root = harness_dir_from_args(args)
        report = audit_skill(root)
    except FlowError as exc:
        print(json.dumps({"status": "BLOCKED", "blockers": [str(exc)]}, indent=2), file=sys.stderr)
        return 2
    report_path = None
    if not args.no_write:
        report_path = (args.write_report or default_report_path(root)).resolve()
        write_audit_markdown(report, report_path)
        json_path = report_path.with_suffix(".json")
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        report["report_path"] = str(report_path)
        report["report_json_path"] = str(json_path)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["verdict"] == "MILESTONE_TOOLBOX" else 3


if __name__ == "__main__":
    raise SystemExit(main())
