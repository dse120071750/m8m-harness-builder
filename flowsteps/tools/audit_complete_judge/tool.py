"""Deterministically accept only one closed Builder source-audit candidate."""

from __future__ import annotations

from typing import Any


def run(
    input_data: dict[str, Any],
    params: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    del params
    candidate = input_data.get("candidate")
    outputs = candidate.get("outputs") if isinstance(candidate, dict) else None
    audit = outputs.get("source_audit") if isinstance(outputs, dict) else None
    ok = bool(
        isinstance(audit, dict)
        and audit.get("schema") == "flowstep_skill_audit_v1"
        and audit.get("status") in {"PASS", "FINDINGS"}
        and bool(audit.get("verdict"))
        and isinstance(audit.get("builder_request"), dict)
        and isinstance(audit.get("proposed_milestones"), list)
        and bool(audit["proposed_milestones"])
    )
    return {
        "ok": ok,
        "code": "source_audit_closed" if ok else "source_audit_incomplete",
        "reasons": [
            "The selected source audit is closed and bound to the Builder request."
            if ok
            else "The source audit is not a closed candidate with verdict and milestone inventory."
        ],
    }
