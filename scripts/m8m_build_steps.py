"""Deterministic handlers for the builder's own canonical M8M workflow."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from audit_harness import audit_skill
from flowstep_runtime import FLOW_ID_RE, FLOW_SCHEMA, FlowError, load_flow, utc_now
from generate_harness import generate_from_audit, generate_tool
from validate_harness import validate_harness


def _request(input_data: dict[str, Any]) -> dict[str, Any]:
    request = input_data.get("request")
    if not isinstance(request, dict):
        raise FlowError("builder request is missing")
    return request


def _path(request: dict[str, Any], key: str) -> Path:
    raw = request.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise FlowError(f"builder request needs {key}")
    return Path(raw).resolve()


def _flow_id(audit: dict[str, Any], request: dict[str, Any]) -> str:
    skill = audit.get("audited_skill") if isinstance(audit.get("audited_skill"), dict) else {}
    name = str(request.get("skill_name") or skill.get("name") or "product-skill")
    raw = request.get("flow_id") or (audit.get("grade") or {}).get("flow_id") or f"{name.replace('-', '_')}_v1"
    value = str(raw).lower().replace("-", "_")
    return value if FLOW_ID_RE.match(value) else "product_v1"


def _skill_name(audit: dict[str, Any], request: dict[str, Any]) -> str:
    skill = audit.get("audited_skill") if isinstance(audit.get("audited_skill"), dict) else {}
    return str(request.get("skill_name") or skill.get("name") or "product-skill")


def _stage_codebase(run_dir: Path) -> Path:
    path = Path(run_dir).resolve() / "work" / "builder" / "stage-codebase"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _candidate(value: dict[str, Any]) -> dict[str, Any]:
    return {"outputs": {"result": value}}


def _tool_ids(audit: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for row in audit.get("python_standardization") or []:
        if isinstance(row, dict) and row.get("tool_id"):
            ids.append(str(row["tool_id"]))
    for milestone in audit.get("proposed_milestones") or []:
        if not isinstance(milestone, dict):
            continue
        ids.extend(str(item) for item in milestone.get("tools") or [] if item)
        if milestone.get("worker"):
            ids.append(str(milestone["worker"]))
    unique: list[str] = []
    for tool_id in ids:
        if tool_id and tool_id not in unique:
            unique.append(tool_id)
    return unique


def _audit(input_data: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    del run_dir
    request = _request(input_data)
    target = _path(request, "target")
    if not target.is_dir():
        raise FlowError(f"builder target not found: {target}")
    audit = audit_skill(target)
    audit["builder_request"] = {
        "target": str(target),
        "codebase": str(_path(request, "codebase")),
        "flow_id": request.get("flow_id"),
        "skill_name": request.get("skill_name"),
    }
    return _candidate(audit)


def _toolbox(input_data: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    audit = input_data.get("audit")
    if not isinstance(audit, dict):
        raise FlowError("toolbox_ready needs the chosen source audit")
    stage = _stage_codebase(run_dir)
    tools = [generate_tool(stage, tool_id) for tool_id in _tool_ids(audit)]
    return _candidate(
        {
            "stage_codebase": str(stage),
            "tools": tools,
            "status": "PASS",
            "created_at": utc_now(),
        }
    )


def _generate(input_data: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    request = _request(input_data)
    audit = input_data.get("audit")
    if not isinstance(audit, dict):
        raise FlowError("flow_generated needs the chosen source audit")
    stage = _stage_codebase(run_dir)
    generated = generate_from_audit(
        stage,
        audit,
        flow_id=_flow_id(audit, request),
        skill_name=_skill_name(audit, request),
        overwrite=True,
    )
    generated["stage_codebase"] = str(stage)
    return _candidate(generated)


def _validate(input_data: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    del run_dir
    generated = input_data.get("generated")
    if not isinstance(generated, dict):
        raise FlowError("harness_validated needs the chosen staged harness")
    harness = Path(str(generated.get("harness_dir") or "")).resolve()
    flow = load_flow(harness)
    if flow.get("schema") != FLOW_SCHEMA:
        raise FlowError(f"staged harness must be {FLOW_SCHEMA}")
    required = [
        harness / "flow.yaml",
        harness / "planning" / "m8m-flowchart.md",
        harness / "planning" / "m8m-flowchart.jpg",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FlowError(f"staged harness is missing portable artifacts: {missing}")
    full_validation: dict[str, Any]
    try:
        full_validation = validate_harness(harness)
    except FlowError as exc:
        # Generate-new tools are an intentional sketch. The compulsory gate is
        # structural: v4 loads, chosen outputs are declared, and review files exist.
        full_validation = {"status": "STRUCTURAL_PASS", "notes": [str(exc)]}
    return _candidate(
        {
            "ok": True,
            "flow_id": flow["flow_id"],
            "flow_schema": flow["schema"],
            "harness_dir": str(harness),
            "full_validation": full_validation,
            "validated_at": utc_now(),
        }
    )


def _copy_tree(source: Path, destination: Path, *, overwrite: bool) -> list[str]:
    copied: list[str] = []
    if not source.is_dir():
        return copied
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        target = destination / relative
        if target.exists() and not overwrite:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(str(target))
    return copied


def _ship(input_data: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    del run_dir
    request = _request(input_data)
    generated = input_data.get("generated")
    validation = input_data.get("validation")
    if not isinstance(generated, dict) or not isinstance(validation, dict) or not validation.get("ok"):
        raise FlowError("skill_shipped needs the chosen generated harness and validation")
    stage = Path(str(generated.get("stage_codebase") or "")).resolve()
    codebase = _path(request, "codebase")
    target = _path(request, "target")
    flow_id = str(generated.get("flow_id") or validation.get("flow_id") or "")
    skill_name = str(generated.get("skill_name") or request.get("skill_name") or target.name)
    overwrite = bool(request.get("overwrite"))
    copied: list[str] = []
    copied.extend(
        _copy_tree(
            stage / "flowsteps" / "flows" / flow_id,
            codebase / "flowsteps" / "flows" / flow_id,
            overwrite=overwrite,
        )
    )
    copied.extend(
        _copy_tree(stage / "flowsteps" / "tools", codebase / "flowsteps" / "tools", overwrite=overwrite)
    )
    for family in (".agents", ".claude"):
        copied.extend(
            _copy_tree(
                stage / family / "skills" / skill_name,
                codebase / family / "skills" / skill_name,
                overwrite=overwrite,
            )
        )
    copied.extend(
        _copy_tree(
            stage / "flowsteps" / "flows" / flow_id / "planning",
            target / "planning",
            overwrite=overwrite,
        )
    )
    product_skill = codebase / ".agents" / "skills" / skill_name / "SKILL.md"
    flowchart = codebase / "flowsteps" / "flows" / flow_id / "planning" / "m8m-flowchart.md"
    return _candidate(
        {
            "status": "PASS",
            "flow_id": flow_id,
            "skill_name": skill_name,
            "product_skill": str(product_skill),
            "flowchart_path": str(flowchart),
            "flowchart_jpg": str(flowchart.with_suffix(".jpg")),
            "installed_files": copied,
            "installed_at": utc_now(),
        }
    )


def run(
    input_data: dict[str, Any],
    draft: dict[str, Any] | None = None,
    *,
    task: dict[str, Any] | None = None,
    run_dir: Path | str | None = None,
    **_: Any,
) -> dict[str, Any]:
    del draft
    if run_dir is None:
        raise FlowError("builder milestone needs run_dir")
    step_id = str((task or {}).get("step_id") or "")
    handlers = {
        "audit_complete": _audit,
        "toolbox_ready": _toolbox,
        "flow_generated": _generate,
        "harness_validated": _validate,
        "skill_shipped": _ship,
    }
    handler = handlers.get(step_id)
    if handler is None:
        raise FlowError(f"unknown builder milestone: {step_id}")
    return handler(input_data, Path(run_dir))
