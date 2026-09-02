"""Derived M8M milestone expectations and closed semantic-judge requests.

The authored authority remains the four existing ``flowstep_flow_v4`` fields:
``success``, ``output_contract``, ``output_schema``, and ``outputs``.  This
module projects them into immutable execution views; it does not create a
second authored contract or persist workflow state.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping


EXPECTATION_SCHEMA = "m8m.milestone_expectation.v1"
JUDGE_REQUEST_SCHEMA = "m8m.milestone_judge_request.v1"


def derive_milestone_expectation(step: Mapping[str, Any]) -> dict[str, Any]:
    """Return the closed expectation projection for one milestone."""

    return {
        "schema": EXPECTATION_SCHEMA,
        "milestone_id": str(step.get("id") or ""),
        "success": str(step.get("success") or "").strip(),
        "output_contract": str(step.get("output_contract") or "").strip(),
        "output_schema_ref": str(step.get("output_schema") or "").strip(),
        "outputs": [
            {
                "id": str(item.get("id") or "").strip(),
                "name": str(item.get("name") or "").strip(),
                "kind": str(item.get("kind") or "").strip(),
                "cardinality": str(item.get("cardinality") or "").strip(),
                "required": bool(item.get("required")),
            }
            for item in step.get("outputs") or []
            if isinstance(item, Mapping)
        ],
    }


def build_milestone_judge_request(
    step: Mapping[str, Any],
    *,
    attempt: int,
    inputs: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the only payload accepted by ``m8m_milestone_judge_v1``."""

    max_attempts = max(
        1,
        int(step.get("max_attempts") or step.get("max_model_attempts") or 1),
    )
    return {
        "schema": JUDGE_REQUEST_SCHEMA,
        "milestone_id": str(step.get("id") or ""),
        "attempt": max(1, int(attempt)),
        "max_attempts": max_attempts,
        "expectation": derive_milestone_expectation(step),
        "inputs": copy.deepcopy(dict(inputs)),
        "candidate": copy.deepcopy(dict(candidate)),
    }


def normalize_success_text(value: Any) -> str:
    """Normalize presentation-only whitespace for Gem/source alignment."""

    text = str(value or "").strip()
    text = re.sub(r"^Rule\s+of\s+success\s*:\s*", "", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def gem_success_rule(markdown: str) -> str:
    """Read the body of the exact second-level ``Rule of success`` section."""

    lines = str(markdown or "").splitlines()
    start: int | None = None
    chunks: list[str] = []
    for index, line in enumerate(lines):
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if not match:
            if start is not None:
                chunks.append(line)
            continue
        title = match.group(1).strip().strip("`")
        if start is not None:
            break
        if title == "Rule of success":
            start = index + 1
    if start is None:
        return ""
    return "\n".join(chunks).strip()


__all__ = [
    "EXPECTATION_SCHEMA",
    "JUDGE_REQUEST_SCHEMA",
    "build_milestone_judge_request",
    "derive_milestone_expectation",
    "gem_success_rule",
    "normalize_success_text",
]
