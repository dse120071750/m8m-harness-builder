"""Advance a v2 FlowStep run by executing each step's Python tool."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from schema_gate import ledger_items, read_receipt, schema_accepts
from session_layout import (
    assert_in_run,
    attach_address,
    copy_envelope_to_slot,
    default_run_dir,
    ensure_session_tree,
    materialize_bytes_into_slot,
    record_skip,
    record_slot,
)
from flowstep_tools import infer_codebase
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
    ensure_session_tree(run_dir, flow)
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


def _need_model_action(
    run_dir: Path,
    step: dict[str, Any],
    folder: Path,
    task: dict[str, Any],
    result: dict[str, Any],
    bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    request = result.get("model_request")
    request_path = folder / "model_request.json"
    write_json(request_path, request)
    draft_path = folder / "draft.json"
    return {
        "schema": ACTION_SCHEMA,
        "state": "ACTION_REQUIRED",
        "action": "run_model_then_advance",
        "execution_mode": "tool",
        "step_id": step["id"],
        "attempt": int(task.get("attempt") or 1),
        "model": result.get("model") or recovery_model(step),
        "task_path": relative_to(run_dir, run_dir / "runtime-tasks" / f"{step['id']}.json"),
        "model_request_path": relative_to(run_dir, request_path),
        "draft_path": relative_to(run_dir, draft_path),
        "draft_schema_path": step.get("draft_schema"),
        "expected_output_path": task["expected_output_path"],
        "input_artifacts": bindings,
        "tools": step.get("tools") or [],
    }


def _run_handler(
    skill_dir: Path,
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    input_data: dict[str, Any],
    draft: dict[str, Any] | None,
    task: dict[str, Any],
    bindings: list[dict[str, Any]],
    fingerprint: str,
    folder: Path,
) -> dict[str, Any]:
    """Return {'result': dict} or {'action': action}."""
    try:
        result = invoke_tool(skill_dir, step, input_data, draft, task)
    except Exception as exc:
        return {
            "action": _recover_or_block(
                skill_dir,
                run_dir,
                flow,
                step,
                bindings,
                fingerprint,
                draft,
                [f"{type(exc).__name__}: {exc}"],
            )
        }
    if not isinstance(result, dict):
        return {
            "action": _recover_or_block(
                skill_dir,
                run_dir,
                flow,
                step,
                bindings,
                fingerprint,
                draft,
                ["tool must return a JSON object"],
            )
        }
    if result.get("_flowstep") == "BLOCKED":
        blockers = [str(item) for item in result.get("blockers") or ["tool returned BLOCKED"]]
        return {
            "action": _recover_or_block(skill_dir, run_dir, flow, step, bindings, fingerprint, draft, blockers)
        }
    if result.get("_flowstep") == NEED_MODEL:
        if step.get("on_tool_fail") == "BLOCKED":
            return {
                "action": _write_blocked(
                    run_dir,
                    flow,
                    step,
                    bindings,
                    fingerprint,
                    [f"{step['id']} requested a model but on_tool_fail is BLOCKED"],
                )
            }
        if step.get("model") == "none" and step.get("on_tool_fail") != "need_model":
            return {
                "action": _write_blocked(
                    run_dir,
                    flow,
                    step,
                    bindings,
                    fingerprint,
                    [f"{step['id']} requested a model but model is none"],
                )
            }
        if not isinstance(result.get("model_request"), dict):
            return {
                "action": _write_blocked(
                    run_dir, flow, step, bindings, fingerprint, ["NEED_MODEL requires model_request"]
                )
            }
        return {"action": _need_model_action(run_dir, step, folder, task, result, bindings)}
    return {"result": result}


def _item_payload(result: dict[str, Any], fallback: Any) -> Any:
    body = {key: value for key, value in result.items() if key != "receipt"}
    if "item" in body:
        return body["item"]
    if "asset" in body:
        return body["asset"]
    return body or fallback


def _place_result(
    run_dir: Path,
    step: dict[str, Any],
    result: dict[str, Any],
    *,
    item_index: int | None = None,
    attempt: int | None = None,
) -> dict[str, Any]:
    kind = str(((step.get("asset") or {}).get("kind") if isinstance(step.get("asset"), dict) else "") or "")
    if kind not in {"file", "image"}:
        return result
    asset = result.get("asset") if isinstance(result.get("asset"), dict) else {}
    has_path = isinstance(asset.get("path"), str) and asset["path"]
    if not has_path:
        return result
    placed = materialize_bytes_into_slot(run_dir, step, result, item_index=item_index, attempt=attempt)
    assert_in_run(run_dir, placed["asset"]["path"])
    return placed


def _pass_artifact(
    skill_dir: Path,
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    result: dict[str, Any],
    bindings: list[dict[str, Any]],
    fingerprint: str,
    attempt: int,
) -> dict[str, Any] | None:
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
    if step.get("branch"):
        blocked = _check_branch(skill_dir, run_dir, flow, step, result, bindings, fingerprint)
        if blocked is not None:
            return blocked
    artifact = make_envelope(
        flow=flow,
        step=step,
        run_id=run_dir.name,
        attempt=attempt,
        status="PASS",
        data=result,
        bindings=bindings,
        fingerprint=fingerprint,
        blockers=[],
    )
    path = expected_artifact_path(run_dir, flow, step)
    write_json(path, artifact, overwrite=False)
    validate_against_schema(artifact, envelope_schema_path())
    copy_envelope_to_slot(run_dir, step, path)
    record_slot(run_dir, step, result)
    _materialize(run_dir, flow, step, artifact, path)
    if step.get("branch"):
        _store_active_branch(run_dir, step, result)
    return None


def _check_branch(
    skill_dir: Path,
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    result: dict[str, Any],
    bindings: list[dict[str, Any]],
    fingerprint: str,
) -> dict[str, Any] | None:
    spec = step.get("branch") if isinstance(step.get("branch"), dict) else {}
    path_ids = [str(item.get("id") or "") for item in (spec.get("paths") or []) if item]
    receipt_rel = spec.get("receipt_schema") or step.get("receipt_schema")
    try:
        receipt = read_receipt(result, skill_dir=skill_dir, schema_rel=receipt_rel, step_id=step["id"])
    except FlowError as exc:
        return _write_blocked(run_dir, flow, step, bindings, fingerprint, [str(exc)])
    if not receipt.get("ok"):
        return _write_blocked(
            run_dir,
            flow,
            step,
            bindings,
            fingerprint,
            [f"{step['id']}: branch receipt not ok (could not decide)"],
        )
    chosen = str(receipt.get("branch") or "")
    if not chosen or (path_ids and chosen not in path_ids):
        return _write_blocked(
            run_dir,
            flow,
            step,
            bindings,
            fingerprint,
            [f"{step['id']}: unknown branch {chosen or '(empty)'}"],
        )
    return None


def _store_active_branch(run_dir: Path, step: dict[str, Any], result: dict[str, Any]) -> None:
    receipt = result.get("receipt") if isinstance(result.get("receipt"), dict) else {}
    chosen = str(receipt.get("branch") or "")
    record_path = _execution_path(run_dir)
    record = read_json(record_path)
    record["active_branch"] = chosen
    record["branch_from"] = step["id"]
    record["updated_at"] = utc_now()
    write_json(record_path, record, overwrite=True)


def _execute_step(
    skill_dir: Path,
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    fingerprint: str,
    draft: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Run one milestone. Return an action to stop, or None to continue."""
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
    loop = str(step.get("loop") or "none")
    max_attempts = int(step.get("max_attempts") or step.get("max_model_attempts") or 8)
    receipt_rel = step.get("receipt_schema")

    if loop == "for":
        ledger = step.get("ledger") or {}
        path = str(ledger.get("path") or "items")
        try:
            items = ledger_items(input_data, path)
        except FlowError as exc:
            return _write_blocked(run_dir, flow, step, bindings, fingerprint, [str(exc)])
        max_items = ledger.get("max_items")
        if isinstance(max_items, int) and len(items) > max_items:
            return _write_blocked(
                run_dir,
                flow,
                step,
                bindings,
                fingerprint,
                [f"{step['id']}: ledger {path} length {len(items)} exceeds max_items {max_items}"],
            )
        item_schema = ledger.get("item_schema")
        item_schema_path = skill_dir / str(item_schema) if item_schema else None
        state_path = folder / "ledger_state.json"
        state = read_json(state_path) if state_path.is_file() else {"index": 0, "done": []}
        done: list[Any] = list(state.get("done") or [])
        index = int(state.get("index") or 0)
        attempts = int(state.get("attempts") or 0)
        current_draft = draft
        while index < len(items):
            attempts += 1
            if attempts > max_attempts:
                return _write_blocked(
                    run_dir,
                    flow,
                    step,
                    bindings,
                    fingerprint,
                    [f"{step['id']}: for-ledger budget {max_attempts} exhausted; remaining {len(items) - index}"],
                )
            item = items[index]
            if item_schema_path is not None and item_schema_path.is_file() and not schema_accepts(item, item_schema_path):
                return _write_blocked(
                    run_dir,
                    flow,
                    step,
                    bindings,
                    fingerprint,
                    [f"{step['id']}: ledger item {index} failed {item_schema}"],
                )
            item_input = dict(input_data)
            item_input["item"] = item
            item_input["ledger"] = items
            item_input["done"] = done
            item_input = attach_address(run_dir, step, item_input, item_index=index)
            outcome = _run_handler(
                skill_dir, run_dir, flow, step, item_input, current_draft, task, bindings, fingerprint, folder
            )
            current_draft = None
            if outcome.get("action") is not None:
                write_json(state_path, {"index": index, "done": done, "attempts": attempts}, overwrite=True)
                return outcome["action"]
            result = outcome["result"]
            try:
                receipt = read_receipt(result, skill_dir=skill_dir, schema_rel=receipt_rel, step_id=step["id"])
            except FlowError as exc:
                return _write_blocked(run_dir, flow, step, bindings, fingerprint, [str(exc)])
            if not receipt["ok"]:
                write_json(state_path, {"index": index, "done": done, "attempts": attempts}, overwrite=True)
                continue
            try:
                result = _place_result(run_dir, step, result, item_index=index)
            except FlowError:
                pass
            done.append(_item_payload(result, item))
            index += 1
            write_json(state_path, {"index": index, "done": done, "attempts": attempts}, overwrite=True)
        final = {
            path: done,
            "receipt": {"ok": True, "remaining": 0, "done": len(done)},
        }
        if len(done) == 1 and isinstance(done[0], dict) and "path" in done[0] and "sha256" in done[0]:
            final["asset"] = {"path": done[0]["path"], "sha256": done[0]["sha256"]}
        return _pass_artifact(skill_dir, run_dir, flow, step, final, bindings, fingerprint, attempts)

    if loop == "judge":
        state_path = folder / "judge_state.json"
        state = read_json(state_path) if state_path.is_file() else {"attempts": 0}
        attempts = int(state.get("attempts") or 0)
        current_draft = draft
        last: dict[str, Any] | None = None
        while True:
            attempts += 1
            if attempts > max_attempts:
                return _write_blocked(
                    run_dir,
                    flow,
                    step,
                    bindings,
                    fingerprint,
                    [f"{step['id']}: judge budget {max_attempts} exhausted; receipt not ok"],
                )
            write_json(state_path, {"attempts": attempts}, overwrite=True)
            judged = attach_address(run_dir, step, input_data, attempt=attempts)
            outcome = _run_handler(
                skill_dir, run_dir, flow, step, judged, current_draft, task, bindings, fingerprint, folder
            )
            current_draft = None
            if outcome.get("action") is not None:
                return outcome["action"]
            result = outcome["result"]
            last = result
            try:
                receipt = read_receipt(result, skill_dir=skill_dir, schema_rel=receipt_rel, step_id=step["id"])
            except FlowError as exc:
                return _write_blocked(run_dir, flow, step, bindings, fingerprint, [str(exc)])
            if receipt["ok"]:
                try:
                    result = _place_result(run_dir, step, result)
                except FlowError as exc:
                    return _write_blocked(run_dir, flow, step, bindings, fingerprint, [str(exc)])
                return _pass_artifact(skill_dir, run_dir, flow, step, result, bindings, fingerprint, attempts)
        return _write_blocked(
            run_dir, flow, step, bindings, fingerprint, [f"{step['id']}: judge ended without ok receipt"]
        )

    input_data = attach_address(run_dir, step, input_data)
    outcome = _run_handler(skill_dir, run_dir, flow, step, input_data, draft, task, bindings, fingerprint, folder)
    if outcome.get("action") is not None:
        return outcome["action"]
    result = outcome["result"]
    try:
        result = _place_result(run_dir, step, result)
    except FlowError as exc:
        return _write_blocked(run_dir, flow, step, bindings, fingerprint, [str(exc)])
    return _pass_artifact(skill_dir, run_dir, flow, step, result, bindings, fingerprint, 1)


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
    skipped = {str(item.get("step_id") or "") for item in (record.get("skipped") or [])}
    pending_draft = read_json(draft_path) if draft_path else None
    if draft_path and not isinstance(pending_draft, dict):
        raise FlowError("--draft must contain one JSON object")
    for step in flow["steps"]:
        artifact_path = expected_artifact_path(run_dir, flow, step)
        materialized_path = run_dir / "materialized" / f"{step['id']}.runtime_step_result.json"
        if step["id"] in skipped:
            continue
        on_path = str(step.get("on_path") or "")
        active = str(record.get("active_branch") or "")
        if on_path and active and on_path != active:
            record_skip(run_dir, step, branch=active, reason=f"on_path {on_path} skipped; branch={active}")
            record = read_json(_execution_path(run_dir))
            skipped_rows = list(record.get("skipped") or [])
            skipped_rows.append(
                {
                    "step_id": step["id"],
                    "branch": active,
                    "skipped": True,
                    "reason": f"on_path {on_path} != {active}",
                }
            )
            record["skipped"] = skipped_rows
            record["updated_at"] = utc_now()
            write_json(_execution_path(run_dir), record, overwrite=True)
            skipped.add(step["id"])
            continue
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
        record = read_json(_execution_path(run_dir))
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
        "run_dir": str(run_dir.resolve()),
        "steps": len(record["steps"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_harness_location_args(parser)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--draft", type=Path)
    parser.add_argument("--flow")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    skill_dir = harness_dir_from_args(args)
    run_dir = args.run_dir
    try:
        if run_dir is None:
            flow = load_flow(skill_dir, find_flow_path(skill_dir, args.flow) if args.flow else None)
            codebase = infer_codebase(skill_dir)
            if codebase is None:
                raise FlowError("pass --run-dir, or run a product flow under flowsteps/flows/<id>")
            run_dir = default_run_dir(codebase, str(flow.get("flow_id") or "flow"))
        result = advance(
            skill_dir,
            run_dir,
            request_path=args.request,
            draft_path=args.draft,
            flow_arg=args.flow,
        )
        if isinstance(result, dict):
            result["run_dir"] = str(Path(run_dir).resolve())
    except FlowError as exc:
        record_path = (run_dir or Path(".")) / "flow-execution-record.json"
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
