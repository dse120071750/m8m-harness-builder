"""Deterministically bind the staged harness report to its source bundle."""

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
    bundle = outputs.get("workflow_source_bundle") if isinstance(outputs, dict) else None
    staged = outputs.get("staged_harness") if isinstance(outputs, dict) else None
    proof = bundle.get("source_bundle_proof") if isinstance(bundle, dict) else None
    digest = proof.get("source_bundle_digest") if isinstance(proof, dict) else None
    ok = bool(
        isinstance(staged, dict)
        and staged.get("schema") == "flowstep_harness_generate_v4"
        and staged.get("status") in {"PASS", "BUILD_REQUIRED"}
        and isinstance(digest, str)
        and digest
        and staged.get("source_bundle_digest") == digest
        and isinstance(staged.get("staged_members"), list)
        and bool(staged.get("staged_members_digest"))
    )
    return {
        "ok": ok,
        "code": "source_bundle_bound" if ok else "source_bundle_unbound",
        "reasons": [
            "The portable source bundle and staged harness report share one exact digest."
            if ok
            else "The staged harness is not bound to the selected portable source bundle."
        ],
    }
