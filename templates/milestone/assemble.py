"""Assemble milestone __STEP_ID__. Call only the listed toolbox ids."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

STEP_ID = "__STEP_ID__"
TOOLS: list[str] = json.loads("""__TOOLS_JSON__""")
INTELLIGENCE = "__INTELLIGENCE__"
IS_LAST = __IS_LAST__


def _codebase() -> Path:
    return Path(__file__).resolve().parents[5]


def _builder_tools() -> Any:
    env = os.environ.get("FLOWSTEP_BUILDER")
    candidates = []
    if env:
        candidates.append(Path(env) / "scripts" / "flowstep_tools.py")
    candidates.append(Path.home() / ".codex" / "skills" / "flowstep-harness-builder" / "scripts" / "flowstep_tools.py")
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "flowstep-harness-builder" / "scripts" / "flowstep_tools.py"
        if candidate.is_file():
            candidates.append(candidate)
    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("f8f_flowstep_tools", path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise RuntimeError("F8F builder not found; set FLOWSTEP_BUILDER to the skill root")


def _first_path(value: Any) -> str | None:
    if isinstance(value, dict):
        raw = value.get("path")
        if isinstance(raw, str) and raw and Path(raw).is_file():
            return raw
        for child in value.values():
            found = _first_path(child)
            if found:
                return found
    if isinstance(value, str) and value and Path(value).is_file():
        return value
    return None


def run(input_data: dict[str, Any], draft: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    __INTEL_GATE__
    payload: dict[str, Any] = dict(input_data)
    if draft:
        payload["draft"] = draft
    tools = _builder_tools()
    codebase = _codebase()
    if "hash_bind" in TOOLS:
        path = _first_path(payload)
        if path:
            payload["asset"] = tools.run_library_tool(codebase, "hash_bind", {"path": path})
    if "schema_validate" in TOOLS and "instance" in payload and "schema" in payload:
        tools.run_library_tool(
            codebase,
            "schema_validate",
            {"instance": payload["instance"], "schema": payload["schema"]},
        )
        payload["valid"] = True
    if IS_LAST:
        asset = payload.get("asset")
        if not isinstance(asset, dict) or "path" not in asset or "sha256" not in asset:
            path = _first_path(payload)
            if not path:
                raise ValueError(f"{STEP_ID}: last milestone must emit asset path+sha256")
            asset = tools.run_library_tool(codebase, "hash_bind", {"path": path})
        return {"asset": {"path": asset["path"], "sha256": asset["sha256"]}}
    return payload
