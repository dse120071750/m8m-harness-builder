"""Deterministic transport archive for one validated M8M source bundle.

The archive is a byte transport envelope, never a workflow or deployment
authority.  It contains the exact canonical source-bundle JSON and every
resource byte named by that bundle.  ZIP members are stored rather than
deflated so archive identity does not depend on a platform's zlib build.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import unicodedata
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from source_bundle import (
    SourceBundleError,
    serialize_source_bundle,
    source_bundle_resource_bytes,
    verify_source_bundle,
)


PACKAGE_ARCHIVE_SCHEMA = "m8m.workflow_package_archive.v1"
PACKAGE_MANIFEST_PATH = "package-manifest.json"
SOURCE_BUNDLE_PATH = "source-bundle.json"
PACKAGE_MEDIA_TYPE = "application/vnd.m8m.workflow-package+zip"

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_WINDOWS_DEVICE_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class WorkflowPackageError(ValueError):
    """Raised when a source bundle cannot become a closed transport archive."""


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise WorkflowPackageError(f"{label} must be a non-empty trimmed path")
    normalized = unicodedata.normalize("NFC", value)
    if "\\" in normalized or ":" in normalized or "\x00" in normalized:
        raise WorkflowPackageError(f"{label} is unsafe: {value}")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WorkflowPackageError(f"{label} is unsafe: {value}")
    for part in path.parts:
        stem = part.rstrip(" .").split(".", 1)[0].casefold()
        if part != part.rstrip(" .") or stem in _WINDOWS_DEVICE_NAMES:
            raise WorkflowPackageError(f"{label} is unsafe on Windows: {value}")
    return path.as_posix()


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (0o100644 & 0xFFFF) << 16
    info.flag_bits = 0x800
    info.extra = b""
    info.comment = b""
    return info


def build_workflow_package(
    source_bundle: Mapping[str, Any],
    *,
    resource_root: Path | str,
) -> tuple[bytes, dict[str, Any]]:
    """Return deterministic ``.m8mpkg`` bytes and its closed manifest."""

    try:
        proof = verify_source_bundle(source_bundle)
    except SourceBundleError as exc:
        raise WorkflowPackageError(f"source bundle is invalid: {exc}") from exc
    bundle = dict(source_bundle)
    source_bundle_digest = (
        str(proof.get("source_bundle_digest") or "")
        if isinstance(proof, Mapping)
        else ""
    )
    if _SHA256_RE.fullmatch(source_bundle_digest) is None:
        raise WorkflowPackageError("source bundle proof digest is invalid")

    resources_by_path: dict[str, dict[str, Any]] = {}
    resource_payloads: dict[str, bytes] = {}
    casefold_paths: dict[str, str] = {}
    for index, raw in enumerate(bundle.get("resources") or []):
        if not isinstance(raw, Mapping):
            raise WorkflowPackageError(f"source bundle resource {index} is invalid")
        source_path = _safe_path(
            raw.get("source_path"), label=f"resource {index} source_path"
        )
        folded = unicodedata.normalize("NFKC", source_path).casefold()
        previous_case = casefold_paths.get(folded)
        if previous_case is not None and previous_case != source_path:
            raise WorkflowPackageError(
                f"resource paths collide by case: {previous_case}, {source_path}"
            )
        casefold_paths[folded] = source_path
        try:
            payload = source_bundle_resource_bytes(resource_root, raw)
        except SourceBundleError as exc:
            raise WorkflowPackageError(str(exc)) from exc
        row = {
            "source_path": source_path,
            "archive_path": f"resources/{source_path}",
            "media_type": str(raw.get("media_type") or ""),
            "byte_count": len(payload),
            "digest": _digest(payload),
        }
        previous = resources_by_path.get(source_path)
        if previous is not None and previous != row:
            raise WorkflowPackageError(
                f"duplicate resource path has conflicting declarations: {source_path}"
            )
        resources_by_path[source_path] = row
        resource_payloads[source_path] = payload

    manifest = {
        "schema": PACKAGE_ARCHIVE_SCHEMA,
        "source_bundle_path": SOURCE_BUNDLE_PATH,
        "source_bundle_digest": source_bundle_digest,
        "resources": [resources_by_path[key] for key in sorted(resources_by_path)],
    }
    members: list[tuple[str, bytes]] = [
        (PACKAGE_MANIFEST_PATH, _canonical_json(manifest)),
        (SOURCE_BUNDLE_PATH, serialize_source_bundle(bundle)),
    ]
    members.extend(
        (resources_by_path[key]["archive_path"], resource_payloads[key])
        for key in sorted(resources_by_path)
    )

    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_STORED) as archive:
        archive.comment = b""
        for archive_path, payload in members:
            archive.writestr(_zip_info(archive_path), payload)
    return output.getvalue(), manifest


def write_workflow_package(
    output_path: Path | str,
    source_bundle: Mapping[str, Any],
    *,
    resource_root: Path | str,
) -> dict[str, Any]:
    """Atomically write one deterministic package and return its identity."""

    payload, manifest = build_workflow_package(
        source_bundle,
        resource_root=resource_root,
    )
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / (
        f".{destination.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return {
        "schema": PACKAGE_ARCHIVE_SCHEMA,
        "media_type": PACKAGE_MEDIA_TYPE,
        "path": str(destination.resolve()),
        "byte_count": len(payload),
        "digest": _digest(payload),
        "source_bundle_digest": manifest["source_bundle_digest"],
        "resource_count": len(manifest["resources"]),
    }


__all__ = [
    "PACKAGE_ARCHIVE_SCHEMA",
    "PACKAGE_MANIFEST_PATH",
    "PACKAGE_MEDIA_TYPE",
    "SOURCE_BUNDLE_PATH",
    "WorkflowPackageError",
    "build_workflow_package",
    "write_workflow_package",
]
