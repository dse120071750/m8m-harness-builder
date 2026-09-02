"""Choose a branch from one runtime-owned, post-admission control request."""

from __future__ import annotations

from typing import Any


def _control(input_data: dict[str, Any]) -> dict[str, Any]:
    value = input_data.get("control")
    return dict(value) if isinstance(value, dict) else {}


def _default(input_data: dict[str, Any], paths: list[str]) -> str:
    raw = _control(input_data).get("default") or ""
    if raw:
        return str(raw)
    return paths[0] if paths else ""


def _recommended(input_data: dict[str, Any]) -> str:
    inputs = input_data.get("inputs") if isinstance(input_data.get("inputs"), dict) else {}
    candidate = input_data.get("candidate") if isinstance(input_data.get("candidate"), dict) else {}
    outputs = candidate.get("outputs") if isinstance(candidate.get("outputs"), dict) else {}
    sources = [outputs, *[value for value in outputs.values() if isinstance(value, dict)], inputs]
    for source in sources:
        value = source.get("recommended_branch") or source.get("branch_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _from_case_type(input_data: dict[str, Any], paths: list[str]) -> str:
    inputs = input_data.get("inputs") if isinstance(input_data.get("inputs"), dict) else {}
    candidate = input_data.get("candidate") if isinstance(input_data.get("candidate"), dict) else {}
    outputs = candidate.get("outputs") if isinstance(candidate.get("outputs"), dict) else {}
    values = [*outputs.values(), inputs, *inputs.values()]
    case_type: Any = None
    for value in values:
        if isinstance(value, dict) and isinstance(value.get("case_type"), str):
            case_type = value["case_type"]
            break
        if isinstance(value, dict):
            request = value.get("request")
            if isinstance(request, dict) and isinstance(request.get("case_type"), str):
                case_type = request["case_type"]
                break
    if not isinstance(case_type, str):
        return ""
    token = case_type.strip()
    if token == "source_case":
        for path in paths:
            if "source_case" in path or path.endswith("source_case"):
                return path
    return ""


def run(input_data: dict[str, Any], params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    del params
    payload = dict(input_data or {})
    control = _control(payload)
    paths = [str(item) for item in control.get("paths") or [] if item]
    default = _default(payload, paths)
    recommended = _recommended(payload) or _from_case_type(payload, paths) or default
    if not recommended:
        return {
            "ok": False,
            "branch": "",
            "skipped": paths,
            "reason": "no branch decided",
        }
    if paths and recommended not in paths:
        return {
            "ok": False,
            "branch": recommended,
            "skipped": paths,
            "reason": f"unknown branch {recommended}",
        }
    skipped = [item for item in paths if item != recommended]
    reason = ""
    if not reason:
        if recommended == default and not _recommended(payload) and not _from_case_type(payload, paths):
            reason = f"default {recommended}"
        elif _from_case_type(payload, paths) == recommended:
            reason = "case_type is source_case" if "source_case" in recommended else "case_type is not source_case"
        else:
            reason = f"branch {recommended}"
        if recommended == default and "source_case" not in recommended:
            reason = "case_type is not source_case" if "source_case" in ",".join(paths) else reason
    return {
        "ok": True,
        "branch": recommended,
        "skipped": skipped,
        "reason": str(reason),
    }
