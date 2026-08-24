"""Run one M8M workflow over a goal ledger using a fresh cache-off child session per row."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from flowstep_runtime import (
    FlowError,
    add_harness_location_args,
    assert_implementation_lock,
    goal_ledger_schema_path,
    harness_dir_from_args,
    implementation_lock,
    load_flow,
    read_json,
    utc_now,
    validate_against_schema,
    write_json,
)
from flowstep_tools import infer_codebase
from run_flow import advance
from session_layout import chosen_output_path, default_run_dir


ROW_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _ledger_path(goal_dir: Path) -> Path:
    return goal_dir / "goal-ledger.json"


def _save_ledger(goal_dir: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = utc_now()
    validate_against_schema(ledger, goal_ledger_schema_path())
    write_json(_ledger_path(goal_dir), ledger, overwrite=True)


def _new_goal_dir(codebase: Path, flow_id: str) -> Path:
    run = default_run_dir(codebase, f"goal_{flow_id}")
    return codebase / "flowsteps" / "goals" / flow_id / run.name


def _initialize_goal(
    goal_dir: Path,
    skill_dir: Path,
    flow: dict[str, Any],
    source_path: Path | None,
) -> dict[str, Any]:
    ledger_path = _ledger_path(goal_dir)
    if ledger_path.is_file():
        return read_json(ledger_path)
    if source_path is None or not source_path.is_file():
        raise FlowError("a new goal requires --goal <json>")
    source = read_json(source_path)
    if not isinstance(source, dict) or not isinstance(source.get("rows"), list) or not source["rows"]:
        raise FlowError("goal JSON requires a non-empty rows array")
    goal_id = str(source.get("goal_id") or goal_dir.name)
    if not ROW_ID_RE.fullmatch(goal_id):
        raise FlowError(f"invalid goal_id: {goal_id}")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(source["rows"]):
        if not isinstance(raw, dict) or not isinstance(raw.get("request"), dict):
            raise FlowError(f"goal row {index} requires an object request")
        row_id = str(raw.get("id") or f"{index + 1:03d}")
        if not ROW_ID_RE.fullmatch(row_id) or row_id in seen:
            raise FlowError(f"invalid or duplicate goal row id: {row_id}")
        seen.add(row_id)
        rows.append(
            {
                "id": row_id,
                "name": str(raw.get("name") or row_id),
                "request": raw["request"],
                "status": "pending",
                "attempt": 0,
            }
        )
    goal_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, goal_dir / "goal-request.json")
    lock = implementation_lock(skill_dir, flow)
    write_json(goal_dir / "implementation-lock.json", lock, overwrite=False)
    shutil.copy2(Path(flow["_flow_path"]), goal_dir / "flow-snapshot.yaml")
    ledger = {
        "schema": "m8m_goal_ledger_v1",
        "goal_id": goal_id,
        "flow_id": flow["flow_id"],
        "status": "in_progress",
        "context_policy": "isolated_per_row",
        "cache_mode": "off",
        "implementation_fingerprint_sha256": lock["fingerprint_sha256"],
        "rows": rows,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    _save_ledger(goal_dir, ledger)
    return ledger


def _goal_lock(goal_dir: Path, skill_dir: Path, flow: dict[str, Any]) -> dict[str, Any]:
    return assert_implementation_lock(goal_dir, skill_dir, flow)


def _current_row(ledger: dict[str, Any], *, allow_blocked: bool) -> dict[str, Any] | None:
    active = {"running"}
    if allow_blocked:
        active.add("blocked")
    for row in ledger["rows"]:
        if row["status"] in active:
            return row
    return next((row for row in ledger["rows"] if row["status"] == "pending"), None)


def _start_row(goal_dir: Path, ledger: dict[str, Any], row: dict[str, Any]) -> Path:
    row["attempt"] = int(row.get("attempt") or 0) + 1
    child = goal_dir / "rows" / row["id"] / f"attempt-{row['attempt']:03d}" / "run"
    request = goal_dir / "rows" / row["id"] / f"attempt-{row['attempt']:03d}" / "request.json"
    write_json(request, row["request"], overwrite=False)
    row["child_run_dir"] = str(child.resolve())
    row["status"] = "running"
    row.pop("last_state", None)
    row.pop("chosen_output", None)
    row.pop("blockers", None)
    ledger["status"] = "in_progress"
    _save_ledger(goal_dir, ledger)
    return request


def advance_goal(
    skill_dir: Path,
    goal_dir: Path,
    *,
    goal_path: Path | None = None,
    draft_path: Path | None = None,
    continue_after_edit: str | None = None,
    abandon_row: bool = False,
) -> dict[str, Any]:
    skill_dir = skill_dir.resolve()
    goal_dir = goal_dir.resolve()
    flow = load_flow(skill_dir)
    ledger = _initialize_goal(goal_dir, skill_dir, flow, goal_path)
    if ledger.get("flow_id") != flow["flow_id"]:
        raise FlowError("goal ledger belongs to a different workflow")
    row = _current_row(ledger, allow_blocked=bool(continue_after_edit or abandon_row))
    if row is None and any(item["status"] == "blocked" for item in ledger["rows"]):
        blocked = next(item for item in ledger["rows"] if item["status"] == "blocked")
        return {
            "schema": "m8m_goal_action_v1",
            "state": "BLOCKED",
            "goal_id": ledger["goal_id"],
            "goal_dir": str(goal_dir),
            "row_id": blocked["id"],
            "blockers": blocked.get("blockers") or ["goal row is blocked"],
        }
    if abandon_row:
        if row is None or row["status"] not in {"running", "blocked"}:
            raise FlowError("--abandon-row requires one running or blocked row")
        prior_value = str(row.get("child_run_dir") or "").strip()
        if prior_value:
            prior = Path(prior_value)
            write_json(
                prior.parent / "abandoned.json",
                {"schema": "m8m_abandoned_attempt_v1", "row_id": row["id"], "abandoned_at": utc_now()},
                overwrite=True,
            )
        row["status"] = "pending"
        row.pop("child_run_dir", None)
        row.pop("last_state", None)
        row.pop("blockers", None)
        ledger["status"] = "in_progress"
        _save_ledger(goal_dir, ledger)
        row = _current_row(ledger, allow_blocked=False)
    if continue_after_edit:
        if row is None or row["status"] not in {"running", "blocked"} or not row.get("child_run_dir"):
            raise FlowError("--continue-after-edit requires one existing running or blocked child run")
    else:
        _goal_lock(goal_dir, skill_dir, flow)

    pending_draft = draft_path
    adoption = continue_after_edit
    while True:
        row = _current_row(ledger, allow_blocked=bool(adoption))
        if row is None:
            ledger["status"] = "complete"
            _save_ledger(goal_dir, ledger)
            return {
                "schema": "m8m_goal_action_v1",
                "state": "COMPLETE",
                "goal_id": ledger["goal_id"],
                "goal_dir": str(goal_dir),
                "rows": len(ledger["rows"]),
            }
        request_path = None
        if row["status"] == "pending":
            request_path = _start_row(goal_dir, ledger, row)
        child = Path(str(row["child_run_dir"])).resolve()
        action = advance(
            skill_dir,
            child,
            request_path=request_path,
            draft_path=pending_draft,
            continue_after_edit=adoption,
            cache_mode="off",
            origin="goal_child",
            parent_goal=ledger["goal_id"],
            goal_row=row["id"],
        )
        pending_draft = None
        if adoption:
            lock = implementation_lock(skill_dir, flow)
            write_json(goal_dir / "implementation-lock.json", lock, overwrite=True)
            shutil.copy2(Path(flow["_flow_path"]), goal_dir / "flow-snapshot.yaml")
            ledger["implementation_fingerprint_sha256"] = lock["fingerprint_sha256"]
            adoption = None
        row["last_state"] = str(action["state"])
        if action["state"] == "COMPLETE":
            row["status"] = "done"
            row["chosen_output"] = str(chosen_output_path(child, flow["steps"][-1]["id"]).resolve())
            _save_ledger(goal_dir, ledger)
            continue
        if action["state"] == "ACTION_REQUIRED":
            row["status"] = "running"
            _save_ledger(goal_dir, ledger)
            return {
                "schema": "m8m_goal_action_v1",
                "state": "ACTION_REQUIRED",
                "goal_id": ledger["goal_id"],
                "goal_dir": str(goal_dir),
                "row_id": row["id"],
                "child_run_dir": str(child),
                "context_policy": "isolated",
                "child_action": action,
            }
        row["status"] = "blocked"
        row["blockers"] = list(action.get("blockers") or ["child workflow blocked"])
        ledger["status"] = "blocked"
        _save_ledger(goal_dir, ledger)
        return {
            "schema": "m8m_goal_action_v1",
            "state": "BLOCKED",
            "goal_id": ledger["goal_id"],
            "goal_dir": str(goal_dir),
            "row_id": row["id"],
            "child_run_dir": str(child),
            "blockers": row["blockers"],
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_harness_location_args(parser)
    parser.add_argument("--goal", type=Path, help="Goal JSON with rows of {id, name, request}")
    parser.add_argument("--goal-dir", type=Path, help="Existing goal folder to resume")
    parser.add_argument("--draft", type=Path, help="Draft for the current child ACTION_REQUIRED")
    parser.add_argument("--continue-after-edit", metavar="MILESTONE")
    parser.add_argument("--abandon-row", action="store_true", help="Leave the current child intact and start a fresh attempt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        skill_dir = harness_dir_from_args(args, require_existing=True)
        flow = load_flow(skill_dir)
        codebase = infer_codebase(skill_dir) or skill_dir
        goal_dir = args.goal_dir or _new_goal_dir(codebase, flow["flow_id"])
        result = advance_goal(
            skill_dir,
            goal_dir,
            goal_path=args.goal,
            draft_path=args.draft,
            continue_after_edit=args.continue_after_edit,
            abandon_row=args.abandon_row,
        )
    except FlowError as exc:
        print(json.dumps({"schema": "m8m_goal_action_v1", "state": "BLOCKED", "blockers": [str(exc)]}, indent=2), file=sys.stderr)
        return 3
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["state"] in {"ACTION_REQUIRED", "COMPLETE"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
