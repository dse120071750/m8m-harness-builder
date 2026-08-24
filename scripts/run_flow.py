"""Advance an M8M v4 run and expose only judge-approved chosen outputs."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from candidate_cache import (
    mark_cache_invalid,
    mark_cache_rejected,
    prepare_candidate_cache,
    store_chosen_candidate,
    validate_cache_mode,
)
from milestone_pair import is_wait_milestone
from schema_gate import ledger_items, read_receipt, schema_accepts
from session_layout import (
    assert_in_run,
    attach_address,
    chosen_output_path,
    cycle_id_of,
    default_run_dir,
    ensure_session_tree,
    find_paused_run,
    first_unfinished,
    ledger_from_asset,
    load_ledger,
    load_chosen_output,
    load_run_roster,
    mark_row,
    mark_roster_blocked,
    mark_roster_complete,
    mark_roster_row,
    materialize_chosen_output,
    materialize_bytes_into_slot,
    pause_run,
    promote_cycle_round,
    purge_cycle_live,
    record_skip,
    record_chosen_output,
    reset_roster_rows,
    resolve_chosen_output,
    resume_run,
    save_ledger,
    wait_draft_path,
    wait_draft_slot,
    waiting_roster_row,
)
from flowstep_tools import infer_codebase, run_library_tool
from flowstep_runtime import (
    ACTION_SCHEMA,
    NEED_MODEL,
    FlowError,
    add_harness_location_args,
    harness_dir_from_args,
    assert_implementation_lock,
    bind_inputs,
    chosen_output_schema_path,
    context_capsule_schema_path,
    envelope_schema_path,
    expected_artifact_path,
    find_flow_path,
    implementation_lock,
    invoke_tool,
    load_flow,
    load_yaml,
    make_envelope,
    recovery_model,
    read_json,
    relative_to,
    sha256_file,
    run_context_schema_path,
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


def initialize_run(
    run_dir: Path,
    skill_dir: Path,
    flow: dict[str, Any],
    request_path: Path | None,
    *,
    cache_mode: str | None = None,
    cache_namespace: str | None = None,
    origin: str = "fresh",
    parent_goal: str | None = None,
    goal_row: str | None = None,
) -> None:
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
    snapshot = run_dir / "flow-snapshot.yaml"
    if not snapshot.exists():
        shutil.copy2(Path(flow["_flow_path"]), snapshot)
    context_path = run_dir / "run-context.json"
    if not context_path.exists():
        existing_record = read_json(_execution_path(run_dir)) if _execution_path(run_dir).is_file() else {}
        existing_cache = existing_record.get("cache") if isinstance(existing_record.get("cache"), dict) else {}
        context_cache_mode = validate_cache_mode(
            cache_mode if cache_mode is not None else str(existing_cache.get("mode") or "off")
        )
        context = {
            "schema": "m8m_run_context_v1",
            "run_id": run_dir.name,
            "flow_id": flow["flow_id"],
            "origin": origin,
            "context_policy": "isolated",
            "chat_history_allowed": False,
            "cross_run_cache_mode": context_cache_mode,
            "request_path": "request.json",
            "created_at": utc_now(),
        }
        if parent_goal:
            context["parent_goal"] = str(parent_goal)
        if goal_row:
            context["goal_row"] = str(goal_row)
        validate_against_schema(context, run_context_schema_path())
        write_json(context_path, context, overwrite=False)
    else:
        validate_against_schema(read_json(context_path), run_context_schema_path())
    if not _execution_path(run_dir).exists():
        write_json(
            _execution_path(run_dir),
            {
                "schema": "flow_execution_record_v2",
                "run_id": run_dir.name,
                "flow_id": flow["flow_id"],
                "flow_version": flow["version"],
                "status": "IN_PROGRESS",
                "context_policy": "isolated",
                "implementation_fingerprint_sha256": implementation_lock(skill_dir, flow)["fingerprint_sha256"],
                "repair_cycles": 0,
                "cache": {
                    "mode": validate_cache_mode(cache_mode or "off"),
                    "namespace": str(cache_namespace or "local"),
                },
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


def _isolate_action(
    run_dir: Path,
    skill_dir: Path,
    flow: dict[str, Any],
    action: dict[str, Any],
) -> dict[str, Any]:
    """Attach a no-history context capsule to model work exposed by the runtime."""
    if action.get("state") != "ACTION_REQUIRED":
        return action
    step_id = str(action.get("step_id") or "")
    step = next((item for item in flow["steps"] if item["id"] == step_id), None)
    if step is None:
        raise FlowError(f"ACTION_REQUIRED references unknown milestone: {step_id}")
    allowed: set[str] = {str((run_dir / "run-context.json").resolve())}
    for key in ("task_path", "model_request_path"):
        raw = action.get(key)
        if isinstance(raw, str) and raw:
            allowed.add(str(assert_in_run(run_dir, raw)))
    input_path = run_dir / "milestones" / step_id / "work" / "input.json"
    if input_path.is_file():
        allowed.add(str(input_path.resolve()))
    for binding in action.get("input_artifacts") or []:
        if not isinstance(binding, dict):
            continue
        raw = binding.get("chosen_output_path")
        if isinstance(raw, str) and raw:
            chosen_path = assert_in_run(run_dir, raw)
            allowed.add(str(chosen_path))
            if chosen_path.is_file() and chosen_path.name == "chosen-output.json":
                manifest = read_json(chosen_path)
                for member in manifest.get("members") or []:
                    if isinstance(member, dict) and isinstance(member.get("path"), str):
                        member_path = assert_in_run(run_dir, member["path"])
                        if member_path.is_file():
                            allowed.add(str(member_path))
    for relative in (
        step.get("gem") or f"references/{step_id}.md",
        step.get("draft_schema"),
        step.get("output_schema"),
    ):
        if relative:
            path = (skill_dir / str(relative)).resolve()
            if path.is_file():
                allowed.add(str(path))
    draft_raw = str(action.get("draft_path") or f"work/{step_id}/draft.json")
    draft_path = assert_in_run(run_dir, draft_raw)
    capsule = {
        "schema": "m8m_context_capsule_v1",
        "run_id": run_dir.name,
        "flow_id": flow["flow_id"],
        "milestone_id": step_id,
        "context_policy": "isolated",
        "chat_history_allowed": False,
        "allowed_files": sorted(allowed),
        "write_file": str(draft_path),
        "instruction": (
            "Start a fresh no-history model worker. Read only allowed_files, treat them as the complete "
            "authority, and write only write_file. Do not use the parent chat, prior runs, or remembered assets."
        ),
        "created_at": utc_now(),
    }
    validate_against_schema(capsule, context_capsule_schema_path())
    capsule_path = run_dir / "runtime-tasks" / f"{step_id}.context.json"
    write_json(capsule_path, capsule, overwrite=True)
    isolated = dict(action)
    isolated["context_policy"] = "isolated"
    isolated["context_capsule_path"] = relative_to(run_dir, capsule_path)
    return isolated


def _has_feedback(draft: dict[str, Any] | None, *paths: Path) -> bool:
    if isinstance(draft, dict) and draft:
        return True
    for path in paths:
        if not path or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data:
            return True
    return False


def _write_wait_draft(run_dir: Path, step: dict[str, Any], draft: dict[str, Any]) -> Path:
    slot = wait_draft_path(run_dir, step["id"])
    slot.parent.mkdir(parents=True, exist_ok=True)
    write_json(slot, draft, overwrite=True)
    folder = work_dir(run_dir, step["id"])
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / "draft.json", draft, overwrite=True)
    return slot


def _pause_wait_action(
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    folder = work_dir(run_dir, step["id"])
    folder.mkdir(parents=True, exist_ok=True)
    slot = wait_draft_slot(step["id"])
    wait_draft_path(run_dir, step["id"]).parent.mkdir(parents=True, exist_ok=True)
    pause_run(run_dir, step["id"], slot=slot)
    request = {
        "milestone": step["id"],
        "paused": True,
        "gem": step.get("gem") or f"references/{step['id']}.md",
        "slot": slot,
        "instruction": (
            "Pause and exit this session. After the reply lands, find <run>/roster.json, "
            "resume, and let this milestone's judge read the gem: does the wait have "
            "feedback now? Write the reply to "
            f"{slot}."
        ),
    }
    request_path = folder / "model_request.json"
    write_json(request_path, request, overwrite=True)
    task_path = run_dir / "runtime-tasks" / f"{step['id']}.json"
    if not task_path.is_file():
        write_json(task_path, _task(run_dir, flow, step, bindings, attempt=1), overwrite=False)
    write_json(
        _progress_path(run_dir),
        {
            "schema": "flow_progress_v2",
            "run_id": run_dir.name,
            "status": "PAUSED",
            "planned_steps": len(flow.get("steps") or []),
            "last_step": step["id"],
            "updated_at": utc_now(),
        },
        overwrite=True,
    )
    return {
        "schema": ACTION_SCHEMA,
        "state": "ACTION_REQUIRED",
        "action": "wait_for_response",
        "execution_mode": "tool",
        "step_id": step["id"],
        "attempt": 1,
        "model": recovery_model(step),
        "paused": True,
        "run_id": run_dir.name,
        "run_dir": str(Path(run_dir).resolve()),
        "roster_path": "roster.json",
        "task_path": relative_to(run_dir, task_path),
        "model_request_path": relative_to(run_dir, request_path),
        "draft_path": slot,
        "draft_schema_path": step.get("draft_schema"),
        "expected_output_path": relative_to(run_dir, expected_artifact_path(run_dir, flow, step)),
        "input_artifacts": bindings,
        "tools": step.get("tools") or [],
    }


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
        chosen_output=None,
    )
    path = expected_artifact_path(run_dir, flow, step)
    write_json(path, artifact, overwrite=False)
    validate_against_schema(artifact, envelope_schema_path())
    _materialize(run_dir, flow, step, artifact, path)
    mark_roster_blocked(run_dir, step["id"])
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
            "the milestone's declared named outputs, like a normal agent. Prefer the listed "
            "tool. The judge must be able to commit the current result as chosen-output.json."
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
        result = invoke_tool(skill_dir, step, input_data, draft, task, run_dir)
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
    if isinstance(result.get("outputs"), dict):
        # v4 candidates stay in work/ until the judge/schema gate accepts them.
        # The chosen-output materializer performs the only copy into out/.
        return result
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
    cache_info: dict[str, Any] | None = None,
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
            [f"{step['id']}: chosen output was not produced; {exc}"],
        )
    if step.get("branch"):
        blocked = _check_branch(skill_dir, run_dir, flow, step, result, bindings, fingerprint)
        if blocked is not None:
            return blocked
    try:
        receipt = result.get("receipt") if isinstance(result.get("receipt"), dict) else None
        # v4 chosen-output milestones require an explicit outputs map. The
        # article flow's legacy handlers already pass their milestone schema;
        # adapt that validated single payload into the declared result output
        # before the v4 chosen-output and hash-bound member gate.
        if not isinstance(result.get("outputs"), dict):
            legacy_value = result.get("asset") if isinstance(result.get("asset"), dict) else result
            result = {**result, "outputs": {"result": legacy_value}}
        chosen = materialize_chosen_output(run_dir, flow, step, result, receipt=receipt)
        validate_against_schema(chosen, chosen_output_schema_path())
    except FlowError as exc:
        return _write_blocked(
            run_dir,
            flow,
            step,
            bindings,
            fingerprint,
            [f"{step['id']}: chosen output was not materialized; {exc}"],
        )
    chosen_rel = relative_to(run_dir, chosen_output_path(run_dir, step["id"]))
    artifact = make_envelope(
        flow=flow,
        step=step,
        run_id=run_dir.name,
        attempt=attempt,
        status="PASS",
        data=chosen,
        bindings=bindings,
        fingerprint=fingerprint,
        blockers=[],
        chosen_output=chosen_rel,
    )
    path = expected_artifact_path(run_dir, flow, step)
    write_json(path, artifact, overwrite=False)
    validate_against_schema(artifact, envelope_schema_path())
    record_chosen_output(run_dir, step, chosen)
    _materialize(run_dir, flow, step, artifact, path)
    mark_roster_row(run_dir, step["id"], status="done", slot=chosen_rel)
    runtime_cache = flow.get("_cache_runtime") if isinstance(flow.get("_cache_runtime"), dict) else {}
    store_chosen_candidate(
        skill_dir,
        run_dir,
        flow,
        step,
        chosen,
        cache_info,
        mode=str(runtime_cache.get("mode") or "off"),
    )
    if step.get("branch"):
        _store_active_branch(run_dir, step, result)
    if step.get("cycle"):
        return _apply_cycle(skill_dir, run_dir, flow, step, result, bindings, fingerprint)
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


def _cycle_wrap(flow: dict[str, Any], cycle_name: str) -> list[dict[str, Any]]:
    wrapped: list[dict[str, Any]] = []
    for item in flow["steps"]:
        spec = item.get("cycle") if isinstance(item.get("cycle"), dict) else {}
        name = str(item.get("on_cycle") or spec.get("id") or "")
        if name == cycle_name or (spec and cycle_id_of(item) == cycle_name):
            wrapped.append(item)
    return wrapped


def _clear_wrap_artifacts(run_dir: Path, flow: dict[str, Any], wrapped: list[dict[str, Any]]) -> None:
    for item in wrapped:
        artifact = expected_artifact_path(run_dir, flow, item)
        if artifact.is_file():
            artifact.unlink()
        materialized = Path(run_dir) / "materialized" / f"{item['id']}.runtime_step_result.json"
        if materialized.is_file():
            materialized.unlink()
        task = Path(run_dir) / "runtime-tasks" / f"{item['id']}.json"
        if task.is_file():
            task.unlink()


def _strip_completed(run_dir: Path, ids: set[str]) -> dict[str, Any]:
    record = read_json(_execution_path(run_dir))
    record["steps"] = [item for item in record.get("steps") or [] if item.get("step_id") not in ids]
    record["updated_at"] = utc_now()
    write_json(_execution_path(run_dir), record, overwrite=True)
    return record


def _ensure_ledger(
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    skill_dir: Path,
    bindings: list[dict[str, Any]],
    fingerprint: str,
) -> dict[str, Any] | dict[str, Any]:
    spec = step.get("cycle") if isinstance(step.get("cycle"), dict) else {}
    cid = cycle_id_of(step)
    existing = load_ledger(run_dir, cid)
    if existing:
        return existing
    source_id = str(spec.get("ledger") or "")
    data: dict[str, Any] = {}
    if source_id:
        source = next((item for item in flow["steps"] if item["id"] == source_id), None)
        if source is None:
            return _write_blocked(
                run_dir, flow, step, bindings, fingerprint, [f"{step['id']}: cycle ledger milestone {source_id} missing"]
            )
        path = chosen_output_path(run_dir, source_id)
        if not path.is_file():
            return _write_blocked(
                run_dir, flow, step, bindings, fingerprint, [f"{step['id']}: freeze the ledger before the cycle"]
            )
        payload = resolve_chosen_output(run_dir, source_id)
        if isinstance(payload, dict) and len(payload) == 1:
            payload = next(iter(payload.values()))
        data = payload if isinstance(payload, dict) else {}
    ledger = ledger_from_asset(data, cycle_id=cid, max_items=int(spec.get("max_rounds") or 8))
    if not ledger.get("rows"):
        return _write_blocked(
            run_dir, flow, step, bindings, fingerprint, [f"{step['id']}: cycle ledger has no rows"]
        )
    save_ledger(run_dir, cid, ledger)
    return ledger


def _apply_cycle(
    skill_dir: Path,
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    result: dict[str, Any],
    bindings: list[dict[str, Any]],
    fingerprint: str,
) -> dict[str, Any] | None:
    spec = step.get("cycle") if isinstance(step.get("cycle"), dict) else {}
    cid = cycle_id_of(step)
    receipt_rel = spec.get("receipt_schema") or step.get("receipt_schema")
    try:
        receipt = read_receipt(result, skill_dir=skill_dir, schema_rel=receipt_rel, step_id=step["id"])
    except FlowError as exc:
        return _write_blocked(run_dir, flow, step, bindings, fingerprint, [str(exc)])
    if not receipt.get("ok"):
        return _write_blocked(
            run_dir, flow, step, bindings, fingerprint, [f"{step['id']}: cycle receipt not ok"]
        )
    chosen = str(receipt.get("cycle") or "")
    if chosen not in {"pass", "fail"}:
        return _write_blocked(
            run_dir, flow, step, bindings, fingerprint, [f"{step['id']}: cycle must be pass or fail"]
        )
    ledger = load_ledger(run_dir, cid) or _ensure_ledger(run_dir, flow, step, skill_dir, bindings, fingerprint)
    if not isinstance(ledger, dict) or ledger.get("schema") == "flow_sequence_action_v2":
        return ledger if isinstance(ledger, dict) and ledger.get("state") == "BLOCKED" else _write_blocked(
            run_dir, flow, step, bindings, fingerprint, [f"{step['id']}: cycle ledger missing"]
        )
    record = read_json(_execution_path(run_dir))
    row_id = str(record.get("cycle_row") or receipt.get("row") or "")
    row = None
    if row_id:
        row = next((item for item in ledger.get("rows") or [] if str(item.get("id")) == row_id), None)
    if row is None:
        row = first_unfinished(ledger)
        row_id = str((row or {}).get("id") or "")
    wrapped = _cycle_wrap(flow, cid)
    wrap_ids = {item["id"] for item in wrapped}
    rounds = int(record.get("cycle_round") or 1)
    max_rounds = int(spec.get("max_rounds") or step.get("max_attempts") or 8)
    if chosen == "fail":
        if rounds >= max_rounds:
            return _write_blocked(
                run_dir,
                flow,
                step,
                bindings,
                fingerprint,
                [f"{step['id']}: cycle budget {max_rounds} exhausted; row {row_id} unfinished"],
            )
        purge_cycle_live(run_dir, wrapped)
        _clear_wrap_artifacts(run_dir, flow, wrapped)
        ledger = mark_row(ledger, row_id, status="unfinished")
        save_ledger(run_dir, cid, ledger)
        reset_roster_rows(run_dir, wrap_ids)
        record = _strip_completed(run_dir, wrap_ids)
        record["cycle_id"] = cid
        record["cycle_row"] = row_id
        record["cycle_round"] = rounds + 1
        record["cycle_restart"] = True
        write_json(_execution_path(run_dir), record, overwrite=True)
        return None
    slot = promote_cycle_round(run_dir, flow, wrapped, row_id or f"{rounds:03d}")
    ledger = mark_row(ledger, row_id, status="done", slot=slot)
    save_ledger(run_dir, cid, ledger)
    more = first_unfinished(ledger)
    if more:
        purge_cycle_live(run_dir, wrapped)
        _clear_wrap_artifacts(run_dir, flow, wrapped)
        reset_roster_rows(run_dir, wrap_ids)
        record = _strip_completed(run_dir, wrap_ids)
        record["cycle_id"] = cid
        record["cycle_row"] = str(more.get("id") or "")
        record["cycle_round"] = rounds + 1
        record["cycle_restart"] = True
        write_json(_execution_path(run_dir), record, overwrite=True)
        return None
    record = read_json(_execution_path(run_dir))
    record["cycle_id"] = cid
    record["cycle_done"] = True
    record["updated_at"] = utc_now()
    write_json(_execution_path(run_dir), record, overwrite=True)
    return None


def _prepare_cycle_start(
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    skill_dir: Path,
    fingerprint: str,
) -> dict[str, Any] | None:
    """Init or resume ledger when entering the first wrapped milestone."""
    owner = next((item for item in flow["steps"] if item.get("cycle") and cycle_id_of(item) == str(step.get("on_cycle") or cycle_id_of(step))), None)
    if owner is None and step.get("cycle"):
        owner = step
    if owner is None:
        return None
    bindings: list[dict[str, Any]] = []
    ledger = _ensure_ledger(run_dir, flow, owner, skill_dir, bindings, fingerprint)
    if isinstance(ledger, dict) and ledger.get("state") == "BLOCKED":
        return ledger
    if not isinstance(ledger, dict):
        return None
    unfinished = first_unfinished(ledger)
    if unfinished is None:
        return None
    record = read_json(_execution_path(run_dir))
    if not record.get("cycle_row"):
        purge_cycle_live(run_dir, _cycle_wrap(flow, cycle_id_of(owner)))
        record["cycle_id"] = cycle_id_of(owner)
        record["cycle_row"] = str(unfinished.get("id") or "")
        record["cycle_round"] = int(record.get("cycle_round") or 1)
        write_json(_execution_path(run_dir), record, overwrite=True)
    return None


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
    if step.get("on_cycle") or step.get("cycle"):
        blocked = _prepare_cycle_start(run_dir, flow, step, skill_dir, fingerprint)
        if blocked is not None:
            return blocked
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
        if is_wait_milestone(step):
            _write_wait_draft(run_dir, step, draft)
    if is_wait_milestone(step) and not _has_feedback(draft, folder / "draft.json", wait_draft_path(run_dir, step["id"])):
        return _pause_wait_action(run_dir, flow, step, bindings)
    record = read_json(_execution_path(run_dir))
    row_id = str(record.get("cycle_row") or "")
    if row_id:
        input_data = dict(input_data)
        input_data["ledger_row"] = row_id
        input_data["row"] = row_id
        cid = str(record.get("cycle_id") or step.get("on_cycle") or "")
        if cid:
            ledger = load_ledger(run_dir, cid)
            if ledger:
                input_data["ledger"] = ledger
    loop = str(step.get("loop") or "none")
    max_attempts = int(step.get("max_attempts") or step.get("max_model_attempts") or 8)
    receipt_rel = step.get("receipt_schema")
    runtime_cache = flow.get("_cache_runtime") if isinstance(flow.get("_cache_runtime"), dict) else {}
    cache_info: dict[str, Any] | None = None
    if step.get("cache"):
        try:
            cache_info = prepare_candidate_cache(
                skill_dir,
                run_dir,
                flow,
                step,
                input_data,
                mode=str(runtime_cache.get("mode") or "off"),
                namespace=str(runtime_cache.get("namespace") or "local"),
                bypass_read=step["id"] in set(runtime_cache.get("bypass_ids") or []),
                row_id=row_id or None,
            )
        except Exception as exc:
            cache_info = {"enabled": False, "candidate": None, "row_id": row_id or None}
            try:
                mark_cache_invalid(
                    run_dir,
                    step,
                    row_id=row_id or None,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
        cached_candidate = cache_info.get("candidate") if isinstance(cache_info, dict) else None
        if isinstance(cached_candidate, dict):
            judged, cache_error = _judge_cached_candidate(skill_dir, step, input_data, cached_candidate)
            if isinstance(judged, dict) and bool((judged.get("receipt") or {}).get("ok")):
                return _pass_artifact(
                    skill_dir,
                    run_dir,
                    flow,
                    step,
                    judged,
                    bindings,
                    fingerprint,
                    1,
                    cache_info,
                )
            if isinstance(judged, dict):
                try:
                    mark_cache_rejected(run_dir, step, row_id=row_id or None, reason=cache_error)
                except Exception:
                    pass
            else:
                try:
                    mark_cache_invalid(run_dir, step, row_id=row_id or None, reason=cache_error)
                except Exception:
                    pass

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
                action = outcome["action"]
                if (
                    is_wait_milestone(step)
                    and action.get("state") == "ACTION_REQUIRED"
                    and not _has_feedback(current_draft, folder / "draft.json", wait_draft_path(run_dir, step["id"]))
                ):
                    return _pause_wait_action(run_dir, flow, step, bindings)
                return action
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
        return _pass_artifact(
            skill_dir, run_dir, flow, step, final, bindings, fingerprint, attempts, cache_info
        )

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
                action = outcome["action"]
                if (
                    is_wait_milestone(step)
                    and action.get("state") == "ACTION_REQUIRED"
                    and not _has_feedback(current_draft, folder / "draft.json", wait_draft_path(run_dir, step["id"]))
                ):
                    return _pause_wait_action(run_dir, flow, step, bindings)
                return action
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
                return _pass_artifact(
                    skill_dir, run_dir, flow, step, result, bindings, fingerprint, attempts, cache_info
                )
        return _write_blocked(
            run_dir, flow, step, bindings, fingerprint, [f"{step['id']}: judge ended without ok receipt"]
        )

    input_data = attach_address(run_dir, step, input_data)
    outcome = _run_handler(skill_dir, run_dir, flow, step, input_data, draft, task, bindings, fingerprint, folder)
    if outcome.get("action") is not None:
        action = outcome["action"]
        if (
            is_wait_milestone(step)
            and action.get("state") == "ACTION_REQUIRED"
            and not _has_feedback(draft, folder / "draft.json", wait_draft_path(run_dir, step["id"]))
        ):
            return _pause_wait_action(run_dir, flow, step, bindings)
        return action
    result = outcome["result"]
    try:
        result = _place_result(run_dir, step, result)
    except FlowError as exc:
        return _write_blocked(run_dir, flow, step, bindings, fingerprint, [str(exc)])
    return _pass_artifact(skill_dir, run_dir, flow, step, result, bindings, fingerprint, 1, cache_info)


def _reference_source(reference: Any) -> str | None:
    if isinstance(reference, str) and reference != "user.request" and "." in reference:
        return reference.split(".", 1)[0]
    if isinstance(reference, dict):
        source = str(reference.get("from") or "")
        return source.split(".", 1)[0] if "." in source else None
    return None


def _judge_cached_candidate(
    skill_dir: Path,
    step: dict[str, Any],
    input_data: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """Apply the current gate to a cached candidate without running candidate FlowSteps."""
    try:
        validate_against_schema(candidate, skill_dir / step["output_schema"])
    except FlowError as exc:
        return None, str(exc)
    if str(step.get("loop") or "none") != "judge":
        return {**candidate, "receipt": {"ok": True, "code": "cache_schema_pass"}}, ""
    worker = str(step.get("worker") or "").strip()
    codebase = infer_codebase(skill_dir)
    if not worker or codebase is None:
        return None, "cached judge worker is unavailable"
    judge_input = dict(input_data)
    judge_input.update(candidate)
    judge_input["candidate"] = candidate
    gem = step.get("gem") or f"references/{step['id']}.md"
    judge_input["gem_path"] = str((skill_dir / str(gem)).resolve())
    try:
        receipt = run_library_tool(codebase, worker, judge_input)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(receipt, dict) or not isinstance(receipt.get("ok"), bool):
        return None, "cached judge did not return boolean ok"
    return {**candidate, "receipt": receipt}, "" if receipt["ok"] else "current judge rejected cached candidate"


def _replacement_ids(flow: dict[str, Any], milestone_id: str) -> set[str]:
    ids = {str(step["id"]) for step in flow["steps"]}
    if milestone_id not in ids:
        raise FlowError(f"unknown --replace-milestone: {milestone_id}")
    edges: dict[str, set[str]] = {mid: set() for mid in ids}
    for step in flow["steps"]:
        target = str(step["id"])
        for reference in (step.get("inputs") or {}).values():
            source = _reference_source(reference)
            if source in edges:
                edges[source].add(target)
        for source in step.get("join") or []:
            if str(source) in edges:
                edges[str(source)].add(target)
    affected = {milestone_id}
    pending = [milestone_id]
    while pending:
        source = pending.pop()
        for target in edges.get(source) or set():
            if target not in affected:
                affected.add(target)
                pending.append(target)
    return affected


def _raw_milestones(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("id") or ""): item
        for item in (raw.get("milestones") or [])
        if isinstance(item, dict) and item.get("id")
    }


def _changed_implementation_owners(
    skill_dir: Path,
    flow: dict[str, Any],
    old_raw: dict[str, Any],
    current_raw: dict[str, Any],
    old_lock: dict[str, Any],
    current_lock: dict[str, Any],
) -> tuple[set[str], list[str]]:
    old_steps = _raw_milestones(old_raw)
    current_steps = _raw_milestones(current_raw)
    changed = {
        milestone_id
        for milestone_id in set(old_steps) | set(current_steps)
        if old_steps.get(milestone_id) != current_steps.get(milestone_id)
    }
    owners: dict[str, set[str]] = {}
    flow_label = f"skill:{Path(flow['_flow_path']).resolve().relative_to(skill_dir.resolve()).as_posix()}"

    def add_owner(label: str, milestone_id: str) -> None:
        owners.setdefault(label, set()).add(milestone_id)

    for source_steps in (old_steps, current_steps):
        for milestone_id, item in source_steps.items():
            for field in ("handler", "input_schema", "output_schema", "draft_schema", "receipt_schema", "gem"):
                relative = item.get(field)
                if relative:
                    add_owner(f"skill:{Path(str(relative)).as_posix()}", milestone_id)
            tool_ids = {str(tool) for tool in (item.get("tools") or []) if tool}
            for flowstep in item.get("flowsteps") or []:
                if isinstance(flowstep, str):
                    tool_ids.add(flowstep)
                elif isinstance(flowstep, dict) and flowstep.get("tool"):
                    tool_ids.add(str(flowstep["tool"]))
            for tool_id in tool_ids:
                prefix = f"project:flowsteps/tools/{tool_id}/"
                for label in set(old_lock.get("files") or {}) | set(current_lock.get("files") or {}):
                    if label.startswith(prefix):
                        add_owner(label, milestone_id)

    unowned: list[str] = []
    old_files = old_lock.get("files") if isinstance(old_lock.get("files"), dict) else {}
    current_files = current_lock.get("files") if isinstance(current_lock.get("files"), dict) else {}
    for label in set(old_files) | set(current_files):
        if old_files.get(label) == current_files.get(label) or label == flow_label:
            continue
        label_owners = owners.get(label) or set()
        if label_owners:
            changed.update(label_owners)
        else:
            unowned.append(label)
    return changed, sorted(unowned)


def _validate_preserved_chosen(run_dir: Path, flow: dict[str, Any], affected: set[str]) -> None:
    for step in flow["steps"]:
        if step["id"] in affected:
            continue
        path = chosen_output_path(run_dir, step["id"])
        if not path.is_file():
            continue
        manifest = load_chosen_output(run_dir, step["id"])
        if manifest.get("output_contract") != step.get("output_contract"):
            raise FlowError(
                f"cannot preserve {step['id']}: output contract changed; continue after edit from that milestone"
            )
        declarations = {str(item["id"]): item for item in step.get("outputs") or []}
        present = {str(item.get("id") or ""): item for item in manifest.get("outputs") or []}
        for output_id, output in present.items():
            declaration = declarations.get(output_id)
            if declaration is None or any(
                output.get(field) != declaration.get(field)
                for field in ("name", "kind", "cardinality", "required")
            ):
                raise FlowError(
                    f"cannot preserve {step['id']}: output port {output_id} changed; "
                    "continue after edit from that milestone"
                )
        missing = [
            output_id
            for output_id, declaration in declarations.items()
            if declaration.get("required") and output_id not in present
        ]
        if missing:
            raise FlowError(
                f"cannot preserve {step['id']}: new required outputs {missing}; "
                "continue after edit from that milestone"
            )


def adopt_implementation_change(
    run_dir: Path,
    skill_dir: Path,
    flow: dict[str, Any],
    milestone_id: str,
) -> tuple[dict[str, Any], set[str]]:
    """Adopt an intentional edit while preserving only compatible upstream chosen state."""
    affected = _replacement_ids(flow, milestone_id)
    lock_path = run_dir / "implementation-lock.json"
    snapshot_path = run_dir / "flow-snapshot.yaml"
    if not lock_path.is_file() or not snapshot_path.is_file():
        raise FlowError("edited-workflow continuation needs implementation-lock.json and flow-snapshot.yaml")
    old_lock = read_json(lock_path)
    current_lock = implementation_lock(skill_dir, flow)
    old_raw = load_yaml(snapshot_path)
    current_raw = load_yaml(Path(flow["_flow_path"]))
    if not isinstance(old_raw, dict) or not isinstance(current_raw, dict):
        raise FlowError("workflow snapshot is invalid; start a fresh run")
    old_ids = [
        str(item.get("id") or "")
        for item in (old_raw.get("milestones") or [])
        if isinstance(item, dict)
    ]
    current_ids = [
        str(item.get("id") or "")
        for item in (current_raw.get("milestones") or [])
        if isinstance(item, dict)
    ]
    if old_ids != current_ids:
        raise FlowError("milestone identity or order changed; start a fresh run")
    old_global = {key: value for key, value in old_raw.items() if key != "milestones"}
    current_global = {key: value for key, value in current_raw.items() if key != "milestones"}
    if old_global != current_global:
        raise FlowError("flow-level configuration changed; start a fresh run")
    changed, unowned = _changed_implementation_owners(
        skill_dir,
        flow,
        old_raw,
        current_raw,
        old_lock,
        current_lock,
    )
    if unowned:
        raise FlowError(f"cannot safely adopt unowned implementation changes: {unowned}")
    outside = sorted(changed - affected)
    if outside:
        raise FlowError(
            f"edited implementation also affects preserved milestones {outside}; "
            f"continue after edit from {outside[0]} or start fresh"
        )
    _validate_preserved_chosen(run_dir, flow, affected)
    receipt = {
        "schema": "m8m_workflow_adoption_v1",
        "run_id": run_dir.name,
        "flow_id": flow["flow_id"],
        "continued_from": milestone_id,
        "changed_milestones": sorted(changed),
        "invalidated_milestones": sorted(affected),
        "previous_fingerprint_sha256": old_lock.get("fingerprint_sha256"),
        "implementation_fingerprint_sha256": current_lock["fingerprint_sha256"],
        "adopted_at": utc_now(),
    }
    write_json(lock_path, current_lock, overwrite=True)
    shutil.copy2(Path(flow["_flow_path"]), snapshot_path)
    write_json(run_dir / "workflow-adoption.json", receipt, overwrite=True)
    record = read_json(_execution_path(run_dir))
    record["implementation_fingerprint_sha256"] = current_lock["fingerprint_sha256"]
    record["updated_at"] = utc_now()
    write_json(_execution_path(run_dir), record, overwrite=True)
    return current_lock, affected


def replace_milestone_state(run_dir: Path, flow: dict[str, Any], milestone_id: str) -> set[str]:
    """Replace one chosen output in place and invalidate every dependent milestone."""
    affected = _replacement_ids(flow, milestone_id)
    for step_id in affected:
        for path in (
            Path(run_dir) / "milestones" / step_id / "out",
            Path(run_dir) / "milestones" / step_id / "work",
            work_dir(run_dir, step_id),
        ):
            if path.exists():
                shutil.rmtree(path)
        for path in (
            expected_artifact_path(run_dir, flow, next(item for item in flow["steps"] if item["id"] == step_id)),
            Path(run_dir) / "materialized" / f"{step_id}.runtime_step_result.json",
            Path(run_dir) / "runtime-tasks" / f"{step_id}.json",
            Path(run_dir) / "milestones" / step_id / "skipped.json",
        ):
            if path.is_file():
                path.unlink()
    record = read_json(_execution_path(run_dir))
    record["steps"] = [item for item in record.get("steps") or [] if item.get("step_id") not in affected]
    record["skipped"] = [item for item in record.get("skipped") or [] if item.get("step_id") not in affected]
    record["status"] = "IN_PROGRESS"
    record.pop("blockers", None)
    for key in ("active_branch", "cycle_id", "cycle_row", "cycle_round", "cycle_done", "cycle_restart"):
        record.pop(key, None)
    record["updated_at"] = utc_now()
    write_json(_execution_path(run_dir), record, overwrite=True)
    manifest_path = Path(run_dir) / "manifest.json"
    if manifest_path.is_file():
        manifest = read_json(manifest_path)
        manifest["chosen_outputs"] = [
            item for item in manifest.get("chosen_outputs") or [] if item.get("milestone") not in affected
        ]
        manifest["skipped"] = [
            item for item in manifest.get("skipped") or [] if item.get("milestone") not in affected
        ]
        manifest["updated_at"] = utc_now()
        write_json(manifest_path, manifest, overwrite=True)
    reset_roster_rows(run_dir, affected)
    ensure_session_tree(run_dir, flow)
    return affected


def advance(
    skill_dir: Path,
    run_dir: Path,
    *,
    request_path: Path | None = None,
    draft_path: Path | None = None,
    flow_arg: str | None = None,
    replace_milestone: str | None = None,
    continue_after_edit: str | None = None,
    cache_mode: str | None = None,
    cache_namespace: str | None = None,
    origin: str = "fresh",
    parent_goal: str | None = None,
    goal_row: str | None = None,
) -> dict[str, Any]:
    skill_dir = skill_dir.resolve()
    run_dir = run_dir.resolve()
    flow = load_flow(skill_dir, find_flow_path(skill_dir, flow_arg) if flow_arg else None)
    requested_cache_mode = validate_cache_mode(cache_mode) if cache_mode is not None else None
    initialize_run(
        run_dir,
        skill_dir,
        flow,
        request_path,
        cache_mode=requested_cache_mode,
        cache_namespace=cache_namespace,
        origin=origin,
        parent_goal=parent_goal,
        goal_row=goal_row,
    )
    if replace_milestone and continue_after_edit:
        raise FlowError("use either --replace-milestone or --continue-after-edit, not both")
    adopted: set[str] = set()
    if continue_after_edit:
        lock, adopted = adopt_implementation_change(run_dir, skill_dir, flow, continue_after_edit)
    else:
        lock = assert_implementation_lock(run_dir, skill_dir, flow)
    record = read_json(_execution_path(run_dir))
    frozen_cache = record.get("cache") if isinstance(record.get("cache"), dict) else {"mode": "off", "namespace": "local"}
    frozen_mode = validate_cache_mode(str(frozen_cache.get("mode") or "off"))
    frozen_namespace = str(frozen_cache.get("namespace") or "local")
    if requested_cache_mode is not None and requested_cache_mode != frozen_mode:
        raise FlowError(f"run cache mode is frozen as {frozen_mode}; start a fresh run")
    if cache_namespace is not None and str(cache_namespace) != frozen_namespace:
        raise FlowError("run cache namespace is frozen; start a fresh run")
    flow["_cache_runtime"] = {
        "mode": frozen_mode,
        "namespace": frozen_namespace,
        "bypass_ids": [],
    }
    if replace_milestone:
        affected = replace_milestone_state(run_dir, flow, replace_milestone)
        flow["_cache_runtime"]["bypass_ids"] = sorted(affected)
    elif continue_after_edit:
        replace_milestone_state(run_dir, flow, continue_after_edit)
        flow["_cache_runtime"]["bypass_ids"] = sorted(adopted)
    record = read_json(_execution_path(run_dir))
    if record["status"] == "BLOCKED":
        raise FlowError("run is terminal BLOCKED; start a fresh run")
    created = record.get("created_at")
    if created and (datetime.now(timezone.utc) - _parse_time(str(created))).total_seconds() > flow["max_run_seconds"]:
        raise FlowError("run exceeded the frozen wall-clock budget; start a fresh run")
    pending_draft = read_json(draft_path) if draft_path else None
    if draft_path and not isinstance(pending_draft, dict):
        raise FlowError("--draft must contain one JSON object")
    roster = load_run_roster(run_dir)
    if roster and str(roster.get("status") or "") == "paused":
        parked = waiting_roster_row(roster)
        mid = str((parked or {}).get("id") or roster.get("current") or "")
        step = next((item for item in flow["steps"] if item["id"] == mid), None)
        if step is not None:
            if pending_draft is not None:
                _write_wait_draft(run_dir, step, pending_draft)
            if not _has_feedback(
                pending_draft,
                wait_draft_path(run_dir, step["id"]),
                work_dir(run_dir, step["id"]) / "draft.json",
            ):
                return _isolate_action(run_dir, skill_dir, flow, _pause_wait_action(run_dir, flow, step, []))
            resume_run(run_dir)
    restart = True
    while restart:
        restart = False
        record = read_json(_execution_path(run_dir))
        completed = {item["step_id"] for item in record["steps"]}
        skipped = {str(item.get("step_id") or "") for item in (record.get("skipped") or [])}
        for step in flow["steps"]:
            artifact_path = expected_artifact_path(run_dir, flow, step)
            chosen_path = chosen_output_path(run_dir, step["id"])
            if step["id"] in skipped:
                continue
            if record.get("cycle_done") and step.get("on_cycle") and not step.get("cycle"):
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
                mark_roster_row(run_dir, step["id"], status="skipped")
                continue
            if step["id"] in completed:
                chosen = load_chosen_output(run_dir, step["id"])
                validate_against_schema(chosen, chosen_output_schema_path())
                continue
            if chosen_path.is_file():
                chosen = load_chosen_output(run_dir, step["id"])
                validate_against_schema(chosen, chosen_output_schema_path())
                record = read_json(_execution_path(run_dir))
                record["steps"].append(
                    {"step_id": step["id"], "status": "PASS", "attempt": 0, "resumed_from_chosen": True}
                )
                record["updated_at"] = utc_now()
                write_json(_execution_path(run_dir), record, overwrite=True)
                mark_roster_row(run_dir, step["id"], status="done", slot=relative_to(run_dir, chosen_path))
                completed.add(step["id"])
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
            elif is_wait_milestone(step):
                for candidate in (
                    wait_draft_path(run_dir, step["id"]),
                    work_dir(run_dir, step["id"]) / "draft.json",
                ):
                    if candidate.is_file():
                        loaded = read_json(candidate)
                        if isinstance(loaded, dict) and loaded:
                            draft = loaded
                            break
            elif step["model"] != "none":
                existing_draft = work_dir(run_dir, step["id"]) / "draft.json"
                if existing_draft.is_file():
                    draft = read_json(existing_draft)
            action = _execute_step(skill_dir, run_dir, flow, step, lock["fingerprint_sha256"], draft)
            if action is not None:
                return _isolate_action(run_dir, skill_dir, flow, action)
            record = read_json(_execution_path(run_dir))
            completed = {item["step_id"] for item in record["steps"]}
            if record.get("cycle_restart"):
                record["cycle_restart"] = False
                write_json(_execution_path(run_dir), record, overwrite=True)
                restart = True
                break
    record = read_json(_execution_path(run_dir))
    record["status"] = "COMPLETE"
    record["updated_at"] = utc_now()
    write_json(_execution_path(run_dir), record)
    mark_roster_complete(run_dir)
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
    parser.add_argument(
        "--run-mode",
        choices=["fresh", "resume"],
        default="fresh",
        help="fresh is the default and refuses an existing session; resume reuses one exact run folder",
    )
    parser.add_argument("--request", type=Path)
    parser.add_argument("--draft", type=Path)
    parser.add_argument("--flow")
    parser.add_argument(
        "--replace-milestone",
        help="Replace one chosen milestone output and invalidate its downstream milestones",
    )
    parser.add_argument(
        "--continue-after-edit",
        metavar="MILESTONE",
        help=(
            "Adopt intentional workflow edits, preserve compatible upstream chosen outputs, "
            "and invalidate this milestone plus its dependents"
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Legacy alias for --run-mode resume; find a paused run when --run-dir is omitted",
    )
    parser.add_argument(
        "--cache-mode",
        choices=["off", "read", "write", "read-write"],
        default=None,
        help="Cross-run candidate cache mode. New runs default to off and freeze this value.",
    )
    parser.add_argument(
        "--cache-namespace",
        default=None,
        help="Cache partition. New local runs default to local; platform launchers must pass the tenant id.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    skill_dir = harness_dir_from_args(args)
    run_dir = args.run_dir
    try:
        run_mode = "resume" if args.resume or args.replace_milestone or args.continue_after_edit else args.run_mode
        if run_dir is None:
            flow = load_flow(skill_dir, find_flow_path(skill_dir, args.flow) if args.flow else None)
            codebase = infer_codebase(skill_dir)
            if run_mode == "resume":
                found = find_paused_run(codebase or skill_dir, str(flow.get("flow_id") or ""))
                if found is None:
                    raise FlowError("resume requires --run-dir or one paused run roster")
                run_dir = found
            else:
                if codebase is None:
                    raise FlowError("pass --run-dir, or run a product flow under flowsteps/flows/<id>")
                run_dir = default_run_dir(codebase, str(flow.get("flow_id") or "flow"))
        else:
            run_dir = run_dir.resolve()
            execution_exists = (run_dir / "flow-execution-record.json").is_file()
            # The product launcher creates the lock and request bootstrap
            # files before invoking the fresh runner.  They are not prior run
            # state; every other pre-existing entry remains a hard refusal.
            bootstrap = {".run.lock", "request.json"}
            nonempty = run_dir.is_dir() and any(item.name not in bootstrap for item in run_dir.iterdir())
            if run_mode == "fresh" and nonempty:
                raise FlowError(
                    "fresh rerun refuses a non-empty folder; omit --run-dir or choose a new folder"
                )
            if run_mode == "resume" and not execution_exists:
                parked = find_paused_run(run_dir)
                if parked is not None:
                    run_dir = parked
                else:
                    raise FlowError("resume requires an existing M8M run folder")
        result = advance(
            skill_dir,
            run_dir,
            request_path=args.request,
            draft_path=args.draft,
            flow_arg=args.flow,
            replace_milestone=args.replace_milestone,
            continue_after_edit=args.continue_after_edit,
            cache_mode=args.cache_mode,
            cache_namespace=args.cache_namespace,
        )
        if isinstance(result, dict):
            result["run_dir"] = str(Path(run_dir).resolve())
    except FlowError as exc:
        print(json.dumps(_blocked_action("", [str(exc)]), indent=2), file=sys.stderr)
        return 3
    print(json.dumps(result, indent=2))
    return 0 if result["state"] in {"ACTION_REQUIRED", "COMPLETE"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
