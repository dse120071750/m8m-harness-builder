"""Advance a v2 FlowStep run by executing each step's Python tool."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from schema_gate import ELSE_BLOCKED, check_foreach, resolve_next
from flowstep_runtime import (
    ACTION_SCHEMA,
    NEED_MODEL,
    FlowError,
    add_harness_location_args,
    harness_dir_from_args,
    assert_implementation_lock,
    bind_inputs,
    envelope_schema_path,
    expected_artifact_path,
    find_flow_path,
    implementation_lock,
    invoke_tool,
    load_flow,
    make_envelope,
    recovery_model,
    read_json,
    relative_to,
    sha256_file,
    utc_now,
    validate_against_schema,
    work_dir,
    write_json,
)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _execution_path(run_dir: Path) -> Path:
    return run_dir / "flow-execution-record.json"


def _progress_path(run_dir: Path) -> Path:
    return run_dir / "progress.json"


def initialize_run(run_dir: Path, skill_dir: Path, flow: dict[str, Any], request_path: Path | None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "request.json"
    if not target.exists():
        if request_path is None:
            raise FlowError("a new run requires --request")
        source = request_path.resolve()
        if not source.is_file():
            raise FlowError(f"request not found: {source}")
        write_json(target, read_json(source), overwrite=False)
    lock_path = run_dir / "implementation-lock.json"
    if not lock_path.exists():
        write_json(lock_path, implementation_lock(skill_dir, flow), overwrite=False)
    if not _execution_path(run_dir).exists():
        write_json(
            _execution_path(run_dir),
            {
                "schema": "flow_execution_record_v2",
                "run_id": run_dir.name,
                "flow_id": flow["flow_id"],
                "flow_version": flow["version"],
                "status": "IN_PROGRESS",
                "implementation_fingerprint_sha256": implementation_lock(skill_dir, flow)["fingerprint_sha256"],
                "repair_cycles": 0,
                "steps": [],
                "created_at": utc_now(),
                "updated_at": utc_now(),
            },
            overwrite=False,
        )


def _task(run_dir: Path, flow: dict[str, Any], step: dict[str, Any], bindings: list[dict[str, Any]], attempt: int) -> dict[str, Any]:
    expected = expected_artifact_path(run_dir, flow, step)
    return {
        "schema": "runtime_task_v2",
        "run_id": run_dir.name,
        "flow_id": flow["flow_id"],
        "flow_version": flow["version"],
        "step_id": step["id"],
        "handler": step["handler"],
        "model": step["model"],
        "attempt": attempt,
        "output_contract": step["output_contract"],
        "expected_output_path": relative_to(run_dir, expected),
        "input_artifacts": bindings,
        "created_at": utc_now(),
    }


def _materialize(run_dir: Path, flow: dict[str, Any], step: dict[str, Any], artifact: dict[str, Any], path: Path) -> None:
    result = {
        "schema": "runtime_step_result_v2",
        "run_id": run_dir.name,
        "flow_id": flow["flow_id"],
        "step_id": step["id"],
        "status": artifact["status"],
        "output_contract": step["output_contract"],
        "artifact_path": relative_to(run_dir, path),
        "artifact_sha256": sha256_file(path),
        "attempt": artifact["evidence"]["attempt"],
        "materialized_at": utc_now(),
    }
    write_json(run_dir / "materialized" / f"{step['id']}.runtime_step_result.json", result, overwrite=False)
    record = read_json(_execution_path(run_dir))
    record["steps"].append(
        {
            "step_id": step["id"],
            "status": artifact["status"],
            "artifact_sha256": result["artifact_sha256"],
            "attempt": artifact["evidence"]["attempt"],
        }
    )
    record["updated_at"] = utc_now()
    if artifact["status"] == "BLOCKED":
        record["status"] = "BLOCKED"
    write_json(_execution_path(run_dir), record)
    write_json(
        _progress_path(run_dir),
        {
            "schema": "flow_progress_v2",
            "run_id": run_dir.name,
            "status": record["status"],
            "planned_steps": len(flow["steps"]),
            "materialized_steps": len(record["steps"]),
            "last_step": step["id"],
            "updated_at": utc_now(),
        },
    )


def _blocked_action(step_id: str, blockers: list[str]) -> dict[str, Any]:
    return {"schema": ACTION_SCHEMA, "state": "BLOCKED", "step_id": step_id, "blockers": blockers}


def _write_blocked(
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    bindings: list[dict[str, Any]],
    fingerprint: str,
    blockers: list[str],
) -> dict[str, Any]:
    artifact = make_envelope(
        flow=flow,
        step=step,
        run_id=run_dir.name,
        attempt=1,
        status="BLOCKED",
        data={},
        bindings=bindings,
        fingerprint=fingerprint,
        blockers=blockers,
    )
    path = expected_artifact_path(run_dir, flow, step)
    write_json(path, artifact, overwrite=False)
    validate_against_schema(artifact, envelope_schema_path())
    _materialize(run_dir, flow, step, artifact, path)
    return _blocked_action(step["id"], blockers)


def _recover_or_block(
    skill_dir: Path,
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    bindings: list[dict[str, Any]],
    fingerprint: str,
    draft: dict[str, Any] | None,
    blockers: list[str],
) -> dict[str, Any]:
    folder = work_dir(run_dir, step["id"])
    folder.mkdir(parents=True, exist_ok=True)
    if step.get("on_tool_fail") != "need_model":
        return _write_blocked(run_dir, flow, step, bindings, fingerprint, blockers)
    fail_path = folder / "tool_failed.json"
    record = read_json(fail_path) if fail_path.is_file() else {"attempts": 0, "blockers": []}
    attempts = int(record.get("attempts") or 0) + 1
    limit = int(step.get("max_model_attempts") or 8)
    write_json(
        fail_path,
        {"attempts": attempts, "blockers": blockers, "max_model_attempts": limit},
        overwrite=True,
    )
    if attempts > limit:
        return _write_blocked(
            run_dir,
            flow,
            step,
            bindings,
            fingerprint,
            blockers + [f"{step['id']}: max_model_attempts {limit} exhausted"],
        )
    request = {
        "milestone": step["id"],
        "blockers": blockers,
        "tools": step.get("tools") or [],
        "attempt": attempts,
        "max_model_attempts": limit,
        "instruction": (
            "The preferred FlowStep tool ran first and failed. Find a way to still produce "
            "the milestone asset (output schema PASS), like a normal agent. Prefer the listed "
            "tool. Do not skip the asset. Missing the asset is BLOCKED."
        ),
        "flowsteps": step.get("flowsteps") or [],
    }
    request_path = folder / "model_request.json"
    write_json(request_path, request)
    return {
        "schema": ACTION_SCHEMA,
        "state": "ACTION_REQUIRED",
        "action": "run_model_then_advance",
        "execution_mode": "tool",
        "step_id": step["id"],
        "attempt": attempts,
        "model": recovery_model(step),
        "task_path": relative_to(run_dir, run_dir / "runtime-tasks" / f"{step['id']}.json"),
        "model_request_path": relative_to(run_dir, request_path),
        "draft_path": relative_to(run_dir, folder / "draft.json"),
        "draft_schema_path": step.get("draft_schema"),
        "expected_output_path": relative_to(run_dir, expected_artifact_path(run_dir, flow, step)),
        "input_artifacts": bindings,
        "tools": step.get("tools") or [],
        "on_tool_fail": "need_model",
    }


def _execute_step(
    skill_dir: Path,
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    fingerprint: str,
    draft: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Run one tool. Return an action to stop, or None to continue."""
    bindings: list[dict[str, Any]] = []
    try:
        input_data, bindings = bind_inputs(run_dir, flow, step)
        validate_against_schema(input_data, skill_dir / step["input_schema"])
    except FlowError as exc:
        return _write_blocked(run_dir, flow, step, bindings, fingerprint, [str(exc)])
    task_path = run_dir / "runtime-tasks" / f"{step['id']}.json"
    if task_path.is_file():
        task = read_json(task_path)
    else:
        task = _task(run_dir, flow, step, bindings, attempt=1)
        write_json(task_path, task, overwrite=False)
    folder = work_dir(run_dir, step["id"])
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / "input.json", input_data)
    if draft is not None:
        draft_rel = step.get("draft_schema")
        draft_path = skill_dir / draft_rel if draft_rel else None
        if draft_path is not None and draft_path.is_file():
            try:
                validate_against_schema(draft, draft_path)
            except FlowError as exc:
                return _write_blocked(run_dir, flow, step, bindings, fingerprint, [str(exc)])
        write_json(folder / "draft.json", draft)
    try:
        result = invoke_tool(skill_dir, step, input_data, draft, task)
    except Exception as exc:
        return _recover_or_block(
            skill_dir,
            run_dir,
            flow,
            step,
            bindings,
            fingerprint,
            draft,
            [f"{type(exc).__name__}: {exc}"],
        )
    if not isinstance(result, dict):
        return _recover_or_block(
            skill_dir,
            run_dir,
            flow,
            step,
            bindings,
            fingerprint,
            draft,
            ["tool must return a JSON object"],
        )
    if result.get("_flowstep") == "BLOCKED":
        blockers = [str(item) for item in result.get("blockers") or ["tool returned BLOCKED"]]
        return _recover_or_block(skill_dir, run_dir, flow, step, bindings, fingerprint, draft, blockers)
    if result.get("_flowstep") == NEED_MODEL:
        if step.get("on_tool_fail") == "BLOCKED":
            return _write_blocked(
                run_dir, flow, step, bindings, fingerprint, [f"{step['id']} requested a model but on_tool_fail is BLOCKED"]
            )
        if step.get("model") == "none" and step.get("on_tool_fail") != "need_model":
            return _write_blocked(
                run_dir, flow, step, bindings, fingerprint, [f"{step['id']} requested a model but model is none"]
            )
        request = result.get("model_request")
        if not isinstance(request, dict):
            return _write_blocked(run_dir, flow, step, bindings, fingerprint, ["NEED_MODEL requires model_request"])
        request_path = folder / "model_request.json"
        write_json(request_path, request)
        draft_path = folder / "draft.json"
        return {
            "schema": ACTION_SCHEMA,
            "state": "ACTION_REQUIRED",
            "action": "run_model_then_advance",
            "execution_mode": "tool",
            "step_id": step["id"],
            "attempt": 1,
            "model": result.get("model") or recovery_model(step),
            "task_path": relative_to(run_dir, run_dir / "runtime-tasks" / f"{step['id']}.json"),
            "model_request_path": relative_to(run_dir, request_path),
            "draft_path": relative_to(run_dir, draft_path),
            "draft_schema_path": step.get("draft_schema"),
            "expected_output_path": task["expected_output_path"],
            "input_artifacts": bindings,
            "tools": step.get("tools") or [],
        }
    try:
        validate_against_schema(result, skill_dir / step["output_schema"])
    except FlowError as exc:
        return _write_blocked(
            run_dir,
            flow,
            step,
            bindings,
            fingerprint,
            [f"{step['id']}: milestone asset not produced; {exc}"],
        )
    try:
        if step.get("foreach"):
            check_foreach(skill_dir, step, result)
    except FlowError as exc:
        return _write_blocked(
            run_dir,
            flow,
            step,
            bindings,
            fingerprint,
            [f"{step['id']}: milestone check failed; {exc}"],
        )
    artifact = make_envelope(
        flow=flow,
        step=step,
        run_id=run_dir.name,
        attempt=1,
        status="PASS",
        data=result,
        bindings=bindings,
        fingerprint=fingerprint,
        blockers=[],
    )
    path = expected_artifact_path(run_dir, flow, step)
    write_json(path, artifact, overwrite=False)
    validate_against_schema(artifact, envelope_schema_path())
    _materialize(run_dir, flow, step, artifact, path)
    return None


def advance(
    skill_dir: Path,
    run_dir: Path,
    *,
    request_path: Path | None = None,
    draft_path: Path | None = None,
    flow_arg: str | None = None,
) -> dict[str, Any]:
    skill_dir = skill_dir.resolve()
    run_dir = run_dir.resolve()
    flow = load_flow(skill_dir, find_flow_path(skill_dir, flow_arg) if flow_arg else None)
    initialize_run(run_dir, skill_dir, flow, request_path)
    lock = assert_implementation_lock(run_dir, skill_dir, flow)
    record = read_json(_execution_path(run_dir))
    if record["status"] == "BLOCKED":
        raise FlowError("run is terminal BLOCKED; start a fresh run")
    created = record.get("created_at")
    if created and (datetime.now(timezone.utc) - _parse_time(str(created))).total_seconds() > flow["max_run_seconds"]:
        raise FlowError("run exceeded the frozen wall-clock budget; start a fresh run")
    completed = {item["step_id"] for item in record["steps"]}
    skip: set[str] = set(record.get("skipped") or [])
    pending_draft = read_json(draft_path) if draft_path else None
    if draft_path and not isinstance(pending_draft, dict):
        raise FlowError("--draft must contain one JSON object")
    for step in flow["steps"]:
        if step["id"] in skip:
            continue
        artifact_path = expected_artifact_path(run_dir, flow, step)
        materialized_path = run_dir / "materialized" / f"{step['id']}.runtime_step_result.json"
        if step["id"] in completed:
            if not artifact_path.is_file() or not materialized_path.is_file():
                raise FlowError(f"materialized step bytes disappeared: {step['id']}")
            artifact = read_json(artifact_path)
            validate_against_schema(artifact, envelope_schema_path())
            if sha256_file(artifact_path) != read_json(materialized_path).get("artifact_sha256"):
                raise FlowError(f"completed artifact was mutated: {step['id']}")
            if artifact["status"] != "PASS":
                raise FlowError(f"step is terminal BLOCKED: {step['id']}")
            continue
        budget = (step.get("params") or {}).get("step_budget_seconds")
        task_path = run_dir / "runtime-tasks" / f"{step['id']}.json"
        if budget and task_path.is_file():
            task = read_json(task_path)
            if (datetime.now(timezone.utc) - _parse_time(task["created_at"])).total_seconds() > budget:
                _, bindings = bind_inputs(run_dir, flow, step)
                return _write_blocked(
                    run_dir, flow, step, bindings, lock["fingerprint_sha256"], ["STEP_BUDGET_EXCEEDED"]
                )
        draft = None
        if pending_draft is not None:
            draft = pending_draft
            pending_draft = None
        elif step["model"] != "none":
            existing_draft = work_dir(run_dir, step["id"]) / "draft.json"
            if existing_draft.is_file():
                draft = read_json(existing_draft)
        action = _execute_step(skill_dir, run_dir, flow, step, lock["fingerprint_sha256"], draft)
        if action is not None:
            return action
        if step.get("next"):
            artifact = read_json(expected_artifact_path(run_dir, flow, step))
            chosen, evidence = resolve_next(step, artifact.get("data") or {}, skill_dir)
            record = read_json(_execution_path(run_dir))
            record.setdefault("gates", []).append(
                {"from": step["id"], **(evidence or {}), "chosen": chosen}
            )
            if chosen == ELSE_BLOCKED or chosen == "BLOCKED":
                record["status"] = "BLOCKED"
                record["skipped"] = sorted(skip)
                record["blockers"] = [f"{step['id']}: no gate matched (else BLOCKED)"]
                record["updated_at"] = utc_now()
                write_json(_execution_path(run_dir), record)
                return _blocked_action(step["id"], [f"{step['id']}: no gate matched (else BLOCKED)"])
            for edge in step.get("next") or []:
                if edge.get("then") != chosen:
                    skip.add(str(edge["then"]))
            record["skipped"] = sorted(skip)
            write_json(_execution_path(run_dir), record)
    record = read_json(_execution_path(run_dir))
    record["status"] = "COMPLETE"
    record["updated_at"] = utc_now()
    write_json(_execution_path(run_dir), record)
    write_json(
        _progress_path(run_dir),
        {
            "schema": "flow_progress_v2",
            "run_id": run_dir.name,
            "status": "COMPLETE",
            "planned_steps": len(flow["steps"]),
            "materialized_steps": len(record["steps"]),
            "updated_at": utc_now(),
        },
    )
    return {
        "schema": ACTION_SCHEMA,
        "state": "COMPLETE",
        "run_id": run_dir.name,
        "steps": len(record["steps"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_harness_location_args(parser)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--draft", type=Path)
    parser.add_argument("--flow")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = advance(
            harness_dir_from_args(args),
            args.run_dir,
            request_path=args.request,
            draft_path=args.draft,
            flow_arg=args.flow,
        )
    except FlowError as exc:
        record_path = args.run_dir / "flow-execution-record.json"
        if record_path.is_file():
            record = read_json(record_path)
            if record.get("status") != "COMPLETE":
                record["status"] = "BLOCKED"
                record["blockers"] = [str(exc)]
                record["updated_at"] = utc_now()
                write_json(record_path, record)
        print(json.dumps(_blocked_action("", [str(exc)]), indent=2), file=sys.stderr)
        return 3
    print(json.dumps(result, indent=2))
    return 0 if result["state"] in {"ACTION_REQUIRED", "COMPLETE"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
