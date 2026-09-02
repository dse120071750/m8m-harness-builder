#!/usr/bin/env python3
"""Compile the Codex-native M8M skill dialect into ``flowstep_flow_v4``.

This module is deliberately source-only.  It reads a bounded skill tree,
validates its authored graph and resource references, and returns deterministic
Python dictionaries/bytes.  It does not write files, execute handlers, access
the network, install a skill, or submit a source bundle.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlsplit

import yaml
from jsonschema import Draft202012Validator
from yaml.events import AliasEvent

from execution_identity import canonical_candidate_executor_ref
from milestone_expectation import gem_success_rule, normalize_success_text


SKILL_SOURCE_SCHEMA = "m8m_skill_source_v1"
CANVAS_SCHEMA = "m8m_skill_canvas_v1"
MILESTONE_SCHEMA = "m8m_milestone_agent_v1"
FLOW_SCHEMA = "flowstep_flow_v4"
MAX_SOURCE_BYTES = 128 * 1024
MAX_SKILL_BODY_BYTES = 2 * 1024

_ROOT = Path(__file__).resolve().parents[1]
_CONTRACTS = _ROOT / "contracts"
_CANVAS_CONTRACT = _CONTRACTS / "m8m_skill_canvas_v1.schema.json"
_MILESTONE_CONTRACT = _CONTRACTS / "m8m_milestone_agent_v1.schema.json"
_FLOW_CONTRACT = _CONTRACTS / "flowstep_flow_v4.schema.json"
_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSIONED_REF = re.compile(
    r"^[a-z][a-z0-9_.-]*@[0-9]+\.[0-9]+\.[0-9]+$"
)
_GLOB = re.compile(r"[*?\[]")
_DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
_HARNESS_ONLY_RULES = {
    "Read only the declared read_paths. Do not set ok, branch, or cycle.",
    "Write only the declared write_paths.",
    "Use only the declared FlowSteps and tool bindings.",
    "Return only the declared draft or output contract.",
    "Do not perform undeclared external side effects.",
}


class SkillSourceError(ValueError):
    """The authored skill source is unsafe, incomplete, or ambiguous."""


class _StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects aliases and duplicate mapping keys."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            raise SkillSourceError("YAML aliases are not allowed in M8M source")
        return super().compose_node(parent, index)


def _construct_mapping(loader: _StrictSafeLoader, node: Any, deep: bool = False) -> dict[str, Any]:
    loader.flatten_mapping(node)
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise SkillSourceError("YAML mapping keys must be strings")
        if key in result:
            raise SkillSourceError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def _json_no_duplicates(text: str, *, label: str) -> Any:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SkillSourceError(f"{label}: duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=pairs_hook)
    except SkillSourceError:
        raise
    except (TypeError, ValueError) as exc:
        raise SkillSourceError(f"{label}: invalid JSON: {exc}") from exc


def _load_contract(path: Path) -> dict[str, Any]:
    data = _json_no_duplicates(path.read_text(encoding="utf-8"), label=path.name)
    if not isinstance(data, dict):
        raise RuntimeError(f"contract is not an object: {path}")
    Draft202012Validator.check_schema(data)
    return data


def _format_validation_error(label: str, error: Any) -> SkillSourceError:
    path = ".".join(str(item) for item in error.absolute_path)
    suffix = f" at {path}" if path else ""
    return SkillSourceError(f"{label}: {error.message}{suffix}")


def _validate_contract(value: Any, contract_path: Path, *, label: str) -> None:
    validator = Draft202012Validator(_load_contract(contract_path))
    errors = sorted(validator.iter_errors(value), key=lambda item: tuple(str(p) for p in item.absolute_path))
    if errors:
        raise _format_validation_error(label, errors[0])


def _safe_relative_path(value: Any, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.strip():
        raise SkillSourceError(f"{label}: expected a non-empty relative path")
    if value != value.strip():
        raise SkillSourceError(f"{label}: surrounding whitespace is not allowed")
    if "\\" in value:
        raise SkillSourceError(f"{label}: use '/' separators, not backslashes")
    if _GLOB.search(value):
        raise SkillSourceError(f"{label}: globs are not allowed")
    if re.match(r"^[A-Za-z]:", value) or value.startswith("/"):
        raise SkillSourceError(f"{label}: absolute paths are not allowed")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SkillSourceError(f"{label}: path must be normalized and stay inside the skill")
    return path


def _source_path(root: Path, value: Any, *, label: str, suffix: str | None = None) -> Path:
    rel = _safe_relative_path(value, label=label)
    if suffix and rel.suffix.lower() != suffix:
        raise SkillSourceError(f"{label}: expected a {suffix} file")
    candidate = root.joinpath(*rel.parts)
    if candidate.is_symlink():
        raise SkillSourceError(f"{label}: symbolic links are not allowed")
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise SkillSourceError(f"{label}: referenced file does not exist: {value}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SkillSourceError(f"{label}: referenced file escapes the skill root") from exc
    if not resolved.is_file():
        raise SkillSourceError(f"{label}: referenced path is not a regular file")
    return resolved


def _read_text_path(path: Path, *, label: str) -> str:
    if path.is_symlink():
        raise SkillSourceError(f"{label}: symbolic links are not allowed")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SkillSourceError(f"{label}: cannot stat source file") from exc
    if size > MAX_SOURCE_BYTES:
        raise SkillSourceError(f"{label}: source file exceeds {MAX_SOURCE_BYTES} bytes")
    try:
        return path.read_text(encoding="utf-8", errors="strict")
    except UnicodeError as exc:
        raise SkillSourceError(f"{label}: source file is not valid UTF-8") from exc
    except OSError as exc:
        raise SkillSourceError(f"{label}: cannot read source file") from exc


def _load_yaml_text(text: str, *, label: str) -> dict[str, Any]:
    try:
        value = yaml.load(text, Loader=_StrictSafeLoader)
    except SkillSourceError:
        raise
    except yaml.YAMLError as exc:
        raise SkillSourceError(f"{label}: invalid YAML: {exc}") from exc
    if not isinstance(value, dict):
        raise SkillSourceError(f"{label}: YAML root must be a mapping")
    return value


def _load_yaml_path(path: Path, *, label: str) -> dict[str, Any]:
    return _load_yaml_text(_read_text_path(path, label=label), label=label)


def _load_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = _read_text_path(path, label="SKILL.md")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillSourceError("SKILL.md: missing opening YAML frontmatter delimiter")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise SkillSourceError("SKILL.md: missing closing YAML frontmatter delimiter") from exc
    metadata = _load_yaml_text("\n".join(lines[1:end]), label="SKILL.md frontmatter")
    for field in ("name", "description"):
        if not isinstance(metadata.get(field), str) or not metadata[field].strip():
            raise SkillSourceError(f"SKILL.md frontmatter: missing non-empty {field}")
    return metadata, "\n".join(lines[end + 1 :]).lstrip("\n")


def _validate_skill_body(body: str) -> None:
    """Require a small invocation/canvas pointer, never a product recipe."""

    if len(body.encode("utf-8")) > MAX_SKILL_BODY_BYTES:
        raise SkillSourceError(
            f"SKILL.md body must be a pointer of at most {MAX_SKILL_BODY_BYTES} UTF-8 bytes"
        )
    lines = body.splitlines()
    headings = [line for line in lines if line.startswith("#")]
    if len(headings) != 1 or not headings[0].startswith("# "):
        raise SkillSourceError("SKILL.md body must contain exactly one level-1 heading")
    if any(
        line.startswith(("##", "```", "~~~", "- ", "* ", "+ "))
        or re.match(r"^\s*\d+[.)]\s", line)
        or (line.startswith("|") and line.endswith("|"))
        for line in lines
    ):
        raise SkillSourceError(
            "SKILL.md body must be invocation/pointer prose without recipes, lists, tables, or code blocks"
        )
    collapsed = " ".join(body.split())
    for token in ("agents/openai.yaml", "references/<milestone>.md"):
        if token not in collapsed:
            raise SkillSourceError(f"SKILL.md body must point to {token}")
    if "invoke" not in collapsed.casefold():
        raise SkillSourceError("SKILL.md body must state how to invoke the skill")


def _resolved_root(skill_root: str | Path) -> Path:
    root = Path(skill_root)
    try:
        root = root.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise SkillSourceError("skill root does not exist") from exc
    if not root.is_dir():
        raise SkillSourceError("skill root is not a directory")
    return root


def _validate_interface(openai: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"interface", "policy", "canvas"}
    unexpected = sorted(set(openai) - allowed)
    if unexpected:
        raise SkillSourceError(f"agents/openai.yaml: unexpected keys: {', '.join(unexpected)}")
    interface = openai.get("interface")
    if not isinstance(interface, dict):
        raise SkillSourceError("agents/openai.yaml: interface must be a mapping")
    allowed_interface = {"display_name", "short_description", "default_prompt"}
    unexpected_interface = sorted(set(interface) - allowed_interface)
    if unexpected_interface:
        raise SkillSourceError(
            "agents/openai.yaml: unexpected interface keys: " + ", ".join(unexpected_interface)
        )
    for key in ("display_name", "short_description", "default_prompt"):
        if not isinstance(interface.get(key), str) or not interface[key].strip():
            raise SkillSourceError(f"agents/openai.yaml: interface.{key} must be non-empty")
    policy = openai.get("policy")
    if policy is not None and not isinstance(policy, dict):
        raise SkillSourceError("agents/openai.yaml: policy must be a mapping")
    return copy.deepcopy(interface)


def _validate_graph(canvas: Mapping[str, Any]) -> dict[str, list[str]]:
    milestones = list(canvas["milestones"])
    milestone_set = set(milestones)
    adjacency: dict[str, list[str]] = {}
    for row in canvas["graph"]:
        source = row["from"]
        if source not in milestone_set:
            raise SkillSourceError(f"canvas graph references unknown source milestone: {source}")
        if source in adjacency:
            raise SkillSourceError(f"canvas graph has duplicate rows for milestone: {source}")
        targets = list(row["to"])
        for target in targets:
            if target not in milestone_set:
                raise SkillSourceError(f"canvas graph references unknown target milestone: {target}")
        adjacency[source] = targets

    missing_rows = [item for item in milestones if item not in adjacency]
    if missing_rows:
        raise SkillSourceError("canvas graph omits milestone rows: " + ", ".join(missing_rows))

    entry = canvas["entry"]
    if entry not in milestone_set:
        raise SkillSourceError(f"canvas entry is not a milestone: {entry}")
    incoming = {item: 0 for item in milestones}
    for source, targets in adjacency.items():
        for target in targets:
            incoming[target] += 1
    roots = [item for item in milestones if incoming[item] == 0]
    if roots != [entry]:
        raise SkillSourceError(
            "canvas graph must have exactly the declared entry root; found: " + ", ".join(roots)
        )

    reached: set[str] = set()
    pending = [entry]
    while pending:
        current = pending.pop()
        if current in reached:
            continue
        reached.add(current)
        pending.extend(reversed(adjacency[current]))
    unreachable = [item for item in milestones if item not in reached]
    if unreachable:
        raise SkillSourceError("canvas graph has unreachable milestones: " + ", ".join(unreachable))

    sinks = [item for item in milestones if not adjacency[item]]
    terminals = list(canvas["terminal_states"]["success"])
    if terminals != sinks:
        raise SkillSourceError(
            "canvas terminal_states.success must exactly list sink milestones in canvas order"
        )
    return adjacency


def _native_graph(agents: list[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Derive the graph represented by v4 roster/branch/cycle declarations.

    Linear sequencing is the roster order. Branch arms and cycles are native
    v4 metadata; raw ``next``/``join`` fields are legacy and must not be emitted.
    """

    order = [agent["agent_id"] for agent in agents]
    by_id = {agent["agent_id"]: agent for agent in agents}
    position = {milestone_id: index for index, milestone_id in enumerate(order)}
    expected: dict[str, list[str]] = {item: [] for item in order}
    managed: set[str] = set()
    globally_known_paths: set[str] = set()
    globally_known_cycles: set[str] = set()

    for agent in agents:
        milestone_id = agent["agent_id"]
        if agent.get("branch") and agent.get("cycle"):
            raise SkillSourceError(f"milestone {milestone_id}: cannot own both branch and cycle")
        if agent.get("on_path") and agent.get("on_cycle"):
            raise SkillSourceError(f"milestone {milestone_id}: cannot be on both a branch and cycle")

    for controller in agents:
        branch = controller.get("branch")
        if not isinstance(branch, dict) or not branch:
            continue
        controller_id = controller["agent_id"]
        if controller.get("on_path") or controller.get("on_cycle"):
            raise SkillSourceError(
                f"milestone {controller_id}: nested branch control is not supported by this source dialect"
            )
        paths = branch.get("paths")
        if not isinstance(paths, list) or len(paths) < 2:
            raise SkillSourceError(f"milestone {controller_id}: branch requires at least two paths")
        join = branch.get("join")
        if not isinstance(join, str) or join not in by_id:
            raise SkillSourceError(f"milestone {controller_id}: branch.join must name a milestone")
        if by_id[join].get("on_path") or by_id[join].get("on_cycle"):
            raise SkillSourceError(f"milestone {controller_id}: branch.join cannot be inside other control")
        branch_targets: list[str] = []
        local_paths: set[str] = set()
        for index, row in enumerate(paths):
            if not isinstance(row, dict):
                raise SkillSourceError(f"milestone {controller_id}: branch path {index} must be a mapping")
            path_id = row.get("id")
            then = row.get("then")
            if not isinstance(path_id, str) or not _ID.fullmatch(path_id):
                raise SkillSourceError(f"milestone {controller_id}: invalid branch path id")
            if path_id in local_paths or path_id in globally_known_paths:
                raise SkillSourceError(f"milestone {controller_id}: ambiguous branch path id {path_id}")
            local_paths.add(path_id)
            globally_known_paths.add(path_id)
            members = [item["agent_id"] for item in agents if item.get("on_path") == path_id]
            if not members:
                raise SkillSourceError(f"milestone {controller_id}: branch path {path_id} has no milestones")
            if then != members[0]:
                raise SkillSourceError(
                    f"milestone {controller_id}: branch path {path_id}.then must name its first milestone"
                )
            if position[controller_id] >= position[members[0]] or any(
                position[member] >= position[join] for member in members
            ):
                raise SkillSourceError(
                    f"milestone {controller_id}: branch roster must place controller, arms, then join"
                )
            branch_targets.append(members[0])
            for source, target in zip(members, members[1:]):
                expected[source] = [target]
            expected[members[-1]] = [join]
            managed.update(members)
        default = branch.get("default")
        if default not in local_paths:
            raise SkillSourceError(f"milestone {controller_id}: branch.default is not a declared path")
        expected[controller_id] = branch_targets
        managed.add(controller_id)

    unknown_on_paths = sorted(
        {
            str(agent.get("on_path"))
            for agent in agents
            if agent.get("on_path") and agent.get("on_path") not in globally_known_paths
        }
    )
    if unknown_on_paths:
        raise SkillSourceError("milestones reference undeclared branch paths: " + ", ".join(unknown_on_paths))

    for controller in agents:
        cycle = controller.get("cycle")
        if not isinstance(cycle, dict) or not cycle:
            continue
        controller_id = controller["agent_id"]
        if controller.get("on_path"):
            raise SkillSourceError(
                f"milestone {controller_id}: nested cycle control is not supported by this source dialect"
            )
        cycle_id = cycle.get("id")
        if not isinstance(cycle_id, str) or not _ID.fullmatch(cycle_id):
            raise SkillSourceError(f"milestone {controller_id}: cycle.id is required")
        if cycle_id in globally_known_cycles:
            raise SkillSourceError(f"milestone {controller_id}: duplicate cycle id {cycle_id}")
        globally_known_cycles.add(cycle_id)
        members = [item["agent_id"] for item in agents if item.get("on_cycle") == cycle_id]
        if not members:
            raise SkillSourceError(f"milestone {controller_id}: cycle {cycle_id} has no milestones")
        start = cycle.get("start")
        join = cycle.get("join")
        ledger = cycle.get("ledger")
        if start != members[0]:
            raise SkillSourceError(f"milestone {controller_id}: cycle.start must be its first milestone")
        if controller_id != members[-1]:
            raise SkillSourceError(f"milestone {controller_id}: cycle owner must be its last milestone")
        if not isinstance(join, str) or join not in by_id:
            raise SkillSourceError(f"milestone {controller_id}: cycle.join must name a milestone")
        if by_id[join].get("on_cycle") or by_id[join].get("on_path"):
            raise SkillSourceError(f"milestone {controller_id}: cycle.join cannot be inside other control")
        if not isinstance(ledger, str) or ledger not in by_id:
            raise SkillSourceError(f"milestone {controller_id}: cycle.ledger must name a milestone")
        if ledger in members:
            raise SkillSourceError(f"milestone {controller_id}: cycle.ledger cannot be inside the cycle")
        if by_id[ledger].get("branch") or by_id[ledger].get("cycle"):
            raise SkillSourceError(
                f"milestone {controller_id}: cycle.ledger cannot also own flow control"
            )
        if not (
            position[ledger] < position[members[0]]
            and position[members[-1]] < position[join]
        ):
            raise SkillSourceError(
                f"milestone {controller_id}: cycle roster must place ledger, body, then join"
            )
        expected[ledger] = [start]
        for source, target in zip(members, members[1:]):
            expected[source] = [target]
        expected[controller_id] = [start, join]
        managed.update(members)
        managed.add(ledger)

    unknown_on_cycles = sorted(
        {
            str(agent.get("on_cycle"))
            for agent in agents
            if agent.get("on_cycle") and agent.get("on_cycle") not in globally_known_cycles
        }
    )
    if unknown_on_cycles:
        raise SkillSourceError("milestones reference undeclared cycles: " + ", ".join(unknown_on_cycles))

    for index, milestone_id in enumerate(order):
        if milestone_id in managed:
            continue
        expected[milestone_id] = [order[index + 1]] if index + 1 < len(order) else []
    return expected


def _validate_native_graph(
    agents: list[Mapping[str, Any]], adjacency: Mapping[str, list[str]]
) -> None:
    expected = _native_graph(agents)
    for milestone_id in (agent["agent_id"] for agent in agents):
        if list(adjacency[milestone_id]) != expected[milestone_id]:
            raise SkillSourceError(
                f"canvas graph for {milestone_id} does not match native v4 "
                f"branch/cycle/roster semantics; expected {expected[milestone_id]}"
            )


def _heading_ids(markdown: str) -> set[str]:
    result: set[str] = set()
    for line in markdown.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if not match:
            continue
        title = match.group(1).strip().strip("`")
        result.add(title)
    return result


def _validate_json_resource(path: Path, *, label: str, schema: bool = False) -> Any:
    value = _json_no_duplicates(_read_text_path(path, label=label), label=label)
    if not isinstance(value, (dict, list)):
        raise SkillSourceError(f"{label}: JSON resource must contain an object or array")
    if schema:
        if not isinstance(value, dict):
            raise SkillSourceError(f"{label}: JSON Schema must be an object")
        try:
            Draft202012Validator.check_schema(value)
        except Exception as exc:
            raise SkillSourceError(f"{label}: invalid JSON Schema: {exc}") from exc
    return value


def _validate_output_schema_ports(
    schema: Mapping[str, Any],
    outputs: Iterable[Mapping[str, Any]],
    *,
    label: str,
) -> None:
    """Prove that one candidate schema exactly represents its declared ports."""

    declared = list(outputs)
    declared_ids = [str(item.get("id") or "") for item in declared]
    required_ids = {
        str(item.get("id") or "") for item in declared if bool(item.get("required"))
    }
    root_properties = schema.get("properties")
    root_required = schema.get("required")
    if schema.get("type") != "object" or not isinstance(root_properties, dict):
        raise SkillSourceError(f"{label}: root must be an object with an outputs property")
    if schema.get("additionalProperties") is not False:
        raise SkillSourceError(f"{label}: root must set additionalProperties to false")
    if not isinstance(root_required, list) or "outputs" not in root_required:
        raise SkillSourceError(f"{label}: root must require outputs")
    output_envelope = root_properties.get("outputs")
    if not isinstance(output_envelope, dict) or output_envelope.get("type") != "object":
        raise SkillSourceError(f"{label}: outputs must be an object schema")
    if output_envelope.get("additionalProperties") is not False:
        raise SkillSourceError(
            f"{label}: outputs object must set additionalProperties to false"
        )
    port_properties = output_envelope.get("properties")
    port_required = output_envelope.get("required")
    if not isinstance(port_properties, dict):
        raise SkillSourceError(f"{label}: outputs.properties must declare every output port")
    actual_ids = set(port_properties)
    expected_ids = set(declared_ids)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        unknown = sorted(actual_ids - expected_ids)
        raise SkillSourceError(
            f"{label}: output schema ports differ from declarations; "
            f"missing={missing}, unknown={unknown}"
        )
    if not isinstance(port_required, list) or set(port_required) != required_ids:
        raise SkillSourceError(
            f"{label}: outputs.required must exactly match required declarations"
        )
    for output in declared:
        output_id = str(output.get("id") or "")
        port_schema = port_properties[output_id]
        if not isinstance(port_schema, dict):
            raise SkillSourceError(f"{label}: output port {output_id} schema must be an object")
        schema_type = port_schema.get("type")
        permits_array = schema_type == "array" or (
            isinstance(schema_type, list) and "array" in schema_type
        )
        cardinality = str(output.get("cardinality") or "")
        if cardinality == "many" and schema_type != "array":
            raise SkillSourceError(
                f"{label}: output port {output_id} cardinality many requires an array schema"
            )
        if cardinality == "one" and permits_array:
            raise SkillSourceError(
                f"{label}: output port {output_id} cardinality one cannot use an array schema"
            )


def _validate_closed_judge_receipt_schema(path: Path, *, label: str) -> None:
    value = _json_no_duplicates(_read_text_path(path, label=label), label=label)
    if not isinstance(value, dict):
        raise SkillSourceError(f"{label}: strict judge result schema must be an object")
    properties = value.get("properties")
    required = value.get("required")
    if value.get("type") != "object":
        raise SkillSourceError(f"{label}: strict judge result schema must be an object")
    if value.get("additionalProperties") is not False:
        raise SkillSourceError(f"{label}: strict judge result schema must be closed")
    expected = {"decision", "reasons", "blockers"}
    if not isinstance(required, list) or set(required) != expected:
        raise SkillSourceError(
            f"{label}: strict judge result schema must require exactly "
            "decision, reasons, and blockers"
        )
    if not isinstance(properties, dict) or set(properties) != expected:
        raise SkillSourceError(
            f"{label}: strict judge result schema properties must be exactly "
            "decision, reasons, and blockers"
        )
    decision = properties["decision"]
    if not isinstance(decision, dict) or set(decision.get("enum") or []) != {
        "PASS",
        "RETRY",
        "BLOCKED",
    }:
        raise SkillSourceError(
            f"{label}: strict judge result decision must enumerate PASS, RETRY, and BLOCKED"
        )
    for field, minimum in (("reasons", 1), ("blockers", 0)):
        member = properties[field]
        if (
            not isinstance(member, dict)
            or member.get("type") != "array"
            or int(member.get("minItems") or 0) != minimum
            or not isinstance(member.get("maxItems"), int)
            or int(member["maxItems"]) > 8
        ):
            raise SkillSourceError(
                f"{label}: strict judge result {field} must be a bounded array"
            )
        items = member.get("items")
        if (
            not isinstance(items, dict)
            or items.get("type") != "string"
            or int(items.get("minLength") or 0) < 1
            or not isinstance(items.get("maxLength"), int)
            or int(items["maxLength"]) > 512
        ):
            raise SkillSourceError(
                f"{label}: strict judge result {field} items must be bounded non-empty strings"
            )


def _schema_refs(value: Any, *, trail: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_trail = f"{trail}.{key}"
            if key == "$ref":
                if not isinstance(child, str):
                    raise SkillSourceError(f"{child_trail}: JSON Schema $ref must be a string")
                yield child_trail, child
            else:
                yield from _schema_refs(child, trail=child_trail)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _schema_refs(child, trail=f"{trail}[{index}]")


def _resolve_schema_fragment(document: Any, fragment: str, *, label: str) -> None:
    if not fragment:
        return
    decoded = unquote(fragment)
    if decoded.startswith("/"):
        current = document
        for raw_token in decoded[1:].split("/"):
            token = raw_token.replace("~1", "/").replace("~0", "~")
            if isinstance(current, dict) and token in current:
                current = current[token]
            elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
                current = current[int(token)]
            else:
                raise SkillSourceError(f"{label}: unresolved JSON Schema fragment #{decoded}")
        return

    pending = [document]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            if current.get("$anchor") == decoded or current.get("$dynamicAnchor") == decoded:
                return
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    raise SkillSourceError(f"{label}: unresolved JSON Schema anchor #{decoded}")


def _schema_ref_path(
    root: Path,
    source_ref: str,
    raw_ref: str,
    *,
    label: str,
) -> tuple[str, str]:
    parsed = urlsplit(raw_ref)
    if parsed.scheme or parsed.netloc or parsed.query:
        raise SkillSourceError(f"{label}: external JSON Schema refs are not allowed")
    raw_path = unquote(parsed.path)
    if not raw_path:
        return source_ref, parsed.fragment
    if "\\" in raw_path or raw_path.startswith("/") or _GLOB.search(raw_path):
        raise SkillSourceError(f"{label}: JSON Schema ref must be a local relative .json path")
    source_path = root.joinpath(*PurePosixPath(source_ref).parts)
    candidate = source_path.parent.joinpath(*PurePosixPath(raw_path).parts)
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise SkillSourceError(f"{label}: referenced JSON Schema is missing: {raw_path}") from exc
    if not resolved.is_relative_to(root) or resolved == root:
        raise SkillSourceError(f"{label}: JSON Schema ref escapes the skill root")
    if resolved.is_symlink() or not resolved.is_file() or resolved.suffix.lower() != ".json":
        raise SkillSourceError(f"{label}: JSON Schema ref must resolve to a regular .json file")
    return resolved.relative_to(root).as_posix(), parsed.fragment


def _expand_schema_resource_dependencies(
    root: Path,
    resources: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Close every authored schema over local, declared, transitive dependencies."""

    schema_paths = {
        str(item["path"])
        for item in resources
        if item.get("kind") == "schema"
    }
    pending = sorted(schema_paths)
    documents: dict[str, dict[str, Any]] = {}
    while pending:
        source_ref = pending.pop(0)
        if source_ref in documents:
            continue
        path = _source_path(
            root,
            source_ref,
            label=f"schema registry {source_ref}",
            suffix=".json",
        )
        value = _json_no_duplicates(
            _read_text_path(path, label=f"schema registry {source_ref}"),
            label=f"schema registry {source_ref}",
        )
        if not isinstance(value, dict):
            raise SkillSourceError(f"schema registry {source_ref}: JSON Schema must be an object")
        try:
            Draft202012Validator.check_schema(value)
        except Exception as exc:
            raise SkillSourceError(
                f"schema registry {source_ref}: invalid JSON Schema: {exc}"
            ) from exc
        documents[source_ref] = value
        for trail, raw_ref in _schema_refs(value):
            target_ref, fragment = _schema_ref_path(
                root,
                source_ref,
                raw_ref,
                label=f"schema registry {source_ref} {trail}",
            )
            if target_ref not in documents and target_ref not in pending:
                pending.append(target_ref)
                pending.sort()
            target_path = _source_path(
                root,
                target_ref,
                label=f"schema registry {target_ref}",
                suffix=".json",
            )
            target_value = documents.get(target_ref)
            if target_value is None:
                target_value = _json_no_duplicates(
                    _read_text_path(target_path, label=f"schema registry {target_ref}"),
                    label=f"schema registry {target_ref}",
                )
            _resolve_schema_fragment(
                target_value,
                fragment,
                label=f"schema registry {source_ref} {trail}",
            )

    by_path = {str(item["path"]): item for item in resources}
    for source_ref in sorted(documents):
        existing = by_path.get(source_ref)
        if existing is not None:
            existing["kind"] = "schema"
            continue
        digest = hashlib.sha256(source_ref.encode("utf-8")).hexdigest()[:16]
        row = {
            "owner": "schema_registry",
            "id": f"dependency_{digest}",
            "kind": "schema",
            "path": source_ref,
        }
        resources.append(row)
        by_path[source_ref] = row
    return resources


def _validate_workflow_contracts(
    root: Path,
    canvas: Mapping[str, Any],
    agents: list[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Validate required authored root I/O contracts and terminal projections.

    These declarations describe the skill-facing request, configuration, and
    result boundary.  They are source-bundle resources, not executable v4 flow
    fields, so ``compile_skill_source`` deliberately omits them.
    """

    contracts = canvas.get("workflow_contracts")
    if not isinstance(contracts, dict):
        raise SkillSourceError(
            "canvas workflow_contracts is required for every skill-native Builder-3 source"
        )

    resources: list[dict[str, str]] = []
    schemas: dict[str, dict[str, Any]] = {}
    for field in ("request_schema", "configuration_schema", "result_schema"):
        ref = contracts[field]
        path = _source_path(root, ref, label=f"canvas workflow_contracts.{field}", suffix=".json")
        label = f"canvas workflow_contracts.{field}"
        value = _json_no_duplicates(_read_text_path(path, label=label), label=label)
        if not isinstance(value, dict):
            raise SkillSourceError(f"{label}: JSON Schema must be an object")
        if value.get("$schema") != _DRAFT_2020_12:
            raise SkillSourceError(
                f"{label}: JSON Schema must declare Draft 2020-12 with $schema"
            )
        try:
            Draft202012Validator.check_schema(value)
        except Exception as exc:
            raise SkillSourceError(f"{label}: invalid JSON Schema: {exc}") from exc
        if value.get("type") != "object" or value.get("additionalProperties") is not False:
            raise SkillSourceError(
                f"{label}: root workflow contract must be a closed object schema"
            )
        schemas[field] = value
        resources.append(
            {
                "owner": "workflow",
                "id": field,
                "kind": "schema",
                "path": ref,
            }
        )

    by_id = {agent["agent_id"]: agent for agent in agents}
    terminal_ids = set(canvas["terminal_states"]["success"])
    names: set[str] = set()
    sources_and_ports: set[tuple[str, str]] = set()
    for index, binding in enumerate(contracts["terminal_bindings"]):
        name = binding["name"]
        if name in names:
            raise SkillSourceError(
                f"canvas workflow_contracts.terminal_bindings[{index}]: duplicate name {name}"
            )
        names.add(name)
        source, contract = binding["from"].split(".", 1)
        if source not in terminal_ids:
            raise SkillSourceError(
                f"canvas workflow_contracts.terminal_bindings[{index}]: "
                f"{source} is not a declared success terminal milestone"
            )
        agent = by_id[source]
        if contract != agent["output_contract"]:
            raise SkillSourceError(
                f"canvas workflow_contracts.terminal_bindings[{index}]: "
                f"contract does not match {source}"
            )
        output = binding["output"]
        output_ids = {item["id"] for item in agent["outputs"]}
        if output not in output_ids:
            raise SkillSourceError(
                f"canvas workflow_contracts.terminal_bindings[{index}]: "
                f"unknown output port {output} on {source}"
            )
        source_and_port = (source, output)
        if source_and_port in sources_and_ports:
            raise SkillSourceError(
                f"canvas workflow_contracts.terminal_bindings[{index}]: "
                f"duplicate terminal output {source}.{output}"
            )
        sources_and_ports.add(source_and_port)

    result_schema = schemas["result_schema"]
    result_properties = result_schema.get("properties")
    result_required = result_schema.get("required")
    if not isinstance(result_properties, dict) or set(result_properties) != names:
        raise SkillSourceError(
            "canvas workflow_contracts.result_schema: properties must exactly match "
            "terminal binding names"
        )
    if not isinstance(result_required, list) or set(result_required) != names:
        raise SkillSourceError(
            "canvas workflow_contracts.result_schema: required must exactly match "
            "terminal binding names"
        )
    return resources


def _validate_agent_resources(root: Path, agent: Mapping[str, Any]) -> list[dict[str, str]]:
    milestone_id = agent["agent_id"]
    gem_ref = agent["gem"]
    expected_gem = f"references/{milestone_id}.md"
    if gem_ref != expected_gem:
        raise SkillSourceError(
            f"milestone {milestone_id}: gem must be the exact path {expected_gem}"
        )
    gem_path = _source_path(root, gem_ref, label=f"milestone {milestone_id} gem", suffix=".md")
    gem_text = _read_text_path(gem_path, label=f"milestone {milestone_id} gem")
    headings = _heading_ids(gem_text)
    if "Rule of success" in headings:
        gem_rule = gem_success_rule(gem_text)
        if not normalize_success_text(gem_rule):
            raise SkillSourceError(
                f"milestone {milestone_id}: Gem Rule of success must project the authored success"
            )
        if normalize_success_text(gem_rule) != normalize_success_text(agent["success"]):
            raise SkillSourceError(
                f"milestone {milestone_id}: Gem Rule of success differs from the authored success; "
                "keep one expectation authority"
            )
    for flowstep in agent["flowsteps"]:
        if flowstep["id"] not in headings:
            raise SkillSourceError(
                f"milestone {milestone_id}: gem lacks ## `{flowstep['id']}`"
            )

    read_paths = list(agent["read_paths"])
    if gem_ref not in read_paths:
        raise SkillSourceError(f"milestone {milestone_id}: read_paths must include its exact gem")

    resources: list[dict[str, str]] = [
        {"owner": milestone_id, "id": "gem", "kind": "gem", "path": gem_ref}
    ]
    seen_paths = {gem_ref}
    for index, ref in enumerate(read_paths):
        path = _source_path(root, ref, label=f"milestone {milestone_id} read_paths[{index}]")
        if ref not in seen_paths:
            resources.append(
                {"owner": milestone_id, "id": f"read_{index}", "kind": "reference", "path": ref}
            )
            seen_paths.add(ref)
        if path.suffix.lower() == ".json":
            _validate_json_resource(path, label=f"milestone {milestone_id} read_paths[{index}]")

    schema_fields = ("output_schema", "input_schema", "draft_schema", "receipt_schema")
    for field in schema_fields:
        ref = agent.get(field)
        if ref is None:
            continue
        path = _source_path(root, ref, label=f"milestone {milestone_id} {field}", suffix=".json")
        schema_value = _validate_json_resource(
            path,
            label=f"milestone {milestone_id} {field}",
            schema=True,
        )
        if field == "output_schema":
            _validate_output_schema_ports(
                schema_value,
                agent["outputs"],
                label=f"milestone {milestone_id} output_schema",
            )
        if (
            field == "receipt_schema"
            and isinstance(agent.get("execution"), dict)
            and isinstance(agent["execution"].get("judge"), dict)
        ):
            _validate_closed_judge_receipt_schema(
                path,
                label=f"milestone {milestone_id} receipt_schema",
            )
        if ref not in seen_paths:
            resources.append(
                {"owner": milestone_id, "id": field, "kind": "schema", "path": ref}
            )
            seen_paths.add(ref)

    for control_field in ("branch", "cycle"):
        control = agent.get(control_field)
        if not isinstance(control, dict) or "receipt_schema" not in control:
            continue
        ref = control["receipt_schema"]
        path = _source_path(
            root,
            ref,
            label=f"milestone {milestone_id} {control_field}.receipt_schema",
            suffix=".json",
        )
        _validate_json_resource(
            path,
            label=f"milestone {milestone_id} {control_field}.receipt_schema",
            schema=True,
        )
        if ref not in seen_paths:
            resources.append(
                {
                    "owner": milestone_id,
                    "id": f"{control_field}_receipt_schema",
                    "kind": "schema",
                    "path": ref,
                }
            )
            seen_paths.add(ref)

    seen_resource_ids: set[str] = set()
    for index, resource in enumerate(agent.get("resources", [])):
        resource_id = resource["id"]
        if resource_id in seen_resource_ids:
            raise SkillSourceError(f"milestone {milestone_id}: duplicate resource id {resource_id}")
        seen_resource_ids.add(resource_id)
        ref = resource["path"]
        _source_path(root, ref, label=f"milestone {milestone_id} resources[{index}]")
        if ref not in seen_paths:
            resources.append(
                {
                    "owner": milestone_id,
                    "id": resource_id,
                    "kind": resource["kind"],
                    "path": ref,
                }
            )
            seen_paths.add(ref)

    for index, ref in enumerate(agent.get("write_paths", [])):
        _safe_relative_path(ref, label=f"milestone {milestone_id} write_paths[{index}]")
    return resources


def _validate_agent_semantics(
    agent: Mapping[str, Any], *, flow_id: str
) -> None:
    milestone_id = agent["agent_id"]
    _safe_relative_path(agent["handler"], label=f"milestone {milestone_id} handler")
    for index, rule in enumerate(agent.get("rules") or []):
        if rule not in _HARNESS_ONLY_RULES:
            raise SkillSourceError(
                f"milestone {milestone_id}: rules[{index}] must be one canonical "
                "harness-only directive; product instructions belong in the milestone Gem"
            )
    for index, ref in enumerate(agent.get("implementation_dependencies", [])):
        _safe_relative_path(
            ref,
            label=f"milestone {milestone_id} implementation_dependencies[{index}]",
        )
    output_ids = [item["id"] for item in agent["outputs"]]
    if len(output_ids) != len(set(output_ids)):
        raise SkillSourceError(f"milestone {milestone_id}: output ids must be unique")
    flowstep_ids = [item["id"] for item in agent["flowsteps"]]
    if len(flowstep_ids) != len(set(flowstep_ids)):
        raise SkillSourceError(f"milestone {milestone_id}: FlowStep ids must be unique")
    derived_tools = list(dict.fromkeys(item["id"] for item in agent["flowsteps"]))
    flowstep_local_tools = {
        str(item["id"]): str(item["tool"])
        for item in agent["flowsteps"]
    }
    if "tools" in agent and list(agent["tools"]) != derived_tools:
        raise SkillSourceError(
            f"milestone {milestone_id}: tools must equal first-use FlowStep id order"
        )
    capabilities = list(agent.get("capabilities") or [])
    capability_ids = [str(item.get("id") or "") for item in capabilities]
    if len(capability_ids) != len(set(capability_ids)):
        raise SkillSourceError(f"milestone {milestone_id}: capability ids must be unique")

    intelligence = str(agent.get("intelligence") or "none")
    loop = str(agent.get("loop") or "none")
    execution = agent.get("execution")
    if not isinstance(execution, dict):
        raise SkillSourceError(
            f"milestone {milestone_id}: execution.candidate_executor and exact tool_bindings are required"
        )
    separate_judge = (
        execution.get("judge")
        if isinstance(execution, dict) and isinstance(execution.get("judge"), dict)
        else None
    )
    if isinstance(execution, dict):
        candidate = execution.get("candidate_executor")
        if not isinstance(candidate, dict):
            raise SkillSourceError(
                f"milestone {milestone_id}: execution.candidate_executor is required"
            )
        candidate_ref = str(candidate.get("ref") or "")
        expected_candidate_ref = canonical_candidate_executor_ref(
            flow_id, milestone_id
        )
        if candidate_ref != expected_candidate_ref:
            raise SkillSourceError(
                f"milestone {milestone_id}: execution.candidate_executor.ref must be "
                f"{expected_candidate_ref}; regenerate with m8m-harness-builder 3.1"
            )
        tool_bindings = list(execution.get("tool_bindings") or [])
        bound_tool_ids = [str(item.get("tool") or "") for item in tool_bindings]
        if len(bound_tool_ids) != len(set(bound_tool_ids)):
            raise SkillSourceError(
                f"milestone {milestone_id}: execution.tool_bindings tools must be unique"
            )
        if bound_tool_ids != derived_tools:
            raise SkillSourceError(
                f"milestone {milestone_id}: execution.tool_bindings must exactly cover FlowStep ids"
            )
        for binding in tool_bindings:
            flowstep_id = str(binding.get("tool") or "")
            ref = str(binding.get("ref") or "")
            match = _VERSIONED_REF.fullmatch(ref)
            local_name = (
                ref.rsplit("@", 1)[0] if match else ""
            )
            if local_name != flowstep_local_tools[flowstep_id]:
                raise SkillSourceError(
                    f"milestone {milestone_id}: execution.tool_bindings[{flowstep_id}].ref "
                    "must be an exact versioned ref for its authored local FlowStep tool"
                )
        if loop == "judge" and not isinstance(execution.get("judge"), dict):
            raise SkillSourceError(
                f"milestone {milestone_id}: judge loop requires explicit execution.judge ref"
            )

        candidate_profile = candidate.get("profile")
        judge = separate_judge
        judge_profile = judge.get("profile") if judge else None
        if intelligence != "none" and not isinstance(candidate_profile, dict):
            raise SkillSourceError(
                f"milestone {milestone_id}: intelligence requires an explicit candidate profile"
            )
        if intelligence == "judge" and not isinstance(judge_profile, dict):
            raise SkillSourceError(
                f"milestone {milestone_id}: intelligence=judge requires a separate judge profile"
            )
        if isinstance(candidate_profile, dict) and intelligence == "none":
            raise SkillSourceError(
                f"milestone {milestone_id}: a candidate profile requires candidate intelligence"
            )
        if isinstance(candidate_profile, dict):
            if list(candidate_profile.get("tools") or []) != derived_tools:
                raise SkillSourceError(
                    f"milestone {milestone_id}: candidate profile tools must exactly cover FlowStep ids"
                )
            if not agent.get("input_schema") or not agent.get("draft_schema"):
                raise SkillSourceError(
                    f"milestone {milestone_id}: AI candidate requires input_schema and draft_schema"
                )
        if isinstance(judge_profile, dict) and judge_profile.get("tools"):
            raise SkillSourceError(
                f"milestone {milestone_id}: AI judge profile must not bind candidate tools"
            )
        if isinstance(judge_profile, dict) and not agent.get("receipt_schema"):
            raise SkillSourceError(
                f"milestone {milestone_id}: AI judge requires receipt_schema"
            )
        if isinstance(judge_profile, dict) and not agent.get("input_schema"):
            raise SkillSourceError(
                f"milestone {milestone_id}: AI judge requires input_schema"
            )
        if (
            isinstance(candidate_profile, dict)
            and isinstance(judge_profile, dict)
            and candidate_profile.get("ref") == judge_profile.get("ref")
        ):
            raise SkillSourceError(
                f"milestone {milestone_id}: candidate and judge profiles must be separate"
            )
        bound_capability_ids = {
            str(capability_id)
            for profile in (candidate_profile, judge_profile)
            if isinstance(profile, dict)
            for capability_id in profile.get("capabilities") or []
        }
        unknown_capabilities = bound_capability_ids - set(capability_ids)
        if unknown_capabilities:
            raise SkillSourceError(
                f"milestone {milestone_id}: profiles bind undeclared capabilities: "
                + ", ".join(sorted(unknown_capabilities))
            )
        if (
            isinstance(candidate_profile, dict) or isinstance(judge_profile, dict)
        ) and bound_capability_ids != set(capability_ids):
            raise SkillSourceError(
                f"milestone {milestone_id}: profiles must exactly bind declared capabilities"
            )
    observer = agent["observer"]
    action_ids = [item["flowstep_id"] for item in observer.get("actions", [])]
    if action_ids and action_ids != flowstep_ids:
        raise SkillSourceError(
            f"milestone {milestone_id}: observer actions must cover FlowSteps once in order"
        )
    observer_output_ids = [item["output_id"] for item in observer.get("outputs", [])]
    if observer_output_ids and observer_output_ids != output_ids:
        raise SkillSourceError(
            f"milestone {milestone_id}: observer outputs must cover output ports once in order"
        )
    if loop == "judge" and not agent.get("worker"):
        raise SkillSourceError(f"milestone {milestone_id}: judge loop requires worker")
    if loop == "judge" and isinstance(separate_judge, dict):
        worker_ref = str(agent.get("worker") or "")
        judge_ref = str(separate_judge.get("ref") or "")
        if worker_ref != judge_ref:
            raise SkillSourceError(
                f"milestone {milestone_id}: worker must exactly equal execution.judge.ref"
            )
        candidate = execution.get("candidate_executor") if isinstance(execution, dict) else None
        candidate_ref = str(candidate.get("ref") or "") if isinstance(candidate, dict) else ""
        tool_refs = {
            str(item.get("ref") or "")
            for item in (execution.get("tool_bindings") or [])
            if isinstance(item, dict)
        }
        if judge_ref == candidate_ref or judge_ref in tool_refs:
            raise SkillSourceError(
                f"milestone {milestone_id}: execution.judge.ref must be distinct from candidate and tool refs"
            )
    judge_abi = str(agent.get("judge_abi") or "")
    if judge_abi and loop != "judge":
        raise SkillSourceError(
            f"milestone {milestone_id}: judge_abi is valid only for loop=judge"
        )
    if judge_abi and not agent.get("receipt_schema"):
        raise SkillSourceError(
            f"milestone {milestone_id}: strict judge ABI requires receipt_schema"
        )
    if separate_judge is not None and judge_abi != "m8m_milestone_judge_v1":
        raise SkillSourceError(
            f"milestone {milestone_id}: execution.judge requires judge_abi m8m_milestone_judge_v1"
        )
    if separate_judge is not None and loop != "judge":
        raise SkillSourceError(
            f"milestone {milestone_id}: execution.judge requires loop=judge"
        )
    milestone_external = agent.get("side_effects", "none") == "external"
    capability_external = any(
        item.get("side_effects") == "external" for item in capabilities
    )
    if milestone_external and "phase_journal" not in agent:
        raise SkillSourceError(f"milestone {milestone_id}: external side effects require phase_journal")
    if milestone_external and not capability_external:
        raise SkillSourceError(
            f"milestone {milestone_id}: external side effects require an explicit external capability"
        )
    if capability_external and not milestone_external:
        raise SkillSourceError(
            f"milestone {milestone_id}: an external capability requires side_effects external"
        )


def _binding_source(binding: Any) -> tuple[str, str] | None:
    value = binding if isinstance(binding, str) else binding.get("from")
    if not isinstance(value, str) or "." not in value:
        return None
    source, contract = value.split(".", 1)
    return source, contract


def _ancestors(adjacency: Mapping[str, list[str]], node: str) -> set[str]:
    reverse: dict[str, set[str]] = {item: set() for item in adjacency}
    for source, targets in adjacency.items():
        for target in targets:
            reverse[target].add(source)
    result: set[str] = set()
    pending = list(reverse[node])
    while pending:
        current = pending.pop()
        if current in result:
            continue
        result.add(current)
        pending.extend(reverse[current])
    return result


def _validate_bindings(
    agents: list[Mapping[str, Any]], adjacency: Mapping[str, list[str]]
) -> None:
    by_id = {agent["agent_id"]: agent for agent in agents}
    for agent in agents:
        milestone_id = agent["agent_id"]
        allowed_upstream = _ancestors(adjacency, milestone_id)
        for input_id, binding in agent.get("inputs", {}).items():
            parsed = _binding_source(binding)
            if parsed is None:
                raise SkillSourceError(
                    f"milestone {milestone_id} input {input_id}: binding must name source.contract"
                )
            source, contract = parsed
            if source == "user":
                continue
            if source not in by_id:
                raise SkillSourceError(
                    f"milestone {milestone_id} input {input_id}: unknown source milestone {source}"
                )
            if source not in allowed_upstream:
                raise SkillSourceError(
                    f"milestone {milestone_id} input {input_id}: {source} is not upstream"
                )
            if contract != by_id[source]["output_contract"]:
                raise SkillSourceError(
                    f"milestone {milestone_id} input {input_id}: contract does not match {source}"
                )
            if isinstance(binding, dict) and "output" in binding:
                output_ids = {item["id"] for item in by_id[source]["outputs"]}
                if binding["output"] not in output_ids:
                    raise SkillSourceError(
                        f"milestone {milestone_id} input {input_id}: unknown output port"
                    )


def _agent_file_names(root: Path, milestones: Iterable[str]) -> None:
    agents_dir = root / "agents"
    if not agents_dir.is_dir():
        raise SkillSourceError("agents directory is missing")
    allowed = {"openai.yaml"}
    for milestone_id in milestones:
        allowed.add(f"{milestone_id}.yaml")
        allowed.add(f"{milestone_id}_judge.yaml")
    for path in agents_dir.iterdir():
        if path.is_symlink():
            raise SkillSourceError(f"agents/{path.name}: symbolic links are not allowed")
        if path.is_file() and path.suffix.lower() in {".yaml", ".yml"} and path.name not in allowed:
            raise SkillSourceError(f"agents/{path.name}: undeclared agent file")


def load_skill_source(skill_root: str | Path) -> dict[str, Any]:
    """Load and validate one skill-native M8M source tree.

    The returned dictionary is base-independent: it contains no absolute root
    path or runtime identity.  All path values remain normalized skill-relative
    references.
    """

    root = _resolved_root(skill_root)
    skill_path = _source_path(root, "SKILL.md", label="SKILL.md", suffix=".md")
    frontmatter, body = _load_frontmatter(skill_path)
    _validate_skill_body(body)
    openai_path = _source_path(root, "agents/openai.yaml", label="agents/openai.yaml", suffix=".yaml")
    openai = _load_yaml_path(openai_path, label="agents/openai.yaml")
    interface = _validate_interface(openai)
    canvas = openai.get("canvas")
    _validate_contract(canvas, _CANVAS_CONTRACT, label="agents/openai.yaml canvas")
    assert isinstance(canvas, dict)
    if "artifact_root" in canvas:
        _safe_relative_path(canvas["artifact_root"], label="canvas artifact_root")
    for index, ref in enumerate(canvas.get("implementation_dependencies", [])):
        _safe_relative_path(ref, label=f"canvas implementation_dependencies[{index}]")
    adjacency = _validate_graph(canvas)
    _agent_file_names(root, canvas["milestones"])

    agents: list[dict[str, Any]] = []
    resources: list[dict[str, str]] = [
        {"owner": "skill", "id": "skill", "kind": "reference", "path": "SKILL.md"},
        {"owner": "skill", "id": "canvas", "kind": "reference", "path": "agents/openai.yaml"},
    ]
    for milestone_id in canvas["milestones"]:
        ref = f"agents/{milestone_id}.yaml"
        path = _source_path(root, ref, label=ref, suffix=".yaml")
        agent = _load_yaml_path(path, label=ref)
        _validate_contract(agent, _MILESTONE_CONTRACT, label=ref)
        if agent["agent_id"] != milestone_id:
            raise SkillSourceError(f"{ref}: agent_id must equal canvas milestone id")
        _validate_agent_semantics(agent, flow_id=str(canvas["flow_id"]))
        resources.append(
            {"owner": milestone_id, "id": "agent", "kind": "reference", "path": ref}
        )
        judge_ref = f"agents/{milestone_id}_judge.yaml"
        judge_candidate = root / "agents" / f"{milestone_id}_judge.yaml"
        if judge_candidate.exists() or judge_candidate.is_symlink():
            raise SkillSourceError(
                f"{judge_ref}: standalone judge agent files are not accepted; "
                "declare the typed execution.judge binding and profile on the parent milestone"
            )
        resources.extend(_validate_agent_resources(root, agent))
        agents.append(copy.deepcopy(agent))

    _validate_native_graph(agents, adjacency)
    _validate_bindings(agents, adjacency)
    resources.extend(_validate_workflow_contracts(root, canvas, agents))
    resources = _expand_schema_resource_dependencies(root, resources)
    source = {
        "schema": SKILL_SOURCE_SCHEMA,
        "skill": copy.deepcopy(frontmatter),
        "interface": interface,
        "canvas": copy.deepcopy(canvas),
        "milestones": agents,
        "resources": resources,
    }
    _validate_generated_flow_snapshot(root, _compile_loaded_source(source))
    return source


_FLOW_MILESTONE_FIELDS = (
    "success",
    "output_contract",
    "output_schema",
    "outputs",
    "asset",
    "handler",
    "implementation_dependencies",
    "test",
    "input_schema",
    "inputs",
    "flowsteps",
    "tools",
    "intelligence",
    "model_justification",
    "draft_schema",
    "on_tool_fail",
    "max_model_attempts",
    "max_tool_attempts",
    "side_effects",
    "phase_journal",
    "loop",
    "worker",
    "judge_abi",
    "receipt_schema",
    "max_attempts",
    "ledger",
    "branch",
    "on_path",
    "cycle",
    "on_cycle",
    "cache",
    "execution",
    "gem",
)


def _compile_loaded_source(source: Mapping[str, Any]) -> dict[str, Any]:
    canvas = source["canvas"]
    flow: dict[str, Any] = {
        "schema": FLOW_SCHEMA,
        "flow_id": canvas["flow_id"],
        "version": canvas["version"],
        "context_policy": "isolated",
    }
    for field in ("max_run_seconds", "artifact_root", "implementation_dependencies"):
        if field in canvas:
            flow[field] = copy.deepcopy(canvas[field])
    milestones: list[dict[str, Any]] = []
    for agent in source["milestones"]:
        milestone: dict[str, Any] = {"id": agent["agent_id"]}
        for field in _FLOW_MILESTONE_FIELDS:
            if field in agent:
                milestone[field] = copy.deepcopy(agent[field])
        binding_refs = {
            str(item.get("tool") or ""): str(item.get("ref") or "")
            for item in (agent.get("execution") or {}).get("tool_bindings") or []
            if isinstance(item, dict)
        }
        milestone["flowsteps"] = [
            {
                "id": str(step["id"]),
                "tool": binding_refs[str(step["id"])],
            }
            for step in agent["flowsteps"]
        ]
        if "tools" not in milestone:
            milestone["tools"] = list(
                dict.fromkeys(step["id"] for step in agent["flowsteps"])
            )
        milestones.append(milestone)
    flow["milestones"] = milestones
    _validate_contract(flow, _FLOW_CONTRACT, label="compiled flow")
    return flow


def _validate_generated_flow_snapshot(root: Path, expected: Mapping[str, Any]) -> None:
    """Accept only a generated review snapshot that still matches authored source."""

    candidate = root / "flow.yaml"
    if not candidate.exists() and not candidate.is_symlink():
        return
    path = _source_path(root, "flow.yaml", label="flow.yaml", suffix=".yaml")
    actual = _load_yaml_path(path, label="flow.yaml")
    comparable = copy.deepcopy(actual)
    for field, default in (("max_run_seconds", 3600), ("artifact_root", "artifacts")):
        if field not in expected and comparable.get(field) == default:
            comparable.pop(field)
    if comparable != expected:
        raise SkillSourceError(
            "flow.yaml differs from the canonical compile of agents/openai.yaml and "
            "milestone agents; remove the hand-authored snapshot or regenerate it"
        )


def compile_skill_source(skill_root: str | Path) -> dict[str, Any]:
    """Compile valid authored source to a canonical-schema-compatible flow dict."""

    return _compile_loaded_source(load_skill_source(skill_root))


def canonical_json_bytes(value: Any) -> bytes:
    """Return stable UTF-8 JSON bytes for a loaded source or compiled flow."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def compile_skill_source_bytes(skill_root: str | Path) -> bytes:
    """Compile a skill and serialize the result deterministically."""

    return canonical_json_bytes(compile_skill_source(skill_root))


__all__ = [
    "CANVAS_SCHEMA",
    "FLOW_SCHEMA",
    "MILESTONE_SCHEMA",
    "SKILL_SOURCE_SCHEMA",
    "SkillSourceError",
    "canonical_json_bytes",
    "compile_skill_source",
    "compile_skill_source_bytes",
    "load_skill_source",
]
