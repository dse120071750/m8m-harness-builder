"""Deterministically verify the bounded local Builder installation receipt."""

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
    receipt = outputs.get("installation_receipt") if isinstance(outputs, dict) else None
    ok = bool(
        isinstance(receipt, dict)
        and receipt.get("status") == "PASS"
        and receipt.get("runnable") is True
        and receipt.get("non_runnable") is False
        and isinstance(receipt.get("installed_files"), list)
        and bool(receipt["installed_files"])
        and bool(receipt.get("source_bundle_digest"))
        and bool(receipt.get("staged_members_digest"))
        and bool(receipt.get("source_bundle_sha256"))
    )
    return {
        "ok": ok,
        "code": "local_installation_bound" if ok else "local_installation_unbound",
        "reasons": [
            "The local installation receipt is bound to the validated source bundle and member set."
            if ok
            else "The local installation receipt is incomplete or not bound to validated source bytes."
        ],
    }
