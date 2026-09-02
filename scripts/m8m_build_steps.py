"""Deterministic handlers for the builder's own canonical M8M workflow."""

from __future__ import annotations

import ast
import os
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from audit_harness import audit_skill
from flowstep_instruction import write_instruction
from flowstep_runtime import (
    FLOW_ID_RE,
    FLOW_SCHEMA,
    FlowError,
    load_flow,
    load_yaml,
    local_tool_package_name,
    read_json,
    runtime_package_name,
    sha256_file,
    utc_now,
    write_json,
)
from generate_harness import generate_tool
from flowstep_tools import infer_codebase, validate_library_tool
from migrate_skill_source import (
    classify_skill,
    freeze_source_tree,
    promote_or_synthesize_tools,
    write_migrated_skill_source,
)
from m8m_flowchart import write_flowchart
from package_archive import WorkflowPackageError, write_workflow_package
from skill_source import (
    SkillSourceError,
    canonical_json_bytes,
    compile_skill_source,
    load_skill_source,
)
from source_bundle import (
    compile_source_bundle,
    digest_json,
    serialize_source_bundle,
    verify_source_bundle,
)
from runtime_release import (
    RuntimeReleaseError,
    stage_runtime_release,
    verify_runtime_release,
)
from tool_vs_intelligence import from_flow as classification_from_flow
from validate_harness import validate_harness


BUILDER_OUTPUT_IDS = {
    "audit_complete": "source_audit",
    "toolbox_ready": "toolbox_manifest",
    "flow_generated": "staged_harness",
    "harness_validated": "validation_report",
    "skill_shipped": "installation_receipt",
}
BUILDER_OUTPUT_PORTS = {
    **{step_id: (output_id,) for step_id, output_id in BUILDER_OUTPUT_IDS.items()},
    "flow_generated": ("workflow_source_bundle", "staged_harness"),
    "harness_validated": ("validation_report", "workflow_package"),
}
BUILDER_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_BUNDLE_FILES = (
    "flowstep_flow_v4.schema.json",
    "flowstep_output_v3.schema.json",
    "m8m_chosen_output_v1.schema.json",
    "m8m_contract_bundle_lock_v1.schema.json",
    "m8m_milestone_expectation_v1.schema.json",
    "m8m_milestone_judge_receipt_v1.schema.json",
    "m8m_milestone_judge_request_v1.schema.json",
    "m8m_workflow_source_bundle_proof_v1.schema.json",
    "m8m_workflow_source_bundle_v1.schema.json",
)
FLOWSTEP_SHELL_SUFFIXES = {".bat", ".cmd", ".ps1", ".sh"}
FLOWSTEP_CONTROL_ARTIFACTS = {
    "chosen-output.json",
    "judge-receipt.json",
    "cache-receipt.json",
    "progress.json",
    "roster.json",
    "goal-ledger.json",
    "cycle-ledger.json",
}
FLOWSTEP_SUBPROCESS_CALLS = {
    "os.popen",
    "os.spawnl",
    "os.spawnle",
    "os.spawnlp",
    "os.spawnlpe",
    "os.spawnv",
    "os.spawnve",
    "os.spawnvp",
    "os.spawnvpe",
    "os.system",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.getoutput",
    "subprocess.Popen",
    "subprocess.run",
}


def _is_unsafe_link(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(is_junction and is_junction())
    except OSError:
        return True


def _request(input_data: dict[str, Any]) -> dict[str, Any]:
    request = input_data.get("request")
    if not isinstance(request, dict):
        raise FlowError("builder request is missing")
    return request


def _path(request: dict[str, Any], key: str) -> Path:
    raw = request.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise FlowError(f"builder request needs {key}")
    return Path(raw).resolve()


def _assert_declared_file_digest(
    value: dict[str, Any],
    path_key: str,
    digest_key: str,
    *,
    label: str,
) -> Path:
    path = Path(str(value.get(path_key) or "")).resolve()
    if not path.is_file():
        raise FlowError(f"{label} is missing its declared file: {path}")
    expected = str(value.get(digest_key) or "")
    actual = sha256_file(path)
    if expected != actual:
        raise FlowError(f"{label} digest does not match {path_key}")
    return path


def _flow_id(audit: dict[str, Any], request: dict[str, Any]) -> str:
    skill = audit.get("audited_skill") if isinstance(audit.get("audited_skill"), dict) else {}
    name = str(request.get("skill_name") or skill.get("name") or "product-skill")
    raw = request.get("flow_id") or (audit.get("grade") or {}).get("flow_id") or f"{name.replace('-', '_')}_v1"
    value = str(raw).lower().replace("-", "_")
    return value if FLOW_ID_RE.match(value) else "product_v1"


def _skill_name(audit: dict[str, Any], request: dict[str, Any]) -> str:
    skill = audit.get("audited_skill") if isinstance(audit.get("audited_skill"), dict) else {}
    return str(request.get("skill_name") or skill.get("name") or "product-skill")


def _stage_codebase(run_dir: Path) -> Path:
    path = Path(run_dir).resolve() / "work" / "builder" / "stage-codebase"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _candidate(value: dict[str, Any], output_id: str = "result") -> dict[str, Any]:
    return {"outputs": {output_id: value}}


def _input_object(input_data: dict[str, Any], *names: str) -> dict[str, Any] | None:
    for name in names:
        value = input_data.get(name)
        if isinstance(value, dict):
            return value
    return None


def _declared_output_id(task: dict[str, Any] | None, step_id: str) -> str:
    declared = (task or {}).get("outputs")
    ids = [str(item.get("id") or "") for item in declared or [] if isinstance(item, dict)]
    preferred = BUILDER_OUTPUT_IDS.get(step_id)
    if preferred and preferred in ids:
        return preferred
    if "result" in ids:
        return "result"
    if len(ids) == 1 and ids[0]:
        return ids[0]
    return preferred or "result"


def _tool_ids(audit: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    compiled = audit.get("compiled_flow")
    if isinstance(compiled, dict):
        for milestone in compiled.get("milestones") or []:
            if not isinstance(milestone, dict):
                continue
            ids.extend(
                local_tool_package_name(
                    str(item.get("ref") or ""),
                    label=(
                        f"{milestone.get('id')}.execution.tool_bindings"
                        f"[{item.get('tool')}].ref"
                    ),
                )
                for item in (milestone.get("execution") or {}).get("tool_bindings") or []
                if isinstance(item, dict) and item.get("ref")
            )
            if milestone.get("worker"):
                worker = str(milestone["worker"])
                ids.append(
                    runtime_package_name(worker, label=f"{milestone.get('id')}.worker")
                    if milestone.get("judge_abi")
                    else worker
                )
        return list(dict.fromkeys(item for item in ids if item))
    for row in audit.get("python_standardization") or []:
        if isinstance(row, dict) and row.get("tool_id"):
            ids.append(str(row["tool_id"]))
    for milestone in audit.get("proposed_milestones") or []:
        if not isinstance(milestone, dict):
            continue
        ids.extend(str(item) for item in milestone.get("tools") or [] if item)
        if milestone.get("worker"):
            worker = str(milestone["worker"])
            ids.append(
                runtime_package_name(worker, label=f"{milestone.get('id')}.worker")
                if milestone.get("judge_abi")
                else worker
            )
    unique: list[str] = []
    for tool_id in ids:
        if tool_id and tool_id not in unique:
            unique.append(tool_id)
    return unique


def _source_snapshot_members(root: Path) -> list[dict[str, Any]]:
    root = root.resolve()
    members: list[dict[str, Any]] = []
    paths = [path for path in root.rglob("*") if path.is_file()]
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        digest = f"sha256:{sha256_file(path)}"
        members.append(
            {
                "path": relative,
                "byte_count": path.stat().st_size,
                "digest": digest,
                "sha256": digest,
            }
        )
    return members


def _freeze_skill_native_source(
    target: Path,
    run_dir: Path,
) -> tuple[Path, dict[str, Any], dict[str, Any], list[dict[str, Any]], str]:
    """Freeze one validated skill-native tree before choosing the audit output."""

    preliminary = load_skill_source(target)
    snapshot = run_dir.resolve() / "work" / "builder" / "source-snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    declared_paths = sorted(_skill_native_source_paths(preliminary))
    for relative in declared_paths:
        source = _bounded_source_file(target, relative)
        if not source.is_file():
            raise FlowError(f"skill-native source member disappeared during audit: {relative}")
        destination = _bounded_source_file(snapshot, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if not destination.is_file() or sha256_file(destination) != sha256_file(source):
                raise FlowError(
                    "frozen source snapshot conflicts with the current target; replace "
                    "audit_complete to start from one coherent source snapshot"
                )
            continue
        shutil.copy2(source, destination)

    actual_paths = [item["path"] for item in _source_snapshot_members(snapshot)]
    if actual_paths != declared_paths:
        raise FlowError(
            "frozen source snapshot contains undeclared or missing members; replace "
            "audit_complete and rebuild the source snapshot"
        )
    source = load_skill_source(snapshot)
    definition = compile_skill_source(snapshot)
    members = _source_snapshot_members(snapshot)
    return snapshot, source, definition, members, digest_json(members)


def _assert_frozen_skill_source(
    audit: dict[str, Any],
    run_dir: Path,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    expected = run_dir.resolve() / "work" / "builder" / "source-snapshot"
    snapshot = Path(str(audit.get("source_snapshot") or "")).resolve()
    if snapshot != expected or not snapshot.is_dir():
        raise FlowError("skill-native audit does not reference this run's frozen source snapshot")
    members = _source_snapshot_members(snapshot)
    if members != audit.get("source_snapshot_members"):
        raise FlowError(
            "frozen skill source changed after audit; replace audit_complete before generation"
        )
    if digest_json(members) != audit.get("source_snapshot_digest"):
        raise FlowError("frozen skill source manifest digest does not match the chosen audit")
    source = load_skill_source(snapshot)
    definition = compile_skill_source(snapshot)
    if source != audit.get("skill_source") or definition != audit.get("compiled_flow"):
        raise FlowError(
            "frozen skill source no longer compiles to the chosen audit; replace audit_complete"
        )
    return snapshot, source, definition


def _audit(input_data: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    request = _request(input_data)
    target = _path(request, "target")
    if not target.is_dir():
        raise FlowError(f"builder target not found: {target}")
    openai_path = target / "agents" / "openai.yaml"
    openai_source = load_yaml(openai_path) if openai_path.is_file() else None
    native_declared = isinstance(openai_source, dict) and isinstance(openai_source.get("canvas"), dict)
    codebase = _path(request, "codebase")
    if native_declared:
        try:
            snapshot, skill_source, compiled_flow, members, members_digest = (
                _freeze_skill_native_source(target, run_dir)
            )
        except SkillSourceError as exc:
            raise FlowError(f"declared skill-native source is invalid: {exc}") from exc
        audit = audit_skill(snapshot)
        audit["target"] = str(target)
        audit["authoring_mode"] = "skill_native"
        audit["skill_source"] = skill_source
        audit["compiled_flow"] = compiled_flow
        audit["source_snapshot"] = str(snapshot)
        audit["source_snapshot_members"] = members
        audit["source_snapshot_digest"] = members_digest
        form = classify_skill(
            target,
            codebase=codebase,
            flow_id=str(compiled_flow.get("flow_id") or request.get("flow_id") or ""),
        )
    else:
        snapshot = freeze_source_tree(
            target,
            Path(run_dir).resolve() / "work" / "builder" / "source-snapshot",
        )
        audit = audit_skill(snapshot)
        audit["target"] = str(target)
        audit["authoring_mode"] = "from_context"
        audit["equivalence_claimed"] = False
        members = _source_snapshot_members(snapshot)
        audit["source_snapshot"] = str(snapshot)
        audit["source_snapshot_members"] = members
        audit["source_snapshot_digest"] = digest_json(members)
        form = classify_skill(
            target,
            codebase=codebase,
            flow_id=str(request.get("flow_id") or (audit.get("grade") or {}).get("flow_id") or ""),
        )
    audit["source_context"] = form["source_context"]
    audit["runtime_ownership"] = form["runtime_ownership"]
    audit["ownership_gaps"] = form["ownership_gaps"]
    audit["builder_runtime_hits"] = form["builder_runtime_hits"]
    audit["equivalence_claimed"] = False
    audit["builder_request"] = {
        "target": str(target),
        "codebase": str(codebase),
        "flow_id": request.get("flow_id"),
        "skill_name": request.get("skill_name"),
    }
    return _candidate(audit)


def _toolbox(input_data: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    request = _request(input_data)
    audit = _input_object(input_data, "source_audit", "audit")
    if not isinstance(audit, dict):
        raise FlowError("toolbox_ready needs the chosen source audit")
    stage = _stage_codebase(run_dir)
    source_codebase = _path(request, "codebase")
    snapshot = Path(str(audit.get("source_snapshot") or "")).resolve()
    if not snapshot.is_dir():
        snapshot = _path(request, "target")
    for tool_id in _tool_ids(audit):
        source_tool = source_codebase / "flowsteps" / "tools" / tool_id
        staged_tool = stage / "flowsteps" / "tools" / tool_id
        if not source_tool.is_dir():
            continue
        if staged_tool.is_dir() and not (staged_tool / "BUILD_REQUIRED").is_file():
            try:
                if not validate_library_tool(stage, tool_id):
                    continue
            except Exception:
                pass
        shutil.copytree(source_tool, staged_tool, dirs_exist_ok=True)
        if not (source_tool / "BUILD_REQUIRED").is_file():
            marker = staged_tool / "BUILD_REQUIRED"
            if marker.is_file():
                marker.unlink()
    if audit.get("authoring_mode") == "from_context":
        promote_or_synthesize_tools(
            stage,
            audit,
            source_root=snapshot,
            codebase=source_codebase,
        )
        tools = []
        for tool_id in dict.fromkeys([*_tool_ids(audit), "candidate_bind"]):
            dest = stage / "flowsteps" / "tools" / tool_id
            if not dest.is_dir():
                continue
            blockers = validate_library_tool(stage, tool_id)
            tools.append(
                {
                    "schema": "flowstep_tool_generate_v3",
                    "status": "PASS" if not blockers else "BUILD_REQUIRED",
                    "tool_id": tool_id,
                    "tool_dir": str(dest),
                    "origin": "from_context",
                    "runnable": not blockers,
                    "non_runnable": bool(blockers),
                    "blockers": blockers,
                    "written": [str(dest)],
                }
            )
    else:
        tools = [generate_tool(stage, tool_id) for tool_id in _tool_ids(audit)]
    build_required = sorted(
        str(item["tool_id"])
        for item in tools
        if item.get("status") != "PASS" or item.get("non_runnable")
    )
    return _candidate(
        {
            "stage_codebase": str(stage),
            "tools": tools,
            "status": "BUILD_REQUIRED" if build_required else "PASS",
            "runnable": not build_required,
            "non_runnable": bool(build_required),
            "build_required_tools": build_required,
            "created_at": utc_now(),
        }
    )


def _contract_bundle_member_manifest() -> dict[str, Any]:
    """Return the byte-identity manifest shared with the native platform."""

    members: list[dict[str, Any]] = []
    for name in CONTRACT_BUNDLE_FILES:
        path = BUILDER_ROOT / "contracts" / name
        payload = path.read_bytes()
        members.append(
            {
                "name": name,
                "byte_count": len(payload),
                "digest": f"sha256:{sha256_file(path)}",
            }
        )
    return {
        "schema": "m8m.contract_bundle_members.v1",
        "members": members,
    }


def _contract_bundle_lock() -> dict[str, str]:
    return {
        "schema": "m8m.contract_bundle_lock.v1",
        "id": "m8m-harness-builder-contracts/3.1",
        "digest": digest_json(_contract_bundle_member_manifest()),
    }


def _source_resources(harness: Path, definition: dict[str, Any]) -> list[dict[str, str]]:
    resources: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    roles = {
        "gem": ("gem", "milestone_gem", "text/markdown"),
        "input_schema": ("json_schema", "milestone_input_schema", "application/schema+json"),
        "output_schema": ("json_schema", "milestone_output_schema", "application/schema+json"),
        "draft_schema": ("json_schema", "milestone_draft_schema", "application/schema+json"),
        "receipt_schema": ("json_schema", "milestone_receipt_schema", "application/schema+json"),
        "test": ("test", "milestone_validation_test", "text/x-python"),
    }
    for milestone in definition.get("milestones") or []:
        if not isinstance(milestone, dict):
            continue
        milestone_id = str(milestone.get("id") or "")
        for field, (kind, role, media_type) in roles.items():
            relative = str(milestone.get(field) or "")
            if not relative:
                continue
            if not (harness / relative).is_file():
                raise FlowError(
                    f"{milestone_id}: referenced source resource is missing: {relative}"
                )
            key = (field, relative)
            if key in seen:
                continue
            seen.add(key)
            resources.append(
                {
                    "ref": f"{kind}.{milestone_id}.{field}",
                    "kind": kind,
                    "role": role,
                    "source_path": Path(relative).as_posix(),
                    "media_type": media_type,
                }
            )
    return resources


def _runtime_release_resources(
    harness: Path,
    generated: dict[str, Any],
) -> list[dict[str, str]]:
    release = generated.get("runtime_release")
    if not isinstance(release, dict):
        raise FlowError("staged harness is missing its immutable runtime release")
    harness = harness.resolve()
    rows: list[dict[str, str]] = []
    for key, kind, role, media_type in (
        (
            "archive_path",
            "runtime_release",
            "runtime_source_closure",
            "application/zip",
        ),
        (
            "harness_lock_path",
            "runtime_lock",
            "workflow_runtime_pin",
            "application/json",
        ),
        (
            "codebase_launcher_path",
            "runtime_launcher",
            "codebase_execution_entrypoint",
            "text/x-python",
        ),
    ):
        path = Path(str(release.get(key) or "")).resolve()
        if not path.is_file() or harness not in path.parents:
            raise FlowError(f"runtime release {key} is missing from the staged harness")
        rows.append(
            {
                "ref": (
                    f"m8m-runtime.{kind}."
                    f"{release.get('runtime_version')}+{str(release.get('runtime_id') or '')[:16]}"
                ),
                "kind": kind,
                "role": role,
                "source_path": path.relative_to(harness).as_posix(),
                "media_type": media_type,
            }
        )
    return rows


_LEGACY_BUILDER_RUNTIME_MARKERS = (
    "M8M_BUILDER",
    "FLOWSTEP_BUILDER",
    "m8m-harness-builder",
    "flowstep-harness-builder",
    ".codex/skills/m8m-harness-builder",
    ".claude/skills/m8m-harness-builder",
    "m8m-harness-builder/scripts/run_flow.py",
    "flowstep-harness-builder/scripts/run_flow.py",
)

_LEGACY_BUILDER_ENVIRONMENT_MARKERS = tuple(
    item.lower() for item in _LEGACY_BUILDER_RUNTIME_MARKERS[:2]
)
_LEGACY_BUILDER_NAMES = tuple(
    item.lower() for item in _LEGACY_BUILDER_RUNTIME_MARKERS[2:4]
)
_LEGACY_BUILDER_EXECUTION_PATH_MARKERS = (
    ".codex/skills/m8m-harness-builder",
    ".claude/skills/m8m-harness-builder",
    ".codex/skills/flowstep-harness-builder",
    ".claude/skills/flowstep-harness-builder",
    "m8m-harness-builder/scripts/run_flow.py",
    "m8m-harness-builder/scripts/run_goal.py",
    "flowstep-harness-builder/scripts/run_flow.py",
    "flowstep-harness-builder/scripts/run_goal.py",
)
_MAX_FOLDED_STRING_BYTES = 4096


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _constant_integer(node: ast.AST) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(
        node.value, bool
    ):
        return int(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _constant_integer(node.operand)
        if value is not None:
            return value if isinstance(node.op, ast.UAdd) else -value
    return None


def _constant_byte_sequence(node: ast.AST, bindings: dict[str, str]) -> bytes | None:
    """Fold small deterministic byte constructors used to hide a runtime path."""

    if not isinstance(node, ast.Call):
        return None
    callee = _call_name(node.func).lower()
    if callee in {"bytes", "bytearray"} and len(node.args) == 1 and not node.keywords:
        sequence = node.args[0]
        if not isinstance(sequence, (ast.List, ast.Tuple)):
            return None
        if len(sequence.elts) > _MAX_FOLDED_STRING_BYTES:
            return None
        values = [_constant_integer(item) for item in sequence.elts]
        if any(item is None or item < 0 or item > 255 for item in values):
            return None
        return bytes(int(item) for item in values if item is not None)
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "fromhex"
        and _call_name(node.func.value).lower() in {"bytes", "bytearray"}
        and len(node.args) == 1
        and not node.keywords
    ):
        encoded = _constant_string(node.args[0], bindings)
        if encoded is None or len(encoded) > _MAX_FOLDED_STRING_BYTES * 2:
            return None
        try:
            return bytes.fromhex(encoded)
        except ValueError:
            return None
    return None


def _constant_string_sequence(
    node: ast.AST, bindings: dict[str, str]
) -> list[str] | None:
    if isinstance(node, (ast.List, ast.Tuple)):
        if len(node.elts) > _MAX_FOLDED_STRING_BYTES:
            return None
        values = [_constant_string(item, bindings) for item in node.elts]
        return None if any(item is None for item in values) else [str(item) for item in values]
    if (
        isinstance(node, ast.Call)
        and _call_name(node.func).lower() == "map"
        and len(node.args) == 2
        and not node.keywords
        and _call_name(node.args[0]).lower() == "chr"
        and isinstance(node.args[1], (ast.List, ast.Tuple))
        and len(node.args[1].elts) <= _MAX_FOLDED_STRING_BYTES
    ):
        integers = [_constant_integer(item) for item in node.args[1].elts]
        if any(item is None or item < 0 or item > 0x10FFFF for item in integers):
            return None
        try:
            return [chr(int(item)) for item in integers if item is not None]
        except ValueError:
            return None
    return None


def _constant_string(node: ast.AST, bindings: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return str(node.value)
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
        left = _constant_string(node.left, bindings)
        right = _constant_string(node.right, bindings)
        if left is None or right is None:
            return None
        separator = "" if isinstance(node.op, ast.Add) else "/"
        return f"{left.rstrip('/')}{separator}{right.lstrip('/')}"
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                part = _constant_string(value.value, bindings)
            else:
                part = _constant_string(value, bindings)
            if part is None:
                return None
            parts.append(part)
        return "".join(parts)
    if isinstance(node, ast.Call):
        callee = _call_name(node.func).lower()
        if callee == "chr" and len(node.args) == 1 and not node.keywords:
            value = _constant_integer(node.args[0])
            if value is not None and 0 <= value <= 0x10FFFF:
                try:
                    return chr(value)
                except ValueError:
                    return None
        if isinstance(node.func, ast.Attribute) and node.func.attr == "decode":
            payload = _constant_byte_sequence(node.func.value, bindings)
            encoding = (
                _constant_string(node.args[0], bindings)
                if len(node.args) == 1 and not node.keywords
                else "utf-8"
                if not node.args and not node.keywords
                else None
            )
            if payload is not None and encoding in {"utf-8", "utf8", "ascii"}:
                try:
                    return payload.decode(str(encoding))
                except (UnicodeDecodeError, LookupError):
                    return None
        if callee.endswith(".home"):
            return "<home>"
        if callee.rsplit(".", 1)[-1] in {"path", "purepath", "str"} and node.args:
            return _constant_string(node.args[0], bindings)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "join":
            separator = _constant_string(node.func.value, bindings)
            sequence = (
                _constant_string_sequence(node.args[0], bindings)
                if len(node.args) == 1
                else None
            )
            if separator is not None and sequence is not None:
                return separator.join(sequence)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            template = _constant_string(node.func.value, bindings)
            arguments = [_constant_string(item, bindings) for item in node.args]
            keywords = {
                str(item.arg): _constant_string(item.value, bindings)
                for item in node.keywords
                if item.arg is not None
            }
            if (
                template is not None
                and all(item is not None for item in arguments)
                and all(item is not None for item in keywords.values())
            ):
                try:
                    return template.format(*arguments, **keywords)
                except (IndexError, KeyError, ValueError):
                    return None
        if callee.endswith(".join"):
            parts = [_constant_string(item, bindings) for item in node.args]
            if parts and all(part is not None for part in parts):
                return "/".join(str(part).strip("/") for part in parts)
    return None


def _direct_text_builder_dependency_marker(source: str) -> str | None:
    normalized = source.replace("\\", "/").lower()
    environment = next(
        (
            item
            for item in _LEGACY_BUILDER_ENVIRONMENT_MARKERS
            if re.search(
                rf"(?<![a-z0-9_]){re.escape(item)}(?![a-z0-9_])",
                normalized,
            )
        ),
        None,
    )
    if environment:
        return environment
    path_marker = next(
        (item for item in _LEGACY_BUILDER_EXECUTION_PATH_MARKERS if item in normalized),
        None,
    )
    if path_marker:
        return path_marker
    builder = next(
        (item for item in _LEGACY_BUILDER_NAMES if item in normalized),
        None,
    )
    if builder and any(item in normalized for item in ("run_flow.py", "run_goal.py")):
        return builder
    return None


def _shell_constant_bindings(source: str) -> dict[str, str]:
    """Fold bounded literal assignments in shell/PowerShell/JS helper text."""

    bindings: dict[str, str] = {}
    assignment_patterns = (
        re.compile(
            r"(?im)^\s*\$(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>[^\r\n#]+)"
        ),
        re.compile(
            r"(?im)^\s*(?:const\s+|let\s+|var\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>[^\r\n#;]+)"
        ),
    )
    for pattern in assignment_patterns:
        for match in pattern.finditer(source):
            expression = match.group("value").strip()
            if len(expression) > _MAX_FOLDED_STRING_BYTES * 4:
                continue
            try:
                tree = ast.parse(expression, mode="eval")
            except SyntaxError:
                continue
            value = _constant_string(tree.body, bindings)
            if value is not None and len(value.encode("utf-8")) <= _MAX_FOLDED_STRING_BYTES:
                bindings[match.group("name")] = value
    return bindings


def _legacy_text_builder_dependency_marker(source: str, path: Path) -> str | None:
    """Lint deterministic non-Python runtime bindings without policing prose."""

    marker = _direct_text_builder_dependency_marker(source)
    if marker:
        return marker
    bindings = _shell_constant_bindings(source)
    if not bindings:
        return None
    expanded = source
    for name, value in sorted(bindings.items(), key=lambda item: -len(item[0])):
        substitutions = (
            rf"\$\{{{re.escape(name)}\}}",
            rf"\${re.escape(name)}\b",
            rf"%{re.escape(name)}%",
        )
        for pattern in substitutions:
            expanded = re.sub(pattern, lambda _match, replacement=value: replacement, expanded)
    return _direct_text_builder_dependency_marker(expanded)


def _read_utf8_product_text(path: Path) -> str | None:
    """Read UTF-8 text incrementally; return None for a binary closure member."""

    chunks: list[str] = []
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                if "\x00" in chunk:
                    return None
                chunks.append(chunk)
    except UnicodeDecodeError:
        return None
    return "".join(chunks)


def _constant_bindings(tree: ast.AST) -> dict[str, str]:
    bindings: dict[str, str] = {}
    assignments = sorted(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
        ),
        key=lambda node: (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)),
    )
    for _ in range(max(1, len(assignments))):
        changed = False
        for node in assignments:
            value_node = node.value
            value = _constant_string(value_node, bindings)
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and bindings.get(target.id) != value:
                    bindings[target.id] = value
                    changed = True
        if not changed:
            break
    return bindings


def _string_literals(node: ast.AST, bindings: dict[str, str]) -> list[str]:
    values: list[str] = []
    for item in ast.walk(node):
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            values.append(str(item.value))
        elif isinstance(item, (ast.Name, ast.BinOp, ast.JoinedStr, ast.Call)):
            value = _constant_string(item, bindings)
            if value is not None:
                values.append(value)
    return [value.replace("\\", "/").lower() for value in values]


def _legacy_builder_dependency_marker(source: str, path: Path) -> str | None:
    """Find executable Builder lookups without rejecting human-facing messages."""

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise FlowError(f"cannot parse product runtime source {path}: {exc}") from exc
    bindings = _constant_bindings(tree)
    environment_markers = {
        item.lower() for item in _LEGACY_BUILDER_RUNTIME_MARKERS[:2]
    }
    builder_names = {
        item.lower() for item in _LEGACY_BUILDER_RUNTIME_MARKERS[2:4]
    }
    path_context = {".codex", ".claude", "skills", "run_flow.py", "run_goal.py"}
    dangerous_calls = {
        "popen",
        "run",
        "run_path",
        "check_call",
        "check_output",
        "spec_from_file_location",
        "import_module",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.lower() in environment_markers:
            return node.id
        if isinstance(node, ast.Attribute) and node.attr.lower() in environment_markers:
            return node.attr
        if isinstance(node, ast.Subscript):
            values = _string_literals(node.slice, bindings)
            match = next((item for item in values if item in environment_markers), None)
            if match:
                return match
        if not isinstance(node, (ast.Call, ast.BinOp, ast.JoinedStr)):
            continue
        literals = _string_literals(node, bindings)
        builder = next(
            (name for name in builder_names if any(name in value for value in literals)),
            None,
        )
        environment = next(
            (
                marker
                for marker in environment_markers
                if any(marker in value for value in literals)
            ),
            None,
        )
        if environment:
            return environment
        if not builder:
            continue
        contextual = any(
            token in value for value in literals for token in path_context
        )
        if isinstance(node, (ast.BinOp, ast.JoinedStr)) and contextual:
            return builder
        if isinstance(node, ast.Call):
            callee = _call_name(node.func).lower().rsplit(".", 1)[-1]
            if contextual or callee in dangerous_calls:
                return builder
    return None


def _assert_python_path_runtime_isolated(
    root: Path,
    *,
    exclude_runtime_releases: bool,
) -> None:
    """Lint every UTF-8 product-closure member for a Builder runtime binding.

    This is an engineering isolation invariant for generated product source and
    configuration.  It deliberately does not claim to sandbox hostile code.
    """

    if not root.exists():
        return
    if _is_unsafe_link(root):
        raise FlowError(f"product runtime audit root uses an unsafe link: {root}")
    if root.is_file():
        paths = [root]
        relative_root = root.parent
    elif root.is_dir():
        members = list(root.rglob("*"))
        for member in members:
            if _is_unsafe_link(member):
                raise FlowError(
                    f"product runtime source tree uses an unsafe link: {member}"
                )
        paths = [
            member
            for member in members
            if member.is_file()
        ]
        relative_root = root
    else:
        raise FlowError(f"product runtime audit root is not a regular path: {root}")
    for path in paths:
        if _is_unsafe_link(path) or not path.is_file():
            raise FlowError(f"product runtime source uses an unsafe link: {path}")
        relative = path.relative_to(relative_root)
        if (
            exclude_runtime_releases
            and relative.parts[:2] == ("runtime", "releases")
        ):
            continue
        try:
            source = _read_utf8_product_text(path)
        except OSError as exc:
            raise FlowError(f"cannot audit product runtime source {path}: {exc}") from exc
        # M8M-authored source/configuration is UTF-8. NUL-bearing or non-UTF-8
        # members are treated as binary assets and remain covered by the
        # implementation digest even though dependency lint cannot parse them.
        if source is None:
            continue
        if path.suffix.lower() in {".py", ".pyw"}:
            marker = _legacy_builder_dependency_marker(source, path)
        else:
            marker = _legacy_text_builder_dependency_marker(source, path)
        if marker:
            raise FlowError(
                "product execution source depends on the mutable M8M Builder "
                f"({marker}): {path}; regenerate it with the codebase-owned runtime"
            )


def _runtime_tool_package_ids(flow: dict[str, Any]) -> set[str]:
    tool_ids: set[str] = set()
    for milestone in flow.get("steps") or flow.get("milestones") or []:
        tool_ids.update(
            local_tool_package_name(
                str(item.get("ref") or ""),
                label=(
                    f"{milestone.get('id')}.execution.tool_bindings"
                    f"[{item.get('tool')}].ref"
                ),
            )
            for item in (milestone.get("execution") or {}).get("tool_bindings") or []
            if isinstance(item, dict) and item.get("ref")
        )
        worker = str(milestone.get("worker") or "")
        if worker:
            tool_ids.add(
                runtime_package_name(worker, label=f"{milestone.get('id')}.worker")
                if milestone.get("judge_abi")
                else worker
            )
    return tool_ids


def _runtime_implementation_dependency_paths(
    flow: dict[str, Any],
    codebase: Path,
) -> set[Path]:
    relative_paths = {
        str(item)
        for item in flow.get("implementation_dependencies") or []
        if item
    }
    for milestone in flow.get("steps") or flow.get("milestones") or []:
        relative_paths.update(
            str(item) for item in milestone.get("implementation_dependencies") or [] if item
        )
    return {_bounded_source_file(codebase, item) for item in relative_paths}


def _flowstep_source_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []
    members = [root] if root.is_file() else list(root.rglob("*"))
    paths: list[Path] = []
    for path in members:
        if not path.is_file():
            continue
        relative = path.name if root.is_file() else path.relative_to(root).as_posix()
        parts = {item.lower() for item in Path(relative).parts}
        if "tests" in parts or "__pycache__" in parts:
            continue
        if path.suffix.lower() not in ({".py"} | FLOWSTEP_SHELL_SUFFIXES):
            continue
        paths.append(path)
    return paths


def _flowstep_source_violation(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in FLOWSTEP_SHELL_SUFFIXES:
        return f"shell source {path.name} is not an in-process FlowStep"
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"cannot read closed FlowStep source: {exc}"
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return f"cannot parse closed FlowStep source: {exc}"
    bindings = _constant_bindings(tree)
    runtime_names = ("run_dir", "harness_root", "execution_root", "nisanruntime")
    runtime_markers = ("c:\\nisanruntime", "c:/nisanruntime", "m8m_harness_root", "nisanruntime")
    command_terminals = {"call", "command", "exec", "execute", "invoke", "popen", "run", "system"}
    write_terminals = {"dump", "open", "save", "write", "write_bytes", "write_json", "write_text"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(alias.name == "subprocess" for alias in node.names):
            return "FlowStep source imports subprocess"
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            return "FlowStep source imports subprocess"
        if not isinstance(node, ast.Call):
            continue
        call = _call_name(node.func)
        terminal = call.rsplit(".", 1)[-1]
        if call in FLOWSTEP_SUBPROCESS_CALLS or terminal == "Popen":
            return f"FlowStep source launches a subprocess through {call}"
        literals = _string_literals(node, bindings)
        command_text = " ".join(literals).lower()
        expression = ast.unparse(node).lower()
        command_call = terminal.lower() in command_terminals
        command_names_runtime = any(marker in command_text for marker in runtime_markers) or any(
            name in expression for name in runtime_names
        )
        if command_call and command_names_runtime and re.search(
            r"(?:^|[^a-z0-9_])rg(?:\.exe)?(?:[^a-z0-9_]|$)", command_text
        ):
            return "FlowStep source uses rg to discover the execution root"
        if (
            command_call
            and command_names_runtime
            and "get-childitem" in command_text
            and "-recurse" in command_text
        ):
            return "FlowStep source recursively enumerates the execution root with Get-ChildItem"
        if terminal in write_terminals:
            control = next(
                (
                    item
                    for item in sorted(FLOWSTEP_CONTROL_ARTIFACTS)
                    if any(item in literal.lower() for literal in literals)
                ),
                None,
            )
            if control:
                return f"FlowStep source authors runtime-owned control artifact {control}"
        recursive = call == "os.walk" or terminal == "rglob"
        if terminal == "glob" and node.args:
            recursive = recursive or any(
                "**" in literal for literal in _string_literals(node.args[0], bindings)
            )
        if recursive and any(name in expression for name in runtime_names):
            return "FlowStep source performs recursive filesystem discovery over execution state"
    return None


def _assert_closed_flowstep_package(root: Path) -> None:
    for path in _flowstep_source_paths(root):
        violation = _flowstep_source_violation(path)
        if violation:
            raise FlowError(
                f"closed FlowStep execution violation in {path}: {violation}; "
                "implement one declared in-process product tool"
            )


def _assert_flowstep_declarations_closed(flow: dict[str, Any]) -> None:
    split_command = re.compile(
        r"(?:^|_)(?:stability_fetch|fetch_stability|generate_receipt|receipt_generate|receipt_generation)(?:_|$)"
    )
    approval = re.compile(r"(?:^|_)(?:approve|approval)(?:_|$)")
    for milestone in flow.get("steps") or flow.get("milestones") or []:
        rows = [item for item in milestone.get("flowsteps") or [] if isinstance(item, dict)]
        ids = [str(item.get("id") or "").strip().lower().replace("-", "_") for item in rows]
        for flowstep_id in ids:
            if split_command.search(flowstep_id):
                raise FlowError(
                    f"{milestone.get('id')}.{flowstep_id}: separate stability-fetch or "
                    "receipt-generation FlowSteps are forbidden; return typed evidence "
                    "from the owning tool"
                )
        approval_index = next(
            (index for index, flowstep_id in enumerate(ids) if approval.search(flowstep_id)),
            None,
        )
        if approval_index is None:
            continue
        post_approval = ids[approval_index + 1 :]
        if post_approval not in ([], ["finalize"]):
            raise FlowError(
                f"{milestone.get('id')}: post-approval FlowSteps must be exactly one "
                "declared finalize operation"
            )


def _runtime_flowstep_tool_package_ids(flow: dict[str, Any]) -> set[str]:
    tool_ids: set[str] = set()
    for milestone in flow.get("steps") or flow.get("milestones") or []:
        for item in (milestone.get("execution") or {}).get("tool_bindings") or []:
            if not isinstance(item, dict) or not item.get("ref"):
                continue
            tool_ids.add(
                local_tool_package_name(
                    str(item["ref"]),
                    label=(
                        f"{milestone.get('id')}.execution.tool_bindings"
                        f"[{item.get('tool')}].ref"
                    ),
                )
            )
    return tool_ids


def _runtime_flowstep_implementation_dependency_paths(
    flow: dict[str, Any],
    codebase: Path,
) -> set[Path]:
    relative_paths: set[str] = set()
    for milestone in flow.get("steps") or flow.get("milestones") or []:
        relative_paths.update(
            str(item)
            for item in milestone.get("implementation_dependencies") or []
            if item
        )
    return {_bounded_source_file(codebase, item) for item in relative_paths}


def _runtime_harness_execution_paths(
    flow: dict[str, Any],
    harness: Path,
) -> set[Path]:
    """Return authored/generated files the harness can read during execution."""

    root = harness.resolve()
    paths: set[Path] = set()

    def add(value: str | Path | None) -> None:
        if not value:
            return
        raw = Path(value)
        candidate = (raw if raw.is_absolute() else root / raw).resolve()
        if candidate != root and root not in candidate.parents:
            raise FlowError(f"product runtime source escapes its harness: {value}")
        if candidate.exists():
            paths.add(candidate)

    add(flow.get("_flow_path"))
    if not flow.get("_flow_path"):
        add("flow.yaml")
        for candidate in sorted((root / "flows").glob("*.y*ml")):
            add(candidate)
    for relative in (
        "launch.py",
        "m8m-runtime-lock.json",
        "runtime/active.json",
    ):
        add(relative)
    for milestone in flow.get("steps") or flow.get("milestones") or []:
        for field in (
            "handler",
            "input_schema",
            "output_schema",
            "draft_schema",
            "receipt_schema",
            "gem",
        ):
            add(milestone.get(field))
    return paths


def _assert_product_runtime_isolated(
    generated: dict[str, Any],
    flow: dict[str, Any] | None = None,
) -> None:
    """Reject a declared product execution closure that reaches into Builder."""

    harness = Path(str(generated.get("harness_dir") or "")).resolve()
    stage = Path(str(generated.get("stage_codebase") or "")).resolve()
    skill_name = str(generated.get("skill_name") or "")
    definition = flow or load_flow(harness)
    _assert_flowstep_declarations_closed(definition)
    inferred = infer_codebase(harness)
    if inferred is None or inferred.resolve() != stage:
        raise FlowError("cannot bind product runtime audit to its exact codebase")
    roots = list(_runtime_harness_execution_paths(definition, harness))
    roots.extend(
        [
            stage / ".agents" / "skills" / skill_name / "scripts",
            stage / ".claude" / "skills" / skill_name / "scripts",
        ]
    )
    roots.extend(
        stage / "flowsteps" / "tools" / tool_id
        for tool_id in sorted(_runtime_tool_package_ids(definition))
    )
    implementation_dependencies = sorted(
        _runtime_implementation_dependency_paths(definition, stage),
        key=lambda item: item.as_posix(),
    )
    roots.extend(implementation_dependencies)
    roots.extend(
        Path(str(item)).resolve()
        for item in generated.get("runtime_audit_roots") or []
        if item
    )
    seen: set[Path] = set()
    for root in roots:
        resolved = root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        _assert_python_path_runtime_isolated(
            root,
            exclude_runtime_releases=False,
        )
    for tool_id in sorted(_runtime_flowstep_tool_package_ids(definition)):
        _assert_closed_flowstep_package(stage / "flowsteps" / "tools" / tool_id)
    flowstep_dependencies = sorted(
        _runtime_flowstep_implementation_dependency_paths(definition, stage),
        key=lambda item: item.as_posix(),
    )
    for dependency in flowstep_dependencies:
        _assert_closed_flowstep_package(dependency)


def _verify_staged_runtime_release(generated: dict[str, Any]) -> dict[str, Any]:
    release = generated.get("runtime_release")
    if not isinstance(release, dict):
        raise FlowError("staged harness has no runtime_release descriptor")
    for path_key, digest_key, label in (
        ("manifest_path", "manifest_sha256", "runtime release manifest"),
        (
            "codebase_launcher_path",
            "codebase_launcher_sha256",
            "codebase runtime launcher",
        ),
        ("archive_path", "archive_sha256", "runtime release archive"),
        ("harness_lock_path", "harness_lock_sha256", "workflow runtime lock"),
    ):
        _assert_declared_file_digest(release, path_key, digest_key, label=label)
    try:
        manifest = verify_runtime_release(Path(str(release["manifest_path"])))
    except RuntimeReleaseError as exc:
        raise FlowError(f"invalid codebase-owned runtime release: {exc}") from exc
    expected = {
        "schema": manifest.get("schema"),
        "runtime_name": manifest.get("runtime_name"),
        "runtime_version": manifest.get("runtime_version"),
        "runtime_id": manifest.get("runtime_id"),
        "cli_abi": manifest.get("cli_abi"),
        "entrypoint": manifest.get("entrypoint"),
        "python_abi": manifest.get("python_abi"),
        "dependencies": manifest.get("dependencies"),
        "payload_digest": manifest.get("payload_digest"),
        "member_count": len(manifest.get("members") or []),
    }
    mismatched = [key for key, value in expected.items() if release.get(key) != value]
    if mismatched:
        raise FlowError(
            "staged runtime release descriptor mismatch: " + ", ".join(mismatched)
        )
    _assert_product_runtime_isolated(generated)
    return release


def _workflow_contracts(
    definition: dict[str, Any],
    source: dict[str, Any] | None = None,
    *,
    allow_legacy_wiring_only: bool = False,
) -> dict[str, Any]:
    """Return the portable workflow boundary plus milestone wiring.

    Root request/configuration/result schemas are authoring metadata: they are
    intentionally absent from ``flowstep_flow_v4`` but must survive in the
    source bundle.  Milestone rows remain derived from the canonical compiled
    definition so the bundle cannot carry a second graph.
    """
    milestones = [item for item in definition.get("milestones") or [] if isinstance(item, dict)]
    rows: list[dict[str, Any]] = []
    for item in milestones:
        inputs = dict(item.get("inputs") or {})
        rows.append(
            {
                "milestone_id": str(item.get("id") or ""),
                "input_schema": str(item.get("input_schema") or ""),
                "inputs": inputs,
                "output_contract": str(item.get("output_contract") or ""),
                "output_schema": str(item.get("output_schema") or ""),
                "outputs": list(item.get("outputs") or []),
            }
        )
    referenced_sources = {
        str(reference.get("from") if isinstance(reference, dict) else reference).split(".", 1)[0]
        for item in milestones
        for reference in (item.get("inputs") or {}).values()
        if str(reference.get("from") if isinstance(reference, dict) else reference) != "user.request"
        and "." in str(reference.get("from") if isinstance(reference, dict) else reference)
    }
    terminal_ids = [
        str(item.get("id") or "")
        for item in milestones
        if str(item.get("id") or "") not in referenced_sources
    ]
    contracts: dict[str, Any] = {
        "milestones": rows,
        "entry_milestones": [
            str(item.get("id") or "")
            for item in milestones
            if all(
                str(binding.get("from") if isinstance(binding, dict) else binding)
                == "user.request"
                for binding in (item.get("inputs") or {}).values()
            )
        ],
        "terminal_milestones": terminal_ids,
    }
    canvas = source.get("canvas") if isinstance(source, dict) else None
    authored = canvas.get("workflow_contracts") if isinstance(canvas, dict) else None
    if not isinstance(authored, dict) and not allow_legacy_wiring_only:
        raise FlowError(
            "skill-native source requires closed workflow_contracts; wiring-only "
            "synthesis is reserved for explicit legacy-import staging"
        )
    if isinstance(authored, dict):
        contracts = {
            "request_schema": str(authored.get("request_schema") or ""),
            "configuration_schema": str(authored.get("configuration_schema") or ""),
            "result_schema": str(authored.get("result_schema") or ""),
            "terminal_bindings": [
                {
                    "name": str(item.get("name") or ""),
                    "from": str(item.get("from") or ""),
                    "output": str(item.get("output") or ""),
                }
                for item in authored.get("terminal_bindings") or []
                if isinstance(item, dict)
            ],
            **contracts,
        }
    return contracts


def _implementation_requirements(
    harness: Path,
    definition: dict[str, Any],
    source: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    harness = harness.resolve()
    codebase = infer_codebase(harness)
    requirements: list[dict[str, Any]] = []
    seen_tool_refs: set[str] = set()
    seen_dependencies: set[str] = set()
    flow_id = str(definition.get("flow_id") or "workflow")
    source_agents = {
        str(item.get("agent_id") or ""): item
        for item in (source or {}).get("milestones") or []
        if isinstance(item, dict)
    }

    def local_file(base: Path, relative: str) -> tuple[Path | None, dict[str, Any]]:
        pure = PurePosixPath(relative)
        if (
            not relative
            or "\\" in relative
            or pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            return None, {}
        base = base.resolve()
        path = base.joinpath(*pure.parts).resolve()
        if path == base or base not in path.parents or not path.is_file() or _is_unsafe_link(path):
            return None, {}
        return path, {
            "digest": f"sha256:{sha256_file(path)}",
            "byte_count": path.stat().st_size,
        }

    def tool_package(tool_id: str) -> tuple[list[str], dict[str, Any], int]:
        blockers = ["staged codebase is unavailable"]
        if codebase is None:
            return blockers, {}, 0
        blockers = validate_library_tool(codebase, tool_id)
        tool_root = codebase / "flowsteps" / "tools" / tool_id
        if (tool_root / "BUILD_REQUIRED").is_file():
            blockers.append("BUILD_REQUIRED marker is present")
        if blockers:
            return blockers, {}, 0
        tool_files = [
            path
            for path in tool_root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix.lower() != ".pyc"
        ]
        ordered = sorted(
            tool_files,
            key=lambda item: item.relative_to(tool_root).as_posix(),
        )
        members = [
            {
                "path": path.relative_to(tool_root).as_posix(),
                "digest": f"sha256:{sha256_file(path)}",
            }
            for path in ordered
        ]
        return [], {"digest": digest_json(members), "member_count": len(members)}, sum(
            path.stat().st_size for path in ordered
        )

    dependency_rows = [str(item) for item in definition.get("implementation_dependencies") or []]
    for milestone in definition.get("milestones") or []:
        if not isinstance(milestone, dict):
            continue
        milestone_id = str(milestone.get("id") or "milestone")
        source_agent = source_agents.get(milestone_id) or {}
        execution = source_agent.get("execution") if isinstance(source_agent, dict) else None
        execution = execution if isinstance(execution, dict) else {}
        candidate_binding = execution.get("candidate_executor")
        candidate_binding = candidate_binding if isinstance(candidate_binding, dict) else {}
        candidate_ref = str(candidate_binding.get("ref") or "")
        handler_ref = str(milestone.get("handler") or "")
        _, handler_proof = local_file(harness, handler_ref)
        handler_built = bool(candidate_ref and handler_proof)
        handler_requirement = {
            "ref": candidate_ref or f"handler.{flow_id}.{milestone_id}@source",
            "kind": "milestone_handler",
            "role": "candidate_executor",
            "milestone_id": milestone_id,
            "runtime_abi": "m8m_milestone_handler_v1",
            "entrypoint": handler_ref,
            "build_state": "built" if handler_built else "BUILD_REQUIRED",
        }
        if handler_built:
            handler_requirement.update(handler_proof)
        requirements.append(handler_requirement)
        dependency_rows.extend(
            str(item) for item in milestone.get("implementation_dependencies") or []
        )
        worker = str(milestone.get("worker") or "")
        judge_worker = worker if milestone.get("loop") == "judge" else ""
        tool_binding_refs = {
            str(item.get("tool") or ""): str(item.get("ref") or "")
            for item in execution.get("tool_bindings") or []
            if isinstance(item, dict)
        }
        for flowstep in milestone.get("flowsteps") or []:
            if not isinstance(flowstep, dict):
                continue
            flowstep_id = str(flowstep.get("id") or "")
            authored_ref = tool_binding_refs.get(flowstep_id) or ""
            tool_id = (
                local_tool_package_name(
                    authored_ref,
                    label=f"{milestone_id}.execution.tool_bindings[{flowstep_id}].ref",
                )
                if authored_ref
                else ""
            )
            if not tool_id or not authored_ref or authored_ref in seen_tool_refs:
                continue
            seen_tool_refs.add(authored_ref)
            blockers, package_proof, _ = tool_package(tool_id)
            tool_built = bool(authored_ref and not blockers)
            tool_requirement = {
                "ref": authored_ref or f"tool.{tool_id}@source",
                "kind": "flowstep_tool",
                "role": "candidate_executor",
                "local_name": tool_id,
                "runtime_abi": "m8m_flowstep_tool_v1",
                "entrypoint": "run",
                "build_state": "built" if tool_built else "BUILD_REQUIRED",
            }
            if tool_built:
                tool_requirement.update(package_proof)
            requirements.append(tool_requirement)
        judge_abi = str(milestone.get("judge_abi") or "")
        if judge_worker and judge_abi == "m8m_milestone_judge_v1":
            judge_package = runtime_package_name(
                judge_worker,
                label=f"{milestone_id}.worker",
            )
            blockers, package_proof, package_bytes = tool_package(judge_package)
            judge_proof = (
                {"digest": package_proof["digest"], "byte_count": package_bytes}
                if not blockers
                else {}
            )
            judge_binding = execution.get("judge")
            judge_binding = judge_binding if isinstance(judge_binding, dict) else {}
            judge_ref = str(judge_binding.get("ref") or "")
            judge_built = bool(judge_ref and not blockers)
            judge_requirement = {
                "ref": judge_ref or f"judge.{flow_id}.{milestone_id}@source",
                "kind": "milestone_judge",
                "role": "milestone_judge",
                "milestone_id": milestone_id,
                "runtime_abi": judge_abi,
                "entrypoint": "run",
                "build_state": "built" if judge_built else "BUILD_REQUIRED",
            }
            if judge_built:
                judge_requirement.update(judge_proof)
            requirements.append(judge_requirement)
    for relative in dependency_rows:
        if relative in seen_dependencies:
            continue
        seen_dependencies.add(relative)
        _, proof = local_file(codebase or harness, relative)
        requirement = {
            "ref": f"dependency.{flow_id}.{len(seen_dependencies)}@source",
            "kind": "shared_dependency",
            "role": "implementation_dependency",
            "runtime_abi": "m8m_shared_dependency_v1",
            "entrypoint": relative,
            "build_state": "built" if proof else "BUILD_REQUIRED",
        }
        requirement.update(proof)
        requirements.append(requirement)
    return requirements


def _agent_profile_requirements(
    definition: dict[str, Any],
    *,
    source: dict[str, Any] | None = None,
    resources: list[dict[str, Any]] | None = None,
    implementations: list[dict[str, Any]] | None = None,
    capabilities: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Emit exact AI profile requirements or explicit BUILD_REQUIRED rows.

    ``intelligence`` remains a canvas/runtime classification.  It is never
    enough to invent a runnable profile.  Only the skill-native ``execution``
    declaration may produce a built profile requirement.
    """

    source_agents = {
        str(item.get("agent_id") or ""): item
        for item in (source or {}).get("milestones") or []
        if isinstance(item, dict)
    }
    resource_ref_by_path = {
        str(item.get("source_path") or ""): str(item.get("ref") or "")
        for item in resources or []
        if isinstance(item, dict)
    }
    implementation_by_ref = {
        str(item.get("ref") or ""): item
        for item in implementations or []
        if isinstance(item, dict) and item.get("ref")
    }
    capability_by_identity = {
        (str(item.get("milestone_id") or ""), str(item.get("id") or "")): item
        for item in capabilities or []
        if isinstance(item, dict) and item.get("id")
    }
    result: list[dict[str, Any]] = []

    def unresolved(milestone_id: str, role: str, missing: list[str]) -> dict[str, Any]:
        return {
            "ref": f"agent_profile.{milestone_id}.{role}@source",
            "milestone_id": milestone_id,
            "role": role,
            "build_state": "BUILD_REQUIRED",
            "missing": list(dict.fromkeys(missing)),
        }

    def compile_profile(
        *,
        milestone: dict[str, Any],
        agent: dict[str, Any],
        role: str,
        binding: dict[str, Any] | None,
    ) -> dict[str, Any]:
        milestone_id = str(milestone.get("id") or "milestone")
        if not isinstance(binding, dict):
            return unresolved(milestone_id, role, [f"execution.{role}"])
        profile = binding.get("profile")
        if not isinstance(profile, dict):
            return unresolved(milestone_id, role, [f"execution.{role}.profile"])
        missing: list[str] = []
        executor_ref = str(binding.get("ref") or "")
        if not executor_ref:
            missing.append(f"execution.{role}.ref")
        else:
            executor = implementation_by_ref.get(executor_ref)
            if executor is None:
                missing.append(f"implementation:{executor_ref}:missing")
            elif executor.get("build_state") != "built":
                missing.append(f"implementation:{executor_ref}:BUILD_REQUIRED")
        gem_ref = resource_ref_by_path.get(str(milestone.get("gem") or ""), "")
        if not gem_ref:
            missing.append("gem")
        schema_fields = (
            ("input_schema", "output_schema", "draft_schema")
            if role == "candidate_executor"
            else ("input_schema", "output_schema", "receipt_schema")
        )
        schema_refs: dict[str, str] = {}
        for field in schema_fields:
            path = str(milestone.get(field) or "")
            ref = resource_ref_by_path.get(path, "")
            if not path or not ref:
                missing.append(field)
            else:
                schema_refs[field] = ref
        tool_refs: list[str] = []
        if role == "candidate_executor":
            binding_refs = {
                str(item.get("tool") or ""): str(item.get("ref") or "")
                for item in (agent.get("execution") or {}).get("tool_bindings") or []
                if isinstance(item, dict)
            }
            for tool_id in profile.get("tools") or []:
                ref = binding_refs.get(str(tool_id), "")
                if not ref:
                    missing.append(f"tool:{tool_id}")
                else:
                    implementation = implementation_by_ref.get(ref)
                    if implementation is None:
                        missing.append(f"implementation:{ref}:missing")
                    elif implementation.get("build_state") != "built":
                        missing.append(f"implementation:{ref}:BUILD_REQUIRED")
                    else:
                        if ref not in tool_refs:
                            tool_refs.append(ref)
        elif profile.get("tools"):
            missing.append("judge_profile.tools_must_be_empty")
        capability_ids = [str(item) for item in profile.get("capabilities") or []]
        for capability_id in capability_ids:
            capability = capability_by_identity.get((milestone_id, capability_id))
            if capability is None:
                missing.append(
                    f"capability:{milestone_id}:{capability_id}:missing"
                )
            elif capability.get("build_state") != "built":
                missing.append(
                    f"capability:{milestone_id}:{capability_id}:BUILD_REQUIRED"
                )
        for field in ("model_configuration", "token_budget", "timeout_seconds"):
            if field not in profile:
                missing.append(field)
        if missing:
            row = unresolved(milestone_id, role, missing)
            if profile.get("ref"):
                row["ref"] = str(profile["ref"])
            return row
        return {
            "ref": str(profile["ref"]),
            "milestone_id": milestone_id,
            "role": role,
            "executor_ref": executor_ref,
            "gem_ref": gem_ref,
            "schema_refs": schema_refs,
            "tool_refs": tool_refs,
            "capability_ids": capability_ids,
            "model_configuration": dict(profile["model_configuration"]),
            "token_budget": dict(profile["token_budget"]),
            "timeout_seconds": int(profile["timeout_seconds"]),
            "build_state": "built",
        }

    for milestone in definition.get("milestones") or []:
        if not isinstance(milestone, dict):
            continue
        intelligence = str(milestone.get("intelligence") or "none")
        milestone_id = str(milestone.get("id") or "milestone")
        agent = source_agents.get(milestone_id)
        candidate_required = intelligence != "none"
        judge_required = intelligence == "judge"
        if not isinstance(agent, dict):
            if candidate_required:
                result.append(
                    unresolved(
                        milestone_id,
                        "candidate_executor",
                        ["authored execution declaration"],
                    )
                )
            if judge_required:
                result.append(
                    unresolved(milestone_id, "ai_judge", ["authored judge profile"])
                )
            continue
        execution = agent.get("execution")
        execution = execution if isinstance(execution, dict) else {}
        candidate = execution.get("candidate_executor")
        judge = execution.get("judge")
        candidate_has_profile = (
            isinstance(candidate, dict) and isinstance(candidate.get("profile"), dict)
        )
        judge_has_profile = (
            isinstance(judge, dict) and isinstance(judge.get("profile"), dict)
        )
        if candidate_required or candidate_has_profile:
            result.append(
                compile_profile(
                    milestone=milestone,
                    agent=agent,
                    role="candidate_executor",
                    binding=candidate if isinstance(candidate, dict) else None,
                )
            )
        if judge_required or judge_has_profile:
            result.append(
                compile_profile(
                    milestone=milestone,
                    agent=agent,
                    role="ai_judge",
                    binding=judge if isinstance(judge, dict) else None,
                )
            )
    return result


def _capability_requirements(
    definition: dict[str, Any],
    *,
    source: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    source_agents = {
        str(item.get("agent_id") or ""): item
        for item in (source or {}).get("milestones") or []
        if isinstance(item, dict)
    }
    result: list[dict[str, Any]] = []
    for milestone in definition.get("milestones") or []:
        if not isinstance(milestone, dict):
            continue
        milestone_id = str(milestone.get("id") or "milestone")
        agent = source_agents.get(milestone_id)
        declarations = (
            list(agent.get("capabilities") or []) if isinstance(agent, dict) else []
        )
        for declaration in declarations:
            if not isinstance(declaration, dict):
                continue
            row: dict[str, Any] = {
                "id": str(declaration.get("id") or ""),
                "ref": str(declaration.get("ref") or f"capability.{milestone_id}.unresolved@0.0.0"),
                "milestone_id": milestone_id,
                "access": str(declaration.get("access") or ""),
                "side_effects": str(declaration.get("side_effects") or "none"),
                "build_state": "built" if declaration.get("ref") else "BUILD_REQUIRED",
            }
            if row["side_effects"] == "external":
                row["phase_journal"] = dict(agent.get("phase_journal") or {})
                if not row["phase_journal"]:
                    row["build_state"] = "BUILD_REQUIRED"
            if row["build_state"] == "BUILD_REQUIRED":
                missing = []
                if not declaration.get("ref"):
                    missing.append("authored capability ref")
                if row["side_effects"] == "external" and not row.get("phase_journal"):
                    missing.append("phase_journal")
                row["missing"] = missing
            result.append(row)
        if milestone.get("side_effects") == "external" and not declarations:
            row = {
                "id": f"unresolved.{milestone_id}.external_side_effect",
                "ref": f"capability.{milestone_id}.external_side_effect@0.0.0",
                "milestone_id": milestone_id,
                "access": "execute",
                "side_effects": "external",
                "build_state": "BUILD_REQUIRED",
                "missing": ["explicit external capability declaration"],
            }
            journal = milestone.get("phase_journal")
            if isinstance(journal, dict) and journal:
                row["phase_journal"] = dict(journal)
            result.append(row)
    return result


def _requirements_are_built(*groups: list[dict[str, Any]]) -> bool:
    return all(item.get("build_state") == "built" for group in groups for item in group)


def _platform_common_profile_metadata(
    definition: dict[str, Any],
) -> dict[str, Any]:
    """Emit advisory platform compatibility without granting admission.

    The Builder remains locally authoritative for its codebase runtime.  This
    portable observer metadata merely reports whether the canonical workflow
    uses the native platform's versioned common linear/DAG profile.  Platform
    admission independently enforces the same boundary.
    """

    unsupported: list[str] = []
    for milestone in definition.get("milestones") or []:
        if not isinstance(milestone, dict):
            continue
        milestone_id = str(milestone.get("id") or "milestone")
        loop = str(milestone.get("loop") or "none")
        if loop not in {"none", "judge"}:
            unsupported.append(f"milestone:{milestone_id}:loop:{loop}")
        for field in ("ledger", "branch", "on_path", "cycle", "on_cycle"):
            if milestone.get(field) is not None:
                unsupported.append(f"milestone:{milestone_id}:{field}")
    unsupported = sorted(set(unsupported))
    return {
        "platform_common_profile": (
            "compatible" if not unsupported else "unsupported"
        ),
        "platform_unsupported_features": unsupported,
    }


def _local_validation_summary(
    harness: Path,
    generated: dict[str, Any],
    *,
    ready: bool,
    requirement_groups: tuple[list[dict[str, Any]], ...],
) -> dict[str, Any]:
    """Preflight the exact staged harness before claiming SOURCE_VALID.

    The authoritative chosen validation receipt is still produced by the next
    Builder milestone.  This bounded duplicate check exists because the source
    bundle itself carries an advisory validation summary and must never claim
    SOURCE_VALID before the same fail-closed harness validator can pass.
    """

    findings = len(generated.get("build_required_tools") or []) + sum(
        item.get("build_state") != "built"
        for group in requirement_groups
        for item in group
    )
    validator_passed = False
    try:
        validate_harness(harness, update_instruction=False)
    except (FlowError, OSError) as exc:
        # The full diagnostic remains in the staged Builder workspace.  Only a
        # deterministic count crosses into the portable source-bundle summary.
        findings += max(1, str(exc).count("\n- "))
    else:
        validator_passed = True
    valid = generated.get("status") == "PASS" and ready and validator_passed
    if not valid and findings == 0:
        findings = 1
    return {
        "status": "SOURCE_VALID" if valid else "BUILD_REQUIRED",
        "findings_count": findings,
    }


def _compile_generated_source_bundle(generated: dict[str, Any]) -> dict[str, Any]:
    harness = Path(str(generated.get("harness_dir") or "")).resolve()
    definition = load_yaml(harness / "flow.yaml")
    if not isinstance(definition, dict):
        raise FlowError("generated flow.yaml is not a workflow object")
    milestones = [item for item in definition.get("milestones") or [] if isinstance(item, dict)]
    workflow_contracts = _workflow_contracts(definition, allow_legacy_wiring_only=True)
    observer = {
        "workflow": {
            "title": str(definition.get("flow_id") or "M8M workflow").replace("_", " ").title()
        },
        "milestones": [
            {
                "id": str(item.get("id") or ""),
                "title": str(item.get("id") or "milestone").replace("_", " ").title(),
            }
            for item in milestones
        ],
        **_platform_common_profile_metadata(definition),
    }
    resources = [
        *_source_resources(harness, definition),
        *_runtime_release_resources(harness, generated),
    ]
    implementations = _implementation_requirements(harness, definition)
    capabilities = _capability_requirements(definition)
    profiles = _agent_profile_requirements(
        definition,
        resources=resources,
        implementations=implementations,
        capabilities=capabilities,
    )
    ready = _requirements_are_built(implementations, profiles, capabilities)
    validation = _local_validation_summary(
        harness,
        generated,
        ready=ready,
        requirement_groups=(implementations, profiles, capabilities),
    )
    return compile_source_bundle(
        source_root=harness,
        contract_bundle=_contract_bundle_lock(),
        definition=definition,
        workflow_contracts=workflow_contracts,
        resources=resources,
        implementation_requirements=implementations,
        agent_profile_requirements=profiles,
        capability_requirements=capabilities,
        observer=observer,
        validation=validation,
        require_ready=False,
        allow_legacy_wiring_only=True,
    )


def _install_roots(stage: Path, flow_id: str, skill_name: str) -> list[Path]:
    return [
        stage / "flowsteps" / "flows" / flow_id,
        stage / "flowsteps" / "tools",
        stage / ".agents" / "skills" / skill_name,
        stage / ".claude" / "skills" / skill_name,
    ]


def _staged_install_members(stage: Path, flow_id: str, skill_name: str) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    planning_root = stage / "flowsteps" / "flows" / flow_id / "planning"
    product_root = stage / ".agents" / "skills" / skill_name
    paths = [path for path in stage.rglob("*") if path.is_file()]
    for path in sorted(paths, key=lambda item: item.relative_to(stage).as_posix()):
        if (
            "__pycache__" in path.parts
            or path.suffix.lower() in {".pyc", ".pyo"}
        ):
            continue
        source = path.relative_to(stage).as_posix()
        destinations = [f"codebase/{source}"]
        try:
            planning_rel = path.relative_to(planning_root)
        except ValueError:
            pass
        else:
            destinations.append(f"target/planning/{planning_rel.as_posix()}")
        try:
            product_rel = path.relative_to(product_root)
        except ValueError:
            pass
        else:
            product_posix = product_rel.as_posix()
            if product_posix == "scripts/m8m_run.py":
                destinations.append(f"target/{product_posix}")
        members.append(
            {
                "source": source,
                "destinations": destinations,
                "byte_count": path.stat().st_size,
                "digest": f"sha256:{sha256_file(path)}",
            }
        )
    return members


def _assert_staged_install_manifest(
    generated: dict[str, Any],
) -> tuple[Path, list[dict[str, Any]], str]:
    stage = Path(str(generated.get("stage_codebase") or "")).resolve()
    flow_id = str(generated.get("flow_id") or "")
    skill_name = str(generated.get("skill_name") or "")
    declared = generated.get("staged_members")
    if not isinstance(declared, list):
        raise FlowError("generated staged_members manifest is missing")
    current = _staged_install_members(stage, flow_id, skill_name)
    if current != declared:
        raise FlowError(
            "staged install members changed after source selection; replace flow_generated "
            "and revalidate before local installation"
        )
    digest = digest_json(current)
    if generated.get("staged_members_digest") != digest:
        raise FlowError("staged_members_digest does not match the exact staged install members")
    return stage, current, digest


def _skill_native_source_paths(source: dict[str, Any]) -> list[str]:
    paths: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in paths:
            paths.append(text)

    for resource in source.get("resources") or []:
        if isinstance(resource, dict):
            add(resource.get("path"))
    canvas = source.get("canvas") if isinstance(source.get("canvas"), dict) else {}
    for value in canvas.get("implementation_dependencies") or []:
        add(value)
    for agent in source.get("milestones") or []:
        if not isinstance(agent, dict):
            continue
        for key in (
            "handler",
            "test",
            "input_schema",
            "output_schema",
            "draft_schema",
            "receipt_schema",
            "gem",
        ):
            add(agent.get(key))
        for key in ("implementation_dependencies", "read_paths"):
            for value in agent.get(key) or []:
                add(value)
    return paths


def _bounded_source_file(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise FlowError(f"unsafe skill-native source path: {relative}")
    root = root.resolve()
    lexical_path = root.joinpath(*pure.parts)
    cursor = root
    for part in pure.parts:
        cursor = cursor / part
        if _is_unsafe_link(cursor):
            raise FlowError(f"skill-native source path uses an unsafe link: {relative}")
    path = lexical_path.resolve()
    if path == root or root not in path.parents:
        raise FlowError(f"skill-native source path escapes its root: {relative}")
    return path


def _copy_skill_native_source(
    *,
    source_root: Path,
    stage: Path,
    harness: Path,
    skill_name: str,
    source: dict[str, Any],
    overwrite: bool,
) -> tuple[list[str], list[str]]:
    written: list[str] = []
    blockers: list[str] = []
    canvas = source.get("canvas") if isinstance(source.get("canvas"), dict) else {}
    project_dependencies = {
        str(item)
        for item in canvas.get("implementation_dependencies") or []
        if item
    }
    for agent in source.get("milestones") or []:
        if isinstance(agent, dict):
            project_dependencies.update(
                str(item) for item in agent.get("implementation_dependencies") or [] if item
            )
    product_roots = [
        stage / ".agents" / "skills" / skill_name,
        stage / ".claude" / "skills" / skill_name,
    ]
    for relative in _skill_native_source_paths(source):
        source_file = _bounded_source_file(source_root, relative)
        if not source_file.is_file():
            blockers.append(f"missing skill-native source member: {relative}")
            continue
        destinations = [harness.joinpath(*PurePosixPath(relative).parts)]
        destinations.extend(root.joinpath(*PurePosixPath(relative).parts) for root in product_roots)
        if relative in project_dependencies:
            destinations.append(stage.joinpath(*PurePosixPath(relative).parts))
        for destination in destinations:
            if destination.exists() and not overwrite:
                if destination.is_file() and sha256_file(destination) == sha256_file(source_file):
                    continue
                blockers.append(f"staged source conflict: {destination}")
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination)
            written.append(str(destination))
    return written, blockers


def _compile_skill_native_bundle(
    *,
    source: dict[str, Any],
    definition: dict[str, Any],
    source_root: Path,
    generated: dict[str, Any],
) -> dict[str, Any]:
    resources: list[dict[str, str]] = []
    for index, item in enumerate(source.get("resources") or []):
        if not isinstance(item, dict):
            continue
        relative = str(item.get("path") or "")
        suffix = PurePosixPath(relative).suffix.lower()
        media_type = (
            "text/markdown"
            if suffix == ".md"
            else "application/schema+json"
            if suffix == ".json"
            else "application/yaml"
            if suffix in {".yaml", ".yml"}
            else "text/x-python"
            if suffix == ".py"
            else "application/octet-stream"
        )
        resources.append(
            {
                "ref": "source.{}.{}.{}".format(
                    str(item.get("owner") or "workflow"),
                    str(item.get("id") or index),
                    index,
                ),
                "kind": str(item.get("kind") or "reference"),
                "role": f"{item.get('owner') or 'workflow'}_{item.get('id') or 'resource'}",
                "source_path": relative,
                "media_type": media_type,
            }
        )
    milestones = [item for item in definition.get("milestones") or [] if isinstance(item, dict)]
    workflow_contracts = _workflow_contracts(definition, source)
    stage_harness = Path(str(generated.get("harness_dir") or "")).resolve()
    implementations = _implementation_requirements(stage_harness, definition, source)
    capabilities = _capability_requirements(definition, source=source)
    agent_profiles = _agent_profile_requirements(
        definition,
        source=source,
        resources=resources,
        implementations=implementations,
        capabilities=capabilities,
    )
    ready = _requirements_are_built(implementations, agent_profiles, capabilities)
    validation = _local_validation_summary(
        stage_harness,
        generated,
        ready=ready,
        requirement_groups=(implementations, agent_profiles, capabilities),
    )
    observer = {
        "workflow": dict((source.get("canvas") or {}).get("observer") or {}),
        "milestones": [
            {
                "id": str(item.get("agent_id") or ""),
                **dict(item.get("observer") or {}),
            }
            for item in source.get("milestones") or []
            if isinstance(item, dict)
        ],
        **_platform_common_profile_metadata(definition),
    }
    known_resource_paths = {item["source_path"] for item in resources}
    for milestone in source.get("milestones") or []:
        if not isinstance(milestone, dict):
            continue
        relative = str(milestone.get("test") or "")
        if not relative or relative in known_resource_paths:
            continue
        known_resource_paths.add(relative)
        resources.append(
            {
                "ref": f"test.{milestone.get('agent_id')}@source",
                "kind": "test",
                "role": "milestone_validation_test",
                "source_path": relative,
                "media_type": "text/x-python",
            }
        )
    resources.extend(_runtime_release_resources(stage_harness, generated))
    return compile_source_bundle(
        source_root=stage_harness,
        contract_bundle=_contract_bundle_lock(),
        definition=definition,
        workflow_contracts=workflow_contracts,
        resources=resources,
        implementation_requirements=implementations,
        agent_profile_requirements=agent_profiles,
        capability_requirements=capabilities,
        observer=observer,
        validation=validation,
        require_ready=False,
    )


def _generate_skill_native(
    *,
    stage: Path,
    source_root: Path,
    source: dict[str, Any],
    source_definition: dict[str, Any],
    audit: dict[str, Any],
    request: dict[str, Any],
    toolbox_manifest: dict[str, Any],
) -> dict[str, Any]:
    definition = dict(source_definition)
    definition.setdefault("max_run_seconds", 3600)
    definition.setdefault("artifact_root", "artifacts")
    flow_id = str(definition.get("flow_id") or "")
    skill_name = _skill_name(audit, request)
    harness = stage / "flowsteps" / "flows" / flow_id
    harness.mkdir(parents=True, exist_ok=True)
    overwrite = bool(request.get("overwrite"))
    written, blockers = _copy_skill_native_source(
        source_root=source_root,
        stage=stage,
        harness=harness,
        skill_name=skill_name,
        source=source,
        overwrite=overwrite,
    )
    try:
        staged_source = load_skill_source(harness)
        staged_definition = compile_skill_source(harness)
    except SkillSourceError as exc:
        raise FlowError(f"staged skill-native source is invalid: {exc}") from exc
    if staged_source != source or staged_definition != source_definition:
        raise FlowError(
            "staged authored source does not match the frozen audit; replace audit_complete"
        )
    canonical_path = harness / "compiled" / "workflow" / "flow.canonical.json"
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_bytes(canonical_json_bytes(definition))
    written.append(str(canonical_path))
    flow_path = harness / "flow.yaml"
    flow_path.write_text(
        yaml.safe_dump(definition, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )
    written.append(str(flow_path))
    normalized = load_flow(harness)
    instruction = write_instruction(harness, normalized, source="skill-source")
    chart = write_flowchart(
        harness,
        normalized["steps"],
        title=str((source.get("canvas") or {}).get("observer", {}).get("title") or skill_name),
        flow_id=flow_id,
        source="skill-source",
    )
    written.extend([str(instruction), str(chart), str(chart.with_suffix(".jpg"))])
    table = classification_from_flow(normalized)
    table_path = harness / "planning" / "tool-vs-intelligence.json"
    table_path.write_bytes(canonical_json_bytes(table))
    written.append(str(table_path))
    tool_results = [dict(item) for item in toolbox_manifest.get("tools") or [] if isinstance(item, dict)]
    build_required = sorted(
        str(item.get("tool_id") or "")
        for item in tool_results
        if item.get("status") != "PASS" or item.get("non_runnable")
    )
    for milestone in definition.get("milestones") or []:
        if not isinstance(milestone, dict):
            continue
        for key in ("handler", "test"):
            relative = str(milestone.get(key) or "")
            if relative and not (harness / relative).is_file():
                blockers.append(f"{milestone.get('id')}: missing authored {key} {relative}")
    if blockers:
        build_required.extend(
            f"source_member_{index}" for index, _ in enumerate(dict.fromkeys(blockers), start=1)
        )
    build_required = list(dict.fromkeys(build_required))
    return {
        "schema": "flowstep_harness_generate_v4",
        "status": "BUILD_REQUIRED" if build_required else "PASS",
        "runnable": not build_required,
        "non_runnable": bool(build_required),
        "build_required_tools": build_required,
        "toolbox": tool_results,
        "product_skill": str(stage / ".agents" / "skills" / skill_name / "SKILL.md"),
        "harness_dir": str(harness),
        "codebase": str(stage),
        "flow_id": flow_id,
        "skill_name": skill_name,
        "milestones": [str(item.get("id") or "") for item in definition.get("milestones") or []],
        "tools": _tool_ids(audit),
        "instruction_path": str(instruction),
        "flowchart_path": str(chart),
        "flowchart_jpg": str(chart.with_suffix(".jpg")),
        "tool_vs_intelligence": table,
        "tool_vs_intelligence_path": str(table_path),
        "tool_generation": tool_results,
        "written": written,
        "notes": ["skill-native source compiled as the sole graph authority", *blockers],
    }


def _generate(input_data: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    request = _request(input_data)
    audit = _input_object(input_data, "source_audit", "audit")
    if not isinstance(audit, dict):
        raise FlowError("flow_generated needs the chosen source audit")
    toolbox_manifest = _input_object(input_data, "toolbox_manifest", "toolbox")
    if not isinstance(toolbox_manifest, dict):
        raise FlowError("flow_generated needs the chosen toolbox manifest")
    stage = _stage_codebase(run_dir)
    manifest_stage = Path(str(toolbox_manifest.get("stage_codebase") or "")).resolve()
    if manifest_stage != stage:
        raise FlowError("flow_generated toolbox manifest names a different staged codebase")
    native_source_root: Path | None = None
    native_source: dict[str, Any] | None = None
    native_definition: dict[str, Any] | None = None
    if audit.get("authoring_mode") == "skill_native":
        native_source_root, native_source, frozen_definition = _assert_frozen_skill_source(
            audit, run_dir
        )
        native_definition = dict(frozen_definition)
        native_definition.setdefault("max_run_seconds", 3600)
        native_definition.setdefault("artifact_root", "artifacts")
        generated = _generate_skill_native(
            stage=stage,
            source_root=native_source_root,
            source=native_source,
            source_definition=frozen_definition,
            audit=audit,
            request=request,
            toolbox_manifest=toolbox_manifest,
        )
    elif audit.get("authoring_mode") == "from_context":
        snapshot = Path(str(audit.get("source_snapshot") or "")).resolve()
        expected = Path(run_dir).resolve() / "work" / "builder" / "source-snapshot"
        if snapshot != expected or not snapshot.is_dir():
            raise FlowError("from_context audit does not reference this run's frozen source snapshot")
        members = _source_snapshot_members(snapshot)
        if members != audit.get("source_snapshot_members"):
            raise FlowError("frozen source context changed after audit; replace audit_complete")
        authored_root = Path(run_dir).resolve() / "work" / "builder" / "authored-source"
        if authored_root.exists():
            shutil.rmtree(authored_root)
        write_migrated_skill_source(
            authored_root,
            audit,
            flow_id=_flow_id(audit, request),
            skill_name=_skill_name(audit, request),
            source_root=snapshot,
        )
        try:
            native_source = load_skill_source(authored_root)
            frozen_definition = compile_skill_source(authored_root)
        except SkillSourceError as exc:
            raise FlowError(f"authored skill-native source is invalid: {exc}") from exc
        native_definition = dict(frozen_definition)
        native_definition.setdefault("max_run_seconds", 3600)
        native_definition.setdefault("artifact_root", "artifacts")
        generated = _generate_skill_native(
            stage=stage,
            source_root=authored_root,
            source=native_source,
            source_definition=frozen_definition,
            audit=audit,
            request=request,
            toolbox_manifest=toolbox_manifest,
        )
        generated["authoring_mode"] = "from_context"
        generated["equivalence_claimed"] = False
        generated.setdefault("notes", []).append(
            "current M8M source was authored from audit context; this is not an equivalence proof"
        )
    else:
        raise FlowError(
            "flow_generated requires skill_native or from_context authoring_mode; lossless "
            "v4 equivalence still uses scripts/import_flow_v4.py inspect, accept, stage, "
            "and verify"
        )
    staged_harness = Path(str(generated.get("harness_dir") or "")).resolve()
    authored_definition = load_yaml(staged_harness / "flow.yaml")
    if not isinstance(authored_definition, dict):
        raise FlowError("staged flow.yaml is not a workflow object")
    canonical_path = staged_harness / "compiled" / "workflow" / "flow.canonical.json"
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_bytes(canonical_json_bytes(authored_definition))
    generated.setdefault("written", []).append(str(canonical_path))
    generated["stage_codebase"] = str(stage)
    product_roots = [
        stage / ".agents" / "skills" / str(generated.get("skill_name") or ""),
        stage / ".claude" / "skills" / str(generated.get("skill_name") or ""),
    ]
    try:
        generated["runtime_release"] = stage_runtime_release(
            builder_root=BUILDER_ROOT,
            harness=staged_harness,
            product_roots=product_roots,
            flow_id=str(generated.get("flow_id") or ""),
            codebase=_path(request, "codebase"),
        )
    except RuntimeReleaseError as exc:
        raise FlowError(f"cannot package the codebase-owned M8M runtime: {exc}") from exc
    _verify_staged_runtime_release(generated)
    compiled = (
        _compile_skill_native_bundle(
            source=native_source,
            definition=native_definition,
            source_root=native_source_root,
            generated=generated,
        )
        if audit.get("authoring_mode") == "skill_native"
        else _compile_generated_source_bundle(generated)
    )
    staged_definition = read_json(
        Path(str(generated.get("harness_dir") or "")).resolve()
        / "compiled"
        / "workflow"
        / "flow.canonical.json"
    )
    if compiled.get("definition") != staged_definition:
        raise FlowError(
            "workflow source bundle definition differs from the staged canonical workflow"
        )
    unresolved = [
        str(item.get("ref") or "unknown requirement")
        for key in (
            "implementation_requirements",
            "agent_profile_requirements",
            "capability_requirements",
        )
        for item in compiled.get(key) or []
        if isinstance(item, dict) and item.get("build_state") != "built"
    ]
    if unresolved:
        generated["status"] = "BUILD_REQUIRED"
        generated["runnable"] = False
        generated["non_runnable"] = True
        generated.setdefault("notes", []).append(
            "unresolved source requirements: " + ", ".join(unresolved)
        )
    generated["source_bundle_digest"] = compiled["source_bundle_proof"]["source_bundle_digest"]
    source_bundle_path = (
        Path(str(generated.get("harness_dir") or "")).resolve()
        / "compiled"
        / "source-bundle.json"
    )
    source_bundle_path.parent.mkdir(parents=True, exist_ok=True)
    source_bundle_path.write_bytes(serialize_source_bundle(compiled))
    generated["source_bundle_path"] = str(source_bundle_path)
    generated["source_bundle_sha256"] = sha256_file(source_bundle_path)
    generated.setdefault("written", []).append(str(source_bundle_path))
    for path_key, digest_key in (
        ("instruction_path", "instruction_sha256"),
        ("flowchart_path", "flowchart_sha256"),
        ("tool_vs_intelligence_path", "tool_vs_intelligence_sha256"),
    ):
        declared = Path(str(generated.get(path_key) or "")).resolve()
        if not declared.is_file():
            raise FlowError(f"staged harness is missing {path_key}: {declared}")
        generated[digest_key] = sha256_file(declared)
    generated["staged_members"] = _staged_install_members(
        stage,
        str(generated.get("flow_id") or ""),
        str(generated.get("skill_name") or ""),
    )
    generated["staged_members_digest"] = digest_json(generated["staged_members"])
    return {
        "outputs": {
            "workflow_source_bundle": compiled,
            "staged_harness": generated,
        }
    }


def _validate_staged_tools(generated: dict[str, Any], harness: Path, flow: dict[str, Any]) -> None:
    blockers: list[str] = []
    if generated.get("status") != "PASS":
        blockers.append(f"generation status is {generated.get('status') or 'missing'}")
    if generated.get("non_runnable") or generated.get("runnable") is False:
        blockers.append("generation report marks the harness non_runnable")
    for tool_id in generated.get("build_required_tools") or []:
        blockers.append(f"{tool_id}: generation report says BUILD_REQUIRED")
    codebase = infer_codebase(harness)
    if codebase is None:
        blockers.append("cannot resolve the staged codebase for toolbox validation")
    else:
        for tool_id in sorted(_runtime_tool_package_ids(flow)):
            marker = codebase / "flowsteps" / "tools" / tool_id / "BUILD_REQUIRED"
            if marker.is_file():
                blockers.append(f"{tool_id}: BUILD_REQUIRED marker is present")
            blockers.extend(validate_library_tool(codebase, tool_id))
    if blockers:
        unique = list(dict.fromkeys(blockers))
        raise FlowError(
            "staged harness is BUILD_REQUIRED and non_runnable:\n- "
            + "\n- ".join(unique)
        )


def _verified_source_bundle_digest(
    bundle: dict[str, Any],
    generated: dict[str, Any],
) -> str:
    if not isinstance(bundle, dict):
        raise FlowError("workflow source bundle output is missing")
    verify_source_bundle(bundle)
    proof = bundle.get("source_bundle_proof")
    digest = str(proof.get("source_bundle_digest") or "") if isinstance(proof, dict) else ""
    if not digest or generated.get("source_bundle_digest") != digest:
        raise FlowError("staged source_bundle_digest does not match the workflow source bundle")
    harness = Path(str(generated.get("harness_dir") or "")).resolve()
    expected_path = harness / "compiled" / "source-bundle.json"
    declared_path = Path(str(generated.get("source_bundle_path") or "")).resolve()
    if declared_path != expected_path or not declared_path.is_file():
        raise FlowError("staged harness is missing its canonical source-bundle.json")
    _assert_declared_file_digest(
        generated,
        "source_bundle_path",
        "source_bundle_sha256",
        label="staged source bundle",
    )
    if declared_path.read_bytes() != serialize_source_bundle(bundle):
        raise FlowError("staged source-bundle.json differs from the chosen workflow source bundle")
    return digest


def _validate(input_data: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    bundle = _input_object(input_data, "workflow_source_bundle")
    generated = _input_object(input_data, "staged_harness", "generated")
    if not isinstance(bundle, dict) or not isinstance(generated, dict):
        raise FlowError("harness_validated needs the chosen source bundle and staged harness")
    harness = Path(str(generated.get("harness_dir") or "")).resolve()
    flow = load_flow(harness)
    if flow.get("schema") != FLOW_SCHEMA:
        raise FlowError(f"staged harness must be {FLOW_SCHEMA}")
    required = [
        harness / "flow.yaml",
        harness / "planning" / "m8m-flowchart.md",
        harness / "planning" / "m8m-flowchart.jpg",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FlowError(f"staged harness is missing portable artifacts: {missing}")
    for path_key, digest_key, label in (
        ("instruction_path", "instruction_sha256", "staged instruction"),
        ("flowchart_path", "flowchart_sha256", "staged flowchart"),
        (
            "tool_vs_intelligence_path",
            "tool_vs_intelligence_sha256",
            "staged tool/intelligence table",
        ),
    ):
        _assert_declared_file_digest(generated, path_key, digest_key, label=label)
    runtime_release = _verify_staged_runtime_release(generated)
    source_bundle_digest = _verified_source_bundle_digest(bundle, generated)
    _, _, staged_members_digest = _assert_staged_install_manifest(generated)
    _validate_staged_tools(generated, harness, flow)
    full_validation = validate_harness(harness, update_instruction=False)
    if full_validation.get("status") != "PASS":
        raise FlowError("staged harness validation did not return PASS")
    instruction = Path(str(full_validation.get("instruction_path") or "")).resolve()
    if not instruction.is_file():
        raise FlowError("staged harness validation did not retain its instruction file")
    full_validation["instruction_sha256"] = sha256_file(instruction)
    package_path = (
        run_dir
        / "work"
        / "builder"
        / "packages"
        / f"{flow['flow_id']}.m8mpkg"
    )
    try:
        workflow_package = write_workflow_package(
            package_path,
            bundle,
            resource_root=harness,
        )
    except WorkflowPackageError as exc:
        raise FlowError(f"cannot package validated workflow source bundle: {exc}") from exc
    validation_report = {
        "status": "PASS",
        "ok": True,
        "runnable": True,
        "non_runnable": False,
        "flow_id": flow["flow_id"],
        "flow_schema": flow["schema"],
        "harness_dir": str(harness),
        "source_bundle_digest": source_bundle_digest,
        "staged_members_digest": staged_members_digest,
        "runtime_release": runtime_release,
        "workflow_package_path": workflow_package["path"],
        "workflow_package_sha256": workflow_package["digest"].removeprefix("sha256:"),
        "workflow_package_byte_count": workflow_package["byte_count"],
        "workflow_package_media_type": workflow_package["media_type"],
        "full_validation": full_validation,
        "validated_at": utc_now(),
    }
    return {
        "outputs": {
            "validation_report": validation_report,
            "workflow_package": {
                "path": workflow_package["path"],
                "sha256": workflow_package["digest"].removeprefix("sha256:"),
            },
        }
    }


def _safe_destination(base: Path, value: str, *, prefix: str) -> Path:
    expected = f"{prefix}/"
    if not value.startswith(expected):
        raise FlowError(f"invalid install destination namespace: {value}")
    relative = value[len(expected) :]
    if "\\" in relative:
        raise FlowError(f"install destination must use '/' separators: {value}")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise FlowError(f"unsafe install destination: {value}")
    base = base.resolve()
    destination = base.joinpath(*pure.parts).resolve()
    if destination == base or base not in destination.parents:
        raise FlowError(f"install destination escapes {prefix}: {value}")
    return destination


def _install_destination(
    *,
    codebase: Path,
    target: Path,
    value: str,
) -> tuple[Path, Path, str]:
    """Resolve one declared file and its independently swappable managed root."""
    if value.startswith("codebase/"):
        base = codebase
        prefix = "codebase"
    elif value.startswith("target/"):
        base = target
        prefix = "target"
    else:
        raise FlowError(f"unknown install destination: {value}")
    destination = _safe_destination(base, value, prefix=prefix)
    relative = PurePosixPath(value.removeprefix(f"{prefix}/"))
    parts = relative.parts
    root_parts: tuple[str, ...]
    if prefix == "target" and parts[:1] == ("planning",):
        root_parts = parts[:1]
    elif prefix == "target" and parts[:1] == ("scripts",):
        # The skill receives only a pointer to the launcher owned by the
        # codebase. Runtime releases remain in the codebase flow harness.
        root_parts = parts[:1]
    elif prefix == "codebase" and parts[:1] in {
        ("contracts",),
        ("scripts",),
        ("templates",),
    }:
        # Builder-owned shared source is promoted as one complete directory
        # snapshot.  The prepared tree starts as a copy of the live directory,
        # so unrelated files are preserved while the declared members change
        # atomically instead of appearing one file at a time.
        root_parts = parts[:1]
    elif len(parts) >= 4 and parts[:2] == ("flowsteps", "flows"):
        root_parts = parts[:3]
    elif len(parts) >= 4 and parts[:2] == ("flowsteps", "tools"):
        root_parts = parts[:3]
    elif len(parts) >= 3 and parts[:2] == ("flowsteps", "lib"):
        # Shared project implementation dependencies are overlaid into one
        # preserved, atomically swappable library root.
        root_parts = parts[:2]
    elif len(parts) >= 4 and parts[:2] == (".agents", "skills"):
        root_parts = parts[:3]
    elif len(parts) >= 4 and parts[:2] == (".claude", "skills"):
        root_parts = parts[:3]
    else:
        raise FlowError(
            f"install destination has no atomic managed root: {value}"
        )
    root = base.resolve().joinpath(*root_parts).resolve()
    if destination == root or root not in destination.parents:
        raise FlowError(f"install destination escapes its managed root: {value}")
    return destination, root, destination.relative_to(root).as_posix()


def _install_member_requires_runtime_isolation(
    destination: Path,
    *,
    codebase: Path,
    target: Path,
) -> bool:
    """Identify declared install members that can participate in execution.

    Install preparation is a final defense against a staged executable member
    acquiring a mutable Builder reference after validation.  It intentionally
    does not scan planning, compiled evidence, or immutable runtime releases:
    those are either presentation artifacts or verified by their own release
    manifest.  Scanning those trees would incorrectly make authored prose part
    of the product runtime dependency policy.
    """

    destination = destination.resolve()
    codebase = codebase.resolve()
    target = target.resolve()
    try:
        relative = destination.relative_to(codebase)
    except ValueError:
        try:
            relative = destination.relative_to(target)
        except ValueError:
            return False
        return relative.parts[:1] == ("scripts",)

    parts = relative.parts
    if parts[:2] in {("flowsteps", "tools"), ("flowsteps", "lib")}:
        return True
    if parts[:2] == ("flowsteps", "flows") and len(parts) >= 4:
        flow_relative = parts[3:]
        # agents/openai.yaml, SKILL.md, and references are compiler inputs or
        # authored evidence.  The runtime consumes the canonical flow and its
        # explicitly bound resources instead, so explanatory text (including
        # a prohibition against a retired Builder path) must not be conflated
        # with an executable dependency during installation.
        if flow_relative == ("SKILL.md",) or flow_relative[:1] in {
            ("agents",),
            ("references",),
        }:
            return False
        return flow_relative[:1] not in {("planning",), ("compiled",), ("runtime",)}
    if parts[:2] in {(".agents", "skills"), (".claude", "skills")}:
        return len(parts) >= 4 and parts[3:4] == ("scripts",)
    return False


def _install_tree_digest(root: Path) -> str | None:
    """Hash one complete managed tree and reject link-based ambiguity."""
    if not root.exists():
        return None
    if _is_unsafe_link(root) or not root.is_dir():
        raise FlowError(f"local install managed root is not a regular directory: {root}")
    members: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if _is_unsafe_link(path):
            raise FlowError(f"local install managed root contains a symlink: {path}")
        if not path.is_file():
            continue
        members.append(
            {
                "path": path.relative_to(root).as_posix(),
                "byte_count": path.stat().st_size,
                "digest": f"sha256:{sha256_file(path)}",
            }
        )
    return digest_json(members)


def _remove_install_tree(path: Path) -> None:
    if _is_unsafe_link(path) or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _verify_installed_files(destinations: list[dict[str, str]]) -> list[str]:
    installed: list[str] = []
    for item in destinations:
        destination = Path(item["path"])
        expected = str(item["digest"])
        if (
            _is_unsafe_link(destination)
            or not destination.is_file()
            or f"sha256:{sha256_file(destination)}" != expected
        ):
            raise FlowError(f"installed member failed read-back verification: {destination}")
        installed.append(str(destination))
    return sorted(set(installed))


def _install_transaction_dir(run_dir: Path, install_key: str) -> Path:
    return (
        run_dir.resolve()
        / "milestones"
        / "skill_shipped"
        / "work"
        / "install-transaction"
        / install_key
    )


def _cleanup_committed_install(journal: dict[str, Any]) -> None:
    for row in journal.get("roots") or []:
        for key in ("prepared", "backup"):
            raw = str(row.get(key) or "")
            if not raw:
                continue
            try:
                _remove_install_tree(Path(raw))
            except OSError:
                # A committed installation remains authoritative. Cleanup is
                # retried by the next idempotent invocation.
                pass


def _validate_install_journal(
    journal: dict[str, Any],
    *,
    install_key: str,
    plan_digest: str,
    roots: list[dict[str, Any]],
) -> None:
    if journal.get("schema") != "m8m_local_skill_install_transaction_v1":
        raise FlowError("local installation journal schema is invalid")
    if journal.get("install_key") != install_key or journal.get("plan_digest") != plan_digest:
        raise FlowError("local installation journal does not match the frozen install plan")
    actual = journal.get("roots")
    if not isinstance(actual, list) or len(actual) != len(roots):
        raise FlowError("local installation journal managed roots are invalid")
    static_fields = ("destination", "prepared", "backup", "members")
    for persisted, expected in zip(actual, roots, strict=True):
        if not isinstance(persisted, dict) or any(
            persisted.get(field) != expected.get(field) for field in static_fields
        ):
            raise FlowError("local installation journal paths do not match the frozen install plan")


def _commit_prepared_install(
    journal_path: Path,
    journal: dict[str, Any],
) -> dict[str, Any]:
    status = str(journal.get("status") or "")
    if status == "COMMITTED":
        _verify_installed_files(list(journal.get("destinations") or []))
        _cleanup_committed_install(journal)
        return journal
    if status != "PREPARED":
        raise FlowError(f"local installation journal has invalid status {status or '(empty)'}")

    try:
        for row in journal.get("roots") or []:
            if not row.get("promote"):
                continue
            destination = Path(row["destination"])
            prepared = Path(row["prepared"])
            backup = Path(row["backup"])
            expected = str(row.get("prepared_tree_digest") or "")
            old_digest = row.get("old_tree_digest")
            current = _install_tree_digest(destination)
            if current == expected:
                continue
            if _install_tree_digest(prepared) != expected:
                raise FlowError(f"prepared local install tree is incomplete: {prepared}")
            if current is not None:
                if current != old_digest:
                    raise FlowError(
                        f"local install destination changed during prepared transaction: {destination}"
                    )
                if backup.exists():
                    raise FlowError(f"local install backup conflicts with live old tree: {backup}")
                os.replace(destination, backup)
            elif old_digest is not None:
                if _install_tree_digest(backup) != old_digest:
                    raise FlowError(f"local install old tree is not recoverable: {destination}")
            os.replace(prepared, destination)
            if _install_tree_digest(destination) != expected:
                raise FlowError(f"installed managed root failed read-back verification: {destination}")
        installed = _verify_installed_files(list(journal.get("destinations") or []))
    except (OSError, FlowError) as exc:
        rollback_errors: list[str] = []
        for row in reversed(journal.get("roots") or []):
            if not row.get("promote"):
                continue
            destination = Path(row["destination"])
            prepared = Path(row["prepared"])
            backup = Path(row["backup"])
            expected = str(row.get("prepared_tree_digest") or "")
            old_digest = row.get("old_tree_digest")
            try:
                if backup.exists():
                    if _install_tree_digest(destination) == expected:
                        if prepared.exists():
                            _remove_install_tree(prepared)
                        os.replace(destination, prepared)
                    elif destination.exists():
                        _remove_install_tree(destination)
                    os.replace(backup, destination)
                elif old_digest is None and _install_tree_digest(destination) == expected:
                    if prepared.exists():
                        _remove_install_tree(prepared)
                    os.replace(destination, prepared)
            except (OSError, FlowError) as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if not rollback_errors:
            for row in journal.get("roots") or []:
                for key in ("prepared", "backup"):
                    _remove_install_tree(Path(row[key]))
            journal_path.unlink(missing_ok=True)
            raise FlowError(f"local installation failed and was rolled back: {exc}") from exc
        raise FlowError(
            "local installation failed; PREPARED journal retained for deterministic recovery: "
            f"{exc}; rollback errors: {rollback_errors}"
        ) from exc

    committed = dict(journal)
    committed["status"] = "COMMITTED"
    committed["installed_files"] = installed
    committed["committed_at"] = str(journal.get("committed_at") or utc_now())
    write_json(journal_path, committed, overwrite=True)
    _cleanup_committed_install(committed)
    return committed


def _install_declared_members(
    *,
    run_dir: Path,
    stage: Path,
    members: list[dict[str, Any]],
    codebase: Path,
    target: Path,
    overwrite: bool,
) -> dict[str, Any]:
    complete_roots = {
        (target.resolve() / "scripts").resolve(),
    }
    release_preserving_roots: set[Path] = set()
    pairs: list[dict[str, Any]] = []
    seen_destinations: dict[Path, str] = {}
    for member in members:
        source_rel = str(member.get("source") or "")
        source = _safe_destination(stage, f"stage/{source_rel}", prefix="stage")
        expected_digest = str(member.get("digest") or "")
        if not source.is_file() or f"sha256:{sha256_file(source)}" != expected_digest:
            raise FlowError(f"staged member no longer matches its manifest: {source_rel}")
        for raw_destination in member.get("destinations") or []:
            raw_destination = str(raw_destination)
            destination, managed_root, managed_relative = _install_destination(
                codebase=codebase,
                target=target,
                value=raw_destination,
            )
            if _install_member_requires_runtime_isolation(
                destination,
                codebase=codebase,
                target=target,
            ):
                _assert_python_path_runtime_isolated(
                    source,
                    exclude_runtime_releases=False,
                )
            previous = seen_destinations.get(destination)
            if previous is not None and previous != expected_digest:
                raise FlowError(f"conflicting staged members target {destination}")
            if previous is None:
                pairs.append(
                    {
                        "source": source,
                        "destination": destination,
                        "digest": expected_digest,
                        "managed_root": managed_root,
                        "managed_relative": managed_relative,
                    }
                )
            seen_destinations[destination] = expected_digest

    pairs.sort(key=lambda item: str(item["destination"]).casefold())
    destinations = [
        {"path": str(item["destination"]), "digest": str(item["digest"])}
        for item in pairs
    ]
    plan_digest = digest_json(destinations)
    install_key = plan_digest.split(":", 1)[-1]
    transaction_dir = _install_transaction_dir(run_dir, install_key)
    journal_path = transaction_dir / "install-journal.json"

    groups: dict[Path, list[dict[str, Any]]] = {}
    for item in pairs:
        groups.setdefault(Path(item["managed_root"]), []).append(item)
    managed_roots = sorted(groups, key=lambda item: str(item).casefold())
    codebase_root = codebase.resolve()
    for root in managed_roots:
        try:
            codebase_relative = root.relative_to(codebase_root)
        except ValueError:
            continue
        if (
            len(codebase_relative.parts) >= 3
            and codebase_relative.parts[:2]
            in {(".agents", "skills"), (".claude", "skills")}
        ):
            complete_roots.add(root)
        if (
            len(codebase_relative.parts) == 3
            and codebase_relative.parts[:2] == ("flowsteps", "flows")
        ):
            complete_roots.add(root)
            release_preserving_roots.add(root)
    for index, root in enumerate(managed_roots):
        for other in managed_roots[index + 1 :]:
            if root in other.parents or other in root.parents:
                raise FlowError(f"local install managed roots overlap: {root} and {other}")

    root_rows: list[dict[str, Any]] = []
    for index, root in enumerate(managed_roots):
        members_for_root = sorted(
            [
                {
                    "path": str(item["managed_relative"]),
                    "digest": str(item["digest"]),
                }
                for item in groups[root]
            ],
            key=lambda item: item["path"],
        )
        root_rows.append(
            {
                "destination": str(root),
                "prepared": str(
                    root.with_name(f".{root.name}.m8m-{install_key[:16]}-{index:06d}.prepared")
                ),
                "backup": str(
                    root.with_name(f".{root.name}.m8m-{install_key[:16]}-{index:06d}.backup")
                ),
                "members": members_for_root,
            }
        )

    if journal_path.is_file():
        journal = read_json(journal_path)
        _validate_install_journal(
            journal,
            install_key=install_key,
            plan_digest=plan_digest,
            roots=root_rows,
        )
        return _commit_prepared_install(journal_path, journal)

    changes_by_root: set[Path] = set()
    for item in pairs:
        destination = Path(item["destination"])
        expected_digest = str(item["digest"])
        if destination.exists():
            if _is_unsafe_link(destination) or not destination.is_file():
                raise FlowError(f"local install destination is not a file: {destination}")
            if destination.name in {"launch.py", "m8m_run.py"}:
                try:
                    existing_launcher = destination.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise FlowError(f"cannot verify existing codebase launcher: {exc}") from exc
                if (
                    (
                        'M8M_CODEBASE_DISPATCH_ABI = "m8m_codebase_dispatch_v2"'
                        in existing_launcher
                        or 'M8M_SKILL_POINTER_ABI = "m8m_skill_pointer_v2"'
                        in existing_launcher
                    )
                    and f"sha256:{sha256_file(destination)}" != expected_digest
                ):
                    raise FlowError(
                        "the installed M8M v2 bootstrap ABI is immutable; a future pointer "
                        "or dispatcher must install side by side instead of rewriting it"
                    )
            if f"sha256:{sha256_file(destination)}" == expected_digest:
                continue
            if not overwrite:
                raise FlowError(
                    f"local install conflict at {destination}; rerun with explicit overwrite "
                    "or choose a fresh destination"
                )
        changes_by_root.add(Path(item["managed_root"]))

    for root in complete_roots & set(managed_roots):
        if not root.is_dir():
            continue
        declared = {
            str(item["managed_relative"]): str(item["digest"])
            for item in groups[root]
        }
        actual = {
            path.relative_to(root).as_posix(): f"sha256:{sha256_file(path)}"
            for path in root.rglob("*")
            if path.is_file() and not _is_unsafe_link(path)
        }
        if root in release_preserving_roots:
            actual = {
                path: digest
                for path, digest in actual.items()
                if not (
                    path.startswith("runtime/releases/") and path not in declared
                )
            }
        if actual != declared:
            if not overwrite:
                raise FlowError(
                    f"managed product skill root contains stale or undeclared files: {root}; "
                    "rerun with explicit overwrite to replace the complete generated root"
                )
            changes_by_root.add(root)

    for row in root_rows:
        root = Path(row["destination"])
        prepared = Path(row["prepared"])
        backup = Path(row["backup"])
        if backup.exists():
            raise FlowError(f"orphaned local install backup requires operator recovery: {backup}")
        if prepared.exists():
            _remove_install_tree(prepared)
        root.parent.mkdir(parents=True, exist_ok=True)
        old_digest = _install_tree_digest(root)
        promote = root in changes_by_root
        row["promote"] = promote
        row["old_tree_digest"] = old_digest
        if not promote:
            row["prepared_tree_digest"] = old_digest
            continue
        if root.is_dir() and root not in complete_roots:
            shutil.copytree(root, prepared)
        else:
            prepared.mkdir(parents=True)
            if root in release_preserving_roots:
                old_releases = root / "runtime" / "releases"
                if old_releases.is_dir():
                    shutil.copytree(
                        old_releases,
                        prepared / "runtime" / "releases",
                        dirs_exist_ok=True,
                    )
                replacing_release_ids = {
                    PurePosixPath(str(item["managed_relative"])).parts[2]
                    for item in groups[root]
                    if len(PurePosixPath(str(item["managed_relative"])).parts) >= 4
                    and PurePosixPath(str(item["managed_relative"])).parts[:2]
                    == ("runtime", "releases")
                }
                for release_id in replacing_release_ids:
                    _remove_install_tree(
                        prepared / "runtime" / "releases" / release_id
                    )
        for item in groups[root]:
            destination = prepared.joinpath(*PurePosixPath(str(item["managed_relative"])).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(item["source"]), destination)
            if f"sha256:{sha256_file(destination)}" != item["digest"]:
                raise FlowError(
                    f"prepared local install member failed digest verification: {item['source']}"
                )
        # Dependency isolation is audited against the exact declared product
        # execution closure before staging validation and again after install.
        # This transaction boundary owns only atomic copy, link safety, and
        # digest verification; scanning the complete prepared tree would make
        # unrelated planning prose part of the runtime policy.
        row["prepared_tree_digest"] = _install_tree_digest(prepared)

    transaction_dir.mkdir(parents=True, exist_ok=True)
    journal = {
        "schema": "m8m_local_skill_install_transaction_v1",
        "status": "PREPARED",
        "run_id": run_dir.name,
        "install_key": install_key,
        "plan_digest": plan_digest,
        "destinations": destinations,
        "roots": root_rows,
        "prepared_at": utc_now(),
    }
    write_json(journal_path, journal, overwrite=False)
    return _commit_prepared_install(journal_path, journal)


def _ship(input_data: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    request = _request(input_data)
    bundle = _input_object(input_data, "workflow_source_bundle")
    generated = _input_object(input_data, "staged_harness", "generated")
    validation = _input_object(input_data, "validation_report", "validation")
    workflow_package = _input_object(input_data, "workflow_package")
    validation_passed = (
        isinstance(validation, dict)
        and validation.get("status") == "PASS"
        and validation.get("ok") is True
        and validation.get("runnable") is True
        and validation.get("non_runnable") is False
        and isinstance(validation.get("full_validation"), dict)
        and validation["full_validation"].get("status") == "PASS"
    )
    if (
        not isinstance(bundle, dict)
        or not isinstance(generated, dict)
        or not isinstance(workflow_package, dict)
        or not validation_passed
    ):
        raise FlowError(
            "skill_shipped needs the chosen source bundle, staged harness, "
            "validation, and workflow package"
        )
    source_bundle_digest = _verified_source_bundle_digest(bundle, generated)
    runtime_release = _verify_staged_runtime_release(generated)
    stage, staged_members, staged_members_digest = _assert_staged_install_manifest(generated)
    if validation.get("source_bundle_digest") != source_bundle_digest:
        raise FlowError("skill_shipped validation is not bound to the generated source bundle")
    if validation.get("staged_members_digest") != staged_members_digest:
        raise FlowError("skill_shipped validation is not bound to the exact staged install members")
    if validation.get("runtime_release") != runtime_release:
        raise FlowError("skill_shipped validation is not bound to the exact runtime release")
    package_path = Path(str(workflow_package.get("path") or "")).resolve()
    if not package_path.is_file():
        raise FlowError("skill_shipped chosen workflow package is missing")
    package_sha256 = sha256_file(package_path)
    if validation.get("workflow_package_sha256") != package_sha256:
        raise FlowError("skill_shipped workflow package differs from validated bytes")
    if validation.get("workflow_package_byte_count") != package_path.stat().st_size:
        raise FlowError("skill_shipped workflow package byte count differs from validation")
    full_validation = validation["full_validation"]
    _assert_declared_file_digest(
        full_validation,
        "instruction_path",
        "instruction_sha256",
        label="chosen harness validation",
    )
    harness = Path(str(generated.get("harness_dir") or "")).resolve()
    flow = load_flow(harness)
    _validate_staged_tools(generated, harness, flow)
    current_validation = validate_harness(harness, update_instruction=False)
    if current_validation.get("status") != "PASS":
        raise FlowError("skill_shipped refuses a harness without a current full PASS validation")
    codebase = _path(request, "codebase")
    target = _path(request, "target")
    flow_id = str(generated.get("flow_id") or validation.get("flow_id") or "")
    skill_name = str(generated.get("skill_name") or request.get("skill_name") or target.name)
    overwrite = bool(request.get("overwrite"))
    installation = _install_declared_members(
        run_dir=run_dir,
        stage=stage,
        members=staged_members,
        codebase=codebase,
        target=target,
        overwrite=overwrite,
    )
    copied = list(installation.get("installed_files") or [])
    product_skill = codebase / ".agents" / "skills" / skill_name / "SKILL.md"
    flowchart = codebase / "flowsteps" / "flows" / flow_id / "planning" / "m8m-flowchart.md"
    if not flowchart.is_file():
        raise FlowError("local installation is missing the declared flowchart")
    installed_harness = codebase / "flowsteps" / "flows" / flow_id
    installed_runtime = dict(runtime_release)
    runtime_id = str(runtime_release.get("runtime_id") or "")
    installed_paths = {
        "manifest_path": (
            installed_harness
            / "runtime"
            / "releases"
            / runtime_id
            / "runtime-manifest.json"
        ),
        "codebase_launcher_path": installed_harness / "launch.py",
        "archive_path": (
            installed_harness
            / "compiled"
            / "runtime"
            / f"m8m-runtime-{runtime_id}.zip"
        ),
        "harness_lock_path": installed_harness / "m8m-runtime-lock.json",
    }
    for path_key, installed_path in installed_paths.items():
        digest_key = path_key.replace("_path", "_sha256")
        if not installed_path.is_file() or sha256_file(installed_path) != runtime_release.get(
            digest_key
        ):
            raise FlowError(f"installed runtime release failed verification: {installed_path}")
        installed_runtime[path_key] = str(installed_path)
    try:
        verify_runtime_release(Path(installed_runtime["manifest_path"]))
    except RuntimeReleaseError as exc:
        raise FlowError(f"installed codebase runtime release is invalid: {exc}") from exc
    _assert_product_runtime_isolated(
        {
            "harness_dir": str(installed_harness),
            "stage_codebase": str(codebase),
            "skill_name": skill_name,
            "runtime_audit_roots": [str(target / "scripts")],
        }
    )
    return _candidate(
        {
            "status": "PASS",
            "runnable": True,
            "non_runnable": False,
            "flow_id": flow_id,
            "skill_name": skill_name,
            "source_bundle_digest": source_bundle_digest,
            "staged_members_digest": staged_members_digest,
            "runtime_release": installed_runtime,
            "workflow_package_path": str(package_path),
            "workflow_package_sha256": package_sha256,
            "workflow_package_byte_count": package_path.stat().st_size,
            "workflow_package_media_type": str(
                validation.get("workflow_package_media_type") or ""
            ),
            "product_skill": str(product_skill),
            "flowchart_path": str(flowchart),
            "flowchart_sha256": sha256_file(flowchart),
            "flowchart_jpg": str(flowchart.with_suffix(".jpg")),
            "installed_files": copied,
            "source_bundle_path": str(generated["source_bundle_path"]),
            "source_bundle_sha256": str(generated["source_bundle_sha256"]),
            "source_bundle_resource_root": str(harness),
            "installed_at": str(installation.get("committed_at") or utc_now()),
        }
    )


def run(
    input_data: dict[str, Any],
    draft: dict[str, Any] | None = None,
    *,
    task: dict[str, Any] | None = None,
    run_dir: Path | str | None = None,
    **_: Any,
) -> dict[str, Any]:
    del draft
    if run_dir is None:
        raise FlowError("builder milestone needs run_dir")
    step_id = str((task or {}).get("step_id") or "")
    handlers = {
        "audit_complete": _audit,
        "toolbox_ready": _toolbox,
        "flow_generated": _generate,
        "harness_validated": _validate,
        "skill_shipped": _ship,
    }
    handler = handlers.get(step_id)
    if handler is None:
        raise FlowError(f"unknown builder milestone: {step_id}")
    result = handler(input_data, Path(run_dir))
    current = result.get("outputs") if isinstance(result, dict) else None
    if not isinstance(current, dict):
        raise FlowError(f"{step_id}: builder handler returned an invalid candidate")
    declared = {
        str(item.get("id") or "")
        for item in (task or {}).get("outputs") or []
        if isinstance(item, dict)
    }
    if not declared:
        declared = set(BUILDER_OUTPUT_PORTS.get(step_id, ()))
    if declared and set(current) == declared:
        return result
    if "result" not in current:
        raise FlowError(f"{step_id}: builder handler returned undeclared output ports")
    return _candidate(current["result"], _declared_output_id(task, step_id))
