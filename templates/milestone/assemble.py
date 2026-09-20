"""Assemble milestone __STEP_ID__. Prefer each FlowStep's one tool, in table order."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from flowstep_tools import run_library_tool

STEP_ID = "__STEP_ID__"
M8M_BUILD_STATUS = "BUILD_REQUIRED"
M8M_RUNNABLE = False
TOOLS: list[str] = json.loads("""__TOOLS_JSON__""")
FLOWSTEPS: list[dict[str, Any]] = json.loads("""__FLOWSTEPS_JSON__""")
TOOL_BINDINGS: list[dict[str, str]] = json.loads("""__TOOL_BINDINGS_JSON__""")
INTELLIGENCE = "__INTELLIGENCE__"
IS_LAST = __IS_LAST__
ASSET_KIND = "__ASSET_KIND__"
OUTPUTS: list[dict[str, Any]] = json.loads(r'''__OUTPUTS_JSON__''')
CONTROL_WORKER = "__WORKER__"
CONTROL_KIND = "__CONTROL_KIND__"
LOOP = "__LOOP__"
HARNESS_DIR = Path(__file__).resolve().parents[2]
GEM_PATH = str(HARNESS_DIR / "references" / f"{STEP_ID}.md")
_VERSIONED_REF = re.compile(
    r"^[a-z][a-z0-9_.-]*@[0-9]+\.[0-9]+\.[0-9]+$"
)


def _local_tool_package(flowstep_id: str, exact_ref: str) -> str:
    if _VERSIONED_REF.fullmatch(exact_ref) is None:
        raise ValueError(f"{flowstep_id}: tool ref must be exact and versioned")
    package = exact_ref.rsplit("@", 1)[0]
    if re.fullmatch(r"[a-z][a-z0-9_]*", package) is None:
        raise ValueError(f"{flowstep_id}: tool ref has no safe local package")
    return package


def _codebase() -> Path:
    return Path(__file__).resolve().parents[5]


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


def _gem_section(flowstep: str) -> str:
    path = Path(GEM_PATH)
    fid = str(flowstep or "").strip().lower().replace("-", "_")
    if not fid or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    current = ""
    chunks: list[str] = []
    found: str = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if current == fid and chunks:
                found = "\n".join(chunks).strip()
                break
            title = stripped.lstrip("#").strip().strip("`").strip()
            token = title.split()[0].strip("`").lower().replace("-", "_") if title else ""
            current = token
            chunks = []
            continue
        if current:
            chunks.append(line)
    if not found and current == fid:
        found = "\n".join(chunks).strip()
    return found


def _need_model(flowstep: str, tool_id: str, error: str) -> dict[str, Any]:
    section = _gem_section(flowstep)
    instruction = (
        f"Preferred tool `{tool_id}` failed FlowStep `{flowstep}`. "
        "Start with the complete milestone master prompt and its bound upstream outputs. "
        "Use any FlowStep notes to produce the milestone's declared named outputs. "
        "Prefer fixing or using the tool. Return the milestone's explicit named outputs for admission."
    )
    if section:
        instruction = instruction + "\n\n" + section
    return {
        "_flowstep": "NEED_MODEL",
        "model": "completion" if INTELLIGENCE == "none" else INTELLIGENCE,
        "model_request": {
            "milestone": STEP_ID,
            "flowstep": flowstep,
            "tool": tool_id,
            "gem_path": GEM_PATH,
            "error": error,
            "instruction": instruction,
        },
    }


def _candidate(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value.get("outputs"), dict):
        raise ValueError(
            f"{STEP_ID}: candidate executor must return explicit {{'outputs': {{...}}}}; "
            "the generated handler will not infer or wrap a default result"
        )
    return {"outputs": dict(value["outputs"])}


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
    if M8M_BUILD_STATUS == "BUILD_REQUIRED" or not M8M_RUNNABLE:
        raise RuntimeError(
            f"{STEP_ID}: generated candidate handler is BUILD_REQUIRED/non-runnable; "
            "implement its milestone-specific FlowSteps so it returns explicit "
            "{'outputs': {...}}, then remove the build markers"
        )
    del kwargs
    payload: dict[str, Any] = dict(input_data)
    if isinstance(draft, dict):
        forbidden_control = {
            "_flowstep",
            "receipt",
            "control_receipt",
            "decision",
            "ok",
            "chosen",
            "branch",
            "cycle",
        }
        payload.update(
            {key: value for key, value in draft.items() if key not in forbidden_control}
        )
        payload["draft"] = draft
    try:
        codebase = _codebase()
    except Exception as exc:
        if draft is None:
            return _need_model(STEP_ID, "", f"{type(exc).__name__}: {exc}")
        raise
    binding_refs = {
        str(item.get("tool") or ""): str(item.get("ref") or "")
        for item in TOOL_BINDINGS
    }
    # Branch/cycle workers are control-plane tools. The runtime invokes their
    # exact bound refs only after this candidate has passed structural
    # admission; a generated candidate handler must never run them as a
    # FlowStep or capture their receipts in candidate data.
    sequence = [
        item
        for item in FLOWSTEPS
        if not (
            CONTROL_KIND in {"branch", "cycle"}
            and str((item or {}).get("id") or "") == CONTROL_WORKER
        )
    ]
    for item in sequence:
        exact_ref = str((item or {}).get("tool") or "")
        flowstep_id = str((item or {}).get("id") or "step")
        if not exact_ref:
            continue
        try:
            if binding_refs.get(flowstep_id) != exact_ref:
                raise ValueError(
                    f"{flowstep_id}: execution binding does not equal FlowStep tool ref"
                )
            tool_id = _local_tool_package(flowstep_id, exact_ref)
            result = run_library_tool(
                codebase,
                tool_id,
                _tool_input(payload, flowstep_id),
            )
        except Exception as exc:
            if draft is None:
                return _need_model(flowstep_id, exact_ref, f"{type(exc).__name__}: {exc}")
            payload[f"{flowstep_id}_error"] = f"{type(exc).__name__}: {exc}"
            continue
        if isinstance(result, dict):
            payload[flowstep_id] = result
            if "path" in result:
                payload["asset"] = result
    if ASSET_KIND in {"file", "image", "video", "audio"}:
        address = payload.get("address") if isinstance(payload.get("address"), dict) else {}
        dest = address.get("write_to")
        asset = dict(payload.get("asset")) if isinstance(payload.get("asset"), dict) else {}
        path = asset.get("path") or _first_path(payload)
        if not path or not Path(str(path)).is_file():
            if draft is None:
                return _need_model(STEP_ID, "", f"{STEP_ID}: declared {ASSET_KIND} output needs an existing path")
            raise ValueError(f"{STEP_ID}: declared {ASSET_KIND} output needs an existing path")
        asset["path"] = str(path)
        if dest:
            dest_path = Path(str(dest))
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if Path(str(asset["path"])).resolve() != dest_path.resolve():
                shutil.copy2(asset["path"], dest_path)
            asset["path"] = str(dest_path)
        out_asset = {"path": asset["path"]}
        if asset.get("sha256"):
            out_asset["sha256"] = asset["sha256"]
        out = {"asset": out_asset}
        if address.get("slot"):
            out["asset"]["slot"] = str(address["slot"])
        return _candidate(out)
    if not payload:
        if draft is None:
            return _need_model(STEP_ID, "", f"{STEP_ID}: no candidate named outputs were produced")
        raise ValueError(f"{STEP_ID}: no candidate named outputs were produced")
    return _candidate(payload)
