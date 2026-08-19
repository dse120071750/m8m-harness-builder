"""F8F factory: audit → toolbox → generate → validate → ship product skill."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from audit_harness import audit_skill, render_audit_markdown
from flowstep_runtime import FLOW_ID_RE, FlowError, utc_now, write_json
from generate_harness import generate_from_audit
from validate_harness import validate_harness


def _flow_id(audit: dict[str, Any], skill_name: str, explicit: str | None) -> str:
    raw = explicit or (audit.get("grade") or {}).get("flow_id") or f"{skill_name.replace('-', '_')}_v1"
    raw = str(raw).lower().replace("-", "_")
    return raw if FLOW_ID_RE.match(raw) else "product_v1"


def run_factory(
    target: Path,
    codebase: Path,
    *,
    flow_id: str | None = None,
    skill_name: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    target = target.resolve()
    codebase = codebase.resolve()
    audit = audit_skill(target)
    name = skill_name or str((audit.get("audited_skill") or {}).get("name") or target.name)
    fid = _flow_id(audit, name, flow_id)
    planning = codebase / "flowsteps" / "flows" / fid / "planning"
    planning.mkdir(parents=True, exist_ok=True)
    audit_json = planning / "flowstep-audit.json"
    audit_md = planning / "flowstep-audit.md"
    write_json(audit_json, audit)
    audit_md.write_text(render_audit_markdown(audit), encoding="utf-8", newline="\n")
    target_planning = target / "planning"
    target_planning.mkdir(parents=True, exist_ok=True)
    write_json(target_planning / "flowstep-audit.json", audit)
    (target_planning / "flowstep-audit.md").write_text(
        render_audit_markdown(audit), encoding="utf-8", newline="\n"
    )

    generated = generate_from_audit(
        codebase, audit, flow_id=fid, skill_name=name, overwrite=overwrite
    )
    validation: dict[str, Any]
    try:
        validation = validate_harness(codebase=codebase, flow_id=fid)
    except FlowError as exc:
        validation = {"status": "BLOCKED", "blockers": [str(exc)]}

    status = "PASS"
    if generated.get("status") != "PASS" or validation.get("status") != "PASS":
        status = "FINDINGS" if validation.get("status") != "BLOCKED" else "BLOCKED"

    return {
        "schema": "f8f_factory_result_v1",
        "status": status,
        "flow_id": fid,
        "skill_name": name,
        "codebase": str(codebase),
        "target": str(target),
        "created_at": utc_now(),
        "milestones": {
            "audit_complete": {
                "status": "PASS",
                "output": str(audit_json),
            },
            "toolbox_ready": {
                "status": generated.get("status"),
                "tools": generated.get("toolbox") or [],
            },
            "flow_generated": {
                "status": generated.get("status"),
                "harness_dir": generated.get("harness_dir"),
                "instruction_path": generated.get("instruction_path"),
            },
            "harness_validated": validation,
            "skill_shipped": {
                "status": "PASS" if generated.get("product_skill") else "BLOCKED",
                "skill_md": generated.get("product_skill"),
            },
        },
        "product_skill": generated.get("product_skill"),
        "audit_json": str(audit_json),
    }
