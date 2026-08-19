"""Normalize request text. This step is the ingest tool, not a worker prompt."""

from __future__ import annotations

import re
from typing import Any


def run(input_data: dict[str, Any], draft: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    del draft
    text = re.sub(r"\s+", " ", str(input_data["request"]["text"])).strip()
    if not text:
        raise ValueError("text is empty after normalize")
    return {"text": text, "char_count": len(text)}
