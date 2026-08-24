"""Convenience API for the builder's canonical self-hosted M8M workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flowstep_runtime import FlowError, write_json
from run_flow import advance
from session_layout import chosen_output_path, default_run_dir, load_chosen_output, resolve_chosen_output


BUILDER_ROOT = Path(__file__).resolve().parents[1]
BUILDER_FLOW = "m8m_build_v1.yaml"
BUILDER_MILESTONES = (
    "audit_complete",
    "toolbox_ready",
    "flow_generated",
    "harness_validated",
    "skill_shipped",
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
) -> dict[str, Any]:
    target = target.resolve()
    codebase = codebase.resolve()
    session = (run_dir or default_run_dir(codebase, "m8m_build_v1")).resolve()
    request_path = session / "request.json"
    if not request_path.is_file():
        write_json(
            request_path,
            {
                "target": str(target),
                "codebase": str(codebase),
                "flow_id": flow_id,
                "skill_name": skill_name,
                "overwrite": overwrite,
            },
            overwrite=False,
        )
    action = advance(
        BUILDER_ROOT,
        session,
        flow_arg=BUILDER_FLOW,
        replace_milestone=replace_milestone,
        continue_after_edit=continue_after_edit,
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
        return {
            "schema": "m8m_factory_result_v2",
            "status": "FINDINGS" if action.get("state") == "BLOCKED" else "ACTION_REQUIRED",
            "run_dir": str(session),
            "milestones": milestones,
            "action": action,
        }
    final = resolve_chosen_output(session, "skill_shipped", output_id="result")
    if not isinstance(final, dict):
        raise FlowError("skill_shipped chosen result is invalid")
    audit_manifest = load_chosen_output(session, "audit_complete")
    generated = resolve_chosen_output(session, "flow_generated", output_id="result")
    audit_member = next(iter(audit_manifest.get("members") or []), {})
    audit_path = session / str(audit_member.get("path") or "")
    return {
        "schema": "m8m_factory_result_v2",
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
        "notes": list(generated.get("notes") or []) if isinstance(generated, dict) else [],
    }
