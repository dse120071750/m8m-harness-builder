"""Ledger receipt worker. ok only when every ledger item is done."""

from __future__ import annotations

from typing import Any


def run(input_data: dict[str, Any], params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    del params
    ledger = input_data.get("ledger")
    done = input_data.get("done")
    if not isinstance(ledger, list) or not isinstance(done, list):
        raise ValueError("ledger_receipt requires ledger and done arrays")
    remaining = max(0, len(ledger) - len(done))
    receipt: dict[str, Any] = {
        "ok": remaining == 0,
        "remaining": remaining,
        "done": len(done),
    }
    if input_data.get("item_id"):
        receipt["item_id"] = str(input_data["item_id"])
    return receipt
