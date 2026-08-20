"""Branch receipt worker. AI drafts the path; this tool writes the receipt."""

from __future__ import annotations

from typing import Any


def _paths(input_data: dict[str, Any]) -> list[str]:
    raw = input_data.get("paths")
    if isinstance(raw, list) and raw:
        return [str(item) for item in raw if item]
    branch = input_data.get("branch")
    if isinstance(branch, dict) and isinstance(branch.get("paths"), list):
        out: list[str] = []
        for item in branch["paths"]:
            if isinstance(item, dict) and item.get("id"):
                out.append(str(item["id"]))
            elif item:
                out.append(str(item))
        return out
    return []


def _default(input_data: dict[str, Any], paths: list[str]) -> str:
    branch = input_data.get("branch") if isinstance(input_data.get("branch"), dict) else {}
    raw = input_data.get("default") or branch.get("default") or ""
    if raw:
        return str(raw)
    return paths[0] if paths else ""


def _recommended(input_data: dict[str, Any]) -> str:
    draft = input_data.get("draft") if isinstance(input_data.get("draft"), dict) else {}
    for source in (input_data, draft):
        for key in ("recommended_branch", "branch_id"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        value = source.get("branch")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _from_case_type(input_data: dict[str, Any], paths: list[str]) -> str:
    case_type = input_data.get("case_type")
    if case_type is None:
        request = input_data.get("request") if isinstance(input_data.get("request"), dict) else {}
        case_type = request.get("case_type")
    if not isinstance(case_type, str):
        nested = input_data.get("intake_ready") if isinstance(input_data.get("intake_ready"), dict) else {}
        case_type = nested.get("case_type")
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
    paths = _paths(payload)
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
    reason = payload.get("reason") or ""
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
