"""Generate a v3 milestone flow: seed toolbox, audit-driven YAML, product skill."""

from __future__ import annotations

import argparse
import json
import shutil
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
from tool_vs_intelligence import from_audit as classification_from_audit
from tool_vs_intelligence import from_flow as classification_from_flow
from tool_vs_intelligence import render_markdown as render_classification_markdown


BUILDER_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = BUILDER_ROOT / "templates"
DEFAULT_BUILDER = BUILDER_ROOT
SEEDS_DIR = BUILDER_ROOT / "seeds"


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
    root = Path(codebase).resolve()
    if root.as_posix().lower().find("/.codex/skills") >= 0 or "\\.codex\\skills" in str(root).lower():
        raise FlowError("--codebase must be the repo root, not .codex/skills")
    dest = tools_root(root) / tool_id
    dest.mkdir(parents=True, exist_ok=True)
    if seed_path(tool_id) is not None:
        written = _copy_seed(root, tool_id, overwrite=overwrite)
        return {
            "schema": "flowstep_tool_generate_v3",
            "status": "PASS",
            "tool_id": tool_id,
            "tool_dir": str(dest),
            "seeded": True,
            "written": written,
        }
    written = _write_step_package(dest, tool_id, previous_id=None, overwrite=overwrite)
    return {
        "schema": "flowstep_tool_generate_v3",
        "status": "FINDINGS",
        "tool_id": tool_id,
        "tool_dir": str(dest),
        "seeded": False,
        "note": "no premade seed; tool.py is a stub until a fixture implementation exists",
        "written": written,
    }


ASSET_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["asset"],
    "properties": {
        "asset": {
            "type": "object",
            "additionalProperties": False,
            "required": ["path", "sha256"],
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
        }
    },
}

PASSTHROUGH_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
}


def _write_json(path: Path, value: Any, *, overwrite: bool) -> bool:
    return _write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n", overwrite=overwrite)


def _asset_schema(schema: dict[str, Any] | None) -> bool:
    if not isinstance(schema, dict):
        return False
    props = schema.get("properties") or {}
    return "asset" in props


def generate_v3_flow(
    codebase: Path,
    flow_id: str,
    milestones: list[str],
    *,
    tools: list[str],
    intelligence: list[str] | None = None,
    overwrite: bool = False,
    milestone_specs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    harness = resolve_harness_dir(codebase=codebase, flow_id=flow_id)
    assert_product_harness_location(harness)
    if milestone_specs:
        milestones = [str(item["id"]) for item in milestone_specs]
    if not milestones:
        raise FlowError("pass at least one --milestone")
    if not tools and not milestone_specs:
        raise FlowError("a milestone flow requires --tools (the pre-made toolbox)")
    intel = set(intelligence or [])
    unknown = sorted(intel - set(milestones))
    if unknown:
        raise FlowError(f"--intelligence names unknown milestones: {unknown}")
    for mid in milestones:
        if not STEP_ID_RE.match(mid):
            raise FlowError(f"invalid milestone id: {mid}")
        if any(mid.startswith(prefix) for prefix in ("if_", "loop_", "switch_", "when_", "else_")):
            raise FlowError(f"{mid}: if/loop/switch are schema gates, not milestones")
        if step_class_hint(mid) == "tool":
            raise FlowError(f"{mid}: use --tool for crop/fetch/hash; milestones are checkpoints")
    for tool_id in sorted({*(tools or []), *[str(t) for spec in (milestone_specs or []) for t in (spec.get("tools") or [])]}):
        if tool_id:
            generate_tool(codebase, tool_id, overwrite=overwrite)
    spec_by_id = {str(item["id"]): item for item in (milestone_specs or [])}
    items = []
    previous = None
    for index, mid in enumerate(milestones):
        spec = spec_by_id.get(mid) or {}
        step_tools = list(spec.get("tools") or tools)
        if not step_tools:
            step_tools = ["hash_bind"]
        is_last = index == len(milestones) - 1
        if is_last and "hash_bind" not in step_tools:
            step_tools.append("hash_bind")
        intel_value = spec.get("intelligence") or ("completion" if mid in intel else "none")
        item: dict[str, Any] = {
            "id": mid,
            "output_contract": spec.get("output_contract") or f"{mid}_v1",
            "output_schema": f"schemas/{mid}_v1.json",
            "input_schema": f"milestones/{mid}/input.schema.json",
            "tools": step_tools,
            "intelligence": intel_value,
            "handler": f"milestones/{mid}/assemble.py",
            "test": f"milestones/{mid}/tests/test_assemble.py",
            "_output_schema_object": spec.get("output_schema_object"),
            "_input_schema_object": spec.get("input_schema_object"),
            "_is_last": is_last,
        }
        if previous is None:
            item["inputs"] = spec.get("inputs") or {"request": "user.request"}
        else:
            item["inputs"] = spec.get("inputs") or {
                previous["id"]: f"{previous['id']}.{previous['output_contract']}"
            }
        if intel_value != "none":
            item["model_justification"] = spec.get("model_justification") or "judgment that is not a typed transform"
            item["draft_schema"] = f"milestones/{mid}/draft.schema.json"
        if spec.get("next"):
            edges = []
            gate_schemas: dict[str, Any] = dict(spec.get("_gate_schemas") or {})
            for edge in spec["next"]:
                public_edge = {"when": edge["when"], "then": edge["then"]}
                edges.append(public_edge)
                if edge.get("schema"):
                    gate_schemas[str(edge["when"])] = edge["schema"]
            item["next"] = edges
            item["else"] = spec.get("else") or "BLOCKED"
            item["_gate_schemas"] = gate_schemas
        if spec.get("foreach"):
            fe = dict(spec["foreach"])
            item["_item_schema_object"] = fe.pop("item_schema_object", None)
            item["foreach"] = fe
        if spec.get("join"):
            item["join"] = spec["join"]
            item["_input_schema_object"] = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": True,
            }
        items.append(item)
        previous = item
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
    public_items = []
    for item in items:
        public = {key: value for key, value in item.items() if not key.startswith("_")}
        public_items.append(public)
    flow_public = dict(flow)
    flow_public["milestones"] = public_items
    if _write_text(flow_path, yaml_dump_v3(flow_public), overwrite=overwrite or not flow_path.exists()):
        created.append(str(flow_path))
    previous_id = None
    for item in items:
        mid = item["id"]
        if item["_is_last"]:
            output_obj = ASSET_OUTPUT_SCHEMA
        elif isinstance(item.get("_output_schema_object"), dict) and not _asset_schema(
            item["_output_schema_object"]
        ):
            output_obj = item["_output_schema_object"]
            output_obj.setdefault("$id", f"{mid}.output.schema.json")
        else:
            output_obj = dict(PASSTHROUGH_SCHEMA)
            output_obj["$id"] = f"{mid}.output.schema.json"
        schema_path = harness / "schemas" / f"{mid}_v1.json"
        if _write_json(schema_path, output_obj, overwrite=overwrite):
            created.append(str(schema_path))
        if previous_id is None:
            input_obj = item.get("_input_schema_object") or {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"{mid}.input.schema.json",
                "type": "object",
                "additionalProperties": True,
                "required": ["request"],
                "properties": {"request": {"type": "object"}},
            }
        else:
            input_obj = item.get("_input_schema_object") or {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"{mid}.input.schema.json",
                "type": "object",
                "additionalProperties": True,
                "required": [previous_id],
                "properties": {previous_id: {"type": "object"}},
            }
        input_path = harness / "milestones" / mid / "input.schema.json"
        if _write_json(input_path, input_obj, overwrite=overwrite):
            created.append(str(input_path))
        if item["intelligence"] != "none":
            intel_gate = (
                "if draft is None:\n"
                "        return {\n"
                '            "_flowstep": "NEED_MODEL",\n'
                f'            "model": {json.dumps(item["intelligence"])},\n'
                '            "model_request": {\n'
                f'                "milestone": {json.dumps(mid)},\n'
                '                "instruction": "Write a draft that the toolbox can admit into the output schema.",\n'
                "            },\n"
                "        }\n"
            )
        else:
            intel_gate = "del kwargs\n"
        mapping = {
            "STEP_ID": mid,
            "TOOLS_JSON": json.dumps(item["tools"]),
            "INTELLIGENCE": item["intelligence"],
            "IS_LAST": "True" if item["_is_last"] else "False",
            "INTEL_GATE": intel_gate,
        }
        assemble = harness / "milestones" / mid / "assemble.py"
        if _write_text(assemble, _render("milestone/assemble.py", mapping), overwrite=overwrite):
            created.append(str(assemble))
        test_path = harness / "milestones" / mid / "tests" / "test_assemble.py"
        if _write_text(test_path, _render("milestone/test_assemble.py", mapping), overwrite=overwrite):
            created.append(str(test_path))
        if item["intelligence"] != "none":
            draft = harness / "milestones" / mid / "draft.schema.json"
            if _write_text(draft, _render("step/draft.schema.json", {"STEP_ID": mid}), overwrite=overwrite):
                created.append(str(draft))
        for rel, schema_obj in (item.get("_gate_schemas") or {}).items():
            if isinstance(schema_obj, dict) and _write_json(harness / rel, schema_obj, overwrite=overwrite):
                created.append(str(harness / rel))
        if item.get("foreach") and isinstance(item.get("_item_schema_object"), dict):
            item_schema_path = harness / str(item["foreach"]["item_schema"])
            if _write_json(item_schema_path, item["_item_schema_object"], overwrite=overwrite):
                created.append(str(item_schema_path))
        previous_id = mid
    loaded = load_flow(harness, flow_path)
    instruction = write_instruction(harness, loaded)
    created.append(str(instruction))
    table = classification_from_flow(loaded)
    table_path = harness / "planning" / "tool-vs-intelligence.json"
    table_path.parent.mkdir(parents=True, exist_ok=True)
    table_path.write_text(json.dumps(table, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    created.append(str(table_path))
    return {
        "schema": "flowstep_harness_generate_v3",
        "status": "PASS",
        "harness_dir": str(harness),
        "codebase": str(Path(codebase).resolve()),
        "flow_id": flow_id,
        "milestones": milestones,
        "tools": sorted({tool for item in items for tool in item["tools"]}),
        "instruction_path": str(instruction),
        "tool_vs_intelligence": table,
        "tool_vs_intelligence_path": str(table_path),
        "written": created,
    }


def load_audit_report(path: Path) -> dict[str, Any]:
    path = path.resolve()
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
    dest = Path(codebase).resolve() / ".agents" / "skills" / skill_name / "SKILL.md"
    table = render_classification_markdown(classification or {"rows": []})
    mapping = {
        "SKILL_NAME": skill_name,
        "FLOW_ID": flow_id,
        "BUILDER_ROOT": str(DEFAULT_BUILDER).replace("\\", "/"),
        "CLASSIFICATION_TABLE": table,
    }
    _write_text(dest, _render("product-SKILL.md", mapping), overwrite=overwrite or not dest.exists())
    return str(dest)


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
        for tool_id in item.get("tools") or []:
            tool_ids.append(str(tool_id))
    if "hash_bind" not in tool_ids:
        tool_ids.append("hash_bind")
    unique_tools: list[str] = []
    for tool_id in tool_ids:
        if tool_id and tool_id not in unique_tools:
            unique_tools.append(tool_id)
    toolbox = [generate_tool(codebase, tool_id, overwrite=overwrite) for tool_id in unique_tools]
    specs = []
    for item in proposed:
        tools = [str(tool_id) for tool_id in (item.get("tools") or []) if tool_id]
        if not tools:
            tools = ["hash_bind"]
        spec = {
            "id": item["id"],
            "tools": tools,
            "intelligence": item.get("intelligence") or "none",
            "output_contract": item.get("output_contract") or f"{item['id']}_v1",
            "output_schema_object": item.get("output_schema"),
            "input_schema_object": item.get("input_schema"),
            "inputs": item.get("inputs"),
            "model_justification": item.get("model_justification"),
            "next": item.get("next"),
            "else": item.get("else"),
            "foreach": item.get("foreach"),
            "join": item.get("join"),
            "_gate_schemas": {
                str(edge["when"]): edge["schema"]
                for edge in (item.get("next") or [])
                if isinstance(edge, dict) and edge.get("schema")
            },
        }
        specs.append(spec)
    result = generate_v3_flow(
        codebase,
        raw_flow_id,
        [item["id"] for item in specs],
        tools=unique_tools,
        overwrite=overwrite,
        milestone_specs=specs,
    )
    _copy_missing_control_schemas(Path(result["harness_dir"]), audit)
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
    if any(item.get("status") != "PASS" for item in toolbox):
        result["status"] = "FINDINGS"
    return result


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
        if item.get("next"):
            lines.append("    next:")
            for edge in item["next"]:
                lines.append(f"      - when: {edge['when']}")
                lines.append(f"        then: {edge['then']}")
            lines.append(f"    else: {item.get('else') or 'BLOCKED'}")
        if item.get("foreach"):
            fe = item["foreach"]
            lines.append("    foreach:")
            lines.append(f"      path: {fe['path']}")
            lines.append(f"      item_schema: {fe['item_schema']}")
            lines.append(f"      tools: [{', '.join(fe['tools'])}]")
            lines.append(f"      max_items: {fe['max_items']}")
            if fe.get("collect"):
                lines.append(f"      collect: {fe['collect']}")
        if item.get("join"):
            lines.append(f"    join: [{', '.join(item['join'])}]")
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
    parser.add_argument("--from-audit", type=Path, help="flowstep-audit.json (or .md next to that JSON).")
    parser.add_argument("--tool", dest="tool_id", help="Install one toolbox function under flowsteps/tools/.")
    parser.add_argument("--step", action="append", default=[], dest="steps")
    parser.add_argument("--milestone", action="append", default=[], dest="milestones")
    parser.add_argument("--tools", help="Comma-separated toolbox ids when not using --from-audit.")
    parser.add_argument("--intelligence", action="append", default=[], help="Milestone ids that may NEED_MODEL.")
    parser.add_argument("--skill-name")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--legacy-v2",
        action="store_true",
        help="Forbidden default. Only for the text_pipeline fixture tests.",
    )
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
            result = generate_v3_flow(
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
            if not args.legacy_v2:
                raise FlowError("v2 --step is forbidden; pass --from-audit or --milestone")
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
        else:
            raise FlowError("pass --from-audit, --milestone, or --tool")
    except FlowError as exc:
        print(json.dumps({"status": "BLOCKED", "blockers": [str(exc)]}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
