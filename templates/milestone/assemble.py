"""Assemble milestone __STEP_ID__. Call only the toolbox ids listed on this milestone."""

from __future__ import annotations

from typing import Any


def run(input_data: dict[str, Any], draft: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    raise NotImplementedError("assemble __STEP_ID__ using run_library_tool on the listed tools")
