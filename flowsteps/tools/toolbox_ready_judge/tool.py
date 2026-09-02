"""Deterministically judge the Builder toolbox-state consistency."""

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
    manifest = outputs.get("toolbox_manifest") if isinstance(outputs, dict) else None
    build_required = manifest.get("build_required_tools") if isinstance(manifest, dict) else None
    unfinished = bool(build_required) if isinstance(build_required, list) else None
    consistent = bool(
        isinstance(manifest, dict)
        and isinstance(manifest.get("tools"), list)
        and isinstance(build_required, list)
        and (
            (
                unfinished is False
                and manifest.get("status") == "PASS"
                and manifest.get("runnable") is True
                and manifest.get("non_runnable") is False
            )
            or (
                unfinished is True
                and manifest.get("status") == "BUILD_REQUIRED"
                and manifest.get("runnable") is False
                and manifest.get("non_runnable") is True
            )
        )
    )
    return {
        "ok": consistent,
        "code": "toolbox_manifest_consistent" if consistent else "toolbox_manifest_inconsistent",
        "reasons": [
            "Every staged tool is consistently classified as runnable or BUILD_REQUIRED."
            if consistent
            else "The toolbox status, runnable flags, and BUILD_REQUIRED list disagree."
        ],
    }
