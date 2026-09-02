"""Deterministically accept only a fully runnable Builder validation report."""

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
    report = outputs.get("validation_report") if isinstance(outputs, dict) else None
    validation = report.get("full_validation") if isinstance(report, dict) else None
    ok = bool(
        isinstance(report, dict)
        and report.get("status") == "PASS"
        and report.get("ok") is True
        and report.get("runnable") is True
        and report.get("non_runnable") is False
        and isinstance(validation, dict)
        and validation.get("status") == "PASS"
        and bool(report.get("source_bundle_digest"))
        and bool(report.get("staged_members_digest"))
    )
    return {
        "ok": ok,
        "code": "harness_validation_passed" if ok else "harness_validation_failed",
        "reasons": [
            "The exact staged harness has a complete fail-closed PASS validation."
            if ok
            else "The staged harness is not proven runnable by the full validator."
        ],
    }
