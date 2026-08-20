"""Judge worker for a milestone gem. Writes {ok}. The model may draft; it may not set ok."""

from __future__ import annotations

from typing import Any


def run(input_data: dict[str, Any], params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    del params
    draft = input_data.get("draft") if isinstance(input_data.get("draft"), dict) else {}
    if "ok" in draft:
        ok = bool(draft["ok"])
    elif "ok" in input_data:
        ok = bool(input_data["ok"])
    else:
        gem = input_data.get("gem_path") or "the milestone gem"
        raise ValueError(
            f"__STEP_ID__ looks at {gem} and the asset. "
            "Draft {ok: true|false} for the rule of success. This tool writes the receipt."
        )
    receipt: dict[str, Any] = {"ok": ok, "code": "pass" if ok else "fail"}
    if input_data.get("gem_path"):
        receipt["gem"] = str(input_data["gem_path"])
    return receipt
