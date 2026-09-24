#!/usr/bin/env python3
"""Explicit, staging-only ``flowstep_flow_v4`` to skill-source importer.

The importer has four separate operations: inspect, digest-bound accept,
stage, and verify.  It never installs a skill, writes a codebase, deploys, or
claims execution closure.  Execution/profile/capability declarations are
operator-supplied because a v4 flow does not contain those Builder 3 facts.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

import yaml
from jsonschema import Draft202012Validator

from flowstep_runtime import (
    FlowError,
    load_flow,
    local_tool_package_name,
    runtime_package_name,
    sha256_file,
)
from skill_source import (
    SkillSourceError,
    _native_graph,
    canonical_json_bytes,
    compile_skill_source,
    load_skill_source,
)


INSPECTION_SCHEMA = "m8m.flow_v4_import_inspection.v1"
ACCEPTANCE_REQUEST_SCHEMA = "m8m.flow_v4_import_acceptance_request.v1"
ACCEPTANCE_SCHEMA = "m8m.flow_v4_import_acceptance.v1"
VERIFY_SCHEMA = "m8m.flow_v4_import_verification.v1"
PROOF_SCHEMA = "m8m.flow_v4_import_equivalence_proof.v1"
RESOURCE_MANIFEST_SCHEMA = "m8m.flow_v4_import_resource_manifest.v1"
_VERSIONED_REF = re.compile(r"^[a-z][a-z0-9_.-]*@[0-9]+\.[0-9]+\.[0-9]+$")
_GLOB = re.compile(r"[*?\[]")


class ImportFlowV4Error(ValueError):
    """The requested import is ambiguous, stale, unsafe, or lossy."""


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _with_digest(value: dict[str, Any], field: str) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result[field] = _digest(result)
    return result


def _assert_digest(value: Mapping[str, Any], field: str, *, label: str) -> None:
    expected = str(value.get(field) or "")
    payload = {key: copy.deepcopy(item) for key, item in value.items() if key != field}
    if expected != _digest(payload):
        raise ImportFlowV4Error(f"{label} digest is missing or does not match its contents")


def _safe_relative(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ImportFlowV4Error(f"{label} must be a normalized non-empty relative path")
    if "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value) or _GLOB.search(value):
        raise ImportFlowV4Error(f"{label} must be a safe relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ImportFlowV4Error(f"{label} must stay inside its declared root")
    return path.as_posix()


def _bounded_file(root: Path, relative: str, *, label: str) -> Path:
    relative = _safe_relative(relative, label=label)
    root = root.resolve()
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    if candidate.is_symlink():
        raise ImportFlowV4Error(f"{label} may not be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ImportFlowV4Error(f"{label} is missing: {relative}") from exc
    if root not in resolved.parents or not resolved.is_file():
        raise ImportFlowV4Error(f"{label} escapes its declared root or is not a file")
    return resolved


def _project_root(harness: Path) -> Path:
    if harness.parent.name == "flows" and harness.parent.parent.name == "flowsteps":
        return harness.parent.parent.parent.resolve()
    return harness.resolve()


def _roots(source_root: str | Path, project_root: str | Path | None = None) -> tuple[Path, Path]:
    harness = Path(source_root).resolve(strict=True)
    if not harness.is_dir():
        raise ImportFlowV4Error("source root is not a directory")
    project = Path(project_root).resolve(strict=True) if project_root is not None else _project_root(harness)
    if not project.is_dir():
        raise ImportFlowV4Error("project root is not a directory")
    return harness, project


_PROJECTION_FIELDS = (
    "id", "handler", "implementation_dependencies", "intelligence", "tools", "flowsteps",
    "inputs", "output_contract", "input_schema", "output_schema", "test", "on_tool_fail",
    "max_model_attempts", "max_tool_attempts", "max_attempts", "loop", "ledger", "worker",
    "judge_abi", "receipt_schema", "branch", "on_path", "cycle", "on_cycle", "success",
    "draft_schema", "model_justification", "asset", "outputs", "cache", "side_effects",
    "phase_journal", "execution",
)


def semantic_projection(skill_dir: Path, flow_path: Path | None = None) -> dict[str, Any]:
    """Return the exact normalized runtime-significant v4 projection."""

    loaded = load_flow(skill_dir, flow_path, allow_unbound_import=True)
    milestones: list[dict[str, Any]] = []
    for step in loaded["steps"]:
        row = {field: copy.deepcopy(step.get(field)) for field in _PROJECTION_FIELDS}
        row["gem"] = str(step.get("gem") or f"references/{step['id']}.md")
        milestones.append(row)
    return {
        "schema": loaded["schema"],
        "flow_id": loaded["flow_id"],
        "version": loaded["version"],
        "context_policy": loaded.get("context_policy"),
        "max_run_seconds": loaded.get("max_run_seconds"),
        "artifact_root": loaded.get("artifact_root"),
        "implementation_dependencies": list(loaded.get("implementation_dependencies") or []),
        "milestones": milestones,
    }


_AGENT_FLOW_FIELDS = (
    "success", "output_contract", "output_schema", "outputs", "asset", "handler",
    "implementation_dependencies", "test", "input_schema", "inputs", "flowsteps", "tools",
    "intelligence", "model_justification", "draft_schema", "on_tool_fail",
    "max_model_attempts", "max_tool_attempts", "side_effects", "phase_journal", "loop",
    "worker", "judge_abi", "receipt_schema", "max_attempts", "ledger", "branch", "on_path",
    "cycle", "on_cycle", "cache", "execution",
)


def _agent_skeleton(step: Mapping[str, Any]) -> dict[str, Any]:
    milestone_id = str(step["id"])
    agent: dict[str, Any] = {
        "schema": "m8m_milestone_agent_v1",
        "agent_id": milestone_id,
        "role": "milestone",
    }
    for field in _AGENT_FLOW_FIELDS:
        value = step.get(field)
        if value is not None and value != []:
            agent[field] = copy.deepcopy(value)
    agent["flowsteps"] = []
    for flowstep in step.get("flowsteps") or []:
        exact_or_local = str(flowstep.get("tool") or "")
        try:
            local_name = local_tool_package_name(
                exact_or_local,
                label=f"{milestone_id}.{flowstep.get('id')}.tool",
            )
        except FlowError:
            local_name = exact_or_local
        agent["flowsteps"].append(
            {"id": str(flowstep["id"]), "tool": local_name}
        )
    agent["gem"] = f"references/{milestone_id}.md"
    agent["read_paths"] = [agent["gem"]]
    agent["observer"] = {
        "title": milestone_id.replace("_", " ").title(),
        "summary": str(step.get("success") or f"Complete {milestone_id}."),
        "actions": [
            {
                "flowstep_id": item["id"],
                "title": str(item["id"]).replace("_", " ").title(),
                "summary": f"Run the declared {item['tool']} FlowStep.",
            }
            for item in step.get("flowsteps") or []
        ],
        "outputs": [
            {
                "output_id": item["id"],
                "title": item["name"],
                "summary": f"Declared {item['cardinality']} {item['kind']} output.",
            }
            for item in step.get("outputs") or []
        ],
    }
    return agent


def _schema_refs(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str):
                found.append(child)
            else:
                found.extend(_schema_refs(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_schema_refs(child))
    return found


def _closed_judge_receipt(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    properties = value.get("properties") if isinstance(value, dict) else None
    required = value.get("required") if isinstance(value, dict) else None
    return bool(
        isinstance(value, dict)
        and value.get("type") == "object"
        and value.get("additionalProperties") is False
        and isinstance(required, list)
        and set(required) == {"decision", "reasons", "blockers"}
        and isinstance(properties, dict)
        and set(properties) == {"decision", "reasons", "blockers"}
        and set((properties.get("decision") or {}).get("enum") or [])
        == {"PASS", "RETRY", "BLOCKED"}
    )


def _collect_resources(
    harness: Path,
    project: Path,
    agents: list[dict[str, Any]],
    flow_dependencies: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    resources: dict[str, dict[str, Any]] = {}
    blockers: list[str] = []

    def add(scope: str, source_relative: str, stage_relative: str, label: str) -> None:
        try:
            source_relative = _safe_relative(source_relative, label=label)
            stage_relative = _safe_relative(stage_relative, label=f"{label} stage path")
            root = harness if scope == "harness" else project
            path = _bounded_file(root, source_relative, label=label)
            row = {
                "source_scope": scope,
                "source_path": source_relative,
                "stage_path": stage_relative,
                "byte_count": path.stat().st_size,
                "sha256": "sha256:" + sha256_file(path),
            }
            prior = resources.get(stage_relative)
            if prior is not None and prior["sha256"] != row["sha256"]:
                raise ImportFlowV4Error(f"two different source files map to {stage_relative}")
            resources[stage_relative] = row
        except (ImportFlowV4Error, OSError) as exc:
            blockers.append(str(exc))

    for agent in agents:
        milestone_id = agent["agent_id"]
        for field in ("handler", "test", "output_schema", "draft_schema", "receipt_schema", "gem"):
            if agent.get(field):
                add("harness", str(agent[field]), str(agent[field]), f"{milestone_id}.{field}")
        for control_field in ("branch", "cycle"):
            control = agent.get(control_field)
            if isinstance(control, dict) and control.get("receipt_schema"):
                ref = str(control["receipt_schema"])
                add("harness", ref, ref, f"{milestone_id}.{control_field}.receipt_schema")
        for dependency in agent.get("implementation_dependencies") or []:
            add("project", str(dependency), str(dependency), f"{milestone_id}.implementation_dependencies")
    for dependency in flow_dependencies:
        add("project", str(dependency), str(dependency), "flow.implementation_dependencies")

    # Close local JSON Schema references so staging cannot silently drop a
    # schema dependency that the skill compiler will later hash-bind.
    pending = [row for row in resources.values() if str(row["stage_path"]).endswith(".json")]
    seen: set[str] = set()
    while pending:
        row = pending.pop(0)
        key = str(row["stage_path"])
        if key in seen:
            continue
        seen.add(key)
        root = harness if row["source_scope"] == "harness" else project
        try:
            path = _bounded_file(root, str(row["source_path"]), label=f"schema {key}")
            document = json.loads(path.read_text(encoding="utf-8"))
        except (ImportFlowV4Error, OSError, UnicodeError, json.JSONDecodeError) as exc:
            blockers.append(f"schema {key} cannot be inspected: {exc}")
            continue
        for raw_ref in _schema_refs(document):
            parts = urlsplit(raw_ref)
            if parts.scheme or parts.netloc or parts.query:
                blockers.append(f"schema {key} has unsupported external $ref: {raw_ref}")
                continue
            if not parts.path:
                continue
            decoded = unquote(parts.path)
            source_rel = (PurePosixPath(str(row["source_path"])).parent / decoded).as_posix()
            stage_rel = (PurePosixPath(key).parent / decoded).as_posix()
            before = set(resources)
            add(str(row["source_scope"]), source_rel, stage_rel, f"schema dependency of {key}")
            if set(resources) != before:
                pending.append(resources[stage_rel])
    return [resources[key] for key in sorted(resources)], list(dict.fromkeys(blockers))


def _tool_package_manifest(project: Path, tool_ids: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    packages: list[dict[str, Any]] = []
    blockers: list[str] = []
    package_ids = list(
        dict.fromkeys(
            tool_id.rsplit("@", 1)[0]
            if _VERSIONED_REF.fullmatch(tool_id)
            else tool_id
            for tool_id in tool_ids
        )
    )
    for tool_id in package_ids:
        tool_root = project / "flowsteps" / "tools" / tool_id
        if not tool_root.is_dir() or tool_root.is_symlink():
            blockers.append(f"tool {tool_id}: package is missing from project flowsteps/tools")
            continue
        entries = sorted(tool_root.rglob("*"), key=lambda item: item.as_posix().lower())
        symlinks = [path.relative_to(tool_root).as_posix() for path in entries if path.is_symlink()]
        if symlinks:
            blockers.append(
                f"tool {tool_id}: symbolic links are not allowed in the frozen package: "
                + ", ".join(symlinks)
            )
            continue
        members: list[dict[str, Any]] = []
        for path in entries:
            if not path.is_file() or "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo"}:
                continue
            relative = path.relative_to(tool_root).as_posix()
            members.append(
                {
                    "path": relative,
                    "byte_count": path.stat().st_size,
                    "sha256": "sha256:" + sha256_file(path),
                }
            )
        if not members:
            blockers.append(f"tool {tool_id}: package contains no bindable files")
            continue
        packages.append(
            {
                "tool_id": tool_id,
                "members": members,
                "package_digest": _digest(members),
                "copied_into_skill": False,
            }
        )
    return packages, blockers


def inspect_flow_v4(
    source_root: str | Path,
    flow_path: str | Path | None = None,
    *,
    skill_name: str | None = None,
    display_name: str | None = None,
    description: str | None = None,
    default_prompt: str | None = None,
) -> dict[str, Any]:
    """Inspect one legacy v4 flow and return a deterministic import plan."""

    harness, project = _roots(source_root)
    chosen_flow = Path(flow_path) if flow_path is not None else harness / "flow.yaml"
    if not chosen_flow.is_absolute():
        chosen_flow = harness / chosen_flow
    try:
        chosen_flow = chosen_flow.resolve(strict=True)
        flow_relative = chosen_flow.relative_to(harness).as_posix()
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise ImportFlowV4Error("flow path must be a file inside source root") from exc
    try:
        loaded = load_flow(harness, chosen_flow, allow_unbound_import=True)
    except FlowError as exc:
        raise ImportFlowV4Error(str(exc)) from exc

    projection = semantic_projection(harness, chosen_flow)
    raw_milestones = list(loaded.get("milestones") or [])
    blockers: list[str] = []
    agents: list[dict[str, Any]] = []
    for raw, step in zip(raw_milestones, loaded["steps"]):
        milestone_id = str(step["id"])
        legacy_fields = [field for field in ("next", "else", "join") if field in raw]
        if legacy_fields:
            blockers.append(
                f"{milestone_id}: raw {', '.join(legacy_fields)} routing is not a native v4 runtime declaration"
            )
        if str(raw.get("loop") or "none") == "for":
            blockers.append(f"{milestone_id}: legacy loop=for needs an operator-authored explicit cycle and join")
        if step.get("loop") == "judge" and step.get("judge_abi") != "m8m_milestone_judge_v1":
            blockers.append(
                f"{milestone_id}: implicit legacy judge behavior cannot be upgraded to the strict judge ABI losslessly"
            )
        if step.get("loop") == "judge" and step.get("judge_abi") == "m8m_milestone_judge_v1":
            receipt_ref = str(step.get("receipt_schema") or "")
            try:
                receipt_path = _bounded_file(harness, receipt_ref, label=f"{milestone_id}.receipt_schema")
            except ImportFlowV4Error as exc:
                blockers.append(str(exc))
            else:
                if not _closed_judge_receipt(receipt_path):
                    blockers.append(
                        f"{milestone_id}: strict judge result schema must be closed with "
                        "exact decision, reasons, and blockers fields"
                    )
        raw_gem = str(raw.get("gem") or f"references/{milestone_id}.md")
        if raw_gem != f"references/{milestone_id}.md":
            blockers.append(f"{milestone_id}: custom gem path cannot be losslessly renamed")
        if not step.get("flowsteps") or any(not item.get("tool") for item in step.get("flowsteps") or []):
            blockers.append(f"{milestone_id}: every imported FlowStep must have one explicit tool")
        agents.append(_agent_skeleton(step))

    try:
        adjacency = _native_graph(agents)
    except SkillSourceError as exc:
        blockers.append(str(exc))
        adjacency = {agent["agent_id"]: [] for agent in agents}
    order = [agent["agent_id"] for agent in agents]
    incoming = {item: 0 for item in order}
    for targets in adjacency.values():
        for target in targets:
            if target in incoming:
                incoming[target] += 1
    roots = [item for item in order if incoming[item] == 0]
    terminals = [item for item in order if not adjacency.get(item)]
    if len(roots) != 1:
        blockers.append("native graph must have exactly one entry root")
    if not terminals:
        blockers.append("native graph must have at least one success terminal")

    resources, resource_blockers = _collect_resources(
        harness,
        project,
        agents,
        list(loaded.get("implementation_dependencies") or []),
    )
    blockers.extend(resource_blockers)
    tool_ids: list[str] = []
    for step in loaded["steps"]:
        for flowstep in step.get("flowsteps") or []:
            tool_ref = str(flowstep.get("tool") or "")
            try:
                tool_ids.append(
                    local_tool_package_name(
                        tool_ref,
                        label=f"{step['id']}.{flowstep.get('id')}.tool",
                    )
                )
            except FlowError:
                if tool_ref:
                    tool_ids.append(tool_ref)
        worker = str(step.get("worker") or "")
        if worker:
            tool_ids.append(
                runtime_package_name(worker, label=f"{step['id']}.worker")
                if step.get("judge_abi")
                else worker
            )
    tool_ids = list(dict.fromkeys(tool_ids))
    tool_packages, tool_blockers = _tool_package_manifest(project, tool_ids)
    blockers.extend(tool_blockers)
    resolved_skill_name = skill_name or str(loaded["flow_id"]).replace("_", "-")
    resolved_display = display_name or str(loaded["flow_id"]).replace("_", " ").title()
    resolved_description = description or f"Imported local M8M workflow {loaded['flow_id']}."
    resolved_prompt = default_prompt or f"Use ${resolved_skill_name} with an explicit request."
    plan = {
        "schema": INSPECTION_SCHEMA,
        "source": {
            "flow_path": flow_relative,
            "flow_sha256": "sha256:" + sha256_file(chosen_flow),
        },
        "skill": {"name": resolved_skill_name, "description": resolved_description},
        "interface": {
            "display_name": resolved_display,
            "short_description": resolved_description,
            "default_prompt": resolved_prompt,
        },
        "flow_id": loaded["flow_id"],
        "version": loaded["version"],
        "semantic_projection": projection,
        "source_semantic_digest": _digest(projection),
        "graph": [{"from": item, "to": list(adjacency[item])} for item in order],
        "entry": roots[0] if len(roots) == 1 else "",
        "terminals": terminals,
        "milestones": [
            {
                "id": agent["agent_id"],
                "intelligence": agent.get("intelligence", "none"),
                "loop": agent.get("loop", "none"),
                "tools": list(agent.get("tools") or []),
                "agent": agent,
            }
            for agent in agents
        ],
        "resources": resources,
        "tool_packages": tool_packages,
        "lossless": not blockers,
        "blockers": list(dict.fromkeys(blockers)),
        "operator_boundary": (
            "Supply exact execution/profile/capability declarations and accept this inspection digest. "
            "The importer proves v4 runtime equivalence only; it does not prove implementations built."
        ),
        "staging_only": True,
    }
    return _with_digest(plan, "inspection_digest")


def _accept_workflow_contracts(
    inspection: Mapping[str, Any], request: Mapping[str, Any], harness: Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    contracts = request.get("workflow_contracts")
    required = {"request_schema", "configuration_schema", "result_schema", "terminal_bindings"}
    if not isinstance(contracts, dict) or set(contracts) != required:
        raise ImportFlowV4Error("acceptance requires exact workflow_contracts schema paths and terminal_bindings")
    resources: list[dict[str, Any]] = []
    documents: dict[str, dict[str, Any]] = {}
    for field in ("request_schema", "configuration_schema", "result_schema"):
        relative = _safe_relative(contracts[field], label=f"workflow_contracts.{field}")
        path = _bounded_file(harness, relative, label=f"workflow_contracts.{field}")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(document)
        except (OSError, UnicodeError, json.JSONDecodeError, Exception) as exc:
            raise ImportFlowV4Error(f"workflow_contracts.{field} is not valid JSON Schema: {exc}") from exc
        if (
            not isinstance(document, dict)
            or document.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
            or document.get("type") != "object"
            or document.get("additionalProperties") is not False
        ):
            raise ImportFlowV4Error(f"workflow_contracts.{field} must be a closed Draft 2020-12 object schema")
        documents[field] = document
        resources.append(
            {
                "source_scope": "harness",
                "source_path": relative,
                "stage_path": relative,
                "byte_count": path.stat().st_size,
                "sha256": "sha256:" + sha256_file(path),
            }
        )
    by_stage = {row["stage_path"]: row for row in resources}
    pending = list(resources)
    while pending:
        row = pending.pop(0)
        path = _bounded_file(harness, row["source_path"], label=f"workflow schema {row['stage_path']}")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ImportFlowV4Error(f"workflow schema dependency is invalid JSON: {row['stage_path']}") from exc
        for raw_ref in _schema_refs(document):
            parts = urlsplit(raw_ref)
            if parts.scheme or parts.netloc or parts.query:
                raise ImportFlowV4Error(f"workflow schema has unsupported external $ref: {raw_ref}")
            if not parts.path:
                continue
            decoded = unquote(parts.path)
            source_rel = (PurePosixPath(row["source_path"]).parent / decoded).as_posix()
            stage_rel = _safe_relative(
                (PurePosixPath(row["stage_path"]).parent / decoded).as_posix(),
                label=f"workflow schema dependency of {row['stage_path']}",
            )
            dependency = _bounded_file(harness, source_rel, label=f"workflow schema dependency {stage_rel}")
            descriptor = {
                "source_scope": "harness",
                "source_path": _safe_relative(source_rel, label=f"workflow schema dependency {stage_rel}"),
                "stage_path": stage_rel,
                "byte_count": dependency.stat().st_size,
                "sha256": "sha256:" + sha256_file(dependency),
            }
            prior = by_stage.get(stage_rel)
            if prior is not None and prior != descriptor:
                raise ImportFlowV4Error(f"workflow schema dependency collision: {stage_rel}")
            if prior is None:
                by_stage[stage_rel] = descriptor
                resources.append(descriptor)
                pending.append(descriptor)
    bindings = contracts["terminal_bindings"]
    if not isinstance(bindings, list) or not bindings:
        raise ImportFlowV4Error("workflow_contracts.terminal_bindings must be non-empty")
    terminals = set(inspection["terminals"])
    by_id = {row["id"]: row["agent"] for row in inspection["milestones"]}
    names: list[str] = []
    seen_sources: set[tuple[str, str]] = set()
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict) or set(binding) != {"name", "from", "output"}:
            raise ImportFlowV4Error(f"terminal binding {index} must contain only name, from, and output")
        name = str(binding["name"])
        source_ref = str(binding["from"])
        if "." not in source_ref:
            raise ImportFlowV4Error(f"terminal binding {name} has an invalid from reference")
        source, contract = source_ref.split(".", 1)
        if source not in terminals or contract != by_id[source]["output_contract"]:
            raise ImportFlowV4Error(f"terminal binding {name} does not name an exact success-terminal contract")
        output = str(binding["output"])
        if output not in {row["id"] for row in by_id[source]["outputs"]}:
            raise ImportFlowV4Error(f"terminal binding {name} names an unknown output port")
        if name in names or (source, output) in seen_sources:
            raise ImportFlowV4Error("workflow terminal binding names and source ports must be unique")
        names.append(name)
        seen_sources.add((source, output))
    result = documents["result_schema"]
    if set(result.get("properties") or {}) != set(names) or set(result.get("required") or []) != set(names):
        raise ImportFlowV4Error("result schema properties/required must exactly match terminal binding names")
    return copy.deepcopy(contracts), [by_stage[key] for key in sorted(by_stage)]


def _validate_profile(
    milestone_id: str,
    role: str,
    profile: Any,
    *,
    expected_tools: list[str],
    capability_ids: set[str],
) -> set[str]:
    if not isinstance(profile, dict):
        raise ImportFlowV4Error(f"{milestone_id}: {role} profile must be an object")
    required = {"ref", "model_configuration", "token_budget", "timeout_seconds", "tools", "capabilities"}
    if set(profile) != required:
        raise ImportFlowV4Error(f"{milestone_id}: {role} profile must contain exactly {sorted(required)}")
    ref = profile["ref"]
    if not isinstance(ref, str) or not ref.strip() or ref != ref.strip():
        raise ImportFlowV4Error(f"{milestone_id}: {role} profile ref must be explicit")
    configuration = profile["model_configuration"]
    if not isinstance(configuration, dict) or set(configuration) - {"model", "reasoning", "temperature"} or not {"model", "reasoning"}.issubset(configuration):
        raise ImportFlowV4Error(f"{milestone_id}: {role} model configuration is incomplete")
    if not isinstance(configuration["model"], str) or not configuration["model"].strip():
        raise ImportFlowV4Error(f"{milestone_id}: {role} model is required")
    if configuration["reasoning"] not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
        raise ImportFlowV4Error(f"{milestone_id}: {role} reasoning is invalid")
    budget = profile["token_budget"]
    if not isinstance(budget, dict) or set(budget) != {"max_input_tokens", "max_output_tokens"}:
        raise ImportFlowV4Error(f"{milestone_id}: {role} token budget is incomplete")
    if any(not isinstance(budget[key], int) or isinstance(budget[key], bool) or budget[key] < 1 for key in budget):
        raise ImportFlowV4Error(f"{milestone_id}: {role} token budgets must be positive integers")
    timeout = profile["timeout_seconds"]
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
        raise ImportFlowV4Error(f"{milestone_id}: {role} timeout must be a positive integer")
    profile_tools = profile["tools"]
    if not isinstance(profile_tools, list) or list(profile_tools) != expected_tools:
        raise ImportFlowV4Error(f"{milestone_id}: {role} profile tools do not match its executor role")
    bound_capabilities = profile["capabilities"]
    if (
        not isinstance(bound_capabilities, list)
        or any(not isinstance(item, str) or not item for item in bound_capabilities)
        or len(bound_capabilities) != len(set(bound_capabilities))
    ):
        raise ImportFlowV4Error(f"{milestone_id}: {role} profile capabilities must be unique")
    unknown = set(bound_capabilities) - capability_ids
    if unknown:
        raise ImportFlowV4Error(f"{milestone_id}: {role} profile binds undeclared capabilities")
    return set(bound_capabilities)


def accept_import(
    inspection: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    source_root: str | Path,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Bind explicit operator declarations to one immutable inspection."""

    _assert_digest(inspection, "inspection_digest", label="inspection")
    if inspection.get("schema") != INSPECTION_SCHEMA or not inspection.get("lossless"):
        raise ImportFlowV4Error("inspection is not lossless; resolve every blocker before acceptance")
    if request.get("schema") != ACCEPTANCE_REQUEST_SCHEMA:
        raise ImportFlowV4Error(f"acceptance request schema must be {ACCEPTANCE_REQUEST_SCHEMA}")
    if request.get("inspection_digest") != inspection.get("inspection_digest"):
        raise ImportFlowV4Error("acceptance request is not bound to this inspection digest")
    acknowledgements = request.get("acknowledgements")
    required_ack = {
        "execution_declarations_are_operator_supplied": True,
        "staging_only": True,
        "no_install_or_deploy": True,
    }
    if acknowledgements != required_ack:
        raise ImportFlowV4Error("acceptance must explicitly acknowledge execution ownership and staging-only scope")
    harness, _project = _roots(source_root, project_root)
    workflow_contracts, workflow_resources = _accept_workflow_contracts(inspection, request, harness)
    known_resources = {row["stage_path"]: row["sha256"] for row in inspection["resources"]}
    for row in workflow_resources:
        prior = known_resources.get(row["stage_path"])
        if prior is not None and prior != row["sha256"]:
            raise ImportFlowV4Error(f"workflow contract conflicts with inspected resource {row['stage_path']}")
    requested = request.get("milestones")
    if not isinstance(requested, list):
        raise ImportFlowV4Error("acceptance request milestones must be a list")
    expected_ids = [row["id"] for row in inspection["milestones"]]
    actual_ids = [str(row.get("id") or "") for row in requested if isinstance(row, dict)]
    if actual_ids != expected_ids or len(actual_ids) != len(requested):
        raise ImportFlowV4Error("acceptance milestones must exactly cover the inspected roster in order")

    semantic_by_id = {
        str(item.get("id") or ""): item
        for item in (inspection.get("semantic_projection") or {}).get("milestones") or []
        if isinstance(item, Mapping)
    }
    for inspected, declared in zip(inspection["milestones"], requested):
        execution = declared.get("execution")
        if not isinstance(execution, dict) or not isinstance(execution.get("candidate_executor"), dict):
            raise ImportFlowV4Error(f"{inspected['id']}: candidate executor declaration is required")
        candidate = execution["candidate_executor"]
        if not _VERSIONED_REF.fullmatch(str(candidate.get("ref") or "")):
            raise ImportFlowV4Error(f"{inspected['id']}: candidate executor ref must be versioned")
        bindings = execution.get("tool_bindings")
        bound_tools = [str(row.get("tool") or "") for row in bindings or [] if isinstance(row, dict)]
        if bound_tools != list(inspected["tools"]) or len(bound_tools) != len(bindings or []):
            raise ImportFlowV4Error(f"{inspected['id']}: tool bindings must exactly cover effective FlowStep tools")
        exact_flowstep_refs = {
            str(item.get("id") or ""): str(item.get("tool") or "")
            for item in semantic_by_id.get(str(inspected["id"]), {}).get("flowsteps") or []
            if isinstance(item, Mapping)
        }
        for binding in bindings:
            ref = str(binding.get("ref") or "")
            if not _VERSIONED_REF.fullmatch(ref):
                raise ImportFlowV4Error(f"{inspected['id']}: tool ref must be versioned")
            exact_flowstep_ref = exact_flowstep_refs.get(str(binding["tool"]), "")
            if _VERSIONED_REF.fullmatch(exact_flowstep_ref) and ref != exact_flowstep_ref:
                raise ImportFlowV4Error(
                    f"{inspected['id']}: tool binding {binding['tool']} must exactly equal "
                    "the inspected executable FlowStep ref"
                )
            flowstep_local = {
                str(item.get("id") or ""): str(item.get("tool") or "")
                for item in inspected["agent"].get("flowsteps") or []
                if isinstance(item, dict)
            }.get(str(binding["tool"]), "")
            try:
                bound_local = local_tool_package_name(
                    ref,
                    label=f"{inspected['id']}.{binding['tool']}.ref",
                )
            except FlowError as exc:
                raise ImportFlowV4Error(str(exc)) from exc
            if bound_local != flowstep_local:
                raise ImportFlowV4Error(
                    f"{inspected['id']}: tool binding {binding['tool']} does not "
                    "resolve to its inspected FlowStep package"
                )
        if inspected.get("loop") == "judge":
            judge = execution.get("judge")
            if not isinstance(judge, dict) or not _VERSIONED_REF.fullmatch(str(judge.get("ref") or "")):
                raise ImportFlowV4Error(f"{inspected['id']}: judge loop needs a versioned judge ref")
            judge_ref = str(judge["ref"])
            worker_ref = str(inspected["agent"].get("worker") or "")
            if worker_ref != judge_ref:
                raise ImportFlowV4Error(
                    f"{inspected['id']}: worker must exactly equal execution.judge.ref"
                )
            candidate_ref = str(candidate.get("ref") or "")
            tool_binding_refs = {
                str(binding.get("ref") or "")
                for binding in bindings
                if isinstance(binding, dict)
            }
            if judge_ref == candidate_ref or judge_ref in tool_binding_refs:
                raise ImportFlowV4Error(
                    f"{inspected['id']}: judge ref must be distinct from candidate and tool refs"
                )
        capabilities = declared.get("capabilities") or []
        if not isinstance(capabilities, list):
            raise ImportFlowV4Error(f"{inspected['id']}: capabilities must be a list")
        capability_ids: list[str] = []
        for capability in capabilities:
            if not isinstance(capability, dict) or set(capability) != {"id", "ref", "access", "side_effects"}:
                raise ImportFlowV4Error(f"{inspected['id']}: every capability declaration must be closed")
            if not _VERSIONED_REF.fullmatch(str(capability.get("ref") or "")):
                raise ImportFlowV4Error(f"{inspected['id']}: capability ref must be versioned")
            if capability.get("access") not in {"read", "write", "execute", "use"} or capability.get("side_effects") not in {"none", "local", "external"}:
                raise ImportFlowV4Error(f"{inspected['id']}: capability access/side_effects are invalid")
            capability_ids.append(str(capability.get("id") or ""))
        if any(not item for item in capability_ids) or len(capability_ids) != len(set(capability_ids)):
            raise ImportFlowV4Error(f"{inspected['id']}: capability ids must be non-empty and unique")
        external = any(isinstance(item, dict) and item.get("side_effects") == "external" for item in capabilities)
        milestone_external = inspected["agent"].get("side_effects", "none") == "external"
        if external != milestone_external:
            raise ImportFlowV4Error(f"{inspected['id']}: external capability and side-effect semantics differ")
        candidate_profile = candidate.get("profile")
        judge_binding = execution.get("judge") if isinstance(execution.get("judge"), dict) else None
        judge_profile = judge_binding.get("profile") if judge_binding else None
        intelligence = str(inspected.get("intelligence") or "none")
        if intelligence != "none" and not isinstance(candidate_profile, dict):
            raise ImportFlowV4Error(f"{inspected['id']}: AI candidate profile must be operator supplied")
        if intelligence == "none" and candidate_profile is not None:
            raise ImportFlowV4Error(f"{inspected['id']}: deterministic candidate cannot accept an AI profile")
        if intelligence == "judge" and not isinstance(judge_profile, dict):
            raise ImportFlowV4Error(f"{inspected['id']}: legacy AI judge role requires an explicit judge profile")
        if judge_binding is not None and (
            inspected.get("loop") != "judge"
            or inspected["agent"].get("judge_abi") != "m8m_milestone_judge_v1"
            or not inspected["agent"].get("receipt_schema")
        ):
            raise ImportFlowV4Error(f"{inspected['id']}: separate judge requires the authored strict judge contract")
        bound_capabilities: set[str] = set()
        if isinstance(candidate_profile, dict):
            bound_capabilities |= _validate_profile(
                inspected["id"],
                "candidate",
                candidate_profile,
                expected_tools=list(inspected["tools"]),
                capability_ids=set(capability_ids),
            )
        if isinstance(judge_profile, dict):
            bound_capabilities |= _validate_profile(
                inspected["id"],
                "judge",
                judge_profile,
                expected_tools=[],
                capability_ids=set(capability_ids),
            )
        if (candidate_profile is not None or judge_profile is not None) and bound_capabilities != set(capability_ids):
            raise ImportFlowV4Error(f"{inspected['id']}: accepted profiles must exactly bind declared capabilities")
        if isinstance(candidate_profile, dict) and isinstance(judge_profile, dict) and candidate_profile["ref"] == judge_profile["ref"]:
            raise ImportFlowV4Error(f"{inspected['id']}: candidate and judge profiles must be separate")

    acceptance = {
        "schema": ACCEPTANCE_SCHEMA,
        "inspection_digest": inspection["inspection_digest"],
        "source_semantic_digest": inspection["source_semantic_digest"],
        "acknowledgements": copy.deepcopy(acknowledgements),
        "workflow_contracts": workflow_contracts,
        "workflow_contract_resources": workflow_resources,
        "milestones": copy.deepcopy(requested),
    }
    return _with_digest(acceptance, "acceptance_digest")


def _validate_acceptance(inspection: Mapping[str, Any], acceptance: Mapping[str, Any]) -> None:
    _assert_digest(inspection, "inspection_digest", label="inspection")
    _assert_digest(acceptance, "acceptance_digest", label="acceptance")
    if acceptance.get("schema") != ACCEPTANCE_SCHEMA:
        raise ImportFlowV4Error(f"acceptance schema must be {ACCEPTANCE_SCHEMA}")
    if acceptance.get("inspection_digest") != inspection.get("inspection_digest"):
        raise ImportFlowV4Error("acceptance is bound to a different inspection")
    if acceptance.get("source_semantic_digest") != inspection.get("source_semantic_digest"):
        raise ImportFlowV4Error("acceptance source semantic digest drifted")


def _source_is_frozen(
    inspection: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    harness: Path,
    project: Path,
) -> None:
    source = inspection["source"]
    flow = _bounded_file(harness, str(source["flow_path"]), label="accepted flow")
    if "sha256:" + sha256_file(flow) != source["flow_sha256"]:
        raise ImportFlowV4Error("accepted flow bytes changed after inspection")
    for row in inspection["resources"]:
        root = harness if row["source_scope"] == "harness" else project
        path = _bounded_file(root, row["source_path"], label=f"accepted resource {row['stage_path']}")
        if "sha256:" + sha256_file(path) != row["sha256"] or path.stat().st_size != row["byte_count"]:
            raise ImportFlowV4Error(f"accepted resource changed after inspection: {row['stage_path']}")
    for row in acceptance["workflow_contract_resources"]:
        path = _bounded_file(harness, row["source_path"], label=f"accepted workflow contract {row['stage_path']}")
        if "sha256:" + sha256_file(path) != row["sha256"] or path.stat().st_size != row["byte_count"]:
            raise ImportFlowV4Error(f"accepted workflow contract changed: {row['stage_path']}")
    expected_tools, blockers = _tool_package_manifest(
        project, [row["tool_id"] for row in inspection["tool_packages"]]
    )
    if blockers or expected_tools != inspection["tool_packages"]:
        raise ImportFlowV4Error("accepted tool-package bytes changed after inspection")


def _write_stage(
    stage: Path,
    inspection: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    harness: Path,
    project: Path,
) -> None:
    for row in inspection["resources"]:
        root = harness if row["source_scope"] == "harness" else project
        source_path = _bounded_file(root, row["source_path"], label=f"resource {row['stage_path']}")
        destination = stage.joinpath(*PurePosixPath(row["stage_path"]).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
    for row in acceptance["workflow_contract_resources"]:
        source_path = _bounded_file(harness, row["source_path"], label=f"workflow contract {row['stage_path']}")
        destination = stage.joinpath(*PurePosixPath(row["stage_path"]).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and "sha256:" + sha256_file(destination) != row["sha256"]:
            raise ImportFlowV4Error(f"workflow contract staging collision: {row['stage_path']}")
        shutil.copy2(source_path, destination)

    skill = inspection["skill"]
    frontmatter = yaml.safe_dump(
        {"name": skill["name"], "description": skill["description"], "metadata": {"version": "3.1"}},
        sort_keys=False,
        allow_unicode=True,
    )
    (stage / "SKILL.md").write_text(
        f"---\n{frontmatter}---\n\n# {inspection['interface']['display_name']}\n\n"
        "Invoke this skill through agents/openai.yaml; milestone instructions live only in "
        "references/<milestone>.md.\n",
        encoding="utf-8",
        newline="\n",
    )
    agents_dir = stage / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    canvas: dict[str, Any] = {
        "schema": "m8m_skill_canvas_v1",
        "flow_id": inspection["flow_id"],
        "version": inspection["version"],
        "context_policy": "isolated",
        "entry": inspection["entry"],
        "milestones": [row["id"] for row in inspection["milestones"]],
        "graph": copy.deepcopy(inspection["graph"]),
        "terminal_states": {"success": list(inspection["terminals"]), "blocked": "BLOCKED"},
        "observer": {
            "title": inspection["interface"]["display_name"],
            "summary": inspection["interface"]["short_description"],
        },
        "workflow_contracts": copy.deepcopy(acceptance["workflow_contracts"]),
    }
    for field in ("max_run_seconds", "artifact_root", "implementation_dependencies"):
        value = inspection["semantic_projection"].get(field)
        if value not in (None, []):
            canvas[field] = copy.deepcopy(value)
    openai = {"interface": copy.deepcopy(inspection["interface"]), "canvas": canvas}
    (agents_dir / "openai.yaml").write_text(
        yaml.safe_dump(openai, sort_keys=False, allow_unicode=True, width=1000),
        encoding="utf-8",
        newline="\n",
    )
    accepted_by_id = {row["id"]: row for row in acceptance["milestones"]}
    for inspected in inspection["milestones"]:
        milestone_id = inspected["id"]
        agent = copy.deepcopy(inspected["agent"])
        declared = accepted_by_id[milestone_id]
        for flowstep in agent.get("flowsteps") or []:
            ref = str(flowstep.get("tool") or "")
            if _VERSIONED_REF.fullmatch(ref):
                flowstep["tool"] = ref.rsplit("@", 1)[0]
        agent["execution"] = copy.deepcopy(declared["execution"])
        if declared.get("capabilities"):
            agent["capabilities"] = copy.deepcopy(declared["capabilities"])
        (agents_dir / f"{milestone_id}.yaml").write_text(
            yaml.safe_dump(agent, sort_keys=False, allow_unicode=True, width=1000),
            encoding="utf-8",
            newline="\n",
        )


def _compiled_projection(stage: Path, compiled: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="m8m-import-verify-") as temporary:
        root = Path(temporary)
        path = root / "flow.yaml"
        path.write_text(yaml.safe_dump(compiled, sort_keys=False, allow_unicode=True), encoding="utf-8")
        projection = semantic_projection(root, path)
        # Profiles and capabilities are operator-supplied Builder 3 closure
        # facts that a legacy v4 source cannot contain. Preserve their bytes in
        # the staged source, but omit them from the legacy-semantic equivalence
        # projection so the comparison remains about workflow behavior.
        for milestone in projection.get("milestones") or []:
            execution = milestone.get("execution")
            if isinstance(execution, dict):
                candidate = execution.get("candidate_executor")
                if isinstance(candidate, dict):
                    candidate.pop("profile", None)
                judge = execution.get("judge")
                if isinstance(judge, dict):
                    judge.pop("profile", None)
            milestone.pop("capabilities", None)
        return projection


def _accepted_projection(
    inspection: Mapping[str, Any], acceptance: Mapping[str, Any]
) -> dict[str, Any]:
    """Project operator-bound execution identity onto a legacy inspection.

    A pre-3.1 flow may name a local FlowStep package without declaring the
    exact executable ref or the closed ``execution`` object.  Acceptance is
    the one boundary where the operator supplies those missing facts.  Stage
    equivalence therefore compares the compiled flow with that accepted
    projection, while an already-versioned source ref remains byte-exact and
    is never rewritten.
    """

    projection = copy.deepcopy(inspection["semantic_projection"])
    accepted_by_id = {
        str(row["id"]): row
        for row in acceptance["milestones"]
        if isinstance(row, Mapping)
    }
    for milestone in projection.get("milestones") or []:
        if not isinstance(milestone, dict):
            continue
        declared = accepted_by_id[str(milestone["id"])]
        source_execution = milestone.get("execution")
        if source_execution is None:
            execution = copy.deepcopy(declared["execution"])
            candidate = execution.get("candidate_executor")
            if isinstance(candidate, dict):
                candidate.pop("profile", None)
            judge = execution.get("judge")
            if isinstance(judge, dict):
                judge.pop("profile", None)
            milestone["execution"] = execution
            refs_by_slot = {
                str(binding["tool"]): str(binding["ref"])
                for binding in execution.get("tool_bindings") or []
                if isinstance(binding, Mapping)
            }
            for flowstep in milestone.get("flowsteps") or []:
                if not isinstance(flowstep, dict):
                    continue
                source_ref = str(flowstep.get("tool") or "")
                if not _VERSIONED_REF.fullmatch(source_ref):
                    flowstep["tool"] = refs_by_slot[str(flowstep["id"])]
        elif isinstance(source_execution, dict):
            # Profiles are operator closure facts and are excluded from the
            # compiled equivalence projection on both sides.
            candidate = source_execution.get("candidate_executor")
            if isinstance(candidate, dict):
                candidate.pop("profile", None)
            judge = source_execution.get("judge")
            if isinstance(judge, dict):
                judge.pop("profile", None)
    return projection


def _first_projection_mismatch(
    expected: Any,
    actual: Any,
    path: str = "$",
) -> str:
    if type(expected) is not type(actual):
        return f"{path}: type {type(expected).__name__} != {type(actual).__name__}"
    if isinstance(expected, dict):
        if set(expected) != set(actual):
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            return f"{path}: keys differ; missing={missing}, extra={extra}"
        for key in expected:
            if expected[key] != actual[key]:
                return _first_projection_mismatch(expected[key], actual[key], f"{path}.{key}")
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path}: list length {len(expected)} != {len(actual)}"
        for index, (left, right) in enumerate(zip(expected, actual)):
            if left != right:
                return _first_projection_mismatch(left, right, f"{path}[{index}]")
    elif expected != actual:
        return f"{path}: {expected!r} != {actual!r}"
    return f"{path}: values differ"


def _resource_manifest(
    inspection: Mapping[str, Any], acceptance: Mapping[str, Any]
) -> dict[str, Any]:
    copied: dict[str, dict[str, Any]] = {}
    for row in [*inspection["resources"], *acceptance["workflow_contract_resources"]]:
        public = {
            "stage_path": row["stage_path"],
            "byte_count": row["byte_count"],
            "sha256": row["sha256"],
        }
        prior = copied.get(row["stage_path"])
        if prior is not None and prior != public:
            raise ImportFlowV4Error(f"resource manifest collision: {row['stage_path']}")
        copied[row["stage_path"]] = public
    manifest = {
        "schema": RESOURCE_MANIFEST_SCHEMA,
        "copied_resources": [copied[key] for key in sorted(copied)],
        "external_tool_packages": copy.deepcopy(inspection["tool_packages"]),
        "local_readiness_evidence_only": True,
    }
    return _with_digest(manifest, "resource_manifest_digest")


def _verify_core(
    inspection: Mapping[str, Any], acceptance: Mapping[str, Any], stage_dir: str | Path
) -> dict[str, Any]:
    """Compile staged source and return a base-independent equivalence proof."""

    _validate_acceptance(inspection, acceptance)
    stage = Path(stage_dir).resolve(strict=True)
    if not stage.is_dir():
        raise ImportFlowV4Error("stage path is not a directory")
    if (stage / "flow.yaml").exists():
        raise ImportFlowV4Error("stage must not contain an authored flow.yaml")
    for row in inspection["resources"]:
        path = _bounded_file(stage, row["stage_path"], label=f"staged resource {row['stage_path']}")
        if "sha256:" + sha256_file(path) != row["sha256"] or path.stat().st_size != row["byte_count"]:
            raise ImportFlowV4Error(f"staged resource differs from accepted bytes: {row['stage_path']}")
    for row in acceptance["workflow_contract_resources"]:
        path = _bounded_file(stage, row["stage_path"], label=f"staged workflow contract {row['stage_path']}")
        if "sha256:" + sha256_file(path) != row["sha256"] or path.stat().st_size != row["byte_count"]:
            raise ImportFlowV4Error(f"staged workflow contract differs from accepted bytes: {row['stage_path']}")
    try:
        source = load_skill_source(stage)
        compiled = compile_skill_source(stage)
        graph = _native_graph(source["milestones"])
    except SkillSourceError as exc:
        raise ImportFlowV4Error(f"staged skill source is invalid: {exc}") from exc
    expected_graph = {row["from"]: list(row["to"]) for row in inspection["graph"]}
    if graph != expected_graph:
        raise ImportFlowV4Error("staged native graph differs from the accepted graph")
    expected_projection = _accepted_projection(inspection, acceptance)
    projection = _compiled_projection(stage, compiled)
    digest = _digest(projection)
    if projection != expected_projection or digest != _digest(expected_projection):
        mismatch = _first_projection_mismatch(expected_projection, projection)
        raise ImportFlowV4Error(
            "staged compiled flow is not canonically equivalent to the accepted v4 flow; "
            + mismatch
        )
    return {
        "schema": PROOF_SCHEMA,
        "status": "PASS",
        "inspection_digest": inspection["inspection_digest"],
        "acceptance_digest": acceptance["acceptance_digest"],
        "source_semantic_digest": inspection["source_semantic_digest"],
        "staged_semantic_digest": digest,
        "compiled_flow_digest": _digest(compiled),
        "resource_manifest_digest": _resource_manifest(inspection, acceptance)["resource_manifest_digest"],
        "installed": False,
        "deployed": False,
    }


def _write_handoff(
    stage: Path, inspection: Mapping[str, Any], acceptance: Mapping[str, Any]
) -> None:
    handoff = stage / "planning" / "import-flow-v4"
    handoff.mkdir(parents=True, exist_ok=True)
    (handoff / "inspection.json").write_bytes(canonical_json_bytes(inspection))
    (handoff / "acceptance.json").write_bytes(canonical_json_bytes(acceptance))
    (handoff / "resource-manifest.json").write_bytes(
        canonical_json_bytes(_resource_manifest(inspection, acceptance))
    )
    (handoff / "equivalence-proof.json").write_bytes(
        canonical_json_bytes(_verify_core(inspection, acceptance, stage))
    )


def verify_staged_import(
    inspection: Mapping[str, Any], acceptance: Mapping[str, Any], stage_dir: str | Path
) -> dict[str, Any]:
    """Recheck source equivalence and the frozen, auditable staged handoff."""

    stage = Path(stage_dir).resolve(strict=True)
    proof = _verify_core(inspection, acceptance, stage)
    handoff = stage / "planning" / "import-flow-v4"
    expected = {
        "inspection.json": inspection,
        "acceptance.json": acceptance,
        "resource-manifest.json": _resource_manifest(inspection, acceptance),
        "equivalence-proof.json": proof,
    }
    for name, value in expected.items():
        path = handoff / name
        if not path.is_file() or path.read_bytes() != canonical_json_bytes(value):
            raise ImportFlowV4Error(f"staged handoff artifact is missing or drifted: {name}")
    return {
        "schema": VERIFY_SCHEMA,
        "status": "PASS",
        "stage_dir": str(stage),
        "handoff_dir": str(handoff),
        **{key: value for key, value in proof.items() if key not in {"schema", "status"}},
    }


def stage_import(
    inspection: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    stage_dir: str | Path,
    *,
    source_root: str | Path,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Materialize an accepted source tree at a new staging path only."""

    _validate_acceptance(inspection, acceptance)
    harness, project = _roots(source_root, project_root)
    _source_is_frozen(inspection, acceptance, harness, project)
    destination = Path(stage_dir).resolve()
    if destination.exists():
        raise ImportFlowV4Error("stage destination already exists; refusing to merge or overwrite")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.import-", dir=destination.parent))
    try:
        _write_stage(temporary, inspection, acceptance, harness, project)
        _verify_core(inspection, acceptance, temporary)
        _write_handoff(temporary, inspection, acceptance)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return verify_staged_import(inspection, acceptance, destination)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ImportFlowV4Error(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ImportFlowV4Error(f"JSON root must be an object: {path}")
    return value


def _emit(value: dict[str, Any], output: Path | None) -> None:
    payload = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if output is None:
        print(payload, end="")
        return
    if output.exists():
        raise ImportFlowV4Error(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit staging-only Builder 2 v4 importer")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect_parser = sub.add_parser("inspect")
    inspect_parser.add_argument("--source-root", type=Path, required=True)
    inspect_parser.add_argument("--flow", type=Path)
    inspect_parser.add_argument("--skill-name")
    inspect_parser.add_argument("--display-name")
    inspect_parser.add_argument("--description")
    inspect_parser.add_argument("--default-prompt")
    inspect_parser.add_argument("--out", type=Path)
    accept_parser = sub.add_parser("accept")
    accept_parser.add_argument("--inspection", type=Path, required=True)
    accept_parser.add_argument("--request", type=Path, required=True)
    accept_parser.add_argument("--source-root", type=Path, required=True)
    accept_parser.add_argument("--project-root", type=Path)
    accept_parser.add_argument("--out", type=Path)
    stage_parser = sub.add_parser("stage")
    stage_parser.add_argument("--inspection", type=Path, required=True)
    stage_parser.add_argument("--acceptance", type=Path, required=True)
    stage_parser.add_argument("--stage-dir", type=Path, required=True)
    stage_parser.add_argument("--source-root", type=Path, required=True)
    stage_parser.add_argument("--project-root", type=Path)
    stage_parser.add_argument("--out", type=Path)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--inspection", type=Path, required=True)
    verify_parser.add_argument("--acceptance", type=Path, required=True)
    verify_parser.add_argument("--stage-dir", type=Path, required=True)
    verify_parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            result = inspect_flow_v4(
                args.source_root,
                args.flow,
                skill_name=args.skill_name,
                display_name=args.display_name,
                description=args.description,
                default_prompt=args.default_prompt,
            )
        elif args.command == "accept":
            result = accept_import(
                _read_json(args.inspection),
                _read_json(args.request),
                source_root=args.source_root,
                project_root=args.project_root,
            )
        elif args.command == "stage":
            result = stage_import(
                _read_json(args.inspection),
                _read_json(args.acceptance),
                args.stage_dir,
                source_root=args.source_root,
                project_root=args.project_root,
            )
        else:
            result = verify_staged_import(
                _read_json(args.inspection), _read_json(args.acceptance), args.stage_dir
            )
        _emit(result, args.out)
    except (ImportFlowV4Error, FlowError, SkillSourceError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ACCEPTANCE_REQUEST_SCHEMA",
    "ACCEPTANCE_SCHEMA",
    "INSPECTION_SCHEMA",
    "ImportFlowV4Error",
    "VERIFY_SCHEMA",
    "accept_import",
    "inspect_flow_v4",
    "semantic_projection",
    "stage_import",
    "verify_staged_import",
]
