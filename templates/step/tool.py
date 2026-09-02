"""BUILD_REQUIRED FlowStep scaffold: __STEP_ID__.

Return a dict that matches output.schema.json. Do not write the envelope.
If this step needs a model, return:

    {"_flowstep": "NEED_MODEL", "model": "completion", "model_request": {...}}

The driver will stop, the agent writes draft.json from that request, and
advance is called again. This function then receives `draft` and must return
the typed payload.

This generated file is deliberately non-runnable. Implement the public
contracts, replace the generated test, and remove both BUILD_REQUIRED
declarations before asking the builder to validate or install the harness.
"""

from __future__ import annotations

from typing import Any

MODEL = "none"
M8M_BUILD_STATUS = "BUILD_REQUIRED"
M8M_RUNNABLE = False


def run(input_data: dict[str, Any], draft: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    raise NotImplementedError("__STEP_ID__ is a generated stub; implement this tool")
