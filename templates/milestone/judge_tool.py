"""Milestone-specific semantic judge scaffold.

The runtime supplies a closed ``m8m.milestone_judge_request.v1`` after
structural admission.  Implement the authored success rule here; never accept
``decision`` from a candidate worker or model draft.
"""

from __future__ import annotations

from typing import Any


def run(input_data: dict[str, Any], params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    del params
    if input_data.get("schema") != "m8m.milestone_judge_request.v1":
        raise ValueError("__STEP_ID__ requires m8m.milestone_judge_request.v1")
    if not isinstance(input_data.get("expectation"), dict):
        raise ValueError("__STEP_ID__ requires the derived milestone expectation")
    if not isinstance(input_data.get("candidate"), dict):
        raise ValueError("__STEP_ID__ requires the admitted named-output candidate")
    raise NotImplementedError(
        "BUILD_REQUIRED: implement __STEP_ID__ semantic approval against "
        "input_data['expectation']['success']; return exactly "
        "{decision: PASS|RETRY|BLOCKED, reasons: [...], blockers: [...]}"
    )
