"""Classify the first sentence. The model may only fill a draft; this tool writes the artifact."""

from __future__ import annotations

from typing import Any

ALLOWED = ("question", "statement", "other")


def run(input_data: dict[str, Any], draft: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    sentence = input_data["segment"]["sentences"][0]
    if draft is None:
        return {
            "_flowstep": "NEED_MODEL",
            "model": "completion",
            "model_request": {
                "instruction": "Classify the sentence as question, statement, or other.",
                "sentence": sentence,
                "allowed": list(ALLOWED),
            },
        }
    label = draft.get("label")
    if label not in ALLOWED:
        raise ValueError(f"draft.label must be one of {ALLOWED}")
    return {"label": label, "sentence": sentence}
