"""Bind a file path to its sha256. Toolbox function, not a milestone."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def run(input_data: dict[str, Any], params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    del params
    path = Path(str(input_data["path"]))
    if not path.is_file():
        raise ValueError(f"file not found: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": path.as_posix(), "sha256": digest}
