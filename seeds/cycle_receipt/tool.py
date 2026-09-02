"""Choose cycle pass|fail from a runtime-owned post-admission request.

It does not treat remaining==0 as the gate. The driver updates the cycle ledger.
"""

from __future__ import annotations

from typing import Any


def _recommended(input_data: dict[str, Any]) -> str:
    inputs = input_data.get("inputs") if isinstance(input_data.get("inputs"), dict) else {}
    candidate = input_data.get("candidate") if isinstance(input_data.get("candidate"), dict) else {}
    outputs = candidate.get("outputs") if isinstance(candidate.get("outputs"), dict) else {}
    sources = [outputs, *[value for value in outputs.values() if isinstance(value, dict)], inputs]
    for source in sources:
        value = source.get("recommended_cycle")
        if isinstance(value, str) and value.strip() in {"pass", "fail"}:
            return value.strip()
        for key in ("accepted", "ready", "quality_passed"):
            value = source.get(key)
            if isinstance(value, bool):
                return "pass" if value else "fail"
    return ""


def run(input_data: dict[str, Any], params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    del params
    payload = dict(input_data or {})
    chosen = _recommended(payload) or "pass"
    control = payload.get("control") if isinstance(payload.get("control"), dict) else {}
    row = str(control.get("row") or "")
    if not chosen:
        return {
            "ok": False,
            "cycle": "",
            "row": row,
            "reason": "no cycle pass or fail decided",
        }
    reason = str(payload.get("reason") or draft_reason(payload, chosen))
    return {
        "ok": True,
        "cycle": chosen,
        "row": row,
        "reason": reason,
    }


def draft_reason(payload: dict[str, Any], chosen: str) -> str:
    control = payload.get("control") if isinstance(payload.get("control"), dict) else {}
    if chosen == "pass":
        return str(control.get("pass_rule") or "round passed; preserve and update ledger")
    return "round failed; purge residue; row stays unfinished"
