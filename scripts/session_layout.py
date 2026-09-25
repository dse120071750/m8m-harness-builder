"""M8M run-local session addresses and frozen milestone output slots."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from flowstep_runtime import FlowError, utc_now, validate_against_schema, write_json


def _absolute(path: str | os.PathLike[str]) -> Path:
    """Normalize lexically without Windows realpath filesystem probes."""
    return Path(os.path.abspath(str(path)))


RUN_STORAGE_SCHEMA = "m8m_run_storage_contract_v1"
RUN_STORAGE_POLICY = "host_local_execution_source_separate_v1"
SOURCE_ASSET_MANIFEST_SCHEMA = "m8m_source_asset_manifest_v1"
JUDGE_RECEIPT_SCHEMA = "m8m.milestone_judge_receipt.v1"


def judge_receipt_schema_path() -> Path:
    return Path(__file__).resolve().parents[1] / "contracts" / "m8m_milestone_judge_receipt_v1.schema.json"


def chosen_output_schema_path() -> Path:
    return Path(__file__).resolve().parents[1] / "contracts" / "m8m_chosen_output_v1.schema.json"


def default_harness_root() -> Path:
    """Return the host-local root for new mutable M8M execution state."""
    configured = os.environ.get("M8M_HARNESS_ROOT")
    if configured:
        return _absolute(configured)
    if os.name == "nt":
        system_drive = str(os.environ.get("SystemDrive") or "C:").rstrip("\\/")
        return _absolute(system_drive + "\\NisanRuntime")
    return _absolute(Path.home() / ".m8m" / "runtime")


def validate_fresh_harness_root(harness_root: Path) -> Path:
    """Fresh Windows execution is allowed only on the system volume."""
    root = _absolute(harness_root)
    if os.name == "nt":
        expected = str(os.environ.get("SystemDrive") or "C:").upper().rstrip("\\/")
        if root.drive.upper().rstrip("\\/") != expected:
            raise FlowError(
                "fresh M8M execution must use the Windows system drive; "
                f"expected {expected}, got {root.drive or '<none>'}"
            )
    return root


def validate_fresh_run_dir(harness_root: Path, run_dir: Path) -> Path:
    """Require an explicitly named fresh run to live below <root>/runs/."""
    root = validate_fresh_harness_root(harness_root)
    runs_root = _absolute(root / "runs")
    candidate = _absolute(run_dir)
    if candidate == runs_root or runs_root not in candidate.parents:
        raise FlowError(
            f"fresh --run-dir must be inside the active harness runs folder: {runs_root}"
        )
    return candidate


def infer_harness_root_from_run(run_dir: Path) -> Path:
    """Infer a local execution root for direct Python API callers."""
    run = _absolute(run_dir)
    if run.parent.name.lower() == "runs":
        return run.parent.parent
    return run.parent


def build_run_storage_contract(
    source_code_root: Path,
    harness_root: Path,
    run_dir: Path,
) -> dict[str, str]:
    execution_root = _absolute(harness_root)
    active_run = _absolute(run_dir)
    return {
        "schema": RUN_STORAGE_SCHEMA,
        "policy_id": RUN_STORAGE_POLICY,
        "source_code_root": str(_absolute(source_code_root)),
        "execution_root": str(execution_root),
        "active_run_dir": str(active_run),
        "cache_root": str(_absolute(execution_root / "cache")),
        "source_asset_policy": "materialize_once_into_active_run",
        "legacy_migration_policy": "never_automatic",
    }


def validate_run_storage_contract(run_dir: Path, contract: dict[str, Any]) -> dict[str, Any]:
    """Fail closed if a v2 run was moved outside its frozen execution root."""
    if contract.get("schema") != RUN_STORAGE_SCHEMA or contract.get("policy_id") != RUN_STORAGE_POLICY:
        raise FlowError("run storage contract is missing or unsupported")
    active_run = _absolute(str(contract.get("active_run_dir") or ""))
    execution_root = _absolute(str(contract.get("execution_root") or ""))
    cache = _absolute(str(contract.get("cache_root") or ""))
    current = _absolute(run_dir)
    if active_run != current:
        raise FlowError(f"run storage contract active directory mismatch: {active_run}")
    if current == execution_root or execution_root not in current.parents:
        raise FlowError(f"active run is outside its frozen execution root: {execution_root}")
    if cache != _absolute(execution_root / "cache"):
        raise FlowError("run storage contract cache root does not match the execution root")
    if current.exists() and execution_root.exists() and _path_volume(current) != _path_volume(execution_root):
        raise FlowError("active run does not physically resolve to its frozen execution volume")
    return contract


def _link_or_copy_immutable(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> str:
    """Promote immutable bytes without a redundant same-volume CopyFile2 call.

    Cycle promotion can be resumed after an interrupted round, so a partial
    destination may already exist.  Remove only that unpublished destination,
    then prefer a hard link.  Volumes without hard-link support retain the
    previous copy2 behavior.
    """

    source_path = Path(source)
    destination_path = Path(destination)
    if destination_path.exists():
        try:
            if os.path.samefile(source_path, destination_path):
                return str(destination_path)
        except OSError:
            pass
        destination_path.unlink()
    try:
        os.link(source_path, destination_path)
        return str(destination_path)
    except OSError:
        return shutil.copy2(source_path, destination_path)


def default_run_dir(harness_root: Path, flow_id: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short = secrets.token_hex(3)
    run_id = f"{flow_id}_{stamp}_{short}"
    return _absolute(harness_root) / "runs" / run_id


def _request_file_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("path"), str)
        and isinstance(value.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256") or "")) is not None
    )


def materialize_request_file_refs(
    run_dir: Path,
    request: Any,
    *,
    source_base: Path,
) -> Any:
    """Copy request file_ref_v2 bytes into the run once and rewrite their paths."""
    root = _absolute(run_dir)
    assets_root = root / "inputs" / "source-assets"
    rows: dict[str, dict[str, Any]] = {}

    def copy_ref(raw: dict[str, Any]) -> dict[str, Any]:
        source = Path(str(raw["path"]))
        if not source.is_absolute():
            source = source_base / source
        source = _absolute(source)
        if not source.is_file():
            raise FlowError(f"request source asset not found: {source}")
        expected = str(raw["sha256"])
        actual = _sha256(source)
        if actual != expected:
            raise FlowError(f"request source asset hash mismatch: {source}")
        suffix = source.suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            suffix = ".bin"
        slot = f"inputs/source-assets/{expected}{suffix}"
        destination = root / Path(slot)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file():
            if _sha256(destination) != expected:
                raise FlowError(f"materialized source asset hash drift: {destination}")
        else:
            temporary = destination.with_name(f".{destination.name}.tmp-{uuid.uuid4().hex}")
            try:
                shutil.copy2(source, temporary)
                if _sha256(temporary) != expected:
                    raise FlowError(f"source asset changed during materialization: {source}")
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
        row = rows.setdefault(
            expected,
            {
                "id": expected,
                "sha256": expected,
                "bytes": destination.stat().st_size,
                "materialized_path": slot,
                "source_paths": [],
            },
        )
        source_text = str(source)
        if source_text not in row["source_paths"]:
            row["source_paths"].append(source_text)
        rewritten = dict(raw)
        rewritten["path"] = str(destination)
        rewritten["slot"] = slot
        return rewritten

    def visit(value: Any) -> Any:
        if _request_file_ref(value):
            return copy_ref(value)
        if isinstance(value, dict):
            return {key: visit(child) for key, child in value.items()}
        if isinstance(value, list):
            return [visit(child) for child in value]
        return value

    rewritten = visit(request)
    write_json(
        root / "source-assets-manifest.json",
        {
            "schema": SOURCE_ASSET_MANIFEST_SCHEMA,
            "run_id": root.name,
            "policy": "copy_once_hash_verified",
            "assets": list(rows.values()),
            "created_at": utc_now(),
        },
        overwrite=False,
    )
    return rewritten


def absolutize_request_file_refs(value: Any, *, source_base: Path) -> Any:
    """Preserve source bytes while making goal-row file references unambiguous."""
    if _request_file_ref(value):
        rewritten = dict(value)
        source = Path(str(rewritten["path"]))
        if not source.is_absolute():
            rewritten["path"] = str(_absolute(source_base / source))
        return rewritten
    if isinstance(value, dict):
        return {key: absolutize_request_file_refs(child, source_base=source_base) for key, child in value.items()}
    if isinstance(value, list):
        return [absolutize_request_file_refs(child, source_base=source_base) for child in value]
    return value


def _kind_ext(kind: str) -> str:
    if kind == "image":
        return "png"
    if kind == "video":
        return "mp4"
    if kind == "audio":
        return "mp3"
    if kind in {"json", "data"}:
        return "json"
    return "bin"


def slot_rel(step_id: str, *, kind: str = "file", item_index: int | None = None, attempt: int | None = None) -> str:
    ext = _kind_ext(kind)
    if item_index is not None:
        return f"milestones/{step_id}/items/{int(item_index):03d}/files/asset.{ext}"
    if attempt is not None:
        return f"milestones/{step_id}/work/attempts/{int(attempt):02d}/files/asset.{ext}"
    return f"milestones/{step_id}/work/candidate/files/asset.{ext}"


def ensure_session_tree(run_dir: Path, flow: dict[str, Any]) -> None:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "record").mkdir(exist_ok=True)
    if os.environ.get("M8M_LAZY_SESSION_TREE") != "1":
        for step in flow.get("steps") or []:
            mid = str(step.get("id") or "")
            if not mid:
                continue
            (run_dir / "milestones" / mid / "in").mkdir(parents=True, exist_ok=True)
            (run_dir / "milestones" / mid / "work" / "attempts").mkdir(parents=True, exist_ok=True)
            (run_dir / "milestones" / mid / "out" / "members").mkdir(parents=True, exist_ok=True)
            if str(step.get("loop") or "none") == "for" or step.get("on_cycle") or step.get("cycle"):
                (run_dir / "milestones" / mid / "items").mkdir(parents=True, exist_ok=True)
            cycle = step.get("cycle") if isinstance(step.get("cycle"), dict) else None
            if cycle:
                cid = str(cycle.get("id") or step.get("on_cycle") or mid)
                (run_dir / "cycles" / cid).mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        write_json(
            manifest_path,
            {
                "schema": "m8m_run_manifest_v1",
                "flow_id": flow.get("flow_id"),
                "run_id": run_dir.name,
                "created_at": utc_now(),
                "slots": [],
            },
            overwrite=False,
        )
    freeze_run_roster(run_dir, flow)


def chosen_output_path(run_dir: Path, milestone_id: str) -> Path:
    return Path(run_dir) / "milestones" / str(milestone_id) / "out" / "chosen-output.json"


def judge_receipt_path(run_dir: Path, milestone_id: str) -> Path:
    return Path(run_dir) / "milestones" / str(milestone_id) / "out" / "judge-receipt.json"


def _candidate_value(item: Any) -> Any:
    if isinstance(item, dict) and "value" in item:
        return item["value"]
    if isinstance(item, dict) and {"id", "name"} <= set(item):
        return {key: value for key, value in item.items() if key not in {"id", "name"}}
    return item


def _candidate_file(run_dir: Path, item: Any) -> Path:
    raw: Any = item
    if isinstance(item, dict):
        raw = item.get("path")
        if raw is None and isinstance(item.get("value"), dict):
            raw = item["value"].get("path")
        if raw is None and isinstance(item.get("value"), str):
            raw = item["value"]
        if raw is None:
            raw = _first_file(item)
    if not isinstance(raw, str) or not raw.strip():
        raise FlowError("chosen file/media member needs a path")
    run_root = Path(os.path.realpath(str(_absolute(run_dir))))
    raw_path = Path(raw)
    path = raw_path if raw_path.is_absolute() else run_root / raw_path
    path = Path(os.path.realpath(str(path)))
    try:
        path.relative_to(run_root)
    except ValueError as exc:
        raise FlowError(
            "chosen file/media member must already be contained by the active run boundary: "
            f"{raw}"
        ) from exc
    if path.is_symlink() or not path.is_file():
        raise FlowError(f"chosen file/media member does not exist: {raw}")
    return path


def _candidate_with_path(item: Any, destination: Path) -> Any:
    """Preserve the declared candidate shape while rebinding its file path."""

    target = str(destination)
    if isinstance(item, str):
        return target
    if not isinstance(item, dict):
        raise FlowError("chosen file/media member must be a path or object")
    normalized = json.loads(json.dumps(item, ensure_ascii=False, allow_nan=False))
    if isinstance(normalized.get("path"), str):
        normalized["path"] = target
        return normalized
    value = normalized.get("value")
    if isinstance(value, dict) and isinstance(value.get("path"), str):
        value["path"] = target
        return normalized
    asset = normalized.get("asset")
    if isinstance(asset, dict) and isinstance(asset.get("path"), str):
        asset["path"] = target
        return normalized
    raise FlowError("chosen file/media member needs an explicit path field")


def _validate_media_extension(source: Path, kind: str) -> None:
    allowed = {
        "image": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"},
        "video": {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"},
        "audio": {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".webm"},
    }.get(kind)
    if allowed is not None and source.suffix.lower() not in allowed:
        raise FlowError(
            f"chosen {kind} member extension does not match its declared kind: {source.name}"
        )


def _recognized_media_kind(path: Path) -> str | None:
    """Recognize common media containers without adding a provider dependency.

    A chosen output is deliberately not content-addressed, so readback cannot
    prove that arbitrary same-kind media bytes are historically identical.
    It can, however, fail closed when the stored bytes are recognizably a
    different media kind (for example MP4 bytes in a declared PNG member).
    Unknown non-empty provider formats remain admissible after their declared
    extension and MIME family have been checked.
    """

    try:
        with path.open("rb") as stream:
            head = stream.read(64)
    except OSError as exc:
        raise FlowError(f"chosen member asset is unreadable: {path}") from exc
    if not head:
        raise FlowError(f"chosen media member is empty: {path}")

    if (
        head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith((b"GIF87a", b"GIF89a", b"BM"))
        or head.startswith((b"II*\x00", b"MM\x00*"))
        or head.startswith(b"\xff\xd8\xff")
        or (head.startswith(b"RIFF") and head[8:12] == b"WEBP")
    ):
        return "image"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if path.suffix.lower() in {".m4a", ".aac"} or brand in {
            b"M4A ",
            b"M4B ",
            b"M4P ",
            b"F4A ",
            b"F4B ",
        }:
            return "audio"
        if brand in {
            b"avif",
            b"avis",
        }:
            return "image"
        return "video"
    if head.startswith(b"\x1aE\xdf\xa3"):
        # Matroska/WebM can carry audio or video. The filename/MIME checks
        # retain responsibility for this intentionally ambiguous container.
        return None
    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return "video"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio"
    if head.startswith((b"ID3", b"fLaC", b"OggS")):
        return "audio"
    if len(head) >= 2 and (
        (head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)  # MP3 frame
        or (head[0] == 0xFF and (head[1] & 0xF6) == 0xF0)  # AAC ADTS
    ):
        return "audio"
    return None


def _validate_chosen_member_bytes(
    asset_path: Path,
    member: dict[str, Any],
) -> Any:
    """Read back one chosen asset and return its JSON value when applicable."""

    kind = str(member.get("kind") or "")
    if kind in {"json", "data"}:
        if asset_path.name != "asset.json":
            raise FlowError(
                f"chosen {kind} member must use asset.json: {asset_path.name}"
            )
        try:
            value = json.loads(asset_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FlowError(
                f"chosen {kind} member asset is invalid JSON: {asset_path}"
            ) from exc
        if str(member.get("mime_type") or "") != "application/json":
            raise FlowError(
                f"chosen {kind} member MIME type does not match its bytes: {asset_path}"
            )
        return value

    # Check readability without generating a checksum for milestone admission.
    try:
        with asset_path.open("rb") as stream:
            while stream.read(1024 * 1024):
                pass
    except OSError as exc:
        raise FlowError(f"chosen member asset is unreadable: {asset_path}") from exc
    if kind == "file":
        return None

    _validate_media_extension(asset_path, kind)
    mime_family = str(member.get("mime_type") or "").split("/", 1)[0]
    ambiguous_webm_audio = (
        kind == "audio"
        and asset_path.suffix.lower() == ".webm"
        and mime_family == "video"
    )
    if mime_family != kind and not ambiguous_webm_audio:
        raise FlowError(
            f"chosen {kind} member MIME type does not match its declaration: {asset_path}"
        )
    recognized = _recognized_media_kind(asset_path)
    if recognized is not None and recognized != kind:
        raise FlowError(
            f"chosen {kind} member bytes are recognizable as {recognized}: {asset_path}"
        )
    return None


def _output_port_schema(
    output_schema: dict[str, Any],
    output_id: str,
    cardinality: str,
) -> dict[str, Any]:
    outputs = (
        output_schema.get("properties", {}).get("outputs", {})
        if isinstance(output_schema.get("properties"), dict)
        else {}
    )
    properties = outputs.get("properties") if isinstance(outputs, dict) else None
    port = properties.get(output_id, {}) if isinstance(properties, dict) else {}
    if cardinality == "many" and isinstance(port, dict):
        port = port.get("items", {})
    return port if isinstance(port, dict) else {}


def _json_value_for_schema(
    value: Any,
    schema: dict[str, Any],
    *,
    member: dict[str, Any],
    collection: bool,
) -> Any:
    properties = schema.get("properties") if isinstance(schema, dict) else None
    required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
    if not collection:
        if isinstance(properties, dict):
            if "value" in required and not (
                isinstance(value, dict) and "value" in value
            ):
                return {"value": value}
            if "asset" in required and not (
                isinstance(value, dict) and "asset" in value
            ):
                return {"asset": value}
        return value

    base = {"id": str(member["id"]), "name": str(member["name"])}
    if isinstance(properties, dict) and "value" in required:
        base["value"] = value
    elif isinstance(value, dict) and not ({"id", "name"} & set(value)):
        base.update(value)
    else:
        base["value"] = value
    return base


def _file_value_for_schema(
    asset_path: Path,
    schema: dict[str, Any],
    *,
    member: dict[str, Any],
    collection: bool,
) -> Any:
    path_value = str(_absolute(asset_path))
    if schema.get("type") == "string":
        value: Any = path_value
    else:
        properties = schema.get("properties") if isinstance(schema, dict) else None
        required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
        fields: dict[str, Any] = {}
        if isinstance(properties, dict):
            if "path" in properties or "path" in required:
                fields["path"] = path_value
            if "sha256" in required:
                fields["sha256"] = _sha256(asset_path)
            if "mime_type" in properties or "mime_type" in required:
                fields["mime_type"] = str(member.get("mime_type") or "")
            if "mime" in properties or "mime" in required:
                fields["mime"] = str(member.get("mime_type") or "")
            if "kind" in properties or "kind" in required:
                fields["kind"] = str(member.get("kind") or "")
            if "width" in properties or "height" in properties or {"width", "height"} & required:
                from PIL import Image

                with Image.open(asset_path) as image:
                    width, height = image.size
                if "width" in properties or "width" in required:
                    fields["width"] = width
                if "height" in properties or "height" in required:
                    fields["height"] = height
            wrapper = next(
                (
                    key
                    for key in ("asset", "file", "value")
                    if key in required and key not in fields
                ),
                None,
            )
            if wrapper:
                child = properties.get(wrapper, {})
                fields[wrapper] = _file_value_for_schema(
                    asset_path,
                    child if isinstance(child, dict) else {},
                    member=member,
                    collection=False,
                )
        if not fields:
            fields = {"path": path_value}
        value = fields
    if not collection:
        return value
    if isinstance(value, dict):
        return {"id": str(member["id"]), "name": str(member["name"]), **value}
    return {"id": str(member["id"]), "name": str(member["name"]), "value": value}


def _reconstruct_chosen_candidate(
    run_dir: Path,
    manifest: dict[str, Any],
    *,
    step: dict[str, Any],
    skill_dir: Path,
    staged_root: Path | None = None,
) -> dict[str, Any]:
    """Rebuild the declared candidate solely from durable chosen members."""

    schema_path = _absolute(Path(skill_dir) / str(step.get("output_schema") or ""))
    try:
        output_schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FlowError(f"schema not found: {schema_path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FlowError(f"invalid JSON: {schema_path}") from exc
    members = {str(item["id"]): item for item in manifest.get("members") or []}
    outputs: dict[str, Any] = {}
    for declaration in step.get("outputs") or []:
        output_id = str(declaration["id"])
        output = next(
            (item for item in manifest.get("outputs") or [] if item.get("id") == output_id),
            None,
        )
        if output is None:
            continue
        cardinality = str(declaration["cardinality"])
        item_schema = _output_port_schema(output_schema, output_id, cardinality)
        values: list[Any] = []
        for member_id in output.get("member_ids") or []:
            member = members[str(member_id)]
            logical = PurePosixPath(str(member["path"]))
            asset_path = (
                Path(staged_root) / "members" / str(member_id) / logical.name
                if staged_root is not None
                else assert_in_run(run_dir, logical.as_posix())
            )
            parsed = _validate_chosen_member_bytes(asset_path, member)
            if str(member["kind"]) in {"json", "data"}:
                value = _json_value_for_schema(
                    parsed,
                    item_schema,
                    member=member,
                    collection=cardinality == "many",
                )
            else:
                value = _file_value_for_schema(
                    asset_path,
                    item_schema,
                    member=member,
                    collection=cardinality == "many",
                )
            values.append(value)
        outputs[output_id] = values if cardinality == "many" else (values[0] if values else None)
    candidate = {"outputs": outputs}
    validate_against_schema(candidate, schema_path)
    return candidate


def _path_volume(path: Path) -> str:
    """Identify the physical volume after following directory reparse points."""
    resolved = Path(os.path.realpath(str(path)))
    if os.name == "nt":
        return resolved.drive.upper().rstrip("\\/")
    return str(resolved.stat().st_dev)


def assert_image_provider_on_execution_volume(run_dir: Path, source: Path) -> None:
    if _path_volume(source) != _path_volume(_absolute(run_dir)):
        raise FlowError(
            "image provider output must live on the active execution volume; "
            f"resolved provider path: {Path(os.path.realpath(str(source)))}"
        )


def _stage_image_provider_output(
    run_dir: Path,
    step: dict[str, Any],
    source: Path,
    *,
    attempt: int,
    member_id: str,
) -> Path:
    assert_image_provider_on_execution_volume(run_dir, source)
    root = _absolute(run_dir)
    normalized_source = _absolute(source)
    suffix = source.suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ".bin"
    destination = (
        root
        / "milestones"
        / str(step["id"])
        / "work"
        / "attempts"
        / f"{int(attempt):02d}"
        / "provider-outputs"
        / str(member_id)
        / f"asset{suffix}"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if normalized_source != _absolute(destination):
        shutil.copy2(source, destination)
    return destination


def _member_extension(path: Path, kind: str) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix and re.fullmatch(r"[a-z0-9]{1,10}", suffix):
        return suffix
    return _kind_ext(kind)


def admit_candidate(
    run_dir: Path,
    skill_dir: Path,
    step: dict[str, Any],
    result: dict[str, Any],
    *,
    attempt: int = 1,
) -> dict[str, Any]:
    """Structurally admit and freeze one candidate before semantic judgment.

    Admission is deliberately not PASS.  It proves that the current FlowStep
    return has exactly one named-output object, satisfies the declared output
    schema and port/cardinality rules, and that every file/media member has
    stable run-local bytes.  The returned candidate is the exact value a judge
    sees and a later PASS may commit.
    """

    run_root = _absolute(run_dir)
    declared = step.get("outputs") or []
    unexpected_top_level = sorted(set(result) - {"outputs"})
    if unexpected_top_level:
        raise FlowError(
            f"{step['id']}: candidate must contain only outputs; "
            f"unexpected top-level fields: {unexpected_top_level}"
        )
    raw_outputs = result.get("outputs")
    if not isinstance(raw_outputs, dict):
        raise FlowError(f"{step['id']}: candidate must return an outputs object")
    candidate: dict[str, Any] = {"outputs": raw_outputs}
    output_schema_path = Path(skill_dir) / str(step["output_schema"])

    def validate_candidate_shape(value: dict[str, Any]) -> None:
        validate_against_schema(value, output_schema_path)

    validate_candidate_shape(candidate)

    declared_ids = {str(item.get("id") or "") for item in declared}
    unknown = sorted(set(raw_outputs) - declared_ids)
    if unknown:
        raise FlowError(f"{step['id']}: candidate returned undeclared outputs: {unknown}")

    admitted_root = (
        run_root
        / "milestones"
        / str(step["id"])
        / "work"
        / "attempts"
        / f"attempt-{max(1, int(attempt)):03d}"
        / "admitted"
    )
    candidate_path = admitted_root / "candidate.json"
    if candidate_path.is_file():
        try:
            prior = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (UnicodeError, json.JSONDecodeError, OSError):
            prior = None
        if prior == candidate:
            admitted = prior
            validate_candidate_shape(admitted)
            return admitted
        shutil.rmtree(admitted_root)
    elif admitted_root.exists():
        shutil.rmtree(admitted_root)
    members_root = admitted_root / "members"
    members_root.mkdir(parents=True, exist_ok=True)

    normalized_outputs: dict[str, Any] = {}
    seen_members: set[str] = set()
    for declaration in declared:
        output_id = str(declaration["id"])
        cardinality = str(declaration["cardinality"])
        kind = str(declaration["kind"])
        present = output_id in raw_outputs and raw_outputs[output_id] is not None
        if not present:
            if declaration.get("required"):
                raise FlowError(f"{step['id']}: required output {output_id} is missing")
            continue
        raw = raw_outputs[output_id]
        values = raw if cardinality == "many" else [raw]
        if cardinality == "many" and not isinstance(values, list):
            raise FlowError(f"{step['id']}.{output_id}: cardinality many requires an array")
        if declaration.get("required") and not values:
            raise FlowError(f"{step['id']}: required output {output_id} is empty")

        normalized_values: list[Any] = []
        for index, item in enumerate(values):
            if cardinality == "many":
                if not isinstance(item, dict):
                    raise FlowError(f"{step['id']}.{output_id}[{index}] needs id and name")
                member_id = str(item.get("id") or "").strip()
                member_name = str(item.get("name") or "").strip()
            else:
                member_id = output_id
                member_name = str(declaration.get("name") or "").strip()
            if not re.fullmatch(r"[a-z][a-z0-9_]*", member_id):
                raise FlowError(f"{step['id']}.{output_id}: invalid member id {member_id}")
            if not member_name:
                raise FlowError(f"{step['id']}.{output_id}: member {member_id} needs a name")
            if member_id in seen_members:
                raise FlowError(f"{step['id']}: duplicate member id {member_id}")
            seen_members.add(member_id)
            member_dir = members_root / member_id
            member_dir.mkdir(parents=True, exist_ok=True)

            if kind in {"json", "data"}:
                frozen_value = json.loads(
                    json.dumps(item, ensure_ascii=False, allow_nan=False)
                )
                write_json(member_dir / "asset.json", _candidate_value(frozen_value), overwrite=False)
                normalized_item = frozen_value
            else:
                source = _candidate_file(run_root, item)
                _validate_media_extension(source, kind)
                if kind == "image":
                    assert_image_provider_on_execution_volume(run_root, source)
                destination = member_dir / f"asset.{_member_extension(source, kind)}"
                shutil.copy2(source, destination)
                normalized_item = _candidate_with_path(item, destination)
            normalized_values.append(normalized_item)
        normalized_outputs[output_id] = normalized_values if cardinality == "many" else normalized_values[0]

    admitted = {"outputs": normalized_outputs}
    validate_candidate_shape(admitted)
    write_json(candidate_path, admitted, overwrite=False)
    admitted = json.loads(candidate_path.read_text(encoding="utf-8"))
    return admitted


def _milestone_max_attempts(step: dict[str, Any]) -> int:
    loop = str(step.get("loop") or "none")
    if (loop in {"judge", "for"} or step.get("on_cycle") or step.get("cycle")
            or step.get("on_tool_fail") == "need_model"):
        return max(1, int(step.get("max_attempts") or step.get("max_model_attempts") or 8))
    return 1


def _milestone_judge_ref(step: dict[str, Any]) -> str:
    """Return the exact judge identity frozen into a durable receipt."""
    execution = step.get("execution") if isinstance(step.get("execution"), dict) else {}
    judge = execution.get("judge") if isinstance(execution.get("judge"), dict) else {}
    return str(
        judge.get("ref")
        or step.get("_control_worker_ref")
        or step.get("worker")
        or "m8m_structural_admission@1.0.0"
    ).strip()


def _bounded_worker_details(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(receipt, dict):
        return None
    reserved = {
        "schema",
        "milestone_id",
        "attempt",
        "decision",
        "success_rule",
        "judge_ref",
        "reasons",
        "blockers",
        "max_attempts",
        "details",
    }
    extra = {
        str(key): value
        for key, value in sorted(receipt.items(), key=lambda item: str(item[0]))
        if str(key) not in reserved
    }
    if not extra and isinstance(receipt.get("details"), dict):
        worker = receipt["details"].get("worker_receipt")
        extra = dict(worker) if isinstance(worker, dict) else {}
    if not extra or len(extra) > 16:
        return None
    try:
        encoded = json.dumps(extra, ensure_ascii=False, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError):
        return None
    if len(encoded) > 8192:
        return None
    return {"worker_receipt": extra}


def normalize_judge_receipt(
    step: dict[str, Any],
    receipt: dict[str, Any] | None,
    *,
    attempt: int,
    decision: str,
    blockers: list[str] | None = None,
) -> dict[str, Any]:
    """Normalize one runtime decision into the durable M8M receipt contract."""
    normalized_decision = str(decision).upper()
    if normalized_decision not in {"PASS", "RETRY", "BLOCKED"}:
        raise FlowError(f"{step.get('id')}: invalid judge decision {decision}")
    raw_reasons = receipt.get("reasons") if isinstance(receipt, dict) else None
    reasons = [str(item).strip() for item in raw_reasons or [] if str(item).strip()]
    normalized_blockers = [str(item).strip() for item in blockers or [] if str(item).strip()]
    if normalized_decision == "PASS":
        normalized_blockers = []
        if not reasons:
            if str(step.get("loop") or "none") == "judge":
                reasons = ["The semantic judge accepted the admitted candidate against the milestone expectation."]
            else:
                reasons = [
                    "Candidate passed the declared output and schema admission; no semantic judge was invoked."
                ]
    elif normalized_decision == "RETRY":
        if not reasons:
            reasons = ["The current candidate does not yet satisfy the milestone success rule."]
    else:
        if not normalized_blockers:
            normalized_blockers = ["The milestone could not produce a valid chosen output."]
        if not reasons:
            reasons = ["The milestone terminated without a chosen output."]
    result: dict[str, Any] = {
        "schema": JUDGE_RECEIPT_SCHEMA,
        "milestone_id": str(step.get("id") or ""),
        "attempt": max(1, int(attempt)),
        "decision": normalized_decision,
        "success_rule": str(step.get("success") or "").strip(),
        "judge_ref": _milestone_judge_ref(step),
        "reasons": reasons,
        "blockers": normalized_blockers,
        "max_attempts": _milestone_max_attempts(step),
    }
    details = _bounded_worker_details(receipt)
    if normalized_decision == "PASS" and (step.get("branch") or step.get("cycle")):
        worker = details.get("worker_receipt") if isinstance(details, dict) else None
        control_field = "branch" if step.get("branch") else "cycle"
        if not isinstance(worker, dict) or not str(worker.get(control_field) or ""):
            raise FlowError(
                f"{step.get('id')}: {control_field} receipt cannot be durably normalized"
            )
    if details:
        result["details"] = details
    validate_against_schema(result, judge_receipt_schema_path())
    return result


def _chosen_stage_dir(run_dir: Path, step_id: str, attempt: int) -> Path:
    return (
        Path(run_dir)
        / "milestones"
        / str(step_id)
        / "work"
        / "attempts"
        / f"attempt-{max(1, int(attempt)):03d}"
        / "chosen-stage"
    )


def _validate_chosen_manifest(
    run_dir: Path,
    manifest: dict[str, Any],
    *,
    step: dict[str, Any] | None = None,
    skill_dir: Path | None = None,
    staged_root: Path | None = None,
) -> dict[str, Any]:
    validate_against_schema(manifest, chosen_output_schema_path())
    milestone_id = str(manifest.get("milestone_id") or "")
    if step is not None:
        if milestone_id != str(step.get("id") or ""):
            raise FlowError(f"{milestone_id}: chosen manifest milestone does not match the workflow")
        if manifest.get("output_contract") != step.get("output_contract"):
            raise FlowError(f"{milestone_id}: chosen manifest output contract does not match the workflow")

    output_rows = manifest.get("outputs") or []
    output_by_id: dict[str, dict[str, Any]] = {}
    for row in output_rows:
        output_id = str(row.get("id") or "")
        if output_id in output_by_id:
            raise FlowError(f"{milestone_id}: duplicate chosen output id {output_id}")
        output_by_id[output_id] = row

    declarations = step.get("outputs") or [] if step is not None else []
    if step is not None:
        declared = {str(item.get("id") or ""): item for item in declarations}
        unknown = sorted(set(output_by_id) - set(declared))
        if unknown:
            raise FlowError(f"{milestone_id}: chosen manifest has undeclared outputs: {unknown}")
        for output_id, declaration in declared.items():
            row = output_by_id.get(output_id)
            if row is None:
                if declaration.get("required"):
                    raise FlowError(f"{milestone_id}: required chosen output {output_id} is missing")
                continue
            for key in ("name", "kind", "cardinality", "required"):
                if row.get(key) != declaration.get(key):
                    raise FlowError(f"{milestone_id}.{output_id}: chosen {key} differs from its declaration")

    members = manifest.get("members") or []
    member_by_id: dict[str, dict[str, Any]] = {}
    for index, member in enumerate(members):
        member_id = str(member.get("id") or "")
        if member_id in member_by_id:
            raise FlowError(f"{milestone_id}: duplicate chosen member id {member_id}")
        if member.get("order") != index:
            raise FlowError(f"{milestone_id}: chosen member order must be contiguous")
        output_id = str(member.get("output_id") or "")
        output = output_by_id.get(output_id)
        if output is None:
            raise FlowError(f"{milestone_id}: member {member_id} names unknown output {output_id}")
        if member.get("kind") != output.get("kind"):
            raise FlowError(f"{milestone_id}: member {member_id} kind differs from output {output_id}")
        logical = str(member.get("path") or "")
        relative = PurePosixPath(logical)
        expected_parent = PurePosixPath("milestones") / milestone_id / "out" / "members" / member_id
        if relative.is_absolute() or ".." in relative.parts or relative.parent != expected_parent:
            raise FlowError(f"{milestone_id}: invalid chosen member path {logical}")
        if not relative.name.startswith("asset.") and relative.name != "asset.json":
            raise FlowError(f"{milestone_id}: chosen member {member_id} has an invalid asset filename")
        asset_path = (
            Path(staged_root) / "members" / member_id / relative.name
            if staged_root is not None
            else assert_in_run(run_dir, logical)
        )
        if asset_path.is_symlink() or not asset_path.is_file():
            raise FlowError(f"{milestone_id}: chosen member asset is missing: {logical}")
        _validate_chosen_member_bytes(asset_path, member)
        member_by_id[member_id] = member

    referenced: list[str] = []
    for output_id, output in output_by_id.items():
        ids = [str(item) for item in output.get("member_ids") or []]
        if len(ids) != len(set(ids)):
            raise FlowError(f"{milestone_id}.{output_id}: duplicate member reference")
        if output.get("cardinality") == "one" and len(ids) != 1:
            raise FlowError(f"{milestone_id}.{output_id}: cardinality one requires exactly one member")
        if output.get("required") and not ids:
            raise FlowError(f"{milestone_id}.{output_id}: required output has no members")
        for member_id in ids:
            member = member_by_id.get(member_id)
            if member is None or member.get("output_id") != output_id:
                raise FlowError(f"{milestone_id}.{output_id}: invalid member reference {member_id}")
        referenced.extend(ids)
    if referenced != [str(item.get("id") or "") for item in members]:
        raise FlowError(f"{milestone_id}: chosen member ordering does not match output ports")

    expected_receipt = f"milestones/{milestone_id}/out/judge-receipt.json"
    if manifest.get("judge_receipt") != expected_receipt:
        raise FlowError(f"{milestone_id}: chosen judge receipt path is invalid")
    receipt_path = (
        Path(staged_root) / "judge-receipt.json"
        if staged_root is not None
        else assert_in_run(run_dir, expected_receipt)
    )
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise FlowError(f"{milestone_id}: chosen judge receipt is missing")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FlowError(f"{milestone_id}: chosen judge receipt is invalid JSON") from exc
    validate_against_schema(receipt, judge_receipt_schema_path())
    if receipt.get("decision") != "PASS" or receipt.get("milestone_id") != milestone_id:
        raise FlowError(f"{milestone_id}: chosen output requires a matching PASS judge receipt")
    if step is not None:
        expected_success = str(step.get("success") or "").strip()
        expected_judge = _milestone_judge_ref(step)
        expected_max = _milestone_max_attempts(step)
        if receipt.get("success_rule") != expected_success:
            raise FlowError(
                f"{milestone_id}: chosen judge receipt success rule does not match the workflow"
            )
        if receipt.get("judge_ref") != expected_judge:
            raise FlowError(
                f"{milestone_id}: chosen judge receipt judge reference does not match the workflow"
            )
        if receipt.get("max_attempts") != expected_max:
            raise FlowError(
                f"{milestone_id}: chosen judge receipt attempt budget does not match the workflow"
            )
        if int(receipt.get("attempt") or 0) > expected_max:
            raise FlowError(
                f"{milestone_id}: chosen judge receipt attempt exceeds the workflow budget"
            )
        if skill_dir is not None and str(step.get("output_schema") or "").strip():
            _reconstruct_chosen_candidate(
                run_dir,
                manifest,
                step=step,
                skill_dir=skill_dir,
                staged_root=staged_root,
            )
    return receipt


def _publish_staged_chosen(
    run_dir: Path,
    step: dict[str, Any],
    staged_root: Path,
) -> dict[str, Any]:
    staged_manifest = staged_root / "chosen-output.json"
    if not staged_manifest.is_file():
        raise FlowError(f"{step['id']}: chosen stage is not commit-ready")
    try:
        manifest = json.loads(staged_manifest.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FlowError(f"{step['id']}: chosen stage manifest is invalid") from exc
    _validate_chosen_manifest(run_dir, manifest, step=step, staged_root=staged_root)
    target = chosen_output_path(run_dir, step["id"])
    if target.exists():
        raise FlowError(f"{step['id']}: chosen output already exists; use --replace-milestone")
    out_dir = target.parent
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(staged_root / "members", out_dir / "members")
    shutil.copy2(staged_root / "judge-receipt.json", out_dir / "judge-receipt.json")
    _validate_chosen_manifest(run_dir, manifest, step=step)
    # This is the commit marker. Every member and the final PASS receipt have
    # been copied and read back before its atomic create-if-absent write.
    write_json(target, manifest, overwrite=False)
    return manifest


def recover_incomplete_chosen_output(
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
) -> dict[str, Any] | None:
    """Finish a validated staged commit after a process interruption."""
    target = chosen_output_path(run_dir, step["id"])
    if target.is_file():
        return load_chosen_output(
            run_dir,
            step["id"],
            step=step,
            skill_dir=(
                Path(flow["_skill_dir"])
                if isinstance(flow, dict) and flow.get("_skill_dir")
                else None
            ),
        )
    attempts = (
        Path(run_dir) / "milestones" / str(step["id"]) / "work" / "attempts"
    )
    ready = sorted(attempts.glob("attempt-*/chosen-stage/chosen-output.json")) if attempts.is_dir() else []
    if not ready:
        return None
    return _publish_staged_chosen(run_dir, step, ready[-1].parent)


def discard_incomplete_chosen_output(run_dir: Path, milestone_id: str) -> None:
    target = chosen_output_path(run_dir, milestone_id)
    if target.exists():
        raise FlowError(f"{milestone_id}: cannot discard a committed chosen output")
    out_dir = target.parent
    if out_dir.exists():
        shutil.rmtree(out_dir)


def materialize_chosen_output(
    run_dir: Path,
    flow: dict[str, Any],
    step: dict[str, Any],
    result: dict[str, Any],
    *,
    receipt: dict[str, Any] | None = None,
    attempt: int = 1,
) -> dict[str, Any]:
    """Stage and manifest-last commit one admitted, optionally judged candidate."""
    run_dir = _absolute(run_dir)
    target = chosen_output_path(run_dir, step["id"])
    if target.exists():
        raise FlowError(f"{step['id']}: chosen output already exists; use --replace-milestone")
    candidate_outputs = result.get("outputs")
    if not isinstance(candidate_outputs, dict):
        raise FlowError(f"{step['id']}: candidate must return an outputs object")
    declared = step.get("outputs") or []
    declared_ids = {str(item.get("id") or "") for item in declared}
    unknown = sorted(set(candidate_outputs) - declared_ids)
    if unknown:
        raise FlowError(f"{step['id']}: candidate returned undeclared outputs: {unknown}")

    staged_root = _chosen_stage_dir(run_dir, step["id"], attempt)
    if staged_root.exists():
        shutil.rmtree(staged_root)
    members_root = staged_root / "members"
    members_root.mkdir(parents=True, exist_ok=True)
    manifest_outputs: list[dict[str, Any]] = []
    manifest_members: list[dict[str, Any]] = []
    seen_members: set[str] = set()

    for declaration in declared:
        output_id = str(declaration["id"])
        cardinality = str(declaration["cardinality"])
        kind = str(declaration["kind"])
        present = output_id in candidate_outputs and candidate_outputs[output_id] is not None
        if not present:
            if declaration.get("required"):
                raise FlowError(f"{step['id']}: required output {output_id} is missing")
            continue
        raw = candidate_outputs[output_id]
        values = raw if cardinality == "many" else [raw]
        if cardinality == "many" and not isinstance(values, list):
            raise FlowError(f"{step['id']}.{output_id}: cardinality many requires an array")
        if declaration.get("required") and not values:
            raise FlowError(f"{step['id']}: required output {output_id} is empty")
        member_ids: list[str] = []
        for index, item in enumerate(values):
            if cardinality == "many":
                if not isinstance(item, dict):
                    raise FlowError(f"{step['id']}.{output_id}[{index}] needs id and name")
                member_id = str(item.get("id") or "").strip()
                member_name = str(item.get("name") or "").strip()
            else:
                member_id = output_id
                member_name = str(declaration["name"])
            if not re.fullmatch(r"[a-z][a-z0-9_]*", member_id):
                raise FlowError(f"{step['id']}.{output_id}: invalid member id {member_id}")
            if not member_name:
                raise FlowError(f"{step['id']}.{output_id}: member {member_id} needs a name")
            if member_id in seen_members:
                raise FlowError(f"{step['id']}: duplicate member id {member_id}")
            seen_members.add(member_id)
            member_ids.append(member_id)
            member_dir = members_root / member_id
            member_dir.mkdir(parents=True, exist_ok=True)
            if kind in {"json", "data"}:
                member_path = member_dir / "asset.json"
                write_json(member_path, _candidate_value(item), overwrite=False)
                mime_type = "application/json"
            else:
                source = _candidate_file(run_dir, item)
                member_path = member_dir / f"asset.{_member_extension(source, kind)}"
                if _absolute(source) != _absolute(member_path):
                    shutil.copy2(source, member_path)
                mime_type = mimetypes.guess_type(member_path.name)[0] or "application/octet-stream"
            manifest_members.append(
                {
                    "id": member_id,
                    "output_id": output_id,
                    "name": member_name,
                    "kind": kind,
                    "path": (
                        PurePosixPath("milestones")
                        / str(step["id"])
                        / "out"
                        / "members"
                        / member_id
                        / member_path.name
                    ).as_posix(),
                    "mime_type": mime_type,
                    "order": len(manifest_members),
                }
            )
        manifest_outputs.append(
            {
                "id": output_id,
                "name": str(declaration["name"]),
                "kind": kind,
                "cardinality": cardinality,
                "required": bool(declaration["required"]),
                "member_ids": member_ids,
            }
        )

    accepted_receipt = normalize_judge_receipt(
        step,
        receipt,
        attempt=attempt,
        decision="PASS",
    )
    staged_receipt_path = staged_root / "judge-receipt.json"
    write_json(staged_receipt_path, accepted_receipt, overwrite=False)
    receipt_path = judge_receipt_path(run_dir, step["id"])
    manifest = {
        "schema": "m8m_chosen_output_v1",
        "run_id": run_dir.name,
        "flow_id": flow["flow_id"],
        "milestone_id": step["id"],
        "output_contract": step["output_contract"],
        "status": "chosen",
        "outputs": manifest_outputs,
        "members": manifest_members,
        "judge_receipt": receipt_path.relative_to(run_dir).as_posix(),
        "created_at": utc_now(),
    }
    # Validate the complete staged data/control plane before publishing the
    # recovery marker. The final out/chosen-output.json is written only after
    # the staged marker, members, and receipt have all survived readback.
    _validate_chosen_manifest(run_dir, manifest, step=step, staged_root=staged_root)
    write_json(staged_root / "chosen-output.json", manifest, overwrite=False)
    return _publish_staged_chosen(run_dir, step, staged_root)


def load_chosen_output(
    run_dir: Path,
    milestone_id: str,
    *,
    step: dict[str, Any] | None = None,
    skill_dir: Path | None = None,
) -> dict[str, Any]:
    path = chosen_output_path(run_dir, milestone_id)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FlowError(f"missing chosen output: {milestone_id}") from exc
    if manifest.get("schema") != "m8m_chosen_output_v1" or manifest.get("status") != "chosen":
        raise FlowError(f"invalid chosen output: {milestone_id}")
    if manifest.get("milestone_id") != str(milestone_id):
        raise FlowError(f"invalid chosen output milestone: {milestone_id}")
    _validate_chosen_manifest(
        Path(run_dir),
        manifest,
        step=step,
        skill_dir=skill_dir,
    )
    return manifest


def resolve_chosen_output(
    run_dir: Path,
    milestone_id: str,
    *,
    output_id: str | None = None,
    member_id: str | None = None,
    step: dict[str, Any] | None = None,
    skill_dir: Path | None = None,
) -> Any:
    manifest = load_chosen_output(
        run_dir,
        milestone_id,
        step=step,
        skill_dir=skill_dir,
    )
    members = {str(item["id"]): item for item in manifest.get("members") or []}

    def resolve_member(item: dict[str, Any]) -> Any:
        path = assert_in_run(run_dir, str(item["path"]))
        if item.get("kind") in {"json", "data"}:
            return json.loads(path.read_text(encoding="utf-8"))
        resolved = dict(item)
        resolved["path"] = str(path)
        return resolved

    if member_id:
        item = members.get(member_id)
        if item is None or (output_id and item.get("output_id") != output_id):
            raise FlowError(f"{milestone_id}: chosen member not found: {member_id}")
        return resolve_member(item)
    grouped: dict[str, Any] = {}
    for output in manifest.get("outputs") or []:
        oid = str(output.get("id") or "")
        values = [resolve_member(members[mid]) for mid in output.get("member_ids") or []]
        grouped[oid] = values if output.get("cardinality") == "many" else (values[0] if values else None)
    if output_id:
        if output_id not in grouped:
            raise FlowError(f"{milestone_id}: chosen output not found: {output_id}")
        return grouped[output_id]
    return grouped


def record_chosen_output(run_dir: Path, step: dict[str, Any], manifest: dict[str, Any]) -> None:
    path = Path(run_dir) / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {
        "schema": "m8m_run_manifest_v1",
        "run_id": Path(run_dir).name,
    }
    chosen = [
        item
        for item in (data.get("chosen_outputs") or [])
        if item.get("milestone") != step["id"]
    ]
    chosen.append(
        {
            "milestone": step["id"],
            "contract": step["output_contract"],
            "path": chosen_output_path(run_dir, step["id"]).relative_to(Path(run_dir)).as_posix(),
            "members": [str(item.get("id") or "") for item in manifest.get("members") or []],
        }
    )
    data["chosen_outputs"] = chosen
    data["updated_at"] = utc_now()
    write_json(path, data, overwrite=True)


def address_for(
    run_dir: Path,
    step: dict[str, Any],
    *,
    item_index: int | None = None,
    attempt: int | None = None,
) -> dict[str, str]:
    kind = str(((step.get("asset") or {}).get("kind") if isinstance(step.get("asset"), dict) else "") or "file")
    slot = slot_rel(str(step["id"]), kind=kind, item_index=item_index, attempt=attempt)
    root = Path(os.path.abspath(str(run_dir)))
    write_to = Path(os.path.abspath(str(root / slot)))
    write_to.parent.mkdir(parents=True, exist_ok=True)
    return {
        "run_dir": str(root),
        "slot": slot.replace("\\", "/"),
        "write_to": str(write_to),
        "kind": kind,
    }


def attach_address(
    run_dir: Path,
    step: dict[str, Any],
    input_data: dict[str, Any],
    *,
    item_index: int | None = None,
    attempt: int | None = None,
) -> dict[str, Any]:
    payload = dict(input_data)
    payload["address"] = address_for(run_dir, step, item_index=item_index, attempt=attempt)
    return payload


def assert_in_run(run_dir: Path, value: str | Path) -> Path:
    path = Path(value)
    root = Path(os.path.abspath(str(run_dir)))
    resolved = Path(os.path.abspath(str(path if path.is_absolute() else root / path)))
    if resolved != root and root not in resolved.parents:
        raise FlowError(f"address leak: {value} is not inside the session run folder")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _first_file(value: Any) -> str | None:
    if isinstance(value, dict):
        raw = value.get("path")
        if isinstance(raw, str) and raw:
            return raw
        for child in value.values():
            found = _first_file(child)
            if found:
                return found
    if isinstance(value, str) and value:
        candidate = Path(value)
        if candidate.is_file():
            return value
    return None


def materialize_bytes_into_slot(
    run_dir: Path,
    step: dict[str, Any],
    result: dict[str, Any],
    *,
    item_index: int | None = None,
    attempt: int | None = None,
) -> dict[str, Any]:
    """Copy generated bytes into the frozen slot. Rewrite asset.path to the slot."""
    kind = str(((step.get("asset") or {}).get("kind") if isinstance(step.get("asset"), dict) else "") or "")
    if kind not in {"file", "image"}:
        return result
    addr = address_for(run_dir, step, item_index=item_index, attempt=attempt)
    dest = Path(addr["write_to"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_raw = None
    asset = result.get("asset") if isinstance(result.get("asset"), dict) else {}
    if isinstance(asset.get("path"), str):
        src_raw = asset["path"]
    if not src_raw:
        src_raw = _first_file(result)
    if not src_raw:
        raise FlowError(f"{step['id']}: no file to place in session slot {addr['slot']}")
    src = Path(src_raw)
    if not src.is_file():
        src = Path(run_dir) / src_raw
    if not src.is_file():
        raise FlowError(f"{step['id']}: generated file not found: {src_raw}")
    if kind == "image":
        assert_image_provider_on_execution_volume(run_dir, src)
    if _absolute(src) != _absolute(dest):
        shutil.copy2(src, dest)
    out = dict(result)
    out["asset"] = {
        "path": dest.as_posix(),
        "slot": addr["slot"],
    }
    if asset.get("sha256"):
        out["asset"]["sha256"] = _sha256(dest)
    assert_in_run(run_dir, dest)
    return out


def record_slot(run_dir: Path, step: dict[str, Any], result: dict[str, Any]) -> None:
    asset = result.get("asset") if isinstance(result.get("asset"), dict) else None
    if not asset or not asset.get("slot"):
        return
    path = Path(run_dir) / "manifest.json"
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = {"schema": "m8m_run_manifest_v1", "run_id": Path(run_dir).name, "slots": []}
    slots = [item for item in (data.get("slots") or []) if item.get("path") != asset.get("slot")]
    slots.append(
        {
            "milestone": step["id"],
            "kind": ((step.get("asset") or {}).get("kind") if isinstance(step.get("asset"), dict) else None),
            "path": asset.get("slot"),
            **({"sha256": asset["sha256"]} if asset.get("sha256") else {}),
        }
    )
    data["slots"] = slots
    data["updated_at"] = utc_now()
    write_json(path, data, overwrite=True)


def record_skip(
    run_dir: Path,
    step: dict[str, Any],
    *,
    branch: str,
    reason: str,
) -> Path:
    dest = Path(run_dir) / "milestones" / step["id"] / "skipped.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        dest,
        {
            "schema": "m8m_skipped_v1",
            "milestone": step["id"],
            "skipped": True,
            "branch": branch,
            "reason": reason,
            "updated_at": utc_now(),
        },
        overwrite=True,
    )
    path = Path(run_dir) / "manifest.json"
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = {"schema": "m8m_run_manifest_v1", "run_id": Path(run_dir).name, "slots": []}
    skipped = [item for item in (data.get("skipped") or []) if item.get("milestone") != step["id"]]
    skipped.append({"milestone": step["id"], "branch": branch, "reason": reason, "skipped": True})
    data["skipped"] = skipped
    data["updated_at"] = utc_now()
    write_json(path, data, overwrite=True)
    return dest


def copy_envelope_to_slot(run_dir: Path, step: dict[str, Any], envelope_path: Path) -> None:
    dest = Path(run_dir) / "milestones" / step["id"] / "out" / "asset.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if envelope_path.is_file() and _absolute(dest) != _absolute(envelope_path):
        shutil.copy2(envelope_path, dest)


def cycle_id_of(step: dict[str, Any]) -> str:
    spec = step.get("cycle") if isinstance(step.get("cycle"), dict) else {}
    return str(spec.get("id") or step.get("on_cycle") or step.get("id") or "cycle")


def cycle_dir(run_dir: Path, cycle_id: str) -> Path:
    path = Path(run_dir) / "cycles" / cycle_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def ledger_path(run_dir: Path, cycle_id: str) -> Path:
    return cycle_dir(run_dir, cycle_id) / "ledger.json"


def load_ledger(run_dir: Path, cycle_id: str) -> dict[str, Any] | None:
    path = ledger_path(run_dir, cycle_id)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_ledger(run_dir: Path, cycle_id: str, ledger: dict[str, Any]) -> Path:
    path = ledger_path(run_dir, cycle_id)
    ledger = dict(ledger)
    ledger.setdefault("schema", "m8m_cycle_ledger_v1")
    ledger["cycle"] = cycle_id
    ledger["updated_at"] = utc_now()
    write_json(path, ledger, overwrite=True)
    return path


def ledger_from_asset(asset: dict[str, Any], *, cycle_id: str, max_items: int = 8) -> dict[str, Any]:
    rows_raw = asset.get("rows")
    if not isinstance(rows_raw, list):
        for key in ("items", "pages", "images", "ledger"):
            candidate = asset.get(key)
            if isinstance(candidate, list):
                rows_raw = candidate
                break
    if not isinstance(rows_raw, list):
        rows_raw = []
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(rows_raw, start=1):
        if isinstance(raw, dict):
            row = dict(raw)
            row.setdefault("id", str(row.get("id") or f"{index:03d}"))
            row.setdefault("status", str(row.get("status") or "unfinished"))
        else:
            row = {"id": f"{index:03d}", "status": "unfinished", "value": raw}
        rows.append(row)
    if len(rows) > max_items:
        rows = rows[:max_items]
    return {
        "schema": "m8m_cycle_ledger_v1",
        "cycle": cycle_id,
        "max_items": max_items,
        "rows": rows,
    }


def first_unfinished(ledger: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(ledger, dict):
        return None
    for row in ledger.get("rows") or []:
        if isinstance(row, dict) and str(row.get("status") or "unfinished") != "done":
            return row
    return None


def mark_row(ledger: dict[str, Any], row_id: str, *, status: str, slot: str | None = None) -> dict[str, Any]:
    out = dict(ledger)
    rows = []
    for row in out.get("rows") or []:
        if not isinstance(row, dict):
            rows.append(row)
            continue
        item = dict(row)
        if str(item.get("id") or "") == str(row_id):
            item["status"] = status
            if slot:
                item["slot"] = slot
        rows.append(item)
    out["rows"] = rows
    return out


def purge_cycle_live(run_dir: Path, steps: list[dict[str, Any]]) -> None:
    """Delete unfinished live residue. Never touch items/<done>/."""
    run_dir = Path(run_dir)
    for step in steps:
        mid = str(step.get("id") or "")
        if not mid:
            continue
        live = run_dir / "milestones" / mid / "out"
        work = run_dir / "milestones" / mid / "work"
        # run_flow also persists model drafts in the public work/<milestone>/
        # slot.  A completed cycle row must never feed that draft into the
        # next row (for example, a 1080x688 scene into a 920x978 chart row).
        public_work = run_dir / "work" / mid
        if live.exists():
            shutil.rmtree(live, ignore_errors=True)
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        if public_work.exists():
            shutil.rmtree(public_work, ignore_errors=True)
        (run_dir / "milestones" / mid / "out" / "files").mkdir(parents=True, exist_ok=True)
        (run_dir / "milestones" / mid / "work" / "attempts").mkdir(parents=True, exist_ok=True)


def promote_cycle_round(
    run_dir: Path,
    flow: dict[str, Any],
    steps: list[dict[str, Any]],
    row_id: str,
) -> str:
    """Copy live out/ to items/<row>/ and keep it. Returns the slot prefix."""
    from flowstep_runtime import expected_artifact_path

    run_dir = Path(run_dir)
    prefix = ""
    for step in steps:
        mid = str(step.get("id") or "")
        dest = run_dir / "milestones" / mid / "items" / str(row_id)
        dest.mkdir(parents=True, exist_ok=True)
        live_out = run_dir / "milestones" / mid / "out"
        if live_out.exists():
            target = dest / "out"
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(live_out, target, copy_function=_link_or_copy_immutable)
        envelope = expected_artifact_path(run_dir, flow, step)
        if envelope.is_file():
            _link_or_copy_immutable(envelope, dest / "asset.json")
        prefix = f"milestones/{mid}/items/{row_id}"
    return prefix.replace("\\", "/")


RUN_ROSTER_SCHEMA = "m8m_run_roster_v1"
_OPEN_ROW = {"unfinished", "waiting"}


def run_roster_path(run_dir: Path) -> Path:
    return Path(run_dir) / "roster.json"


def wait_draft_slot(step_id: str) -> str:
    return f"milestones/{step_id}/work/draft.json"


def wait_draft_path(run_dir: Path, step_id: str) -> Path:
    return Path(run_dir) / "milestones" / str(step_id) / "work" / "draft.json"


def load_run_roster(run_dir: Path) -> dict[str, Any] | None:
    path = run_roster_path(run_dir)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_run_roster(run_dir: Path, roster: dict[str, Any]) -> Path:
    path = run_roster_path(run_dir)
    payload = dict(roster)
    payload.setdefault("schema", RUN_ROSTER_SCHEMA)
    payload["run_id"] = Path(run_dir).name
    payload["updated_at"] = utc_now()
    write_json(path, payload, overwrite=True)
    return path


def freeze_run_roster(run_dir: Path, flow: dict[str, Any]) -> dict[str, Any]:
    """One unfinished row per canvas milestone. Not a cycle ledger. Not a canvas node."""
    existing = load_run_roster(run_dir)
    if existing:
        return existing
    steps = flow.get("steps") or flow.get("milestones") or []
    rows: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        mid = str(step.get("id") or "")
        if mid:
            rows.append({"id": mid, "status": "unfinished"})
    completed: set[str] = set()
    skipped: set[str] = set()
    exec_path = Path(run_dir) / "flow-execution-record.json"
    if exec_path.is_file():
        try:
            record = json.loads(exec_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            record = {}
        if isinstance(record, dict):
            completed = {str(item.get("step_id") or "") for item in (record.get("steps") or []) if item}
            skipped = {str(item.get("step_id") or "") for item in (record.get("skipped") or []) if item}
    for row in rows:
        mid = str(row.get("id") or "")
        if mid in skipped:
            row["status"] = "skipped"
        elif mid in completed:
            row["status"] = "done"
    roster = {
        "schema": RUN_ROSTER_SCHEMA,
        "flow_id": flow.get("flow_id"),
        "run_id": Path(run_dir).name,
        "status": "running",
        "current": "",
        "rows": rows,
        "created_at": utc_now(),
    }
    roster = _set_current(roster)
    if not first_open_roster_row(roster):
        roster["status"] = "complete"
    save_run_roster(run_dir, roster)
    return roster


def first_open_roster_row(roster: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(roster, dict):
        return None
    for row in roster.get("rows") or []:
        if isinstance(row, dict) and str(row.get("status") or "unfinished") in _OPEN_ROW:
            return row
    return None


def waiting_roster_row(roster: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(roster, dict):
        return None
    for row in roster.get("rows") or []:
        if isinstance(row, dict) and str(row.get("status") or "") == "waiting":
            return row
    return None


def _set_current(roster: dict[str, Any]) -> dict[str, Any]:
    nxt = first_open_roster_row(roster)
    roster["current"] = str((nxt or {}).get("id") or "")
    return roster


def mark_roster_row(
    run_dir: Path,
    milestone_id: str,
    *,
    status: str,
    slot: str | None = None,
) -> dict[str, Any] | None:
    roster = load_run_roster(run_dir)
    if not roster:
        return None
    roster = mark_row(roster, milestone_id, status=status, slot=slot)
    if status == "waiting":
        roster["status"] = "paused"
        roster["current"] = str(milestone_id)
    else:
        roster = _set_current(roster)
        if roster.get("status") not in {"blocked"}:
            if not first_open_roster_row(roster):
                roster["status"] = "complete"
            elif roster.get("status") == "paused" and status != "waiting":
                roster["status"] = "running"
            elif not roster.get("status"):
                roster["status"] = "running"
    save_run_roster(run_dir, roster)
    return roster


def pause_run(run_dir: Path, milestone_id: str, *, slot: str | None = None) -> dict[str, Any] | None:
    return mark_roster_row(
        run_dir,
        milestone_id,
        status="waiting",
        slot=slot or wait_draft_slot(milestone_id),
    )


def resume_run(run_dir: Path) -> dict[str, Any] | None:
    roster = load_run_roster(run_dir)
    if not roster:
        return None
    if str(roster.get("status") or "") == "paused":
        roster["status"] = "running"
        save_run_roster(run_dir, roster)
    return roster


def reset_roster_rows(run_dir: Path, ids: set[str]) -> dict[str, Any] | None:
    roster = load_run_roster(run_dir)
    if not roster:
        return None
    for row_id in ids:
        roster = mark_row(roster, row_id, status="unfinished")
    roster = _set_current(roster)
    if str(roster.get("status") or "") not in {"paused", "blocked"}:
        roster["status"] = "running"
    save_run_roster(run_dir, roster)
    return roster


def mark_roster_blocked(run_dir: Path, milestone_id: str | None = None) -> dict[str, Any] | None:
    roster = load_run_roster(run_dir)
    if not roster:
        return None
    roster["status"] = "blocked"
    if milestone_id:
        roster["current"] = str(milestone_id)
    save_run_roster(run_dir, roster)
    return roster


def mark_roster_complete(run_dir: Path) -> dict[str, Any] | None:
    roster = load_run_roster(run_dir)
    if not roster:
        return None
    roster["status"] = "complete"
    roster["current"] = ""
    save_run_roster(run_dir, roster)
    return roster


def find_paused_run(root: Path, flow_id: str | None = None) -> Path | None:
    """Newest paused roster under the active harness root or the run itself."""
    root = Path(root)
    hits: list[tuple[str, float, Path]] = []

    def consider(path: Path) -> None:
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict) or data.get("schema") != RUN_ROSTER_SCHEMA:
            return
        if str(data.get("status") or "") != "paused":
            return
        if flow_id and str(data.get("flow_id") or "") not in {"", str(flow_id)}:
            return
        stamp = str(data.get("updated_at") or "")
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        hits.append((stamp, mtime, path.parent))

    consider(root / "roster.json")
    patterns = (
        "*/roster.json",
        "runs/*/roster.json",
    )
    for pattern in patterns:
        try:
            found = root.glob(pattern)
        except OSError:
            continue
        for path in found:
            consider(path)
    if not hits:
        return None
    hits.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return hits[0][2]
