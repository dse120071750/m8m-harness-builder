"""Split normalized text into sentences. Next step may only read this payload."""

from __future__ import annotations

import re
from typing import Any


def run(input_data: dict[str, Any], draft: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    del draft
    text = str(input_data["ingest"]["text"])
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    if not sentences:
        raise ValueError("no sentences")
    return {"sentences": sentences, "sentence_count": len(sentences)}
