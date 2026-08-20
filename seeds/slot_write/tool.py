"""Copy a temp/generated file into the frozen session slot, then hash it."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any


def run(input_data: dict[str, Any], params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    del params
    src = Path(str(input_data["path"]))
    if not src.is_file():
        raise ValueError(f"file not found: {src}")
    address = input_data.get("address") if isinstance(input_data.get("address"), dict) else {}
    dest_raw = address.get("write_to") or input_data.get("write_to")
    if not dest_raw:
        raise ValueError("slot_write requires address.write_to")
    dest = Path(str(dest_raw))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    slot = str(address.get("slot") or dest.as_posix())
    return {"path": dest.as_posix(), "sha256": digest, "slot": slot}
