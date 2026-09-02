"""Convenience API for the builder's canonical self-hosted M8M workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flowstep_runtime import FlowError, read_json, write_json
from run_flow import advance
from session_layout import (
    chosen_output_path,
    default_harness_root,
    default_run_dir,
    load_chosen_output,
    resolve_chosen_output,
    validate_fresh_harness_root,
    validate_fresh_run_dir,
)


BUILDER_ROOT = Path(__file__).resolve().parents[1]
BUILDER_FLOW = "m8m_build_v2.yaml"
BUILDER_FLOW_ID = "m8m_build_v2"
BUILDER_MILESTONES = (
    "audit_complete",
    "toolbox_ready",
    "flow_generated",
    "harness_validated",
    "skill_shipped",
)
BUILDER_OUTPUTS = {
    "audit_complete": "source_audit",
    "toolbox_ready": "toolbox_manifest",
    "flow_generated": "staged_harness",
    "harness_validated": "validation_report",
    "skill_shipped": "installation_receipt",
}
BUILDER_WORKFLOW_SOURCE_BUNDLE_OUTPUT = "workflow_source_bundle"


def _request_identity(
    *,
    target: Path,
    codebase: Path,
    flow_id: str | None,
    skill_name: str | None,
    overwrite: bool,
) -> dict[str, Any]:
    return {
        "target": str(target.resolve()),
        "codebase": str(codebase.resolve()),
        "flow_id": flow_id,
        "skill_name": skill_name,
        "overwrite": bool(overwrite),
    }


def _assert_frozen_request(path: Path, requested: dict[str, Any]) -> None:
    if not path.is_file():
        raise FlowError("initialized builder run is missing its frozen request.json")
    frozen = read_json(path)
    if not isinstance(frozen, dict):
        raise FlowError("initialized builder request.json is not an object")
    normalized = {
        "target": str(Path(str(frozen.get("target") or "")).resolve()),
        "codebase": str(Path(str(frozen.get("codebase") or "")).resolve()),
        "flow_id": frozen.get("flow_id"),
        "skill_name": frozen.get("skill_name"),
        "overwrite": bool(frozen.get("overwrite")),
    }
    if normalized != requested:
        raise FlowError(
            "resume arguments do not match this builder run's frozen request; "
            "resume the exact target/codebase/options or start a fresh session"
        )


def run_factory(
    target: Path,
    codebase: Path,
    *,
    flow_id: str | None = None,
    skill_name: str | None = None,
    overwrite: bool = False,
    run_dir: Path | None = None,
    replace_milestone: str | None = None,
    continue_after_edit: str | None = None,
    harness_root: Path | None = None,
) -> dict[str, Any]:
    target = target.resolve()
    codebase = codebase.resolve()
    requested_root = (harness_root or default_harness_root()).resolve()
    if run_dir is None:
        execution_root = validate_fresh_harness_root(requested_root)
        session = default_run_dir(execution_root, BUILDER_FLOW_ID).resolve()
    else:
        session = run_dir.resolve()
        context_path = session / "run-context.json"
        initialized = context_path.is_file() and (session / "flow-execution-record.json").is_file()
        if initialized:
            context = read_json(context_path)
            storage = context.get("storage_contract") if isinstance(context.get("storage_contract"), dict) else {}
            execution_root = Path(storage.get("execution_root") or requested_root).resolve()
        else:
            execution_root = validate_fresh_harness_root(requested_root)
    initialized = (session / "run-context.json").is_file() and (session / "flow-execution-record.json").is_file()
    if initialized:
        record = read_json(session / "flow-execution-record.json")
        initialized_flow_id = str(record.get("flow_id") or "")
        if initialized_flow_id != BUILDER_FLOW_ID:
            raise FlowError(
                "Builder 3 cannot resume or execute a Builder 2 run; start a fresh "
                "m8m_build_v2 session and import the old staged source as untrusted input"
            )
    if not initialized:
        validate_fresh_run_dir(execution_root, session)
        if session.is_dir() and any(session.iterdir()):
            raise FlowError("fresh builder --run-dir must be empty; resume one exact initialized session")
    request_path = session / "request.json"
    requested = _request_identity(
        target=target,
        codebase=codebase,
        flow_id=flow_id,
        skill_name=skill_name,
        overwrite=overwrite,
    )
    if initialized:
        _assert_frozen_request(request_path, requested)
    if not request_path.is_file():
        write_json(
            request_path,
            requested,
            overwrite=False,
        )
    action = advance(
        BUILDER_ROOT,
        session,
        flow_arg=BUILDER_FLOW,
        replace_milestone=replace_milestone,
        continue_after_edit=continue_after_edit,
        harness_root=execution_root,
        source_code_root=codebase,
    )
    milestones: dict[str, Any] = {}
    for milestone_id in BUILDER_MILESTONES:
        path = chosen_output_path(session, milestone_id)
        if path.is_file():
            manifest = load_chosen_output(session, milestone_id)
            milestones[milestone_id] = {
                "status": "PASS",
                "chosen_output": str(path),
                "members": manifest.get("members") or [],
            }
        else:
            milestones[milestone_id] = {"status": "PENDING"}
    if action.get("state") != "COMPLETE":
        action_state = str(action.get("state") or "ACTION_REQUIRED")
        if action_state == "BLOCKED":
            blocked_id = str(action.get("step_id") or "")
            if blocked_id in milestones and milestones[blocked_id].get("status") == "PENDING":
                milestones[blocked_id] = {
                    "status": "BLOCKED",
                    "blockers": [str(item) for item in action.get("blockers") or []],
                }
        partial_handoff: dict[str, Any] = {}
        generated_path = chosen_output_path(session, "flow_generated")
        if generated_path.is_file():
            try:
                staged_harness = resolve_chosen_output(
                    session,
                    "flow_generated",
                    output_id=BUILDER_OUTPUTS["flow_generated"],
                )
                workflow_source_bundle = resolve_chosen_output(
                    session,
                    "flow_generated",
                    output_id=BUILDER_WORKFLOW_SOURCE_BUNDLE_OUTPUT,
                )
                proof = (
                    workflow_source_bundle.get("source_bundle_proof")
                    if isinstance(workflow_source_bundle, dict)
                    else None
                )
                digest = str(proof.get("source_bundle_digest") or "") if isinstance(proof, dict) else ""
                if isinstance(staged_harness, dict) and digest:
                    partial_handoff = {
                        "source_bundle_path": staged_harness.get("source_bundle_path"),
                        "source_bundle_resource_root": staged_harness.get("harness_dir"),
                        "source_bundle_digest": digest,
                        "runtime_release": staged_harness.get("runtime_release"),
                        "notes": list(staged_harness.get("notes") or []),
                    }
            except FlowError:
                # The terminal action remains authoritative. A malformed
                # optional handoff must not hide its exact BLOCKED evidence.
                partial_handoff = {}
        return {
            "schema": "m8m_factory_result_v3",
            "status": "BLOCKED" if action_state == "BLOCKED" else "ACTION_REQUIRED",
            "run_dir": str(session),
            "milestones": milestones,
            "action": action,
            **partial_handoff,
        }
    final = resolve_chosen_output(
        session,
        "skill_shipped",
        output_id=BUILDER_OUTPUTS["skill_shipped"],
    )
    if not isinstance(final, dict):
        raise FlowError("skill_shipped chosen result is invalid")
    audit_manifest = load_chosen_output(session, "audit_complete")
    staged_harness = resolve_chosen_output(
        session,
        "flow_generated",
        output_id=BUILDER_OUTPUTS["flow_generated"],
    )
    if not isinstance(staged_harness, dict):
        raise FlowError("flow_generated staged harness report is invalid")
    workflow_source_bundle = resolve_chosen_output(
        session,
        "flow_generated",
        output_id=BUILDER_WORKFLOW_SOURCE_BUNDLE_OUTPUT,
    )
    if not isinstance(workflow_source_bundle, dict):
        raise FlowError("flow_generated workflow source bundle is invalid")
    proof = workflow_source_bundle.get("source_bundle_proof")
    source_bundle_digest = (
        str(proof.get("source_bundle_digest") or "") if isinstance(proof, dict) else ""
    )
    if not source_bundle_digest:
        raise FlowError("flow_generated workflow source bundle has no proof digest")
    audit_member = next(iter(audit_manifest.get("members") or []), {})
    audit_path = session / str(audit_member.get("path") or "")
    return {
        "schema": "m8m_factory_result_v3",
        "status": "PASS",
        "run_dir": str(session),
        "flow_id": final.get("flow_id"),
        "skill_name": final.get("skill_name"),
        "target": str(target),
        "codebase": str(codebase),
        "milestones": milestones,
        "product_skill": final.get("product_skill"),
        "audit_json": str(audit_path),
        "flowchart_path": final.get("flowchart_path"),
        "flowchart_jpg": final.get("flowchart_jpg"),
        "source_bundle_path": final.get("source_bundle_path"),
        "source_bundle_resource_root": final.get("source_bundle_resource_root"),
        "source_bundle_digest": source_bundle_digest,
        "runtime_release": final.get("runtime_release"),
        "workflow_package_path": final.get("workflow_package_path"),
        "workflow_package_sha256": final.get("workflow_package_sha256"),
        "workflow_package_byte_count": final.get("workflow_package_byte_count"),
        "workflow_package_media_type": final.get("workflow_package_media_type"),
        "notes": list(staged_harness.get("notes") or []),
    }
