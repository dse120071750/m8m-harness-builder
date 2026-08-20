"""Judge/for receipt worker. Toolbox function: ok or not ok. Not a milestone."""

from __future__ import annotations

from typing import Any


def run(input_data: dict[str, Any], params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    del params
    if "ok" not in input_data:
        raise ValueError("ok_receipt requires ok")
    ok = bool(input_data["ok"])
    receipt: dict[str, Any] = {"ok": ok}
    if input_data.get("code"):
        receipt["code"] = str(input_data["code"])
    else:
        receipt["code"] = "pass" if ok else "fail"
    if input_data.get("attempt") is not None:
        receipt["attempt"] = int(input_data["attempt"])
    return receipt
