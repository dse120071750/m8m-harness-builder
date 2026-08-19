"""FlowStep tool: __STEP_ID__.

Return a dict that matches output.schema.json. Do not write the envelope.
If this step needs a model, return:

    {"_flowstep": "NEED_MODEL", "model": "completion", "model_request": {...}}

The driver will stop, the agent writes draft.json from that request, and
advance is called again. This function then receives `draft` and must return
the typed payload.
"""

from __future__ import annotations

from typing import Any

MODEL = "none"


def run(input_data: dict[str, Any], draft: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    raise NotImplementedError("__STEP_ID__ is a generated stub; implement this tool")
