"""Assemble milestone __STEP_ID__. Prefer each FlowStep's one tool, in table order."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

STEP_ID = "__STEP_ID__"
TOOLS: list[str] = json.loads("""__TOOLS_JSON__""")
FLOWSTEPS: list[dict[str, Any]] = json.loads("""__FLOWSTEPS_JSON__""")
INTELLIGENCE = "__INTELLIGENCE__"
IS_LAST = __IS_LAST__
ASSET_KIND = "__ASSET_KIND__"


def _codebase() -> Path:
    return Path(__file__).resolve().parents[5]


def _builder_tools() -> Any:
    env = os.environ.get("M8M_BUILDER") or os.environ.get("FLOWSTEP_BUILDER")
    candidates = []
    if env:
        candidates.append(Path(env) / "scripts" / "flowstep_tools.py")
    for home_skills in (Path.home() / ".codex" / "skills", Path.home() / ".claude" / "skills"):
        for skill_name in ("m8m-harness-builder", "flowstep-harness-builder"):
            candidates.append(home_skills / skill_name / "scripts" / "flowstep_tools.py")
    here = Path(__file__).resolve()
    for parent in here.parents:
        for skill_name in ("m8m-harness-builder", "flowstep-harness-builder"):
            candidate = parent / skill_name / "scripts" / "flowstep_tools.py"
            if candidate.is_file():
                candidates.append(candidate)
    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("m8m_flowstep_tools", path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise RuntimeError("M8M builder not found; set M8M_BUILDER to the skill root")


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


def _need_model(flowstep: str, tool_id: str, error: str) -> dict[str, Any]:
    return {
        "_flowstep": "NEED_MODEL",
        "model": "completion" if INTELLIGENCE == "none" else INTELLIGENCE,
        "model_request": {
            "milestone": STEP_ID,
            "flowstep": flowstep,
            "tool": tool_id,
            "error": error,
            "instruction": (
                f"Preferred tool `{tool_id}` failed FlowStep `{flowstep}`. "
                "Find a way to still produce this milestone's required asset. "
                "Prefer fixing or using the tool. Do not skip the asset."
            ),
        },
    }


def _tool_input(payload: dict[str, Any], tool_id: str) -> dict[str, Any]:
    if tool_id == "hash_bind":
        path = _first_path(payload)
        if not path:
            raise ValueError("hash_bind needs a file path")
        return {"path": path}
    if tool_id == "schema_validate":
        if "instance" in payload and "schema" in payload:
            return {"instance": payload["instance"], "schema": payload["schema"]}
        raise ValueError("schema_validate needs instance and schema")
    nested = payload.get(tool_id)
    if isinstance(nested, dict):
        return nested
    req = payload.get("request") if isinstance(payload.get("request"), dict) else None
    if isinstance(req, dict):
        return req
    return payload


def run(input_data: dict[str, Any], draft: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    del kwargs
    payload: dict[str, Any] = dict(input_data)
    if isinstance(draft, dict):
        payload.update({key: value for key, value in draft.items() if key != "_flowstep"})
        payload["draft"] = draft
    try:
        tools = _builder_tools()
        codebase = _codebase()
    except Exception as exc:
        if draft is None:
            return _need_model(STEP_ID, "", f"{type(exc).__name__}: {exc}")
        raise
    sequence = FLOWSTEPS or [{"id": tool_id, "tool": tool_id} for tool_id in TOOLS]
    for item in sequence:
        tool_id = str((item or {}).get("tool") or "")
        flowstep_id = str((item or {}).get("id") or tool_id or "step")
        if not tool_id:
            continue
        try:
            result = tools.run_library_tool(codebase, tool_id, _tool_input(payload, tool_id))
        except Exception as exc:
            if draft is None:
                return _need_model(flowstep_id, tool_id, f"{type(exc).__name__}: {exc}")
            payload[f"{tool_id}_error"] = f"{type(exc).__name__}: {exc}"
            continue
        if isinstance(result, dict):
            payload[tool_id] = result
            if "path" in result and "sha256" in result:
                payload["asset"] = result
    if ASSET_KIND in {"file", "image"} or IS_LAST:
        asset = payload.get("asset")
        if not isinstance(asset, dict) or "path" not in asset or "sha256" not in asset:
            path = _first_path(payload)
            if not path:
                if draft is None:
                    return _need_model(STEP_ID, "hash_bind", f"{STEP_ID}: milestone asset not produced (need {ASSET_KIND} path+sha256)")
                raise ValueError(f"{STEP_ID}: milestone asset not produced (need {ASSET_KIND} path+sha256)")
            try:
                asset = tools.run_library_tool(codebase, "hash_bind", {"path": path})
            except Exception as exc:
                if draft is None:
                    return _need_model(STEP_ID, "hash_bind", f"{type(exc).__name__}: {exc}")
                raise ValueError(f"{STEP_ID}: milestone asset not produced (need {ASSET_KIND} path+sha256)") from exc
        return {"asset": {"path": asset["path"], "sha256": asset["sha256"]}}
    if not payload:
        if draft is None:
            return _need_model(STEP_ID, "", f"{STEP_ID}: milestone asset not produced (empty {ASSET_KIND} proof)")
        raise ValueError(f"{STEP_ID}: milestone asset not produced (empty {ASSET_KIND} proof)")
    return payload
