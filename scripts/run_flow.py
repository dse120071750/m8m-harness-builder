"""Advance an M8M v4 run and expose only admitted, optionally judged chosen outputs."""

from __future__ import annotations

import argparse
import contextvars
import faulthandler
import functools
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_BROWSER_EVIDENCE_BOOTSTRAP_FILES = {
    "source-article.html",
    "source-article.txt",
    "source-browser-capture-receipt.json",
    "source-full-page.png",
    "source-visible-semantic-blocks.json",
}


def _is_admissible_browser_evidence_bootstrap(run_dir: Path, bootstrap: set[str]) -> bool:
    """Admit one hash-bound browser capture without admitting prior run state."""
    entries = {item.name for item in run_dir.iterdir()}
    evidence = entries - bootstrap
    if not evidence:
        return True
    if evidence != _BROWSER_EVIDENCE_BOOTSTRAP_FILES:
        return False
    receipt_path = run_dir / "source-browser-capture-receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if receipt.get("schema") != "article_infographic_source_browser_capture_receipt_v1":
        return False
    if receipt.get("status") != "PASS":
        return False
    if not str(receipt.get("requested_url") or "").startswith(("http://", "https://")):
        return False
    if not str(receipt.get("final_url") or "").startswith(("http://", "https://")):
        return False
    hashes = receipt.get("hashes")
    if not isinstance(hashes, dict):
        return False
    for name in _BROWSER_EVIDENCE_BOOTSTRAP_FILES - {receipt_path.name}:
        expected = str(hashes.get(name) or "").lower()
        if len(expected) != 64:
            return False
        try:
            actual = hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
        except OSError:
            return False
        if actual != expected:
            return False
    return True

from gem_text import read_gem_section
from session_layout import (
    admit_candidate,
    assert_in_run,
    attach_address,
    build_run_storage_contract,
    chosen_output_path,
    cycle_id_of,
    default_harness_root,
    default_run_dir,
    discard_incomplete_chosen_output,
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
    materialize_request_file_refs,
    normalize_judge_receipt,
    judge_receipt_path,
    recover_incomplete_chosen_output,
    validate_run_storage_contract,
    infer_harness_root_from_run,
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
    validate_fresh_harness_root,
    validate_fresh_run_dir,
)
from milestone_expectation import (
    build_milestone_judge_request,
    derive_milestone_expectation,
)
from flowstep_runtime import (
    ACTION_SCHEMA,
    NEED_MODEL,
    FlowError,
    add_harness_location_args,
    assert_external_phase_journal,
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
    local_tool_package_name,
    make_envelope,
    recovery_model,
    read_json,
    relative_to,
    runtime_package_name,
    sha256_file,
    run_context_schema_path,
    source_asset_manifest_schema_path,
    utc_now,
    validate_against_schema,
    work_dir,
    write_json,
)
from runtime_release import RuntimeReleaseError, bind_runtime_to_run


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _execution_path(run_dir: Path) -> Path:
    return run_dir / "flow-execution-record.json"


def _expectation_schema_path() -> Path:
    return Path(__file__).resolve().parents[1] / "contracts" / "m8m_milestone_expectation_v1.schema.json"


def _judge_request_schema_path() -> Path:
    return Path(__file__).resolve().parents[1] / "contracts" / "m8m_milestone_judge_request_v1.schema.json"


def _progress_path(run_dir: Path) -> Path:
    return run_dir / "progress.json"


_ACTIVE_WINDOW_STARTED: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "m8m_active_window_started", default=None
)


_CACHE_MODES = {"off", "read", "write", "read-write"}
_WAIT_TOKENS = {"response", "reply", "confirm", "wait"}
_MAX_PRIOR_FEEDBACK_ITEMS = 8
_MAX_PRIOR_FEEDBACK_ITEM_CHARS = 512
_MAX_PRIOR_FEEDBACK_TOTAL_CHARS = 4096
_MAX_CONTROL_REQUEST_BYTES = 1024 * 1024


def infer_codebase(skill_dir: Path) -> Path:
    from flowstep_tools import infer_codebase as implementation

    return implementation(skill_dir)


def run_library_tool(codebase: Path, tool_id: str, input_data: dict[str, Any]) -> dict[str, Any]:
    from flowstep_tools import run_library_tool as implementation

    return implementation(codebase, tool_id, input_data)


def validate_cache_mode(mode: str) -> str:
    """Validate the lightweight run option without importing cache machinery."""
    value = str(mode or "off").strip().lower()
    if value not in _CACHE_MODES:
        raise FlowError(f"cache mode must be one of {sorted(_CACHE_MODES)}")
    return value


def prepare_candidate_cache(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from candidate_cache import prepare_candidate_cache as implementation

    return implementation(*args, **kwargs)


def mark_cache_invalid(*args: Any, **kwargs: Any) -> None:
    from candidate_cache import mark_cache_invalid as implementation

    implementation(*args, **kwargs)


def mark_cache_rejected(*args: Any, **kwargs: Any) -> None:
    from candidate_cache import mark_cache_rejected as implementation

    implementation(*args, **kwargs)


def store_chosen_candidate(
    skill_dir: Path,
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    chosen: dict[str, Any],
    cache_info: dict[str, Any] | None,
    *,
    mode: str,
) -> None:
    # Every non-cache run used to import the full candidate cache package here
    # after a successful milestone even though there was nothing to store.
    if not cache_info or not cache_info.get("enabled"):
        return
    from candidate_cache import store_chosen_candidate as implementation

    implementation(
        skill_dir,
        run_dir,
        flow,
        step,
        chosen,
        cache_info,
        mode=mode,
    )


def is_wait_milestone(item: dict[str, Any]) -> bool:
    name = str(item.get("id") or "").lower().replace("-", "_")
    return bool(set(name.split("_")) & _WAIT_TOKENS)


def ledger_items(data: dict[str, Any], path: str) -> list[Any]:
    from schema_gate import ledger_items as implementation

    return implementation(data, path)


def read_receipt(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from schema_gate import read_receipt as implementation

    return implementation(*args, **kwargs)


def schema_accepts(instance: Any, schema: dict[str, Any] | Path) -> bool:
    from schema_gate import schema_accepts as implementation

    return implementation(instance, schema)


def _active_seconds(record: dict[str, Any]) -> float:
    total = float(record.get("active_seconds") or 0.0)
    started = _ACTIVE_WINDOW_STARTED.get()
    if started is not None:
        total += max(0.0, time.monotonic() - started)
    return total


def _track_active_time(func):
    """Count only time spent inside explicit advance calls.

    Time parked for a model, user response, tunnel repair, or other
    infrastructure recovery is deliberately outside the run budget.
    """

    @functools.wraps(func)
    def wrapped(skill_dir: Path, run_dir: Path, *args: Any, **kwargs: Any):
        started = time.monotonic()
        token = _ACTIVE_WINDOW_STARTED.set(started)
        try:
            return func(skill_dir, run_dir, *args, **kwargs)
        finally:
            elapsed = max(0.0, time.monotonic() - started)
            _ACTIVE_WINDOW_STARTED.reset(token)
            record_path = _execution_path(Path(run_dir))
            try:
                if record_path.is_file():
                    record = read_json(record_path)
                    record["active_seconds"] = round(
                        float(record.get("active_seconds") or 0.0) + elapsed,
                        6,
                    )
                    record["last_active_window_seconds"] = round(elapsed, 6)
                    record["updated_at"] = utc_now()
                    write_json(record_path, record, overwrite=True)
            except (OSError, FlowError, ValueError, TypeError):
                # Never mask the real workflow result with secondary budget
                # bookkeeping failure. The next explicit resume can retry it.
                pass

    return wrapped


def initialize_run(
    run_dir: Path,
    skill_dir: Path,
    flow: dict[str, Any],
    request_path: Path | None,
    *,
    harness_root: Path | None = None,
    source_code_root: Path | None = None,
    cache_mode: str | None = None,
    cache_namespace: str | None = None,
    origin: str = "fresh",
    parent_goal: str | None = None,
    goal_row: str | None = None,
) -> dict[str, Any] | None:
    # A durable execution record is written only after the complete session
    # tree has been initialized. Re-running every nested mkdir on resume is
    # redundant and can block for minutes on Windows when metadata access is
    # temporarily serialized by another process or filesystem filter.
    execution_exists = _execution_path(run_dir).is_file()
    if execution_exists:
        return None
    run_dir.mkdir(parents=True, exist_ok=True)
    ensure_session_tree(run_dir, flow)
    execution_root = Path(
        os.path.abspath(str(harness_root or infer_harness_root_from_run(run_dir)))
    )
    inferred_source_root = source_code_root or infer_codebase(skill_dir) or skill_dir
    storage_contract = build_run_storage_contract(
        Path(inferred_source_root), execution_root, run_dir
    )
    target = run_dir / "request.json"
    if not target.exists():
        if request_path is None:
            raise FlowError("a new run requires --request")
        source = Path(os.path.abspath(str(request_path)))
        if not source.is_file():
            raise FlowError(f"request not found: {source}")
        request_payload = materialize_request_file_refs(
            run_dir,
            read_json(source),
            source_base=source.parent,
        )
        write_json(target, request_payload, overwrite=False)
    elif not (run_dir / "source-assets-manifest.json").is_file():
        request_payload = materialize_request_file_refs(
            run_dir,
            read_json(target),
            source_base=target.parent,
        )
        write_json(target, request_payload, overwrite=True)
    validate_against_schema(
        read_json(run_dir / "source-assets-manifest.json"),
        source_asset_manifest_schema_path(),
    )
    lock_path = run_dir / "implementation-lock.json"
    lock_payload: dict[str, Any]
    if not lock_path.exists():
        lock_payload = implementation_lock(skill_dir, flow)
        write_json(lock_path, lock_payload, overwrite=False)
    else:
        lock_payload = read_json(lock_path)
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
            "schema": "m8m_run_context_v2",
            "run_id": run_dir.name,
            "flow_id": flow["flow_id"],
            "origin": origin,
            "context_policy": "isolated",
            "chat_history_allowed": False,
            "cross_run_cache_mode": context_cache_mode,
            "request_path": "request.json",
            "storage_contract": storage_contract,
            "source_asset_manifest_path": "source-assets-manifest.json",
            "created_at": utc_now(),
        }
        if parent_goal:
            context["parent_goal"] = str(parent_goal)
        if goal_row:
            context["goal_row"] = str(goal_row)
        validate_against_schema(context, run_context_schema_path(context["schema"]))
        write_json(context_path, context, overwrite=False)
    else:
        existing_context = read_json(context_path)
        validate_against_schema(
            existing_context,
            run_context_schema_path(str(existing_context.get("schema") or "")),
        )
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
                "implementation_fingerprint_sha256": lock_payload["fingerprint_sha256"],
                "repair_cycles": 0,
                "active_seconds": 0.0,
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
    return lock_payload


def _bounded_prior_feedback(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a small, deterministic retry hint without reopening the judge ABI."""

    if not isinstance(receipt, dict):
        return None
    values: dict[str, list[str]] = {"reasons": [], "blockers": []}
    total_items = 0
    used = 0
    for field in values:
        raw = receipt.get(field)
        rows = raw if isinstance(raw, list) else []
        for item in rows:
            remaining = _MAX_PRIOR_FEEDBACK_TOTAL_CHARS - used
            if remaining <= 0 or total_items >= _MAX_PRIOR_FEEDBACK_ITEMS:
                break
            text = " ".join(str(item or "").split())[
                : min(_MAX_PRIOR_FEEDBACK_ITEM_CHARS, remaining)
            ]
            if text and text not in values[field]:
                values[field].append(text)
                total_items += 1
                used += len(text)
        if used >= _MAX_PRIOR_FEEDBACK_TOTAL_CHARS or total_items >= _MAX_PRIOR_FEEDBACK_ITEMS:
            break
    if not values["reasons"] and not values["blockers"]:
        return None
    return {
        "judge_attempt": max(1, int(receipt.get("attempt") or 1)),
        "reasons": values["reasons"],
        "blockers": values["blockers"],
    }


def _candidate_task_path(
    run_dir: Path,
    step_id: str,
    attempt: int,
    *,
    row_id: str | None = None,
) -> Path:
    root = run_dir / "runtime-tasks" / str(step_id)
    if str(row_id or "").strip():
        root = root / "items" / str(row_id)
    return root / f"attempt-{max(1, int(attempt)):03d}" / "candidate-request.json"


def _task_cycle_row(task: dict[str, Any]) -> str | None:
    inputs = task.get("inputs") if isinstance(task.get("inputs"), dict) else {}
    row_id = str(inputs.get("ledger_row") or "").strip()
    return row_id or None


def _task(
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    bindings: list[dict[str, Any]],
    attempt: int,
    *,
    inputs: dict[str, Any] | None = None,
    prior_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expected = expected_artifact_path(run_dir, flow, step)
    record_path = _execution_path(run_dir)
    record = read_json(record_path) if record_path.is_file() else {}
    task = {
        "schema": "runtime_task_v2",
        "run_id": run_dir.name,
        "flow_id": flow["flow_id"],
        "flow_version": flow["version"],
        "step_id": step["id"],
        "handler": step["handler"],
        "model": step["model"],
        "attempt": attempt,
        "max_attempts": (
            int(step.get("max_attempts") or step.get("max_model_attempts") or 1)
            if str(step.get("loop") or "none") in {"judge", "for"}
            or step.get("on_cycle")
            or step.get("cycle")
            else 1
        ),
        "expectation": derive_milestone_expectation(step),
        "execution": step.get("execution") or {},
        "output_contract": step["output_contract"],
        "expected_output_path": relative_to(run_dir, expected),
        "input_artifacts": bindings,
        "inputs": inputs or {},
        "prior_feedback": prior_feedback,
        "active_seconds_at_start": _active_seconds(record),
        "created_at": utc_now(),
    }
    return task


def _candidate_task(
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    bindings: list[dict[str, Any]],
    inputs: dict[str, Any],
    attempt: int,
    *,
    prior_feedback: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    """Freeze one closed candidate request for one milestone attempt."""

    task = _task(
        run_dir,
        flow,
        step,
        bindings,
        attempt,
        inputs=inputs,
        prior_feedback=prior_feedback,
    )
    path = _candidate_task_path(
        run_dir,
        step["id"],
        attempt,
        row_id=_task_cycle_row(task),
    )
    if path.is_file():
        existing = read_json(path)
        comparable = dict(existing)
        comparable["created_at"] = task["created_at"]
        comparable["active_seconds_at_start"] = task["active_seconds_at_start"]
        if comparable != task:
            raise FlowError(
                f"{step['id']}: attempt {attempt} candidate request conflicts with frozen run state"
            )
        task = existing
    else:
        write_json(path, task, overwrite=False)
    return task, path


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
    materialized_path = run_dir / "materialized" / f"{step['id']}.runtime_step_result.json"
    if materialized_path.is_file():
        existing = read_json(materialized_path)
        for field in ("run_id", "flow_id", "step_id", "status", "artifact_path", "artifact_sha256"):
            if existing.get(field) != result.get(field):
                raise FlowError(
                    f"{step['id']}: materialized execution residue does not match the durable artifact"
                )
        result = existing
    else:
        write_json(materialized_path, result, overwrite=False)
    record = read_json(_execution_path(run_dir))
    prior = next(
        (item for item in record.get("steps") or [] if item.get("step_id") == step["id"]),
        None,
    )
    if prior is not None:
        if (
            prior.get("status") != artifact["status"]
            or prior.get("artifact_sha256") != result["artifact_sha256"]
        ):
            raise FlowError(f"{step['id']}: execution record conflicts with the durable artifact")
    else:
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


def _restore_orphan_artifact(
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
) -> dict[str, Any] | None:
    """Reconcile a durable envelope whose execution-record write was interrupted."""

    artifact_path = expected_artifact_path(run_dir, flow, step)
    chosen_path = chosen_output_path(run_dir, step["id"])
    if not artifact_path.is_file() or chosen_path.is_file():
        return None
    artifact = read_json(artifact_path)
    validate_against_schema(artifact, envelope_schema_path())
    if artifact.get("status") == "BLOCKED":
        _materialize(run_dir, flow, step, artifact, artifact_path)
        mark_roster_blocked(run_dir, step["id"])
        return _blocked_action(
            step["id"], [str(item) for item in artifact.get("blockers") or []]
        )
    chosen = artifact.get("data")
    if not isinstance(chosen, dict) or chosen.get("schema") != "m8m_chosen_output_v1":
        raise FlowError(
            f"{step['id']}: PASS artifact exists without a recoverable chosen manifest"
        )
    validate_against_schema(chosen, chosen_output_schema_path())
    write_json(chosen_path, chosen, overwrite=False)
    load_chosen_output(
        run_dir,
        step["id"],
        step=step,
        skill_dir=Path(flow["_skill_dir"]),
    )
    return None


def _resume_chosen_step(
    skill_dir: Path,
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    fingerprint: str,
) -> dict[str, Any] | None:
    """Finish the commit record for a valid chosen output, without rerunning its tool."""

    chosen = load_chosen_output(
        run_dir,
        step["id"],
        step=step,
        skill_dir=skill_dir,
    )
    validate_against_schema(chosen, chosen_output_schema_path())
    receipt_path = run_dir / str(chosen.get("judge_receipt") or "")
    final_receipt = read_json(receipt_path)
    final_attempt = max(1, int(final_receipt.get("attempt") or 1))
    artifact_path = expected_artifact_path(run_dir, flow, step)
    task_path = run_dir / "runtime-tasks" / f"{step['id']}.json"
    task = read_json(task_path) if task_path.is_file() else {}
    bindings = list(task.get("input_artifacts") or [])
    if not bindings:
        _, bindings = bind_inputs(run_dir, flow, step)
    if artifact_path.is_file():
        artifact = read_json(artifact_path)
        validate_against_schema(artifact, envelope_schema_path())
        if artifact.get("status") != "PASS" or artifact.get("data") != chosen:
            raise FlowError(
                f"{step['id']}: durable artifact conflicts with the chosen output"
            )
    else:
        artifact = make_envelope(
            flow=flow,
            step=step,
            run_id=run_dir.name,
            attempt=final_attempt,
            status="PASS",
            data=chosen,
            bindings=bindings,
            fingerprint=fingerprint,
            blockers=[],
            chosen_output=relative_to(run_dir, chosen_output_path(run_dir, step["id"])),
        )
        write_json(artifact_path, artifact, overwrite=False)
        validate_against_schema(artifact, envelope_schema_path())
    record_chosen_output(run_dir, step, chosen)
    _materialize(run_dir, flow, step, artifact, artifact_path)
    mark_roster_row(
        run_dir,
        step["id"],
        status="done",
        slot=relative_to(run_dir, chosen_output_path(run_dir, step["id"])),
    )

    resolved = resolve_chosen_output(run_dir, step["id"])
    result = resolved.get("result") if isinstance(resolved, dict) else resolved
    result = dict(result) if isinstance(result, dict) else {"value": result}
    if receipt_path.is_file():
        # The committed judge receipt is the control-plane authority on
        # resume.  Candidate payloads may contain a legacy receipt for human
        # inspection, but branch/cycle reconciliation must use the normalized
        # receipt that was committed with the chosen output.
        result["receipt"] = final_receipt
    if step.get("branch"):
        _store_active_branch(run_dir, step, result)
    if step.get("cycle"):
        return _apply_cycle(
            skill_dir,
            run_dir,
            flow,
            step,
            result,
            bindings,
            fingerprint,
        )
    return None


def _blocked_action(step_id: str, blockers: list[str]) -> dict[str, Any]:
    return {"schema": ACTION_SCHEMA, "state": "BLOCKED", "step_id": step_id, "blockers": blockers}


def _scandir_entry(path: Path, cache: dict[Path, dict[str, os.DirEntry[str]]]) -> os.DirEntry[str] | None:
    """Resolve one path from a cached parent enumeration without Path.stat."""
    parent = path.parent
    entries = cache.get(parent)
    if entries is None:
        try:
            with os.scandir(parent) as directory:
                entries = {entry.name.casefold(): entry for entry in directory}
        except FileNotFoundError:
            entries = {}
        cache[parent] = entries
    return entries.get(path.name.casefold())


def _isolate_action(
    run_dir: Path,
    skill_dir: Path,
    flow: dict[str, Any],
    action: dict[str, Any],
) -> dict[str, Any]:
    """Attach a no-history context capsule to model work exposed by the runtime."""
    if action.get("state") != "ACTION_REQUIRED":
        return action
    if action.get("action") == "retry_tool_then_resume":
        return {**action, "context_policy": "isolated"}
    step_id = str(action.get("step_id") or "")
    step = next((item for item in flow["steps"] if item["id"] == step_id), None)
    if step is None:
        raise FlowError(f"ACTION_REQUIRED references unknown milestone: {step_id}")
    expectation = derive_milestone_expectation(step)
    action_task_ref = action.get("task_path")
    task_path = (
        assert_in_run(run_dir, action_task_ref)
        if isinstance(action_task_ref, str) and action_task_ref
        else run_dir / "runtime-tasks" / f"{step_id}.json"
    )
    task: dict[str, Any] = {}
    if task_path.is_file():
        task = read_json(task_path)
        task_expectation = task.get("expectation")
        if task_expectation != expectation:
            raise FlowError(
                f"{step_id}: runtime task expectation differs from the current derived expectation"
            )
        if (task.get("execution") or {}) != (step.get("execution") or {}):
            raise FlowError(
                f"{step_id}: runtime task execution binding differs from the current workflow"
            )
    allowed: set[str] = {str(Path(os.path.abspath(str(run_dir / "run-context.json"))))}
    for key in ("task_path", "model_request_path"):
        raw = action.get(key)
        if isinstance(raw, str) and raw:
            allowed.add(str(assert_in_run(run_dir, raw)))
    request_raw = action.get("model_request_path")
    if isinstance(request_raw, str) and request_raw:
        request_path = assert_in_run(run_dir, request_raw)
        if request_path.is_file():
            request = read_json(request_path)
            scandir_cache: dict[Path, dict[str, os.DirEntry[str]]] = {}
            def add_declared_file(raw: Any) -> None:
                if not isinstance(raw, str) or not raw:
                    return
                declared_path = Path(os.path.abspath(raw))
                entry = _scandir_entry(declared_path, scandir_cache)
                if entry is not None and entry.is_file(follow_symlinks=True):
                    allowed.add(str(declared_path))

            if isinstance(request, dict):
                add_declared_file(request.get("read"))
                add_declared_file(request.get("seed_path"))
            references = request.get("references") if isinstance(request, dict) else None
            if isinstance(references, str) and references:
                reference_root = Path(os.path.abspath(references))
                reference_entry = _scandir_entry(reference_root, scandir_cache)
                if reference_entry is not None and reference_entry.is_dir(follow_symlinks=True):
                    with os.scandir(reference_root) as directory:
                        reference_files = sorted(
                            (
                                entry for entry in directory
                                if entry.name.casefold().endswith(".md")
                                and entry.is_file(follow_symlinks=True)
                            ),
                            key=lambda entry: entry.name.casefold(),
                        )
                    for reference_file in reference_files:
                        allowed.add(str(Path(os.path.abspath(reference_file.path))))
                else:
                    add_declared_file(references)
            elif isinstance(references, list):
                for reference in references:
                    add_declared_file(reference)
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
            path = Path(os.path.abspath(str(skill_dir / str(relative))))
            if path.is_file():
                allowed.add(str(path))
    draft_raw = str(action.get("draft_path") or f"work/{step_id}/draft.json")
    draft_path = assert_in_run(run_dir, draft_raw)
    capsule = {
        "schema": "m8m_context_capsule_v1",
        "run_id": run_dir.name,
        "flow_id": flow["flow_id"],
        "milestone_id": step_id,
        "attempt": max(1, int(task.get("attempt") or action.get("attempt") or 1)),
        "prior_feedback": task.get("prior_feedback"),
        "context_policy": "isolated",
        "chat_history_allowed": False,
        "expectation": expectation,
        "allowed_files": sorted(allowed),
        "write_file": str(draft_path),
        "instruction": (
            "Start a fresh no-history model worker. Read only allowed_files, treat them as the complete "
            "authority, and write only write_file. Do not use the parent chat, prior runs, or remembered assets."
        ),
        "created_at": utc_now(),
    }
    validate_against_schema(capsule, context_capsule_schema_path())
    attempt = max(1, int(task.get("attempt") or action.get("attempt") or 1))
    capsule_path = task_path.parent / "context-capsule.json"
    if capsule_path.is_file():
        existing = read_json(capsule_path)
        comparable = dict(existing)
        comparable["created_at"] = capsule["created_at"]
        if comparable != capsule:
            differing_keys = sorted(
                key for key in set(comparable) | set(capsule)
                if comparable.get(key) != capsule.get(key)
            )
            raise FlowError(
                f"{step_id}: attempt {attempt} context capsule conflicts with frozen run state; "
                f"differing keys: {', '.join(differing_keys)}"
            )
    else:
        write_json(capsule_path, capsule, overwrite=False)
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
            "resume this milestone. Its FlowSteps use the Gem; any declared judge reads "
            "only the derived expectation, resolved inputs, and current candidate. Write the reply to "
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
        "run_dir": os.path.abspath(str(run_dir)),
        "roster_path": "roster.json",
        "task_path": relative_to(run_dir, task_path),
        "model_request_path": relative_to(run_dir, request_path),
        "draft_path": slot,
        "draft_schema_path": step.get("draft_schema"),
        "expected_output_path": relative_to(run_dir, expected_artifact_path(run_dir, flow, step)),
        "input_artifacts": bindings,
        "tools": step.get("tools") or [],
    }


def _observed_milestone_attempt(
    run_dir: Path,
    step: dict[str, Any],
    preferred: int | None = None,
) -> int:
    values = [max(1, int(preferred))] if preferred is not None else [1]
    public = work_dir(run_dir, step["id"])
    for name in ("judge_state.json", "ledger_state.json", "tool_failed.json", "model_failed.json"):
        path = public / name
        if not path.is_file():
            continue
        try:
            row = read_json(path)
            value = row.get("attempts") if isinstance(row, dict) else None
            if isinstance(value, int) and value > 0:
                values.append(value)
        except FlowError:
            continue
    task_path = run_dir / "runtime-tasks" / f"{step['id']}.json"
    if task_path.is_file():
        try:
            task = read_json(task_path)
            value = task.get("attempt") if isinstance(task, dict) else None
            if isinstance(value, int) and value > 0:
                values.append(value)
        except FlowError:
            pass
    return max(values)


def _record_judge_attempt(
    run_dir: Path,
    step: dict[str, Any],
    result: dict[str, Any],
    receipt: dict[str, Any] | None,
    *,
    attempt: int,
    decision: str,
) -> None:
    folder = (
        run_dir
        / "milestones"
        / str(step["id"])
        / "work"
        / "attempts"
        / f"attempt-{max(1, int(attempt)):03d}"
    )
    normalized = normalize_judge_receipt(
        step,
        receipt,
        attempt=attempt,
        decision=decision,
        blockers=(receipt.get("blockers") if isinstance(receipt, dict) else None),
    )
    write_json(folder / "candidate.json", result, overwrite=True)
    write_json(folder / "judge-receipt.json", normalized, overwrite=True)


def _write_blocked(
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    bindings: list[dict[str, Any]],
    fingerprint: str,
    blockers: list[str],
    *,
    attempt: int | None = None,
    receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    actual_attempt = _observed_milestone_attempt(run_dir, step, attempt)
    discard_incomplete_chosen_output(run_dir, step["id"])
    final_receipt = normalize_judge_receipt(
        step,
        receipt,
        attempt=actual_attempt,
        decision="BLOCKED",
        blockers=blockers,
    )
    write_json(judge_receipt_path(run_dir, step["id"]), final_receipt, overwrite=False)
    artifact = make_envelope(
        flow=flow,
        step=step,
        run_id=run_dir.name,
        attempt=actual_attempt,
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
    task: dict[str, Any],
) -> dict[str, Any]:
    folder = work_dir(run_dir, step["id"])
    folder.mkdir(parents=True, exist_ok=True)
    policy = str(step.get("on_tool_fail") or "BLOCKED")
    if policy == "retryable":
        fail_path = folder / "tool_failed.json"
        record = read_json(fail_path) if fail_path.is_file() else {"attempts": 0}
        attempts = int(record.get("attempts") or 0) + 1
        limit = int(step.get("max_tool_attempts") or 3)
        text = " | ".join(blockers).lower()
        if any(
            token in text
            for token in (
                "timeout",
                "timed out",
                "connection refused",
                "connection reset",
                "temporarily unavailable",
                "name resolution",
                "http 502",
                "http 503",
                "http 504",
            )
        ):
            failure_kind = "TRANSIENT_INFRASTRUCTURE"
        elif any(token in text for token in ("401", "403", "unauthorized", "forbidden", "auth")):
            failure_kind = "AUTHORIZATION"
        else:
            failure_kind = "TOOL_FAILURE"
        write_json(
            fail_path,
            {
                "schema": "m8m_retryable_tool_failure_v1",
                "attempts": attempts,
                "max_tool_attempts": limit,
                "failure_kind": failure_kind,
                "blockers": blockers,
                "updated_at": utc_now(),
            },
            overwrite=True,
        )
        if attempts >= limit:
            return _write_blocked(
                run_dir,
                flow,
                step,
                bindings,
                fingerprint,
                blockers
                + [
                    f"{step['id']}: retryable tool budget {limit} exhausted; "
                    "no model fallback was permitted"
                ],
            )
        return {
            "schema": ACTION_SCHEMA,
            "state": "ACTION_REQUIRED",
            "action": "retry_tool_then_resume",
            "execution_mode": "tool",
            "step_id": step["id"],
            "attempt": attempts,
            "max_tool_attempts": limit,
            "model": "none",
            "retryable": True,
            "failure_kind": failure_kind,
            "retry_after_seconds": min(30, 2 ** max(0, attempts - 1)),
            "blockers": blockers,
            "task_path": relative_to(
                run_dir,
                _candidate_task_path(
                    run_dir,
                    step["id"],
                    max(1, int(task.get("attempt") or attempts)),
                    row_id=_task_cycle_row(task),
                ),
            ),
            "expected_output_path": relative_to(
                run_dir, expected_artifact_path(run_dir, flow, step)
            ),
            "input_artifacts": bindings,
            "tools": step.get("tools") or [],
            "on_tool_fail": "retryable",
        }
    if policy != "need_model":
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
    flowsteps = step.get("flowsteps") or []
    first_flowstep = flowsteps[0] if flowsteps else ""
    fallback_flowstep = (
        str(first_flowstep.get("id") or first_flowstep.get("tool") or "")
        if isinstance(first_flowstep, dict)
        else str(first_flowstep)
    ).strip()
    flowstep_id = fallback_flowstep
    gem_path = skill_dir / str(step.get("gem") or f"references/{step['id']}.md")
    gem_section = read_gem_section(gem_path, flowstep_id) if flowstep_id else ""
    if not gem_section and flowstep_id != "" and fallback_flowstep and fallback_flowstep != flowstep_id:
        flowstep_id = fallback_flowstep
        gem_section = read_gem_section(gem_path, flowstep_id)
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
    if flowstep_id:
        request["flowstep"] = flowstep_id
    if gem_path.is_file():
        request["gem_path"] = relative_to(skill_dir, gem_path)
    if gem_section:
        request["instruction"] = f"{request['instruction']}\n\n{gem_section}"
    candidate_attempt = max(1, int(task.get("attempt") or attempts))
    request_path = (
        folder
        / "attempts"
        / f"attempt-{candidate_attempt:03d}"
        / "model-request.json"
    )
    write_json(request_path, request)
    return {
        "schema": ACTION_SCHEMA,
        "state": "ACTION_REQUIRED",
        "action": "run_model_then_advance",
        "execution_mode": "tool",
        "step_id": step["id"],
        "attempt": candidate_attempt,
        "model": recovery_model(step),
        "task_path": relative_to(
            run_dir,
            _candidate_task_path(
                run_dir,
                step["id"],
                candidate_attempt,
                row_id=_task_cycle_row(task),
            ),
        ),
        "model_request_path": relative_to(run_dir, request_path),
        "draft_path": relative_to(run_dir, folder / "draft.json"),
        "draft_schema_path": step.get("draft_schema"),
        "expected_output_path": relative_to(run_dir, expected_artifact_path(run_dir, flow, step)),
        "input_artifacts": bindings,
        "tools": step.get("tools") or [],
        "on_tool_fail": "need_model",
    }


def _need_model_action(
    skill_dir: Path,
    run_dir: Path,
    step: dict[str, Any],
    folder: Path,
    task: dict[str, Any],
    result: dict[str, Any],
    bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_request = result.get("model_request")
    request = dict(raw_request) if isinstance(raw_request, dict) else {}
    flowsteps = step.get("flowsteps") or []
    fallback_flowstep = ""
    if flowsteps:
        first = flowsteps[0]
        fallback_flowstep = str(first.get("id") or first.get("tool") or "") if isinstance(first, dict) else str(first)
    flowstep_id = str(request.get("flowstep") or fallback_flowstep).strip()
    if flowstep_id:
        gem_path = skill_dir / str(step.get("gem") or f"references/{step['id']}.md")
        section = read_gem_section(gem_path, flowstep_id)
        if not section and fallback_flowstep and fallback_flowstep != flowstep_id:
            flowstep_id = fallback_flowstep
            section = read_gem_section(gem_path, flowstep_id)
        request["flowstep"] = flowstep_id
        instruction = str(request.get("instruction") or "").strip()
        if section and section not in instruction:
            request["instruction"] = f"{instruction}\n\n{section}".strip()
        request.setdefault("gem_path", relative_to(skill_dir, gem_path) if gem_path.is_file() else str(gem_path))
    attempt = max(1, int(task.get("attempt") or 1))
    request_path = folder / "attempts" / f"attempt-{attempt:03d}" / "model-request.json"
    write_json(request_path, request)
    draft_path = folder / "draft.json"
    return {
        "schema": ACTION_SCHEMA,
        "state": "ACTION_REQUIRED",
        "action": "run_model_then_advance",
        "execution_mode": "tool",
        "step_id": step["id"],
        "attempt": attempt,
        "model": result.get("model") or recovery_model(step),
        "task_path": relative_to(
            run_dir,
            _candidate_task_path(
                run_dir,
                step["id"],
                attempt,
                row_id=_task_cycle_row(task),
            ),
        ),
        "model_request_path": relative_to(run_dir, request_path),
        "draft_path": relative_to(run_dir, draft_path),
        "draft_schema_path": step.get("draft_schema"),
        "expected_output_path": task["expected_output_path"],
        "input_artifacts": bindings,
        "tools": step.get("tools") or [],
    }


def _invalid_draft_action(
    run_dir: Path,
    step: dict[str, Any],
    folder: Path,
    task: dict[str, Any],
    bindings: list[dict[str, Any]],
    blocker: str,
) -> dict[str, Any]:
    """Request a corrected draft without changing the frozen candidate attempt."""

    attempt = max(1, int(task.get("attempt") or 1))
    attempt_dir = folder / "attempts" / f"attempt-{attempt:03d}"
    request_path = attempt_dir / "model-request.json"
    if not request_path.is_file():
        raise FlowError(
            f"{step['id']}: attempt {attempt} model request is missing from frozen run state"
        )
    write_json(
        attempt_dir / "draft-validation.json",
        {
            "schema": "m8m.draft_validation_diagnostic.v1",
            "milestone_id": step["id"],
            "attempt": attempt,
            "status": "ACTION_REQUIRED",
            "blockers": [blocker],
            "candidate_request_path": relative_to(
                run_dir,
                _candidate_task_path(
                    run_dir,
                    step["id"],
                    attempt,
                    row_id=_task_cycle_row(task),
                ),
            ),
            "recorded_at": utc_now(),
        },
        overwrite=True,
    )
    return {
        "schema": ACTION_SCHEMA,
        "state": "ACTION_REQUIRED",
        "action": "run_model_then_advance",
        "execution_mode": "tool",
        "step_id": step["id"],
        "attempt": attempt,
        "model": recovery_model(step),
        "task_path": relative_to(
            run_dir,
            _candidate_task_path(
                run_dir,
                step["id"],
                attempt,
                row_id=_task_cycle_row(task),
            ),
        ),
        "model_request_path": relative_to(run_dir, request_path),
        "draft_path": relative_to(run_dir, folder / "draft.json"),
        "draft_schema_path": step.get("draft_schema"),
        "expected_output_path": task["expected_output_path"],
        "input_artifacts": bindings,
        "tools": step.get("tools") or [],
        "blockers": [blocker],
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
                task,
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
                task,
            )
        }
    if result.get("_flowstep") == "BLOCKED":
        blockers = [str(item) for item in result.get("blockers") or ["tool returned BLOCKED"]]
        return {
            "action": _recover_or_block(
                skill_dir,
                run_dir,
                flow,
                step,
                bindings,
                fingerprint,
                draft,
                blockers,
                task,
            )
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
        return {"action": _need_model_action(skill_dir, run_dir, step, folder, task, result, bindings)}
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
    receipt = result.get("receipt") if isinstance(result.get("receipt"), dict) else None
    schema_candidate = {key: value for key, value in result.items() if key != "receipt"}
    try:
        assert_external_phase_journal(run_dir, step)
        output_schema_path = skill_dir / step["output_schema"]
        validate_against_schema(schema_candidate, output_schema_path)
    except FlowError as exc:
        return _write_blocked(
            run_dir,
            flow,
            step,
            bindings,
            fingerprint,
            [f"{step['id']}: chosen output was not produced; {exc}"],
            attempt=attempt,
        )
    if step.get("branch"):
        blocked = _check_branch(skill_dir, run_dir, flow, step, result, bindings, fingerprint)
        if blocked is not None:
            return blocked
    try:
        _record_judge_attempt(
            run_dir,
            step,
            result,
            receipt,
            attempt=attempt,
            decision="PASS",
        )
        chosen = materialize_chosen_output(
            run_dir,
            flow,
            step,
            result,
            receipt=receipt,
            attempt=attempt,
        )
        validate_against_schema(chosen, chosen_output_schema_path())
    except FlowError as exc:
        return _write_blocked(
            run_dir,
            flow,
            step,
            bindings,
            fingerprint,
            [f"{step['id']}: chosen output was not materialized; {exc}"],
            attempt=attempt,
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
        receipt = read_receipt(
            _with_worker_receipt(result),
            skill_dir=skill_dir,
            schema_rel=receipt_rel,
            step_id=step["id"],
        )
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


def _worker_receipt(receipt: dict[str, Any] | None) -> dict[str, Any]:
    """Unwrap the bounded ingress receipt preserved by the durable judge receipt."""
    if not isinstance(receipt, dict):
        return {}
    if receipt.get("schema") != "m8m.milestone_judge_receipt.v1":
        return dict(receipt)
    details = receipt.get("details") if isinstance(receipt.get("details"), dict) else {}
    worker = details.get("worker_receipt")
    return dict(worker) if isinstance(worker, dict) else {}


def _with_worker_receipt(result: dict[str, Any]) -> dict[str, Any]:
    durable = result.get("receipt") if isinstance(result.get("receipt"), dict) else None
    unwrapped = _worker_receipt(durable)
    if isinstance(durable, dict) and durable.get("schema") == "m8m.milestone_judge_receipt.v1":
        # ``ok`` is redundant with the normalized decision and is therefore
        # not necessarily retained in bounded worker details. Reconstruct it
        # for legacy branch/cycle receipt schemas at the compatibility ingress.
        unwrapped.setdefault("ok", durable.get("decision") == "PASS")
    return {**result, "receipt": unwrapped}


def _store_active_branch(run_dir: Path, step: dict[str, Any], result: dict[str, Any]) -> None:
    receipt = _worker_receipt(
        result.get("receipt") if isinstance(result.get("receipt"), dict) else None
    )
    chosen = str(receipt.get("branch") or "")
    spec = step.get("branch") if isinstance(step.get("branch"), dict) else {}
    path_ids = {
        str(item.get("id") or "")
        for item in (spec.get("paths") or [])
        if isinstance(item, dict) and item.get("id")
    }
    if not chosen or (path_ids and chosen not in path_ids):
        raise FlowError(f"{step['id']}: cannot persist invalid branch {chosen or '(empty)'}")
    record_path = _execution_path(run_dir)
    record = read_json(record_path)
    prior_from = str(record.get("branch_from") or "")
    prior = str(record.get("active_branch") or "")
    if prior_from == str(step["id"]) and prior:
        if prior != chosen:
            raise FlowError(
                f"{step['id']}: committed branch {chosen} conflicts with persisted branch {prior}"
            )
        return
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
        receipt = read_receipt(
            _with_worker_receipt(result),
            skill_dir=skill_dir,
            schema_rel=receipt_rel,
            step_id=step["id"],
        )
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
            # _pass_artifact materializes the current cycle candidate before
            # applying its receipt. Remove that live candidate before writing
            # the terminal BLOCKED envelope, otherwise the real cycle-budget
            # failure is masked by an immutable-artifact collision.
            purge_cycle_live(run_dir, wrapped)
            _clear_wrap_artifacts(run_dir, flow, wrapped)
            _strip_completed(run_dir, wrap_ids)
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
        record["cycle_row_attempt"] = int(record.get("cycle_row_attempt") or 1) + 1
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
        record["cycle_row_attempt"] = 1
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
        record["cycle_row_attempt"] = 1
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
        expectation = derive_milestone_expectation(step)
        validate_against_schema(expectation, _expectation_schema_path())
    except FlowError as exc:
        return _write_blocked(run_dir, flow, step, bindings, fingerprint, [str(exc)])
    folder = work_dir(run_dir, step["id"])
    folder.mkdir(parents=True, exist_ok=True)
    record = read_json(_execution_path(run_dir))
    row_id = str(record.get("cycle_row") or "")
    cycle_attempt = (
        max(1, int(record.get("cycle_row_attempt") or 1)) if row_id else 1
    )
    if row_id:
        input_data = dict(input_data)
        input_data["ledger_row"] = row_id
        input_data["row"] = row_id
        cid = str(record.get("cycle_id") or step.get("on_cycle") or "")
        if cid:
            ledger = load_ledger(run_dir, cid)
            if ledger:
                input_data["ledger"] = ledger
    write_json(folder / "input.json", input_data, overwrite=True)
    task = _task(run_dir, flow, step, bindings, attempt=1, inputs=input_data)
    # Retain the stable task location for run bookkeeping and old local tools;
    # model actions always point at the closed per-attempt candidate request.
    task_path = run_dir / "runtime-tasks" / f"{step['id']}.json"
    if not task_path.is_file():
        write_json(task_path, task, overwrite=False)
    else:
        frozen = read_json(task_path)
        if frozen.get("expectation") != expectation or (
            frozen.get("execution") or {}
        ) != (step.get("execution") or {}):
            return _write_blocked(
                run_dir,
                flow,
                step,
                bindings,
                fingerprint,
                [f"{step['id']}: frozen runtime task differs from current workflow"],
            )
    if draft is not None:
        draft_rel = step.get("draft_schema")
        draft_path = skill_dir / draft_rel if draft_rel else None
        if draft_path is not None and draft_path.is_file():
            try:
                validate_against_schema(draft, draft_path)
            except FlowError as exc:
                # A user/model draft is recoverable input, not durable product
                # output.  Invalid draft bytes must keep the same operation at
                # ACTION_REQUIRED so the caller can correct and resubmit them;
                # terminalizing the run here forces a second writer even though
                # no milestone output was committed.
                candidate_path = _candidate_task_path(
                    run_dir,
                    step["id"],
                    cycle_attempt,
                    row_id=row_id or None,
                )
                if not candidate_path.is_file():
                    raise FlowError(
                        f"{step['id']}: attempt 1 candidate request is missing from frozen run state"
                    )
                task = read_json(candidate_path)
                return _invalid_draft_action(
                    run_dir,
                    step,
                    folder,
                    task,
                    bindings,
                    str(exc),
                )
        write_json(folder / "draft.json", draft)
        if is_wait_milestone(step):
            _write_wait_draft(run_dir, step, draft)
    if is_wait_milestone(step) and not _has_feedback(draft, folder / "draft.json", wait_draft_path(run_dir, step["id"])):
        return _pause_wait_action(run_dir, flow, step, bindings)
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
                cache_root_path=(
                    Path(str(runtime_cache["root"]))
                    if runtime_cache.get("root")
                    else None
                ),
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
            judged, cache_error = _judge_current_candidate(
                skill_dir,
                run_dir,
                step,
                input_data,
                cached_candidate,
                attempt=1,
            )
            if (
                isinstance(judged, dict)
                and (judged.get("receipt") or {}).get("decision") == "PASS"
            ):
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

    # The candidate worker sees the exact derived expectation before its first
    # FlowStep.  This is an execution view, not an authored workflow input and
    # therefore is added only after request validation and cache-key creation.
    input_data = {**input_data, "expectation": expectation}

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
        resume_candidate = bool(state.get("awaiting_candidate")) and attempts > 0
        current_draft = draft
        while index < len(items):
            if resume_candidate:
                resume_candidate = False
            else:
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
            write_json(
                state_path,
                {
                    "index": index,
                    "done": done,
                    "attempts": attempts,
                    "awaiting_candidate": True,
                },
                overwrite=True,
            )
            task, _ = _candidate_task(
                run_dir,
                flow,
                step,
                bindings,
                {key: value for key, value in item_input.items() if key != "expectation"},
                attempt=attempts,
            )
            outcome = _run_handler(
                skill_dir, run_dir, flow, step, item_input, current_draft, task, bindings, fingerprint, folder
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
            write_json(
                state_path,
                {
                    "index": index,
                    "done": done,
                    "attempts": attempts,
                    "awaiting_candidate": False,
                },
                overwrite=True,
            )
            try:
                receipt = read_receipt(result, skill_dir=skill_dir, schema_rel=receipt_rel, step_id=step["id"])
            except FlowError as exc:
                return _write_blocked(run_dir, flow, step, bindings, fingerprint, [str(exc)])
            if not receipt["ok"]:
                try:
                    _record_judge_attempt(
                        run_dir,
                        step,
                        result,
                        receipt,
                        attempt=attempts,
                        decision="RETRY",
                    )
                except FlowError as exc:
                    return _write_blocked(
                        run_dir,
                        flow,
                        step,
                        bindings,
                        fingerprint,
                        [str(exc)],
                        attempt=attempts,
                        receipt=receipt,
                    )
                write_json(
                    state_path,
                    {
                        "index": index,
                        "done": done,
                        "attempts": attempts,
                        "awaiting_candidate": False,
                    },
                    overwrite=True,
                )
                continue
            try:
                result = _place_result(run_dir, step, result, item_index=index)
            except FlowError:
                pass
            done.append(_item_payload(result, item))
            index += 1
            write_json(
                state_path,
                {
                    "index": index,
                    "done": done,
                    "attempts": attempts,
                    "awaiting_candidate": False,
                },
                overwrite=True,
            )
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
        state = (
            read_json(state_path)
            if state_path.is_file()
            else {
                "attempts": 0,
                "prior_feedback": None,
                "awaiting_candidate": False,
            }
        )
        attempts = int(state.get("attempts") or 0)
        resume_candidate = bool(state.get("awaiting_candidate")) and attempts > 0
        prior_feedback = (
            state.get("prior_feedback")
            if isinstance(state.get("prior_feedback"), dict)
            else None
        )
        current_draft = draft
        last: dict[str, Any] | None = None
        while True:
            if resume_candidate:
                resume_candidate = False
            else:
                attempts += 1
            if attempts > max_attempts:
                return _write_blocked(
                    run_dir,
                    flow,
                    step,
                    bindings,
                    fingerprint,
                    [f"{step['id']}: judge budget {max_attempts} exhausted without PASS"],
                )
            write_json(
                state_path,
                {
                    "attempts": attempts,
                    "prior_feedback": prior_feedback,
                    "awaiting_candidate": True,
                },
                overwrite=True,
            )
            judged = attach_address(run_dir, step, input_data, attempt=attempts)
            if prior_feedback is not None:
                judged["prior_feedback"] = prior_feedback
            task, _ = _candidate_task(
                run_dir,
                flow,
                step,
                bindings,
                {key: value for key, value in judged.items() if key != "expectation"},
                attempt=attempts,
                prior_feedback=prior_feedback,
            )
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
            write_json(
                state_path,
                {
                    "attempts": attempts,
                    "prior_feedback": prior_feedback,
                    "awaiting_candidate": False,
                },
                overwrite=True,
            )
            last = result
            if step.get("judge_abi") != "m8m_milestone_judge_v1":
                return _write_blocked(
                    run_dir,
                    flow,
                    step,
                    bindings,
                    fingerprint,
                    [
                        f"{step['id']}: semantic judge loop requires "
                        "judge_abi m8m_milestone_judge_v1"
                    ],
                    attempt=attempts,
                )
            judged_result, judge_error = _judge_current_candidate(
                skill_dir,
                run_dir,
                step,
                input_data,
                result,
                attempt=attempts,
            )
            if not isinstance(judged_result, dict):
                return _write_blocked(
                    run_dir,
                    flow,
                    step,
                    bindings,
                    fingerprint,
                    [f"{step['id']}: current judge failed; {judge_error}"],
                    attempt=attempts,
                )
            result = judged_result
            last = result
            receipt = result.get("receipt")
            if not isinstance(receipt, dict):
                return _write_blocked(
                    run_dir,
                    flow,
                    step,
                    bindings,
                    fingerprint,
                    [f"{step['id']}: semantic judge did not return a durable decision"],
                )
            decision = str(receipt.get("decision") or "")
            if decision == "PASS":
                try:
                    result = _place_result(run_dir, step, result)
                except FlowError as exc:
                    return _write_blocked(run_dir, flow, step, bindings, fingerprint, [str(exc)])
                # A judge-wrapped cycle milestone owns a fresh retry budget
                # for each ledger row. The shared milestone work directory is
                # reused by the next row, so remove the completed row's state
                # before the cycle advances. Failed/incomplete rows keep it.
                if step.get("on_cycle") and state_path.is_file():
                    state_path.unlink()
                return _pass_artifact(
                    skill_dir, run_dir, flow, step, result, bindings, fingerprint, attempts, cache_info
                )
            if decision == "BLOCKED":
                blockers = [str(item) for item in receipt.get("blockers") or []]
                return _write_blocked(
                    run_dir,
                    flow,
                    step,
                    bindings,
                    fingerprint,
                    blockers or [f"{step['id']}: semantic judge returned BLOCKED"],
                    attempt=attempts,
                    receipt=receipt,
                )
            try:
                _record_judge_attempt(
                    run_dir,
                    step,
                    result,
                    receipt,
                    attempt=attempts,
                    decision="RETRY",
                )
            except FlowError as exc:
                return _write_blocked(
                    run_dir,
                    flow,
                    step,
                    bindings,
                    fingerprint,
                    [str(exc)],
                    attempt=attempts,
                    receipt=receipt,
                )
            prior_feedback = _bounded_prior_feedback(
                {**receipt, "attempt": attempts}
            )
            write_json(
                state_path,
                {
                    "attempts": attempts,
                    "prior_feedback": prior_feedback,
                    "awaiting_candidate": False,
                },
                overwrite=True,
            )
        return _write_blocked(
            run_dir,
            flow,
            step,
            bindings,
            fingerprint,
            [f"{step['id']}: semantic judge ended without a PASS decision"],
        )

    input_data = attach_address(run_dir, step, input_data)
    task, _ = _candidate_task(
        run_dir,
        flow,
        step,
        bindings,
        {key: value for key, value in input_data.items() if key != "expectation"},
        attempt=cycle_attempt,
    )
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
    candidate = outcome["result"]
    result, candidate_error = _judge_current_candidate(
        skill_dir,
        run_dir,
        step,
        input_data,
        candidate,
        attempt=cycle_attempt,
    )
    if not isinstance(result, dict):
        return _write_blocked(
            run_dir,
            flow,
            step,
            bindings,
            fingerprint,
            [f"{step['id']}: candidate/control admission failed; {candidate_error}"],
            attempt=cycle_attempt,
        )
    try:
        result = _place_result(run_dir, step, result)
    except FlowError as exc:
        return _write_blocked(run_dir, flow, step, bindings, fingerprint, [str(exc)])
    return _pass_artifact(
        skill_dir,
        run_dir,
        flow,
        step,
        result,
        bindings,
        fingerprint,
        cycle_attempt,
        cache_info,
    )


def _reference_source(reference: Any) -> str | None:
    if isinstance(reference, str) and reference != "user.request" and "." in reference:
        return reference.split(".", 1)[0]
    if isinstance(reference, dict):
        source = str(reference.get("from") or "")
        return source.split(".", 1)[0] if "." in source else None
    return None


def _judge_current_candidate(
    skill_dir: Path,
    run_dir: Path,
    step: dict[str, Any],
    input_data: dict[str, Any],
    candidate: dict[str, Any],
    *,
    attempt: int,
) -> tuple[dict[str, Any] | None, str]:
    """Admit one candidate, then optionally apply the semantic judge.

    Cache hits and normal execution deliberately share this path.  Admission
    always precedes judgment and freezes any file/media bytes into this run.
    """
    try:
        admitted = admit_candidate(
            run_dir,
            skill_dir,
            step,
            candidate,
            attempt=attempt,
        )
    except FlowError as exc:
        return None, str(exc)
    current_candidate = admitted
    if str(step.get("loop") or "none") != "judge":
        try:
            control_evidence = _invoke_topology_control_worker(
                skill_dir,
                run_dir,
                step,
                input_data,
                current_candidate,
            )
        except FlowError as exc:
            return None, str(exc)
        receipt = normalize_judge_receipt(
            step,
            control_evidence,
            attempt=attempt,
            decision="PASS",
        )
        return {
            **current_candidate,
            "receipt": receipt,
        }, ""
    worker_ref = str(step.get("worker") or "").strip()
    try:
        worker = runtime_package_name(
            worker_ref,
            label=f"{step.get('id')}.worker",
        )
    except FlowError as exc:
        return None, str(exc)
    codebase = infer_codebase(skill_dir)
    if codebase is None:
        local_package = Path(skill_dir) / "flowsteps" / "tools" / worker
        if worker and local_package.is_dir():
            codebase = Path(skill_dir)
    if not worker_ref or codebase is None:
        return None, "current judge worker is unavailable"
    resolved_inputs = {
        key: value for key, value in input_data.items() if key != "expectation"
    }
    judge_input = build_milestone_judge_request(
        step,
        attempt=attempt,
        inputs=resolved_inputs,
        candidate=current_candidate,
    )
    try:
        validate_against_schema(judge_input, _judge_request_schema_path())
        judge_result = run_library_tool(codebase, worker, judge_input)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    try:
        schema_path = Path(skill_dir) / str(step.get("receipt_schema") or "")
        validate_against_schema(judge_result, schema_path)
        decision = str(judge_result.get("decision") or "").upper()
        blockers = [str(item).strip() for item in judge_result.get("blockers") or []]
        if decision == "PASS" and blockers:
            raise FlowError(
                f"{step.get('id')}: semantic judge PASS must have no blockers"
            )
        if decision == "PASS":
            control_evidence = _invoke_topology_control_worker(
                skill_dir,
                run_dir,
                step,
                input_data,
                current_candidate,
            )
            if control_evidence is not None:
                judge_result = {**judge_result, **control_evidence}
        receipt = normalize_judge_receipt(
            step,
            judge_result,
            attempt=attempt,
            decision=decision,
            blockers=blockers,
        )
    except FlowError as exc:
        return None, str(exc)
    return (
        {**current_candidate, "receipt": receipt},
        "" if receipt["decision"] == "PASS" else "current judge rejected candidate",
    )


def _bounded_control_value(value: Any, *, label: str) -> Any:
    """Freeze one bounded JSON value before it enters a control worker."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FlowError(f"{label} is not closed JSON") from exc
    if len(encoded) > _MAX_CONTROL_REQUEST_BYTES:
        raise FlowError(
            f"{label} exceeds {_MAX_CONTROL_REQUEST_BYTES} bytes"
        )
    return json.loads(encoded.decode("utf-8"))


def _control_worker_binding(step: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    branch = step.get("branch") if isinstance(step.get("branch"), dict) else None
    cycle = step.get("cycle") if isinstance(step.get("cycle"), dict) else None
    if branch and cycle:
        raise FlowError(
            f"{step.get('id')}: one admitted candidate cannot drive branch and cycle control"
        )
    spec = branch or cycle
    if not spec:
        return None
    kind = "branch" if branch else "cycle"
    worker_id = str(spec.get("worker") or "").strip()
    execution = step.get("execution") if isinstance(step.get("execution"), dict) else {}
    bindings = execution.get("tool_bindings")
    matches = [
        item
        for item in bindings or []
        if isinstance(item, dict)
        and str(item.get("tool") or "") == worker_id
    ]
    if len(matches) != 1:
        raise FlowError(
            f"{step.get('id')}.{kind}.worker {worker_id or '(empty)'} must have "
            "one exact execution.tool_bindings entry"
        )
    exact_ref = str(matches[0].get("ref") or "").strip()
    package = local_tool_package_name(
        exact_ref,
        label=f"{step.get('id')}.{kind}.worker binding",
    )
    frozen_ref = str(step.get("_control_worker_ref") or "").strip()
    if frozen_ref and frozen_ref != exact_ref:
        raise FlowError(
            f"{step.get('id')}.{kind}.worker binding differs from the loaded workflow"
        )
    return kind, package, spec


def _topology_control_request(
    run_dir: Path,
    step: dict[str, Any],
    kind: str,
    spec: dict[str, Any],
    input_data: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    inputs = {
        key: value
        for key, value in input_data.items()
        if key not in {"expectation", "address", "prior_feedback", "draft"}
    }
    if kind == "branch":
        paths = [
            str(item.get("id") or "")
            for item in spec.get("paths") or []
            if isinstance(item, dict) and item.get("id")
        ]
        control = {
            "kind": "branch",
            "paths": paths,
            "default": str(spec.get("default") or ""),
            "join": str(spec.get("join") or ""),
        }
        schema = "m8m.branch_control_request.v1"
    else:
        record_path = _execution_path(run_dir)
        record = read_json(record_path) if record_path.is_file() else {}
        control = {
            "kind": "cycle",
            "cycle_id": cycle_id_of(step),
            "row": str(record.get("cycle_row") or inputs.get("row") or ""),
            "round": max(1, int(record.get("cycle_round") or 1)),
            "max_rounds": max(1, int(spec.get("max_rounds") or 1)),
            "pass_rule": str(spec.get("pass") or ""),
        }
        schema = "m8m.cycle_control_request.v1"
    return _bounded_control_value(
        {
            "schema": schema,
            "milestone_id": str(step.get("id") or ""),
            "inputs": inputs,
            "candidate": candidate,
            "control": control,
        },
        label=f"{step.get('id')}.{kind} control request",
    )


def _invoke_topology_control_worker(
    skill_dir: Path,
    run_dir: Path,
    step: dict[str, Any],
    input_data: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any] | None:
    """Run branch/cycle control after admission, never inside the candidate handler."""

    bound = _control_worker_binding(step)
    if bound is None:
        return None
    kind, package, spec = bound
    codebase = infer_codebase(skill_dir)
    if codebase is None:
        local_package = Path(skill_dir) / "flowsteps" / "tools" / package
        if local_package.is_dir():
            codebase = Path(skill_dir)
    if codebase is None:
        raise FlowError(f"{step.get('id')}.{kind}: control worker is unavailable")
    request = _topology_control_request(
        run_dir,
        step,
        kind,
        spec,
        input_data,
        candidate,
    )
    try:
        raw = run_library_tool(codebase, package, request)
        receipt = read_receipt(
            {"receipt": raw},
            skill_dir=skill_dir,
            schema_rel=spec.get("receipt_schema") or step.get("receipt_schema"),
            step_id=str(step.get("id") or ""),
        )
    except Exception as exc:
        if isinstance(exc, FlowError):
            raise
        raise FlowError(
            f"{step.get('id')}.{kind} control worker failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not receipt.get("ok"):
        raise FlowError(
            f"{step.get('id')}: {kind} control worker did not accept the admitted candidate"
        )
    if kind == "branch":
        allowed = {
            str(item.get("id") or "")
            for item in spec.get("paths") or []
            if isinstance(item, dict) and item.get("id")
        }
        chosen = str(receipt.get("branch") or "")
        if not chosen or chosen not in allowed:
            raise FlowError(
                f"{step.get('id')}: unknown branch {chosen or '(empty)'}"
            )
    else:
        chosen = str(receipt.get("cycle") or "")
        if chosen not in {"pass", "fail"}:
            raise FlowError(
                f"{step.get('id')}: cycle control must be pass or fail"
            )
        expected_row = str(request["control"].get("row") or "")
        actual_row = str(receipt.get("row") or "")
        if actual_row and expected_row and actual_row != expected_row:
            raise FlowError(
                f"{step.get('id')}: cycle control row {actual_row} differs from {expected_row}"
            )
    # The candidate remains exactly {'outputs': ...}. This bounded value is
    # private control evidence retained only inside the runtime judge receipt.
    return _bounded_control_value(
        receipt,
        label=f"{step.get('id')}.{kind} control evidence",
    )


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
    for owner in flow["steps"]:
        spec = owner.get("cycle") if isinstance(owner.get("cycle"), dict) else {}
        if not spec:
            continue
        cycle_id = cycle_id_of(owner)
        wrapped_ids = {
            str(item["id"])
            for item in _cycle_wrap(flow, cycle_id)
            if str(item.get("id") or "") in edges
        }
        ledger_id = str(spec.get("ledger") or "")
        join_id = str(spec.get("join") or "")
        sources = set(wrapped_ids)
        if ledger_id in edges:
            sources.add(ledger_id)
        targets = set(wrapped_ids)
        if join_id in edges:
            targets.add(join_id)
        for source in sources:
            edges[source].update(target for target in targets if target != source)
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
    flow_path = Path(os.path.abspath(str(flow["_flow_path"])))
    skill_root = Path(os.path.abspath(str(skill_dir)))
    flow_label = f"skill:{flow_path.relative_to(skill_root).as_posix()}"

    def add_owner(label: str, milestone_id: str) -> None:
        owners.setdefault(label, set()).add(milestone_id)

    root = Path(os.path.abspath(str(skill_dir)))
    project = root.parent.parent.parent if root.parent.name == "flows" and root.parent.parent.name == "flowsteps" else root

    def dependency_label(value: str) -> str:
        path = Path(value)
        normalized = Path(os.path.abspath(str(path if path.is_absolute() else project / path)))
        try:
            return f"project:{normalized.relative_to(project).as_posix()}"
        except ValueError as exc:
            raise FlowError(f"implementation dependency escapes project directory: {value}") from exc

    all_milestones = set(old_steps) | set(current_steps)
    for raw in (old_raw, current_raw):
        for dependency in raw.get("implementation_dependencies") or []:
            label = dependency_label(str(dependency))
            for milestone_id in all_milestones:
                add_owner(label, milestone_id)

    for source_steps in (old_steps, current_steps):
        for milestone_id, item in source_steps.items():
            for dependency in item.get("implementation_dependencies") or []:
                add_owner(dependency_label(str(dependency)), milestone_id)
            for field in ("handler", "input_schema", "output_schema", "draft_schema", "receipt_schema", "gem"):
                relative = item.get(field)
                if relative:
                    add_owner(f"skill:{Path(str(relative)).as_posix()}", milestone_id)
            tool_ids = {
                local_tool_package_name(
                    str(binding.get("ref") or ""),
                    label=(
                        f"{milestone_id}.execution.tool_bindings"
                        f"[{binding.get('tool')}].ref"
                    ),
                )
                for binding in (item.get("execution") or {}).get("tool_bindings") or []
                if isinstance(binding, dict) and binding.get("ref")
            }
            if item.get("judge_abi") and item.get("worker"):
                # A strict semantic judge is intentionally outside the
                # candidate FlowStep tool list, but its implementation bytes
                # are still locked by implementation_lock(). Attribute those
                # files to the milestone so an explicit workflow adoption can
                # invalidate and replay the owning milestone instead of
                # rejecting the new judge package as unowned drift. Legacy
                # snapshots may contain an unversioned worker, so normalize
                # the package name without requiring the current ref grammar.
                tool_ids.add(str(item["worker"]).rsplit("@", 1)[0])
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
        manifest = load_chosen_output(
            run_dir,
            step["id"],
            step=step,
            skill_dir=Path(flow["_skill_dir"]),
        )
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


def _has_frozen_external_recovery(
    run_dir: Path,
    flow: dict[str, Any],
    milestone_id: str,
) -> bool:
    """Allow code adoption without replaying historical preimage milestones.

    Once an external operation is frozen, compatible upstream chosen outputs
    are historical evidence for that exact operation.  Re-running them after a
    successful commit can be impossible because their preimages were consumed.
    The external milestone is still cleared and must query the exact operation;
    its handler remains forbidden from planning or committing when the frozen
    session is not publish-ready.
    """

    step = next(
        (item for item in flow["steps"] if item["id"] == milestone_id),
        None,
    )
    if not isinstance(step, dict) or step.get("side_effects") != "external":
        return False
    spec = step.get("phase_journal")
    if not isinstance(spec, dict) or not spec.get("path"):
        return False
    path = run_dir / str(spec["path"])
    if not path.is_file():
        return False
    journal = read_json(path)
    phases = {
        str(item.get("phase") or "")
        for item in journal.get("phases") or []
        if isinstance(item, dict)
    }
    return (
        journal.get("schema") == "m8m_external_phase_journal_v1"
        and journal.get("milestone_id") == milestone_id
        and bool(str(journal.get("operation_id") or ""))
        and bool(str(journal.get("plan_sha256") or ""))
        and "plan_frozen" in phases
    )


def _recommended_continue_root(flow: dict[str, Any], changed: set[str]) -> str | None:
    """Return the first workflow root whose downstream closure covers drift."""

    for step in flow["steps"]:
        milestone_id = str(step["id"])
        if changed.issubset(_replacement_ids(flow, milestone_id)):
            return milestone_id
    return None


def _compatible_additive_flow_metadata(
    old_global: dict[str, Any],
    current_global: dict[str, Any],
) -> bool:
    """Allow additive safety metadata and non-decreasing runtime budgets.

    Existing semantic flow-level values remain immutable.  Increasing an
    existing active-execution ceiling only relaxes an operational guard and is
    safe for an explicit adoption; decreasing or removing it could strand the
    preserved run and remains forbidden.  The two admitted additions tighten
    execution/isolation and implementation locking without changing milestone
    identity, ordering, or dataflow.  Declared shared dependencies are still
    owned by every milestone, so the normal dependency analysis below forces
    replay from the earliest affected milestone.
    """

    old_comparable = dict(old_global)
    current_comparable = dict(current_global)
    if old_comparable.get("max_run_seconds") != current_comparable.get("max_run_seconds"):
        old_budget = old_comparable.get("max_run_seconds")
        current_budget = current_comparable.get("max_run_seconds")
        numeric = (int, float)
        if (
            isinstance(old_budget, bool)
            or isinstance(current_budget, bool)
            or not isinstance(old_budget, numeric)
            or not isinstance(current_budget, numeric)
            or current_budget < old_budget
        ):
            return False
        old_comparable.pop("max_run_seconds")
        current_comparable.pop("max_run_seconds")
    if any(current_comparable.get(key) != value for key, value in old_comparable.items()):
        return False
    added = set(current_comparable) - set(old_comparable)
    if not added.issubset({"context_policy", "implementation_dependencies"}):
        return False
    if "context_policy" in added and current_comparable.get("context_policy") != "isolated":
        return False
    if "implementation_dependencies" in added:
        dependencies = current_comparable.get("implementation_dependencies")
        if (
            not isinstance(dependencies, list)
            or not dependencies
            or any(not isinstance(item, str) or not item.strip() for item in dependencies)
            or len(set(dependencies)) != len(dependencies)
        ):
            return False
    return True


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
    if old_global != current_global and not _compatible_additive_flow_metadata(
        old_global,
        current_global,
    ):
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
    external_recovery = bool(outside) and _has_frozen_external_recovery(
        run_dir,
        flow,
        milestone_id,
    )
    if outside and not external_recovery:
        recommended = _recommended_continue_root(flow, changed)
        instruction = (
            f"continue after edit from {recommended}"
            if recommended
            else "start fresh"
        )
        raise FlowError(
            f"edited implementation also affects preserved milestones {outside}; "
            f"{instruction} or start fresh"
        )
    _validate_preserved_chosen(run_dir, flow, affected)
    receipt = {
        "schema": "m8m_workflow_adoption_v1",
        "state": "prepared",
        "run_id": run_dir.name,
        "flow_id": flow["flow_id"],
        "continued_from": milestone_id,
        "changed_milestones": sorted(changed),
        "invalidated_milestones": sorted(affected),
        "external_recovery_preserved_changed_milestones": (
            outside if external_recovery else []
        ),
        "previous_fingerprint_sha256": old_lock.get("fingerprint_sha256"),
        "implementation_fingerprint_sha256": current_lock["fingerprint_sha256"],
        "flow_snapshot_sha256": sha256_file(Path(flow["_flow_path"])),
        "prepared_at": utc_now(),
    }
    write_json(run_dir / "workflow-adoption.json", receipt, overwrite=True)
    return current_lock, affected


def _mark_adoption_invalidated(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "workflow-adoption.json"
    receipt = read_json(path)
    receipt["state"] = "invalidated"
    receipt["invalidated_at"] = utc_now()
    write_json(path, receipt, overwrite=True)
    return receipt


def _complete_adoption_journal(run_dir: Path) -> dict[str, Any]:
    """Publish the final adoption phase after the new lock is authoritative."""
    path = run_dir / "workflow-adoption.json"
    receipt = read_json(path)
    receipt["state"] = "complete"
    receipt["adopted_at"] = utc_now()
    write_json(path, receipt, overwrite=True)
    return receipt


def _assert_adoption_outputs_invalidated(run_dir: Path, receipt: dict[str, Any]) -> None:
    survivors = [
        str(milestone_id)
        for milestone_id in receipt.get("invalidated_milestones") or []
        if chosen_output_path(run_dir, str(milestone_id)).is_file()
    ]
    if survivors:
        raise FlowError(
            "workflow adoption cannot publish its new implementation lock while old chosen "
            f"outputs survive: {survivors}"
        )


def _finalize_implementation_change(
    run_dir: Path,
    flow: dict[str, Any],
    current_lock: dict[str, Any],
) -> None:
    """Publish snapshot/record and the new lock only after invalidation."""
    receipt = read_json(run_dir / "workflow-adoption.json")
    if receipt.get("state") != "invalidated":
        raise FlowError("workflow adoption must invalidate affected milestones before commit")
    if (
        receipt.get("implementation_fingerprint_sha256")
        != current_lock.get("fingerprint_sha256")
    ):
        raise FlowError("workflow adoption journal does not match the implementation lock")
    _assert_adoption_outputs_invalidated(run_dir, receipt)
    snapshot_path = run_dir / "flow-snapshot.yaml"
    shutil.copy2(Path(flow["_flow_path"]), snapshot_path)
    if sha256_file(snapshot_path) != receipt.get("flow_snapshot_sha256"):
        raise FlowError("workflow adoption snapshot failed durable readback")
    record = read_json(_execution_path(run_dir))
    record["implementation_fingerprint_sha256"] = current_lock["fingerprint_sha256"]
    record["updated_at"] = utc_now()
    write_json(_execution_path(run_dir), record, overwrite=True)
    # The lock is the adoption commit marker.  It is intentionally last among
    # all state mutations, so the new implementation identity can never
    # coexist with chosen outputs produced under the old implementation.
    write_json(run_dir / "implementation-lock.json", current_lock, overwrite=True)
    _complete_adoption_journal(run_dir)


def _reconcile_implementation_adoption(
    run_dir: Path,
    lock: dict[str, Any],
) -> None:
    """Finish only a journal write interrupted after the new lock commit."""
    path = run_dir / "workflow-adoption.json"
    if not path.is_file():
        return
    receipt = read_json(path)
    if receipt.get("state") == "complete":
        return
    if (
        lock.get("fingerprint_sha256")
        != receipt.get("implementation_fingerprint_sha256")
    ):
        # The old lock is still authoritative. Explicit --continue-after-edit
        # can safely retry the prepare/invalidate transaction.
        return
    _assert_adoption_outputs_invalidated(run_dir, receipt)
    snapshot_path = run_dir / "flow-snapshot.yaml"
    if (
        not snapshot_path.is_file()
        or sha256_file(snapshot_path) != receipt.get("flow_snapshot_sha256")
    ):
        raise FlowError("committed workflow adoption has an invalid flow snapshot")
    record = read_json(_execution_path(run_dir))
    if (
        record.get("implementation_fingerprint_sha256")
        != lock.get("fingerprint_sha256")
    ):
        raise FlowError("committed workflow adoption has a stale execution record")
    _complete_adoption_journal(run_dir)


def replace_milestone_state(run_dir: Path, flow: dict[str, Any], milestone_id: str) -> set[str]:
    """Replace one chosen output in place and invalidate every dependent milestone."""
    affected = _replacement_ids(flow, milestone_id)
    invalidated_cycles: list[tuple[str, set[str]]] = []
    for owner in flow["steps"]:
        spec = owner.get("cycle") if isinstance(owner.get("cycle"), dict) else {}
        if not spec:
            continue
        cycle_id = cycle_id_of(owner)
        wrapped_ids = {
            str(item["id"])
            for item in _cycle_wrap(flow, cycle_id)
            if str(item.get("id") or "")
        }
        ledger_id = str(spec.get("ledger") or "")
        invalidators = set(wrapped_ids)
        if ledger_id:
            invalidators.add(ledger_id)
        if affected.intersection(invalidators):
            invalidated_cycles.append((cycle_id, wrapped_ids))
            # Cycle wrappers are control-flow dependants even when the static
            # artifact graph does not list them as ordinary downstream nodes.
            # Include them in the affected set so stale judge/model budgets in
            # work/<step>/ cannot survive an upstream replacement or workflow
            # adoption and poison the fresh cycle.
            affected.update(wrapped_ids)
    for cycle_id, wrapped_ids in invalidated_cycles:
        cycle_root = Path(run_dir) / "cycles" / cycle_id
        if cycle_root.exists():
            shutil.rmtree(cycle_root)
        for step_id in wrapped_ids:
            items_root = Path(run_dir) / "milestones" / step_id / "items"
            if items_root.exists():
                shutil.rmtree(items_root)
    for step_id in affected:
        for path in (
            Path(run_dir) / "milestones" / step_id / "out",
            Path(run_dir) / "milestones" / step_id / "work",
            work_dir(run_dir, step_id),
            Path(run_dir) / "runtime-tasks" / step_id,
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
    # Preserve a branch decision when replacing work strictly downstream of
    # its still-chosen branch source. Clearing it makes every ``on_path`` step
    # look eligible and can execute a mutating workflow's wrong branch.
    branch_from = str(record.get("branch_from") or "")
    if not branch_from or branch_from in affected:
        record.pop("active_branch", None)
        record.pop("branch_from", None)
    for key in (
        "cycle_id",
        "cycle_row",
        "cycle_round",
        "cycle_row_attempt",
        "cycle_done",
        "cycle_restart",
    ):
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


@_track_active_time
def advance(
    skill_dir: Path,
    run_dir: Path,
    *,
    request_path: Path | None = None,
    draft_path: Path | None = None,
    draft_for: str | None = None,
    flow_arg: str | None = None,
    replace_milestone: str | None = None,
    continue_after_edit: str | None = None,
    cache_mode: str | None = None,
    cache_namespace: str | None = None,
    origin: str = "fresh",
    parent_goal: str | None = None,
    goal_row: str | None = None,
    harness_root: Path | None = None,
    source_code_root: Path | None = None,
) -> dict[str, Any]:
    skill_dir = Path(os.path.abspath(str(skill_dir)))
    run_dir = Path(os.path.abspath(str(run_dir)))
    flow = load_flow(skill_dir, find_flow_path(skill_dir, flow_arg) if flow_arg else None)
    requested_cache_mode = validate_cache_mode(cache_mode) if cache_mode is not None else None
    initialized_lock = initialize_run(
        run_dir,
        skill_dir,
        flow,
        request_path,
        harness_root=harness_root,
        source_code_root=source_code_root,
        cache_mode=requested_cache_mode,
        cache_namespace=cache_namespace,
        origin=origin,
        parent_goal=parent_goal,
        goal_row=goal_row,
    )
    if replace_milestone and continue_after_edit:
        raise FlowError("use either --replace-milestone or --continue-after-edit, not both")
    # Read caller feedback before implementation adoption invalidates milestone
    # work directories. A standard draft path may live under work/<milestone>/,
    # which replace_milestone_state intentionally removes.
    pending_draft = read_json(draft_path) if draft_path else None
    if draft_path and not isinstance(pending_draft, dict):
        raise FlowError("--draft must contain one JSON object")
    if draft_for and pending_draft is None:
        raise FlowError("--draft-for requires --draft")
    pending_draft_for = str(draft_for or "").strip() or None
    if pending_draft_for and not any(step["id"] == pending_draft_for for step in flow["steps"]):
        raise FlowError(f"unknown --draft-for milestone: {pending_draft_for}")
    adopted: set[str] = set()
    if continue_after_edit:
        lock, adopted = adopt_implementation_change(run_dir, skill_dir, flow, continue_after_edit)
        replaced = replace_milestone_state(run_dir, flow, continue_after_edit)
        if replaced != adopted:
            journal = read_json(run_dir / "workflow-adoption.json")
            journal["invalidated_milestones"] = sorted(replaced)
            write_json(run_dir / "workflow-adoption.json", journal, overwrite=True)
            adopted = replaced
        _mark_adoption_invalidated(run_dir)
        _finalize_implementation_change(run_dir, flow, lock)
    else:
        # A fresh run has just computed and frozen this exact lock. Re-hashing
        # the same implementation set immediately can double initialization
        # time on filtered Windows filesystems without adding integrity.
        lock = initialized_lock or assert_implementation_lock(run_dir, skill_dir, flow)
        _reconcile_implementation_adoption(run_dir, lock)
    record = read_json(_execution_path(run_dir))
    frozen_cache = record.get("cache") if isinstance(record.get("cache"), dict) else {"mode": "off", "namespace": "local"}
    frozen_mode = validate_cache_mode(str(frozen_cache.get("mode") or "off"))
    frozen_namespace = str(frozen_cache.get("namespace") or "local")
    run_context = read_json(run_dir / "run-context.json")
    storage = (
        run_context.get("storage_contract")
        if isinstance(run_context.get("storage_contract"), dict)
        else {}
    )
    if storage:
        validate_run_storage_contract(run_dir, storage)
    legacy_cache_root = None
    if not storage:
        legacy_codebase = infer_codebase(skill_dir)
        if legacy_codebase is not None:
            legacy_cache_root = str(legacy_codebase / "flowsteps" / "cache")
    if requested_cache_mode is not None and requested_cache_mode != frozen_mode:
        raise FlowError(f"run cache mode is frozen as {frozen_mode}; start a fresh run")
    if cache_namespace is not None and str(cache_namespace) != frozen_namespace:
        raise FlowError("run cache namespace is frozen; start a fresh run")
    flow["_cache_runtime"] = {
        "mode": frozen_mode,
        "namespace": frozen_namespace,
        "bypass_ids": [],
        "root": storage.get("cache_root") or legacy_cache_root,
    }
    if replace_milestone:
        affected = replace_milestone_state(run_dir, flow, replace_milestone)
        flow["_cache_runtime"]["bypass_ids"] = sorted(affected)
    elif continue_after_edit:
        flow["_cache_runtime"]["bypass_ids"] = sorted(adopted)
    record = read_json(_execution_path(run_dir))
    if record["status"] == "BLOCKED":
        raise FlowError("run is terminal BLOCKED; start a fresh run")
    if _active_seconds(record) > float(flow["max_run_seconds"]):
        raise FlowError("run exceeded the frozen active-execution budget; start a fresh run")
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
                _, wait_bindings = bind_inputs(run_dir, flow, step)
                return _isolate_action(
                    run_dir,
                    skill_dir,
                    flow,
                    _pause_wait_action(run_dir, flow, step, wait_bindings),
                )
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
            if not chosen_path.is_file():
                try:
                    recover_incomplete_chosen_output(run_dir, flow, step)
                except FlowError as exc:
                    _, recovery_bindings = bind_inputs(run_dir, flow, step)
                    return _write_blocked(
                        run_dir,
                        flow,
                        step,
                        recovery_bindings,
                        lock["fingerprint_sha256"],
                        [f"{step['id']}: incomplete chosen commit could not be recovered; {exc}"],
                    )
            orphan_action = _restore_orphan_artifact(run_dir, flow, step)
            if orphan_action is not None:
                return orphan_action
            if step["id"] in completed:
                chosen = load_chosen_output(
                    run_dir,
                    step["id"],
                    step=step,
                    skill_dir=skill_dir,
                )
                validate_against_schema(chosen, chosen_output_schema_path())
                branch_from = str(record.get("branch_from") or "")
                needs_branch_reconcile = bool(step.get("branch")) and (
                    not str(record.get("active_branch") or "")
                    or branch_from == str(step["id"])
                )
                needs_cycle_reconcile = bool(step.get("cycle")) and not bool(
                    record.get("cycle_done")
                )
                if needs_branch_reconcile or needs_cycle_reconcile:
                    action = _resume_chosen_step(
                        skill_dir,
                        run_dir,
                        flow,
                        step,
                        lock["fingerprint_sha256"],
                    )
                    if action is not None:
                        return _isolate_action(run_dir, skill_dir, flow, action)
                    record = read_json(_execution_path(run_dir))
                    if record.get("cycle_restart"):
                        record["cycle_restart"] = False
                        write_json(_execution_path(run_dir), record, overwrite=True)
                        restart = True
                        break
                    completed = {item["step_id"] for item in record["steps"]}
                continue
            if chosen_path.is_file():
                action = _resume_chosen_step(
                    skill_dir,
                    run_dir,
                    flow,
                    step,
                    lock["fingerprint_sha256"],
                )
                if action is not None:
                    return _isolate_action(run_dir, skill_dir, flow, action)
                record = read_json(_execution_path(run_dir))
                if record.get("cycle_restart"):
                    record["cycle_restart"] = False
                    write_json(_execution_path(run_dir), record, overwrite=True)
                    restart = True
                    break
                completed = {item["step_id"] for item in record["steps"]}
                continue
            budget = (step.get("params") or {}).get("step_budget_seconds")
            task_path = run_dir / "runtime-tasks" / f"{step['id']}.json"
            if budget and task_path.is_file():
                task = read_json(task_path)
                started_active = float(task.get("active_seconds_at_start") or 0.0)
                if _active_seconds(record) - started_active > float(budget):
                    _, bindings = bind_inputs(run_dir, flow, step)
                    return _write_blocked(
                        run_dir, flow, step, bindings, lock["fingerprint_sha256"], ["STEP_BUDGET_EXCEEDED"]
                    )
            draft = None
            if pending_draft is not None and (
                pending_draft_for is None or pending_draft_for == step["id"]
            ):
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
                isolated = _isolate_action(run_dir, skill_dir, flow, action)
                if pending_draft is not None and pending_draft_for:
                    isolated["deferred_draft_for"] = pending_draft_for
                return isolated
            record = read_json(_execution_path(run_dir))
            completed = {item["step_id"] for item in record["steps"]}
            if record.get("cycle_restart"):
                record["cycle_restart"] = False
                write_json(_execution_path(run_dir), record, overwrite=True)
                restart = True
                break
    if pending_draft is not None:
        raise FlowError(
            f"draft target {pending_draft_for or '(next milestone)'} was not reached; draft was not consumed"
        )
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
        "run_dir": str(Path(os.path.abspath(str(run_dir)))),
        "steps": len(record["steps"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_harness_location_args(parser)
    parser.add_argument(
        "--harness-root",
        type=Path,
        default=None,
        help="Host-local execution root. Defaults to M8M_HARNESS_ROOT or the system-drive NisanRuntime folder.",
    )
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument(
        "--run-mode",
        choices=["fresh", "resume"],
        default="fresh",
        help="fresh is the default and refuses an existing session; resume reuses one exact run folder",
    )
    parser.add_argument("--request", type=Path)
    parser.add_argument("--draft", type=Path)
    parser.add_argument(
        "--draft-for",
        help="Bind --draft to one milestone so recovery cannot consume it downstream",
    )
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
    debug_stack_seconds = int(os.environ.get("M8M_DEBUG_STACK_SECONDS", "0") or "0")
    if debug_stack_seconds > 0:
        faulthandler.dump_traceback_later(debug_stack_seconds, repeat=True)
    skill_dir = harness_dir_from_args(args)
    run_dir = args.run_dir
    try:
        run_mode = "resume" if args.resume or args.replace_milestone or args.continue_after_edit else args.run_mode
        harness_root = Path(
            os.path.abspath(str(args.harness_root or default_harness_root()))
        )
        codebase = infer_codebase(skill_dir)
        source_code_root = Path(args.codebase) if args.codebase else (codebase or skill_dir)
        if run_dir is None:
            flow = load_flow(skill_dir, find_flow_path(skill_dir, args.flow) if args.flow else None)
            if run_mode == "resume":
                harness_root = validate_fresh_harness_root(harness_root)
                found = find_paused_run(harness_root, str(flow.get("flow_id") or ""))
                if found is None:
                    raise FlowError("resume requires --run-dir or one paused run roster")
                run_dir = found
            else:
                harness_root = validate_fresh_harness_root(harness_root)
                run_dir = default_run_dir(harness_root, str(flow.get("flow_id") or "flow"))
        else:
            run_dir = Path(os.path.abspath(str(run_dir)))
            execution_exists = (run_dir / "flow-execution-record.json").is_file()
            # The product launcher creates the lock and request bootstrap
            # files before invoking the fresh runner.  They are not prior run
            # state; every other pre-existing entry remains a hard refusal.
            bootstrap = {".run.lock", "request.json"}
            if run_mode == "fresh":
                harness_root = validate_fresh_harness_root(harness_root)
                validate_fresh_run_dir(harness_root, run_dir)
                admissible = (
                    not run_dir.is_dir()
                    or _is_admissible_browser_evidence_bootstrap(run_dir, bootstrap)
                )
                if not admissible:
                    raise FlowError(
                        "fresh rerun refuses prior state; only a complete hash-bound browser evidence bootstrap is admissible"
                    )
            if run_mode == "resume" and not execution_exists:
                raise FlowError("resume --run-dir must name one exact initialized M8M run folder")
        try:
            bind_runtime_to_run(
                skill_dir,
                run_dir,
                run_mode=run_mode,
                executing_entrypoint=Path(__file__),
            )
        except RuntimeReleaseError as exc:
            raise FlowError(str(exc)) from exc
        result = advance(
            skill_dir,
            run_dir,
            request_path=args.request,
            draft_path=args.draft,
            draft_for=args.draft_for,
            flow_arg=args.flow,
            replace_milestone=args.replace_milestone,
            continue_after_edit=args.continue_after_edit,
            cache_mode=args.cache_mode,
            cache_namespace=args.cache_namespace,
            harness_root=harness_root,
            source_code_root=source_code_root,
        )
        if isinstance(result, dict):
            result["run_dir"] = str(Path(os.path.abspath(str(run_dir))))
    except FlowError as exc:
        print(json.dumps(_blocked_action("", [str(exc)]), indent=2), file=sys.stderr)
        return 3
    finally:
        if debug_stack_seconds > 0:
            faulthandler.cancel_dump_traceback_later()
    print(json.dumps(result, indent=2))
    return 0 if result["state"] in {"ACTION_REQUIRED", "COMPLETE"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
