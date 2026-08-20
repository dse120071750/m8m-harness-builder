"""Cycle receipt worker. AI drafts pass|fail; this tool writes the receipt.

It does not treat remaining==0 as the gate. The driver updates the run ledger.
"""

from __future__ import annotations

from typing import Any


def _recommended(input_data: dict[str, Any]) -> str:
    draft = input_data.get("draft") if isinstance(input_data.get("draft"), dict) else {}
    for source in (input_data, draft):
        for key in ("recommended_cycle", "cycle"):
            value = source.get(key)
            if isinstance(value, str) and value.strip() in {"pass", "fail"}:
                return value.strip()
    return ""


def run(input_data: dict[str, Any], params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    del params
    payload = dict(input_data or {})
    chosen = _recommended(payload)
    row = str(payload.get("row") or payload.get("ledger_row") or "")
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
    if chosen == "pass":
        return str(payload.get("pass") or "round passed; preserve and update ledger")
    return "round failed; purge residue; row stays unfinished"
