"""Assemble milestone __STEP_ID__. Prefer each FlowStep's one tool, in table order."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path
from typing import Any

STEP_ID = "__STEP_ID__"
TOOLS: list[str] = json.loads("""__TOOLS_JSON__""")
FLOWSTEPS: list[dict[str, Any]] = json.loads("""__FLOWSTEPS_JSON__""")
INTELLIGENCE = "__INTELLIGENCE__"
IS_LAST = __IS_LAST__
ASSET_KIND = "__ASSET_KIND__"
OUTPUTS: list[dict[str, Any]] = json.loads(r'''__OUTPUTS_JSON__''')
WORKER = "__WORKER__"
LOOP = "__LOOP__"
HARNESS_DIR = Path(__file__).resolve().parents[2]
GEM_PATH = str(HARNESS_DIR / "references" / f"{STEP_ID}.md")
SCHEMA_PATH = str(HARNESS_DIR / "schemas" / f"{STEP_ID}_v1.json")


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
        "Do this FlowStep as its gem section says. Still produce the milestone's declared named outputs. "
        "Prefer fixing or using the tool. The judge must be able to choose the current bundle."
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


def _candidate(value: dict[str, Any], receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(value.get("outputs"), dict):
        candidate = dict(value)
    else:
        clean = {key: item for key, item in value.items() if key not in {"receipt", "address", "draft"}}
        if len(OUTPUTS) == 1:
            output_values = {str(OUTPUTS[0]["id"]): clean}
        else:
            output_values = {
                str(output["id"]): clean[str(output["id"])]
                for output in OUTPUTS
                if str(output["id"]) in clean
            }
        candidate = {"outputs": output_values}
    accepted = receipt or (value.get("receipt") if isinstance(value.get("receipt"), dict) else None)
    if isinstance(accepted, dict):
        candidate["receipt"] = accepted
    return candidate


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
            if "ok" in result:
                payload["receipt"] = result
    if WORKER and WORKER not in {str((item or {}).get("tool") or "") for item in sequence}:
        try:
            receipt_input = dict(payload)
            receipt_input["gem_path"] = GEM_PATH
            if isinstance(payload.get("asset"), dict):
                receipt_input["asset"] = payload["asset"]
            if isinstance(draft, dict):
                receipt_input["draft"] = draft
            if WORKER == "ok_receipt":
                if isinstance(draft, dict) and "ok" in draft:
                    receipt_input = {"ok": bool(draft["ok"]), "code": "pass" if draft["ok"] else "fail"}
                elif "ok" in payload:
                    receipt_input = {"ok": bool(payload["ok"])}
                else:
                    raise ValueError(f"{STEP_ID}: ok_receipt looks at the gem; draft {{ok}} for the rule of success")
            elif WORKER == "schema_validate":
                receipt_input = {"schema_path": SCHEMA_PATH, "instance": _candidate(payload)}
            elif WORKER == "hash_bind":
                asset = payload.get("asset") if isinstance(payload.get("asset"), dict) else {}
                path = asset.get("path") or _first_path(payload)
                if not path:
                    raise ValueError(f"{STEP_ID}: hash_bind worker needs an asset path")
                receipt_input = {"path": path}
            elif WORKER == "ledger_receipt":
                ledger = payload.get("ledger") or payload.get("items") or []
                done = payload.get("done")
                if not isinstance(done, list):
                    done = list(ledger) if isinstance(ledger, list) else []
                receipt_input = {"ledger": ledger if isinstance(ledger, list) else [], "done": done}
            elif WORKER in {"branch_receipt", "cycle_receipt"}:
                receipt_input = dict(payload)
                receipt_input["gem_path"] = GEM_PATH
                if isinstance(draft, dict):
                    receipt_input["draft"] = draft
            receipt = tools.run_library_tool(codebase, WORKER, receipt_input)
            if isinstance(receipt, dict):
                payload["receipt"] = receipt
                if "ok" in receipt:
                    payload["ok"] = receipt["ok"]
        except Exception as exc:
            if draft is None:
                return _need_model(STEP_ID, WORKER, f"{type(exc).__name__}: {exc}")
            payload[f"{WORKER}_error"] = f"{type(exc).__name__}: {exc}"
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
        if isinstance(payload.get("receipt"), dict):
            out["receipt"] = payload["receipt"]
        return _candidate(out, payload.get("receipt") if isinstance(payload.get("receipt"), dict) else None)
    if not payload:
        if draft is None:
            return _need_model(STEP_ID, "", f"{STEP_ID}: no candidate named outputs were produced")
        raise ValueError(f"{STEP_ID}: no candidate named outputs were produced")
    return _candidate(payload)
