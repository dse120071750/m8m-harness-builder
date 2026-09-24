"""Optional cross-run candidate cache. Never a source of M8M workflow state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flowstep_runtime import (
    FlowError,
    cache_receipt_schema_path,
    candidate_cache_schema_path,
    canonical_json,
    local_tool_package_name,
    read_json,
    runtime_package_name,
    sha256_bytes,
    sha256_file,
    utc_now,
    validate_against_schema,
    write_json,
)
from session_layout import infer_harness_root_from_run


CACHE_SCHEMA = "m8m_candidate_cache_entry_v1"
CACHE_MODES = {"off", "read", "write", "read-write"}
RECEIPT_SCHEMA = "m8m_cache_receipt_v1"
CONTROL_KEYS = {
    "address",
    "attempt",
    "cycle_round",
    "done",
    "ledger",
    "remaining",
    "run_dir",
    "run_id",
}
ROW_CONTROL_KEYS = {"attempt", "created_at", "status", "updated_at"}
IMPLEMENTATION_SUFFIXES = {".json", ".md", ".py", ".yaml", ".yml"}


def validate_cache_mode(mode: str) -> str:
    value = str(mode or "off").strip().lower()
    if value not in CACHE_MODES:
        raise FlowError(f"cache mode must be one of {sorted(CACHE_MODES)}")
    return value


def cache_reads(mode: str) -> bool:
    return validate_cache_mode(mode) in {"read", "read-write"}


def cache_writes(mode: str) -> bool:
    return validate_cache_mode(mode) in {"write", "read-write"}


def namespace_sha256(namespace: str) -> str:
    return sha256_bytes(str(namespace or "local").encode("utf-8"))


def _project_root(skill_dir: Path) -> Path:
    root = Path(skill_dir).resolve()
    if root.parent.name == "flows" and root.parent.parent.name == "flowsteps":
        return root.parent.parent.parent
    return root


def cache_root(
    skill_dir: Path,
    flow: dict[str, Any],
    *,
    runtime_root: Path | None = None,
) -> Path:
    if runtime_root is not None:
        return Path(runtime_root).resolve() / "cache" / "v1" / str(flow["flow_id"])
    # Legacy run contexts did not freeze a host-local cache root. Keep their
    # exact project cache only for explicit resume; new runs always pass one.
    return _project_root(skill_dir) / "flowsteps" / "cache" / "v1" / str(flow["flow_id"])


def _safe_row_id(row_id: str | None) -> str:
    value = str(row_id or "").strip()
    if value and re.fullmatch(r"[A-Za-z0-9._-]+", value):
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else ""


def cache_work_dir(run_dir: Path, step: dict[str, Any], row_id: str | None = None) -> Path:
    if row_id:
        return Path(run_dir) / "milestones" / str(step["id"]) / "items" / _safe_row_id(row_id) / "work"
    return Path(run_dir) / "milestones" / str(step["id"]) / "work"


def cache_receipt_path(run_dir: Path, step: dict[str, Any], row_id: str | None = None) -> Path:
    return cache_work_dir(run_dir, step, row_id) / "cache-receipt.json"


def _write_receipt(
    run_dir: Path,
    step: dict[str, Any],
    status: str,
    *,
    row_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = cache_receipt_path(run_dir, step, row_id)
    existing = read_json(path) if path.is_file() else {}
    payload = {
        "schema": RECEIPT_SCHEMA,
        "milestone_id": step["id"],
        "status": status,
        "updated_at": utc_now(),
    }
    if row_id:
        payload["cycle_row"] = str(row_id)
    if isinstance(existing, dict):
        for key in (
            "cache_key_sha256",
            "entry",
            "input_fingerprint_sha256",
            "implementation_fingerprint_sha256",
            "lookup_status",
            "namespace_sha256",
            "write_status",
        ):
            if key in existing:
                payload[key] = existing[key]
    if details:
        payload.update(details)
    validate_against_schema(payload, cache_receipt_schema_path())
    write_json(path, payload, overwrite=True)
    return payload


def mark_cache_rejected(
    run_dir: Path,
    step: dict[str, Any],
    *,
    row_id: str | None = None,
    reason: str = "current judge rejected cached candidate",
) -> None:
    _write_receipt(
        run_dir,
        step,
        "rejected",
        row_id=row_id,
        details={"lookup_status": "rejected", "reason": reason},
    )


def mark_cache_invalid(
    run_dir: Path,
    step: dict[str, Any],
    *,
    row_id: str | None = None,
    reason: str,
) -> None:
    _write_receipt(
        run_dir,
        step,
        "invalid",
        row_id=row_id,
        details={"lookup_status": "invalid", "reason": reason},
    )


def _file_identity(path: Path) -> dict[str, Any]:
    return {
        "kind": "file-bytes",
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
    }


def _semantic(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key in sorted(value):
            if key in CONTROL_KEYS:
                continue
            item = value[key]
            if key == "path" and isinstance(item, str):
                path = Path(item)
                if path.is_file():
                    result[key] = _file_identity(path)
                    continue
            result[str(key)] = _semantic(item)
        return result
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    if isinstance(value, str):
        path = Path(value)
        if path.is_file():
            return _file_identity(path)
    return value


def semantic_inputs(input_data: dict[str, Any], row_id: str | None = None) -> dict[str, Any]:
    source = {key: value for key, value in input_data.items() if key not in CONTROL_KEYS | {"row", "ledger_row"}}
    result = _semantic(source)
    if row_id:
        ledger = input_data.get("ledger")
        row = None
        if isinstance(ledger, dict):
            rows = ledger.get("rows")
            if isinstance(rows, list):
                row = next((item for item in rows if isinstance(item, dict) and str(item.get("id")) == str(row_id)), None)
        if isinstance(row, dict):
            result["cycle_row"] = _semantic({key: value for key, value in row.items() if key not in ROW_CONTROL_KEYS})
        else:
            result["cycle_row"] = {"id": str(row_id)}
    return result


def _skill_file(skill_dir: Path, relative: str | None) -> Path | None:
    if not relative:
        return None
    path = (Path(skill_dir) / str(relative)).resolve()
    return path if path.is_file() else None


def milestone_implementation_fingerprint(skill_dir: Path, step: dict[str, Any]) -> str:
    files: dict[str, str] = {}
    for field in ("handler", "output_schema", "draft_schema", "receipt_schema", "gem"):
        path = _skill_file(skill_dir, step.get(field))
        if path is not None:
            files[f"milestone:{field}:{path.name}"] = sha256_file(path)
    project = _project_root(skill_dir)
    implementation_tools = {
        local_tool_package_name(
            str(item.get("ref") or ""),
            label=(
                f"{step.get('id')}.execution.tool_bindings"
                f"[{item.get('tool')}].ref"
            ),
        )
        for item in (step.get("execution") or {}).get("tool_bindings") or []
        if isinstance(item, dict) and item.get("ref")
    }
    if step.get("worker"):
        worker = str(step["worker"])
        implementation_tools.add(
            runtime_package_name(worker, label=f"{step.get('id')}.worker")
            if step.get("judge_abi")
            else worker
        )
    for tool_id in sorted(implementation_tools):
        tool_root = project / "flowsteps" / "tools" / tool_id
        if not tool_root.is_dir():
            continue
        for path in sorted(tool_root.rglob("*")):
            if path.is_file() and path.suffix.lower() in IMPLEMENTATION_SUFFIXES and "__pycache__" not in path.parts:
                files[f"tool:{tool_id}/{path.relative_to(tool_root).as_posix()}"] = sha256_file(path)
    definition = {
        "id": step["id"],
        "success": step.get("success"),
        "output_contract": step.get("output_contract"),
        "outputs": step.get("outputs") or [],
        "flowsteps": step.get("flowsteps") or [],
        "tools": step.get("tools") or [],
        "worker": step.get("worker"),
        "judge_abi": step.get("judge_abi"),
        "model": step.get("model"),
        "intelligence": step.get("intelligence"),
        "execution": step.get("execution") or {},
    }
    return sha256_bytes(canonical_json({"definition": definition, "files": files}))


def _key_info(
    skill_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    input_data: dict[str, Any],
    namespace: str,
    row_id: str | None,
) -> dict[str, str]:
    semantic = semantic_inputs(input_data, row_id)
    input_fingerprint = sha256_bytes(canonical_json(semantic))
    implementation = milestone_implementation_fingerprint(skill_dir, step)
    namespace_hash = namespace_sha256(namespace)
    cache_key = sha256_bytes(
        canonical_json(
            {
                "schema": CACHE_SCHEMA,
                "flow_id": flow["flow_id"],
                "flow_version": flow["version"],
                "milestone_id": step["id"],
                "output_contract": step["output_contract"],
                "namespace_sha256": namespace_hash,
                "input_fingerprint_sha256": input_fingerprint,
                "implementation_fingerprint_sha256": implementation,
            }
        )
    )
    return {
        "cache_key_sha256": cache_key,
        "input_fingerprint_sha256": input_fingerprint,
        "implementation_fingerprint_sha256": implementation,
        "namespace_sha256": namespace_hash,
    }


def _entry_dir(
    skill_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    info: dict[str, str],
    *,
    cache_root_path: Path | None = None,
) -> Path:
    base = (
        Path(cache_root_path).resolve() / "v1" / str(flow["flow_id"])
        if cache_root_path is not None
        else cache_root(skill_dir, flow)
    )
    return base / info["namespace_sha256"] / str(step["id"]) / info["cache_key_sha256"]


def _validate_entry_structure(
    entry: dict[str, Any],
    flow: dict[str, Any],
    step: dict[str, Any],
    info: dict[str, str],
) -> datetime:
    """Validate cache-only identity and bundle topology before hydrating bytes."""
    expected_identity = {
        "flow_id": flow["flow_id"],
        "flow_version": flow["version"],
        "milestone_id": step["id"],
        "output_contract": step["output_contract"],
        **info,
    }
    for key, value in expected_identity.items():
        if entry.get(key) != value:
            raise FlowError(f"cache {key} mismatch")

    declarations = {str(item["id"]): item for item in step.get("outputs") or []}
    outputs = entry.get("outputs") or []
    output_ids = [str(item.get("id") or "") for item in outputs]
    if len(output_ids) != len(set(output_ids)):
        raise FlowError("cache output ids are not unique")
    present = set(output_ids)
    missing_required = [
        output_id
        for output_id, declaration in declarations.items()
        if declaration.get("required") and output_id not in present
    ]
    if missing_required:
        raise FlowError(f"cache required outputs are missing: {missing_required}")

    members = entry.get("members") or []
    member_ids = [str(item.get("id") or "") for item in members]
    if len(member_ids) != len(set(member_ids)):
        raise FlowError("cache member ids are not unique")
    member_map = {str(item["id"]): item for item in members}
    referenced: list[str] = []
    for output in outputs:
        output_id = str(output["id"])
        declaration = declarations.get(output_id)
        if declaration is None:
            raise FlowError(f"cache output is undeclared: {output_id}")
        for field in ("id", "name", "kind", "cardinality", "required"):
            if output.get(field) != declaration.get(field):
                raise FlowError(f"cache output declaration mismatch: {output_id}.{field}")
        ids = [str(item) for item in output.get("member_ids") or []]
        if len(ids) != len(set(ids)):
            raise FlowError(f"cache output has duplicate members: {output_id}")
        if output["cardinality"] == "one" and len(ids) != 1:
            raise FlowError(f"cache output cardinality mismatch: {output_id}")
        if output.get("required") and not ids:
            raise FlowError(f"cache required output is empty: {output_id}")
        for member_id in ids:
            member = member_map.get(member_id)
            if member is None:
                raise FlowError(f"cache output references missing member: {member_id}")
            if member.get("output_id") != output_id or member.get("kind") != output.get("kind"):
                raise FlowError(f"cache member declaration mismatch: {member_id}")
        referenced.extend(ids)
    if referenced != member_ids:
        raise FlowError("cache members are unreferenced or out of order")
    for index, member in enumerate(members):
        if member.get("order") != index:
            raise FlowError(f"cache member order mismatch: {member.get('id')}")

    created = datetime.fromisoformat(str(entry["created_at"]).replace("Z", "+00:00"))
    expires = datetime.fromisoformat(str(entry["expires_at"]).replace("Z", "+00:00"))
    if expires <= created:
        raise FlowError("cache expiry is not after creation")
    current_ttl = int((step.get("cache") or {})["ttl_seconds"])
    return min(expires, created + timedelta(seconds=current_ttl))


def _candidate_from_entry(entry_dir: Path, entry: dict[str, Any], target: Path) -> dict[str, Any]:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    members = {str(item["id"]): item for item in entry.get("members") or []}
    values: dict[str, Any] = {}
    for output in entry.get("outputs") or []:
        items: list[Any] = []
        for member_id in output.get("member_ids") or []:
            member = members[str(member_id)]
            source = (entry_dir / str(member["path"])).resolve()
            if entry_dir.resolve() not in source.parents or not source.is_file():
                raise FlowError(f"cache member missing or unsafe: {member_id}")
            if sha256_file(source) != member["sha256"]:
                raise FlowError(f"cache member digest mismatch: {member_id}")
            if member["kind"] in {"json", "data"}:
                value: Any = json.loads(source.read_text(encoding="utf-8"))
            else:
                member_dir = target / str(member_id)
                member_dir.mkdir(parents=True, exist_ok=True)
                copied = member_dir / source.name
                shutil.copy2(source, copied)
                value = {"path": str(copied.resolve())}
            if output["cardinality"] == "many":
                if isinstance(value, dict) and member["kind"] in {"json", "data"}:
                    value = {"id": member["id"], "name": member["name"], "value": value}
                elif isinstance(value, dict):
                    value = {"id": member["id"], "name": member["name"], **value}
                else:
                    value = {"id": member["id"], "name": member["name"], "value": value}
            items.append(value)
        values[str(output["id"])] = items if output["cardinality"] == "many" else (items[0] if items else None)
    candidate = {"outputs": values}
    write_json(target / "candidate.json", candidate, overwrite=True)
    return candidate


def _validate_hydrated_paths(candidate: dict[str, Any], step: dict[str, Any], target: Path) -> None:
    outputs = candidate.get("outputs") if isinstance(candidate.get("outputs"), dict) else {}
    root = target.resolve()
    for declaration in step.get("outputs") or []:
        if declaration.get("kind") in {"json", "data"}:
            continue
        output_id = str(declaration["id"])
        if output_id not in outputs:
            continue
        raw = outputs.get(output_id)
        values = raw if declaration.get("cardinality") == "many" else [raw]
        for item in values if isinstance(values, list) else []:
            value = item.get("value") if isinstance(item, dict) and "value" in item else item
            raw_path = value.get("path") if isinstance(value, dict) else value
            if not isinstance(raw_path, str):
                raise FlowError(f"cached media member needs a path: {declaration['id']}")
            path = Path(raw_path).resolve()
            if root not in path.parents or not path.is_file():
                raise FlowError(f"run-local cached media member is missing or unsafe: {declaration['id']}")


def prepare_candidate_cache(
    skill_dir: Path,
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    input_data: dict[str, Any],
    *,
    mode: str,
    namespace: str,
    cache_root_path: Path | None = None,
    bypass_read: bool = False,
    row_id: str | None = None,
) -> dict[str, Any]:
    policy = step.get("cache") if isinstance(step.get("cache"), dict) else None
    if not policy:
        return {"enabled": False, "candidate": None}
    if cache_root_path is None:
        cache_root_path = infer_harness_root_from_run(run_dir) / "cache"
    mode = validate_cache_mode(mode)
    scope = cache_work_dir(run_dir, step, row_id)
    scope.mkdir(parents=True, exist_ok=True)
    receipt = cache_receipt_path(run_dir, step, row_id)
    if receipt.is_file():
        recorded = read_json(receipt)
        candidate_path = scope / "cache-candidate" / "candidate.json"
        candidate = None
        if recorded.get("status") == "hit":
            if not candidate_path.is_file():
                recorded = _write_receipt(
                    run_dir,
                    step,
                    "invalid",
                    row_id=row_id,
                    details={"lookup_status": "invalid", "reason": "run-local cache candidate is missing"},
                )
            else:
                try:
                    candidate = read_json(candidate_path)
                    _validate_hydrated_paths(candidate, step, candidate_path.parent)
                except Exception as exc:
                    recorded = _write_receipt(
                        run_dir,
                        step,
                        "invalid",
                        row_id=row_id,
                        details={
                            "lookup_status": "invalid",
                            "reason": f"{type(exc).__name__}: {exc}",
                        },
                    )
        return {"enabled": True, "candidate": candidate, "receipt": recorded, "row_id": row_id}
    if mode == "off":
        recorded = _write_receipt(run_dir, step, "disabled", row_id=row_id, details={"reason": "run cache mode is off"})
        return {"enabled": True, "candidate": None, "receipt": recorded, "row_id": row_id}
    info = _key_info(skill_dir, flow, step, input_data, namespace, row_id)
    entry_dir = _entry_dir(
        skill_dir,
        flow,
        step,
        info,
        cache_root_path=cache_root_path,
    )
    info_path = scope / "cache-key.json"
    write_json(info_path, {**info, "entry": str(entry_dir.resolve())}, overwrite=True)
    base = {**info, "entry": str(entry_dir.resolve())}
    if bypass_read or not cache_reads(mode):
        reason = "replacement bypass" if bypass_read else "run cache mode does not read"
        recorded = _write_receipt(
            run_dir,
            step,
            "bypassed",
            row_id=row_id,
            details={**base, "lookup_status": "bypassed", "reason": reason},
        )
        return {"enabled": True, "candidate": None, "receipt": recorded, "row_id": row_id, **info}
    manifest_path = entry_dir / "cache-entry.json"
    if not manifest_path.is_file():
        recorded = _write_receipt(
            run_dir, step, "miss", row_id=row_id, details={**base, "lookup_status": "miss"}
        )
        return {"enabled": True, "candidate": None, "receipt": recorded, "row_id": row_id, **info}
    try:
        entry = read_json(manifest_path)
        validate_against_schema(entry, candidate_cache_schema_path())
        effective_expiry = _validate_entry_structure(entry, flow, step, info)
        if effective_expiry <= datetime.now(timezone.utc):
            recorded = _write_receipt(
                run_dir, step, "expired", row_id=row_id, details={**base, "lookup_status": "expired"}
            )
            return {"enabled": True, "candidate": None, "receipt": recorded, "row_id": row_id, **info}
        candidate = _candidate_from_entry(entry_dir, entry, scope / "cache-candidate")
        _validate_hydrated_paths(candidate, step, scope / "cache-candidate")
    except Exception as exc:
        recorded = _write_receipt(
            run_dir,
            step,
            "invalid",
            row_id=row_id,
            details={**base, "lookup_status": "invalid", "reason": f"{type(exc).__name__}: {exc}"},
        )
        return {"enabled": True, "candidate": None, "receipt": recorded, "row_id": row_id, **info}
    recorded = _write_receipt(
        run_dir, step, "hit", row_id=row_id, details={**base, "lookup_status": "hit"}
    )
    return {"enabled": True, "candidate": candidate, "receipt": recorded, "row_id": row_id, **info}


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
    if not cache_info or not cache_info.get("enabled") or not cache_writes(mode):
        return
    row_id = cache_info.get("row_id")
    scope = cache_work_dir(run_dir, step, row_id)
    key_path = scope / "cache-key.json"
    if not key_path.is_file():
        return
    info = read_json(key_path)
    entry_dir = Path(str(info["entry"])).resolve()
    parent = entry_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    lock_path = parent / f".{entry_dir.name}.lock"
    lock_fd: int | None = None
    temp_dir: Path | None = None
    old_dir: Path | None = None
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        temp_dir = Path(tempfile.mkdtemp(prefix=f".{entry_dir.name}.tmp-", dir=parent))
        members_root = temp_dir / "members"
        members_root.mkdir(parents=True, exist_ok=True)
        cached_members: list[dict[str, Any]] = []
        for member in chosen.get("members") or []:
            source = (Path(run_dir) / str(member["path"])).resolve()
            if Path(run_dir).resolve() not in source.parents or not source.is_file():
                raise FlowError(f"chosen member missing or unsafe: {member.get('id')}")
            member_dir = members_root / str(member["id"])
            member_dir.mkdir(parents=True, exist_ok=True)
            target = member_dir / source.name
            shutil.copy2(source, target)
            cached_members.append(
                {
                    "id": member["id"],
                    "output_id": member["output_id"],
                    "name": member["name"],
                    "kind": member["kind"],
                    "path": target.relative_to(temp_dir).as_posix(),
                    "mime_type": member["mime_type"],
                    "order": member["order"],
                    "sha256": sha256_file(target),
                }
            )
        now = datetime.now(timezone.utc)
        ttl = int((step.get("cache") or {})["ttl_seconds"])
        entry = {
            "schema": CACHE_SCHEMA,
            "flow_id": flow["flow_id"],
            "flow_version": flow["version"],
            "milestone_id": step["id"],
            "output_contract": step["output_contract"],
            "namespace_sha256": info["namespace_sha256"],
            "cache_key_sha256": info["cache_key_sha256"],
            "input_fingerprint_sha256": info["input_fingerprint_sha256"],
            "implementation_fingerprint_sha256": info["implementation_fingerprint_sha256"],
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(seconds=ttl)).isoformat().replace("+00:00", "Z"),
            "outputs": chosen.get("outputs") or [],
            "members": cached_members,
        }
        write_json(temp_dir / "cache-entry.json", entry, overwrite=False)
        validate_against_schema(entry, candidate_cache_schema_path())
        old = parent / f".{entry_dir.name}.old-{uuid.uuid4().hex}"
        if entry_dir.exists():
            entry_dir.rename(old)
            old_dir = old
        temp_dir.rename(entry_dir)
        temp_dir = None
        if old.exists():
            shutil.rmtree(old)
            old_dir = None
        existing = read_json(cache_receipt_path(run_dir, step, row_id))
        lookup = str(existing.get("lookup_status") or existing.get("status") or "bypassed")
        status = lookup if lookup in {"hit", "rejected"} else "stored"
        _write_receipt(
            run_dir,
            step,
            status,
            row_id=row_id,
            details={
                "cache_key_sha256": info["cache_key_sha256"],
                "namespace_sha256": info["namespace_sha256"],
                "entry": str(entry_dir),
                "lookup_status": lookup,
                "write_status": "stored",
            },
        )
    except Exception as exc:
        if old_dir is not None and old_dir.exists() and not entry_dir.exists():
            try:
                old_dir.rename(entry_dir)
                old_dir = None
            except OSError:
                pass
        try:
            _write_receipt(
                run_dir,
                step,
                "invalid",
                row_id=row_id,
                details={"write_status": "failed", "warning": f"{type(exc).__name__}: {exc}"},
            )
        except Exception:
            # Cache observability is best effort. A receipt I/O failure must
            # never escape into the already-PASS workflow state transition.
            pass
    finally:
        if temp_dir is not None and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        if lock_fd is not None:
            os.close(lock_fd)
        if lock_fd is not None and lock_path.exists():
            try:
                lock_path.unlink()
            except OSError:
                pass


def prune_expired(root: Path, *, now: datetime | None = None) -> dict[str, int]:
    current = now or datetime.now(timezone.utc)
    removed = 0
    kept = 0
    invalid = 0
    base = Path(root).resolve()
    if not base.is_dir():
        return {"removed": 0, "kept": 0, "invalid": 0}
    for manifest_path in list(base.rglob("cache-entry.json")):
        entry_dir = manifest_path.parent
        try:
            entry = read_json(manifest_path)
            validate_against_schema(entry, candidate_cache_schema_path())
            expires = datetime.fromisoformat(str(entry["expires_at"]).replace("Z", "+00:00"))
            expired = expires <= current
        except Exception:
            expired = True
            invalid += 1
        if expired:
            shutil.rmtree(entry_dir)
            removed += 1
        else:
            kept += 1
    return {"removed": removed, "kept": kept, "invalid": invalid}
