"""Build and verify immutable, codebase-owned M8M runtime releases.

The Builder is a compiler/packager.  Product execution must never import the
mutable Builder installation.  A build copies this closed runtime payload into
the owning codebase beside the compiled harness, addresses it by its payload
digest, and pins that digest in every run before workflow execution starts.
The product skill contains only a pointer to the codebase launcher.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import secrets
import shutil
import sys
import zipfile
from copy import deepcopy
from contextlib import contextmanager
import platform
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


RUNTIME_RELEASE_SCHEMA = "m8m.runtime_release.v1"
RUNTIME_LOCK_SCHEMA = "m8m.runtime_lock.v1"
RUNTIME_NAME = "m8m-runtime"
RUNTIME_VERSION = "1.0.0"
RUNTIME_CLI_ABI = "m8m_runtime_cli_v1"
RUNTIME_ENTRYPOINT = "scripts/run_flow.py"
RUNTIME_LOCK_FILENAME = "m8m-runtime-lock.json"
RUNTIME_MANIFEST_ENV = "M8M_RUNTIME_MANIFEST"
# A one-use result created only by full verification inside the isolated
# bootstrap. Never persisted, inherited from an environment flag or reused by
# another process/resume.
_startup_verification = None
RUNTIME_DEPENDENCIES = (
    "attrs",
    "jsonschema",
    "jsonschema-specifications",
    "PyYAML",
    "referencing",
    "rpds-py",
)

# This is the product execution closure.  Compiler, source-bundle, chart, and
# Builder dogfood modules are deliberately absent.
RUNTIME_SCRIPT_FILES = (
    "candidate_cache.py",
    "execution_identity.py",
    "flowstep_runtime.py",
    "flowstep_tools.py",
    "gem_text.py",
    "m8m_cache.py",
    "milestone_expectation.py",
    "project_imports.py",
    "run_flow.py",
    "run_goal.py",
    "runtime_release.py",
    "schema_gate.py",
    "session_layout.py",
)

RUNTIME_CONTRACT_FILES = (
    "file_ref_v2.schema.json",
    "flow_sequence_action_v2.schema.json",
    "flowstep_flow_v4.schema.json",
    "flowstep_output_v3.schema.json",
    "m8m_cache_receipt_v1.schema.json",
    "m8m_candidate_cache_entry_v1.schema.json",
    "m8m_chosen_output_v1.schema.json",
    "m8m_context_capsule_v1.schema.json",
    "m8m_goal_ledger_v1.schema.json",
    "m8m_milestone_expectation_v1.schema.json",
    "m8m_milestone_judge_receipt_v1.schema.json",
    "m8m_milestone_judge_request_v1.schema.json",
    "m8m_run_context_v1.schema.json",
    "m8m_run_context_v2.schema.json",
    "m8m_run_storage_contract_v1.schema.json",
    "m8m_runtime_lock_v1.schema.json",
    "m8m_runtime_release_v1.schema.json",
    "m8m_source_asset_manifest_v1.schema.json",
)


class RuntimeReleaseError(ValueError):
    """The runtime release, lock, or launch boundary is invalid."""


def _is_unsafe_link(path: Path) -> bool:
    """Treat Windows junctions/reparse directories like symbolic links."""

    try:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(is_junction and is_junction())
    except OSError:
        return True


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pretty_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _safe_relative(value: str) -> str:
    if not value or "\\" in value:
        raise RuntimeReleaseError(f"unsafe runtime member path: {value!r}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise RuntimeReleaseError(f"unsafe runtime member path: {value!r}")
    return pure.as_posix()


def _source_members(builder_root: Path) -> list[tuple[str, Path]]:
    root = builder_root.resolve()
    members: list[tuple[str, Path]] = []
    for name in RUNTIME_SCRIPT_FILES:
        members.append((f"scripts/{name}", root / "scripts" / name))
    for name in RUNTIME_CONTRACT_FILES:
        members.append((f"contracts/{name}", root / "contracts" / name))
    missing = [str(path) for _, path in members if not path.is_file()]
    if missing:
        raise RuntimeReleaseError(
            "runtime release source is incomplete: " + ", ".join(missing)
        )
    return members


def _python_identity() -> tuple[str, str]:
    python_abi = str(sys.implementation.cache_tag or "")
    if not python_abi:
        raise RuntimeReleaseError("cannot determine the Python runtime ABI")
    executable = Path(sys.executable).absolute()
    if not executable.is_file() or _is_unsafe_link(executable):
        raise RuntimeReleaseError(
            "cannot determine a safe Python interpreter executable identity"
        )
    return python_abi, f"sha256:{_sha256_file(executable)}"


def validate_product_distributions(value: Any) -> list[dict[str, str]]:
    """Validate exact authored roots without consulting host packages."""
    if value is None:
        return []
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise RuntimeReleaseError("runtime_distributions must contain 1–32 exact distribution pins")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in value:
        if not isinstance(row, dict) or set(row) != {"name", "version"}:
            raise RuntimeReleaseError("runtime_distributions rows require only name and version")
        name, version = row["name"], row["version"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", name):
            raise RuntimeReleaseError("runtime_distributions has an invalid distribution name")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]*", version):
            raise RuntimeReleaseError("runtime_distributions requires exact versions, without ranges, extras or URLs")
        canonical = re.sub(r"[-_.]+", "-", name).lower()
        if canonical in seen:
            raise RuntimeReleaseError(f"duplicate canonical runtime distribution: {canonical}")
        seen.add(canonical)
        result.append({"name": name, "version": version})
    return result


def assert_product_distribution_lock(value: Any, dependencies: Sequence[Mapping[str, Any]]) -> None:
    """Compare authored roots only with the already verified runtime lock."""
    locked = {re.sub(r"[-_.]+", "-", str(row["name"])).lower(): str(row["version"])
              for row in dependencies}
    for row in validate_product_distributions(value):
        canonical = re.sub(r"[-_.]+", "-", row["name"]).lower()
        if locked.get(canonical) != row["version"]:
            raise RuntimeReleaseError(
                f"declared runtime distribution is absent or differs from the verified runtime lock: "
                f"{row['name']}=={row['version']} (locked {locked.get(canonical)!r})"
            )


_marker_environment_lock = threading.RLock()


@contextmanager
def _local_windows_marker_queries():
    """Use CPython's local OS fallback, not a potentially stuck WMI service.

Packaging re-queries the default environment even when evaluate() receives an
explicit environment. Keep the fallback scoped over the entire closure pass;
restore the process hook on both success and error. No OS service is modified.
"""
    if sys.platform != 'win32' or not hasattr(platform, '_wmi_query'):
        yield
        return
    with _marker_environment_lock:
        original = platform._wmi_query
        def unavailable(*args, **kwargs):
            raise OSError('Builder uses local Windows marker information')
        platform._wmi_query = unavailable
        try:
            yield
        finally:
            platform._wmi_query = original


def _resolved_runtime_distributions(product_distributions: Any = None) -> list[tuple[str, Any]]:
    with _local_windows_marker_queries():
        return _resolve_runtime_distributions_local(product_distributions)


def _resolve_runtime_distributions_local(product_distributions: Any = None) -> list[tuple[str, Any]]:
    """Resolve the marker-aware transitive dependency closure for this host."""

    try:
        from packaging.markers import default_environment
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name
    except ImportError as exc:
        raise RuntimeReleaseError(
            "runtime dependency closure requires the Builder's packaging library"
        ) from exc

    environment = default_environment()
    environment["extra"] = ""
    roots = validate_product_distributions(product_distributions)
    try:
        pending = [Requirement(name) for name in RUNTIME_DEPENDENCIES]
        pending.extend(Requirement(f"{row['name']}=={row['version']}") for row in roots)
    except Exception as exc:
        raise RuntimeReleaseError(f"invalid exact runtime distribution pin: {exc}") from exc
    resolved: dict[str, tuple[str, Any]] = {}
    while pending:
        pending.sort(key=lambda item: canonicalize_name(item.name))
        requirement = pending.pop(0)
        if requirement.marker is not None and not requirement.marker.evaluate(environment):
            continue
        if requirement.extras or requirement.url:
            raise RuntimeReleaseError(f"runtime dependency extras/direct URLs are unsupported: {requirement}")
        canonical = canonicalize_name(requirement.name)
        try:
            distribution = importlib.metadata.distribution(requirement.name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeReleaseError(
                f"transitive runtime dependency is not installed: {requirement.name}"
            ) from exc
        if requirement.specifier and not requirement.specifier.contains(
            str(distribution.version), prereleases=True
        ):
            raise RuntimeReleaseError(
                "installed transitive runtime dependency does not satisfy its "
                f"requirement: {requirement} (found {distribution.version})"
            )
        if canonical in resolved:
            continue
        declared_name = str(distribution.metadata.get("Name") or requirement.name)
        resolved[canonical] = (declared_name, distribution)
        child_requirements = []
        for raw_requirement in distribution.requires or []:
            try:
                child = Requirement(raw_requirement)
            except Exception as exc:
                raise RuntimeReleaseError(
                    f"invalid Requires-Dist for {declared_name}: {raw_requirement}"
                ) from exc
            if child.marker is None or child.marker.evaluate(environment):
                child_requirements.append(child)
        pending.extend(child_requirements)
    rows = [resolved[key] for key in sorted(resolved)]
    # Exact strings also reject a host's local-version substitution for an
    # authored public version, even where PEP 440 equality would allow it.
    assert_product_distribution_lock(product_distributions,
        [{"name": name, "version": str(dist.version)} for name, dist in rows])
    return rows


def _dependency_sources(product_distributions: Any = None, *, member_rows=None) -> tuple[list[dict[str, Any]], list[tuple[str, Path]]]:
    """Freeze every installed distribution into its own import root.

    Distribution paths that deliberately escape site-packages (for example a
    generated console script) are not importable package members and are
    excluded.  Everything inside the distribution root is copied and hashed.
    Separate vendor roots avoid cross-distribution path collisions while the
    dispatcher prepends all locked vendor roots to the isolated interpreter.
    """

    dependencies: list[dict[str, Any]] = []
    sources: list[tuple[str, Path]] = []
    vendor_roots: set[str] = set()
    for name, distribution in _resolved_runtime_distributions(product_distributions):
        raw_files = distribution.files
        if not raw_files:
            raise RuntimeReleaseError(
                f"runtime dependency has no deterministic file inventory: {name}"
            )
        vendor_id = "".join(
            character.lower() if character.isalnum() else "-"
            for character in name
        ).strip("-")
        vendor_path = f"vendor/{vendor_id}"
        if not vendor_id or vendor_path in vendor_roots:
            raise RuntimeReleaseError(
                f"runtime dependency vendor root collides: {name}"
            )
        vendor_roots.add(vendor_path)
        file_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw_file in sorted(raw_files, key=lambda item: str(item).casefold()):
            raw_relative = str(raw_file).replace("\\", "/")
            pure = PurePosixPath(raw_relative)
            # Console entrypoints may live outside site-packages. They are not
            # imported by the runtime and must not escape the vendor root.
            if (
                not raw_relative
                or pure.is_absolute()
                or any(part in {"", ".", ".."} for part in pure.parts)
            ):
                continue
            # Bytecode caches are interpreter-generated, may disappear during
            # clean installation, and are never an authored dependency asset.
            # Source files and native extension modules remain in the lock.
            if "__pycache__" in pure.parts or pure.suffix.lower() in {".pyc", ".pyo"}:
                continue
            relative = pure.as_posix()
            if relative in seen:
                raise RuntimeReleaseError(
                    f"runtime dependency contains a duplicate file: {name}: {relative}"
                )
            source = Path(distribution.locate_file(raw_file)).absolute()
            if not source.is_file() or _is_unsafe_link(source):
                raise RuntimeReleaseError(
                    f"runtime dependency file is missing or unsafe: {name}: {relative}"
                )
            seen.add(relative)
            member_path = f"{vendor_path}/{relative}"
            row = {
                "path": member_path,
                "byte_count": source.stat().st_size,
                "digest": f"sha256:{_sha256_file(source)}",
            }
            file_rows.append(row)
            if member_rows is not None:
                member_rows[member_path] = row
            sources.append((member_path, source))
        if not file_rows:
            raise RuntimeReleaseError(
                f"runtime dependency has no vendorable files: {name}"
            )
        dependencies.append(
            {
                "name": name,
                "version": str(distribution.version),
                "vendor_path": vendor_path,
                "file_count": len(file_rows),
                "files_digest": _digest_bytes(_canonical_bytes(file_rows)),
            }
        )
    return dependencies, sources


def _runtime_identity(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return every field that can change runtime execution semantics."""

    return {
        "schema": manifest["schema"],
        "runtime_name": manifest["runtime_name"],
        "runtime_version": manifest["runtime_version"],
        "cli_abi": manifest["cli_abi"],
        "entrypoint": manifest["entrypoint"],
        "python_abi": manifest["python_abi"],
        "python_executable_digest": manifest["python_executable_digest"],
        "dependencies": manifest["dependencies"],
        "members": manifest["members"],
    }


def _manifest_from_sources(builder_root: Path, product_distributions: Any = None) -> tuple[dict[str, Any], list[tuple[str, Path]]]:
    sources = _source_members(builder_root)
    dependency_rows = {}
    dependencies, dependency_sources = _dependency_sources(product_distributions, member_rows=dependency_rows)
    sources.extend(dependency_sources)
    members = [
        dependency_rows[relative] if relative in dependency_rows else {
            "path": relative,
            "byte_count": source.stat().st_size,
            "digest": f"sha256:{_sha256_file(source)}",
        }
        for relative, source in sources
    ]
    python_abi, python_executable_digest = _python_identity()
    identity = {
        "schema": RUNTIME_RELEASE_SCHEMA,
        "runtime_name": RUNTIME_NAME,
        "runtime_version": RUNTIME_VERSION,
        "cli_abi": RUNTIME_CLI_ABI,
        "entrypoint": RUNTIME_ENTRYPOINT,
        "python_abi": python_abi,
        "python_executable_digest": python_executable_digest,
        "dependencies": dependencies,
        "members": members,
    }
    payload_digest = _digest_bytes(
        _canonical_bytes(_runtime_identity(identity))
    )
    runtime_id = payload_digest.split(":", 1)[1]
    return (
        {
            "schema": RUNTIME_RELEASE_SCHEMA,
            "runtime_name": RUNTIME_NAME,
            "runtime_version": RUNTIME_VERSION,
            "runtime_id": runtime_id,
            "cli_abi": RUNTIME_CLI_ABI,
            "entrypoint": RUNTIME_ENTRYPOINT,
            "python_abi": python_abi,
            "python_executable_digest": python_executable_digest,
            "dependencies": dependencies,
            "payload_digest": payload_digest,
            "members": members,
        },
        sources,
    )


def _runtime_lock(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": RUNTIME_LOCK_SCHEMA,
        "runtime_name": str(manifest["runtime_name"]),
        "runtime_version": str(manifest["runtime_version"]),
        "runtime_id": str(manifest["runtime_id"]),
        "cli_abi": str(manifest["cli_abi"]),
        "entrypoint": str(manifest["entrypoint"]),
        "python_abi": str(manifest["python_abi"]),
        "python_executable_digest": str(manifest["python_executable_digest"]),
        "dependencies": [dict(item) for item in manifest["dependencies"]],
        "payload_digest": str(manifest["payload_digest"]),
        "manifest_digest": _digest_bytes(_canonical_bytes(dict(manifest))),
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _pretty_json_bytes(value)
    if path.is_file() and path.read_bytes() == encoded:
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(encoded)
    os.replace(temporary, path)


def _assert_release_bytes(
    release_dir: Path,
    manifest: Mapping[str, Any],
    sources: Sequence[tuple[str, Path]],
) -> Path:
    """Accept an existing release only when every byte is already identical."""

    manifest_path = release_dir / "runtime-manifest.json"
    expected = {relative for relative, _ in sources}
    expected.add("runtime-manifest.json")
    actual: set[str] = set()
    for candidate in release_dir.rglob("*"):
        if _is_unsafe_link(candidate):
            raise RuntimeReleaseError(
                f"immutable runtime release contains an unsafe link: {candidate}"
            )
        if candidate.is_file():
            actual.add(candidate.relative_to(release_dir).as_posix())
    if actual != expected:
        raise RuntimeReleaseError(
            "immutable runtime release directory already exists with different bytes"
        )
    for relative, source in sources:
        destination = release_dir.joinpath(*PurePosixPath(relative).parts)
        if (
            not destination.is_file()
            or destination.stat().st_size != source.stat().st_size
            or _sha256_file(destination) != _sha256_file(source)
        ):
            raise RuntimeReleaseError(
                "immutable runtime release directory already exists with different bytes"
            )
    if not manifest_path.is_file() or manifest_path.read_bytes() != _pretty_json_bytes(manifest):
        raise RuntimeReleaseError(
            "immutable runtime release directory already exists with different bytes"
        )
    verify_runtime_release(manifest_path)
    return manifest_path


def _copy_release(
    release_dir: Path,
    manifest: Mapping[str, Any],
    sources: Sequence[tuple[str, Path]],
) -> Path:
    parent = release_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    if release_dir.exists():
        if not release_dir.is_dir() or _is_unsafe_link(release_dir):
            raise RuntimeReleaseError("immutable runtime release path is unsafe")
        return _assert_release_bytes(release_dir, manifest, sources)

    temporary = parent / (
        f".{release_dir.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    temporary.mkdir(parents=False, exist_ok=False)
    try:
        for relative, source in sources:
            destination = temporary.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        manifest_path = temporary / "runtime-manifest.json"
        manifest_path.write_bytes(_pretty_json_bytes(manifest))
        verify_runtime_release(manifest_path)
        try:
            os.rename(temporary, release_dir)
        except OSError:
            # A concurrent writer may have won. It is acceptable only when it
            # published the exact same immutable release.
            if not release_dir.is_dir() or _is_unsafe_link(release_dir):
                raise
            _assert_release_bytes(release_dir, manifest, sources)
        return release_dir / "runtime-manifest.json"
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _write_release_archive(
    archive_path: Path,
    manifest: Mapping[str, Any],
    sources: Sequence[tuple[str, Path]],
) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive_path.with_name(f".{archive_path.name}.{os.getpid()}.tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        payloads = [(relative, source.read_bytes()) for relative, source in sources]
        payloads.append(("runtime-manifest.json", _canonical_bytes(dict(manifest))))
        for relative, payload in sorted(payloads):
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            archive.writestr(info, payload)
    if archive_path.is_file():
        if archive_path.read_bytes() != temporary.read_bytes():
            temporary.unlink(missing_ok=True)
            raise RuntimeReleaseError(
                "immutable runtime release archive already exists with different bytes"
            )
        temporary.unlink(missing_ok=True)
        return
    os.replace(temporary, archive_path)


def stage_runtime_release(
    *,
    builder_root: Path,
    harness: Path,
    product_roots: Sequence[Path],
    flow_id: str,
    codebase: Path,
    product_distributions: Any = None,
) -> dict[str, Any]:
    """Stage one immutable runtime owned by the compiled codebase harness.

    Product skills receive only a tiny pointer to ``harness/launch.py``.  They
    never receive or import runtime implementation modules.
    """

    manifest, sources = _manifest_from_sources(builder_root, product_distributions)
    runtime_id = str(manifest["runtime_id"])
    lock = _runtime_lock(manifest)
    harness = harness.absolute()
    if _is_unsafe_link(harness):
        raise RuntimeReleaseError("staged harness uses an unsafe link")
    harness.mkdir(parents=True, exist_ok=True)
    (harness / "BUILD_REQUIRED_RUNTIME").unlink(missing_ok=True)
    release_dir = harness / "runtime" / "releases" / runtime_id
    for boundary in (
        harness / "runtime",
        harness / "runtime" / "releases",
        release_dir,
    ):
        if boundary.exists() and _is_unsafe_link(boundary):
            raise RuntimeReleaseError(
                f"runtime release staging boundary uses an unsafe link: {boundary}"
            )
    manifest_path = _copy_release(release_dir, manifest, sources)
    _write_json(harness / "runtime" / "active.json", lock)
    codebase_launcher_source = (
        builder_root.resolve() / "templates" / "codebase-launcher.py"
    )
    skill_pointer_source = builder_root.resolve() / "templates" / "run.py"
    if not codebase_launcher_source.is_file() or not skill_pointer_source.is_file():
        raise RuntimeReleaseError("codebase launcher templates are missing")
    codebase_launcher = harness / "launch.py"
    codebase_launcher.write_text(
        codebase_launcher_source.read_text(encoding="utf-8"),
        encoding="utf-8",
        newline="\n",
    )
    codebase_launcher_sha256 = _sha256_file(codebase_launcher)
    for product_root in product_roots:
        root = product_root.resolve()
        launcher = root / "scripts" / "m8m_run.py"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        installed_launcher = (
            codebase.resolve() / "flowsteps" / "flows" / flow_id / "launch.py"
        )
        pointer = (
            skill_pointer_source.read_text(encoding="utf-8")
            .replace(
                "__CODEBASE_LAUNCHER__",
                str(installed_launcher).replace("\\", "/"),
            )
            .replace("__CODEBASE_LAUNCHER_SHA256__", codebase_launcher_sha256)
        )
        launcher.write_text(pointer, encoding="utf-8", newline="\n")
    harness_lock_path = harness / RUNTIME_LOCK_FILENAME
    _write_json(harness_lock_path, lock)
    compiled = harness / "compiled" / "runtime"
    archive_path = compiled / f"m8m-runtime-{runtime_id}.zip"
    _write_release_archive(archive_path, manifest, sources)
    archive_manifest_path = compiled / "runtime-manifest.json"
    _write_json(archive_manifest_path, manifest)
    return {
        "schema": RUNTIME_RELEASE_SCHEMA,
        "runtime_name": RUNTIME_NAME,
        "runtime_version": RUNTIME_VERSION,
        "runtime_id": runtime_id,
        "cli_abi": RUNTIME_CLI_ABI,
        "entrypoint": RUNTIME_ENTRYPOINT,
        "python_abi": str(manifest["python_abi"]),
        "python_executable_digest": str(manifest["python_executable_digest"]),
        "dependencies": [dict(item) for item in manifest["dependencies"]],
        "payload_digest": str(manifest["payload_digest"]),
        "manifest_digest": str(lock["manifest_digest"]),
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256_file(manifest_path),
        "codebase_launcher_path": str(codebase_launcher),
        "codebase_launcher_sha256": codebase_launcher_sha256,
        "archive_path": str(archive_path),
        "archive_sha256": _sha256_file(archive_path),
        "harness_lock_path": str(harness_lock_path),
        "harness_lock_sha256": _sha256_file(harness_lock_path),
        "member_count": len(manifest["members"]),
    }


def verify_runtime_release(manifest_path: Path) -> dict[str, Any]:
    path = manifest_path.absolute()
    for boundary in (path, path.parent, path.parent.parent, path.parent.parent.parent):
        if boundary.exists() and _is_unsafe_link(boundary):
            raise RuntimeReleaseError(
                f"runtime release path uses an unsafe link: {boundary}"
            )
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeReleaseError(f"cannot read runtime release manifest: {path}: {exc}") from exc
    required = {
        "schema",
        "runtime_name",
        "runtime_version",
        "runtime_id",
        "cli_abi",
        "entrypoint",
        "python_abi",
        "python_executable_digest",
        "dependencies",
        "payload_digest",
        "members",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise RuntimeReleaseError("runtime release manifest has an invalid closed shape")
    if manifest.get("schema") != RUNTIME_RELEASE_SCHEMA:
        raise RuntimeReleaseError("runtime release schema is invalid")
    if manifest.get("runtime_name") != RUNTIME_NAME:
        raise RuntimeReleaseError("runtime release name is invalid")
    if manifest.get("cli_abi") != RUNTIME_CLI_ABI:
        raise RuntimeReleaseError("runtime release CLI ABI is unsupported")
    members = manifest.get("members")
    if not isinstance(members, list) or not members:
        raise RuntimeReleaseError("runtime release has no members")
    dependencies = manifest.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        raise RuntimeReleaseError("runtime release has no dependency lock")
    normalized_dependencies: list[dict[str, Any]] = []
    dependency_names: set[str] = set()
    dependency_vendor_paths: set[str] = set()
    for item in dependencies:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "version",
            "vendor_path",
            "file_count",
            "files_digest",
        }:
            raise RuntimeReleaseError("runtime dependency lock has an invalid closed shape")
        name = str(item.get("name") or "")
        version = str(item.get("version") or "")
        vendor_path = _safe_relative(str(item.get("vendor_path") or ""))
        file_count = item.get("file_count")
        files_digest = str(item.get("files_digest") or "")
        if (
            not name
            or not version
            or name in dependency_names
            or vendor_path in dependency_vendor_paths
            or len(PurePosixPath(vendor_path).parts) != 2
            or PurePosixPath(vendor_path).parts[0] != "vendor"
            or not isinstance(file_count, int)
            or isinstance(file_count, bool)
            or file_count < 1
            or len(files_digest) != 71
            or not files_digest.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in files_digest[7:])
        ):
            raise RuntimeReleaseError("runtime dependency lock has an invalid entry")
        dependency_names.add(name)
        dependency_vendor_paths.add(vendor_path)
        normalized_dependencies.append(
            {
                "name": name,
                "version": version,
                "vendor_path": vendor_path,
                "file_count": file_count,
                "files_digest": files_digest,
            }
        )
    python_abi = str(manifest.get("python_abi") or "")
    python_executable_digest = str(manifest.get("python_executable_digest") or "")
    current_abi, current_executable_digest = _python_identity()
    if (
        python_abi != current_abi
        or python_executable_digest != current_executable_digest
    ):
        raise RuntimeReleaseError(
            "runtime host interpreter does not match the immutable release"
        )
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    release_root = path.parent
    for item in members:
        if not isinstance(item, dict) or set(item) != {"path", "byte_count", "digest"}:
            raise RuntimeReleaseError("runtime release member has an invalid closed shape")
        relative = _safe_relative(str(item.get("path") or ""))
        if relative in seen:
            raise RuntimeReleaseError(f"duplicate runtime release member: {relative}")
        seen.add(relative)
        member = release_root.joinpath(*PurePosixPath(relative).parts).resolve()
        if release_root != member and release_root not in member.parents:
            raise RuntimeReleaseError(f"runtime release member escapes its root: {relative}")
        if not member.is_file() or _is_unsafe_link(member):
            raise RuntimeReleaseError(f"runtime release member is missing: {relative}")
        actual = f"sha256:{_sha256_file(member)}"
        if actual != item.get("digest") or member.stat().st_size != item.get("byte_count"):
            raise RuntimeReleaseError(f"runtime release member digest mismatch: {relative}")
        normalized.append(
            {"path": relative, "byte_count": member.stat().st_size, "digest": actual}
        )
    covered_vendor_members: set[str] = set()
    for dependency in normalized_dependencies:
        prefix = str(dependency["vendor_path"]) + "/"
        dependency_members = [
            item for item in normalized if str(item["path"]).startswith(prefix)
        ]
        if (
            len(dependency_members) != dependency["file_count"]
            or _digest_bytes(_canonical_bytes(dependency_members))
            != dependency["files_digest"]
        ):
            raise RuntimeReleaseError(
                f"runtime dependency file identity is invalid: {dependency['name']}"
            )
        covered_vendor_members.update(str(item["path"]) for item in dependency_members)
    all_vendor_members = {
        str(item["path"])
        for item in normalized
        if str(item["path"]).startswith("vendor/")
    }
    if all_vendor_members != covered_vendor_members:
        raise RuntimeReleaseError(
            "runtime release has dependency files outside its closed vendor locks"
        )
    expected_files = {"runtime-manifest.json", *seen}
    actual_files: set[str] = set()
    for member in release_root.rglob("*"):
        if _is_unsafe_link(member):
            raise RuntimeReleaseError(
                f"runtime release contains an undeclared link: {member.relative_to(release_root)}"
            )
        if member.is_file():
            actual_files.add(member.relative_to(release_root).as_posix())
    extras = sorted(actual_files - expected_files)
    missing = sorted(expected_files - actual_files)
    if extras or missing:
        detail = []
        if extras:
            detail.append("undeclared files: " + ", ".join(extras))
        if missing:
            detail.append("missing files: " + ", ".join(missing))
        raise RuntimeReleaseError("runtime release is not a closed directory: " + "; ".join(detail))
    normalized_manifest = {
        **manifest,
        "python_abi": python_abi,
        "python_executable_digest": python_executable_digest,
        "dependencies": normalized_dependencies,
        "members": normalized,
    }
    payload_digest = _digest_bytes(
        _canonical_bytes(_runtime_identity(normalized_manifest))
    )
    runtime_id = payload_digest.split(":", 1)[1]
    if manifest.get("payload_digest") != payload_digest or manifest.get("runtime_id") != runtime_id:
        raise RuntimeReleaseError("runtime release payload identity is invalid")
    if str(manifest.get("entrypoint") or "") not in seen:
        raise RuntimeReleaseError("runtime release entrypoint is not a declared member")
    return manifest


def verify_harness_runtime(harness: Path) -> dict[str, Any]:
    """Verify positive codebase-owned runtime packaging evidence for a harness."""

    root = harness.absolute()
    launcher = root / "launch.py"
    harness_lock_path = root / RUNTIME_LOCK_FILENAME
    active_lock_path = root / "runtime" / "active.json"
    missing = [
        path.name if path.parent == root else path.relative_to(root).as_posix()
        for path in (launcher, harness_lock_path, active_lock_path)
        if not path.is_file()
    ]
    if missing:
        raise RuntimeReleaseError(
            "codebase-owned runtime packaging is incomplete: " + ", ".join(missing)
        )
    link_boundaries = (
        root,
        root.parent,
        root.parent.parent,
        launcher,
        harness_lock_path,
        root / "runtime",
        root / "runtime" / "releases",
        active_lock_path,
    )
    if any(_is_unsafe_link(path) for path in link_boundaries):
        raise RuntimeReleaseError("codebase-owned runtime packaging uses an unsafe link")
    if launcher.stat().st_size == 0:
        raise RuntimeReleaseError("codebase-owned runtime launcher is empty")
    harness_lock = _read_lock(harness_lock_path, label="workflow runtime lock")
    active_lock = _read_lock(active_lock_path, label="active runtime lock")
    if not _lock_matches(harness_lock, active_lock):
        raise RuntimeReleaseError("workflow and active runtime locks do not match")
    runtime_id = str(harness_lock.get("runtime_id") or "")
    release_root = root / "runtime" / "releases" / runtime_id
    manifest_path = release_root / "runtime-manifest.json"
    if _is_unsafe_link(release_root) or _is_unsafe_link(manifest_path):
        raise RuntimeReleaseError("codebase-owned runtime release uses an unsafe link")
    manifest = verify_runtime_release(manifest_path)
    expected_lock = _runtime_lock(manifest)
    if not _lock_matches(harness_lock, expected_lock):
        raise RuntimeReleaseError("workflow runtime lock does not match its release")
    return manifest


def _read_lock(path: Path, *, label: str) -> dict[str, Any]:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeReleaseError(f"cannot read {label}: {path}: {exc}") from exc
    expected = {
        "schema",
        "runtime_name",
        "runtime_version",
        "runtime_id",
        "cli_abi",
        "entrypoint",
        "python_abi",
        "python_executable_digest",
        "dependencies",
        "payload_digest",
        "manifest_digest",
    }
    if not isinstance(lock, dict) or set(lock) != expected:
        raise RuntimeReleaseError(f"{label} has an invalid closed shape")
    if lock.get("schema") != RUNTIME_LOCK_SCHEMA:
        raise RuntimeReleaseError(f"{label} schema is invalid")
    return lock


def _lock_matches(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in _runtime_lock_keys())


def _runtime_lock_keys() -> tuple[str, ...]:
    return (
        "schema",
        "runtime_name",
        "runtime_version",
        "runtime_id",
        "cli_abi",
        "entrypoint",
        "python_abi",
        "python_executable_digest",
        "dependencies",
        "payload_digest",
        "manifest_digest",
    )


def verify_runtime_for_startup(manifest_path: Path) -> dict[str, Any]:
    """Verify before loading vendor/product code, then allow one same-process bind."""
    global _startup_verification
    _startup_verification = None
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise RuntimeReleaseError("runtime bootstrap requires isolated, no-site, no-bytecode Python")
    manifest = verify_runtime_release(manifest_path)
    _startup_verification = (os.getpid(), str(manifest_path.absolute()), deepcopy(manifest))
    return manifest


def _runtime_for_binding(manifest_path: Path) -> dict[str, Any]:
    global _startup_verification
    verified, _startup_verification = _startup_verification, None
    if verified is not None:
        pid, path, manifest = verified
        if pid == os.getpid() and path == str(manifest_path.absolute()):
            # Detect changed manifest identity between bootstrap and bind.
            current = json.loads(manifest_path.read_text(encoding="utf-8"))
            if current != manifest:
                raise RuntimeReleaseError("runtime manifest changed after bootstrap verification")
            return manifest
    return verify_runtime_release(manifest_path)


def bind_runtime_to_run(
    harness: Path,
    run_dir: Path,
    *,
    run_mode: str,
    executing_entrypoint: Path | None = None,
    product_distributions: Any = None,
    execution_mode: str = "packaged",
) -> dict[str, Any] | None:
    """Pin/verify the executing runtime before any milestone work starts."""

    harness_lock_path = harness.resolve() / RUNTIME_LOCK_FILENAME
    run_lock_path = run_dir.resolve() / RUNTIME_LOCK_FILENAME
    manifest_value = os.environ.get(RUNTIME_MANIFEST_ENV, "").strip()
    if execution_mode not in {"coordination", "packaged"}:
        raise RuntimeReleaseError("execution mode must be coordination or packaged")
    if execution_mode == "coordination":
        if manifest_value or harness_lock_path.exists() or run_lock_path.exists():
            raise RuntimeReleaseError(
                "a packaged workflow or run must use its existing codebase launcher; "
                "coordination cannot bypass a runtime pin"
            )
        return None
    if not manifest_value:
        if harness.resolve() == Path(__file__).resolve().parents[1]:
            return None
        if harness_lock_path.is_file() or run_lock_path.is_file():
            raise RuntimeReleaseError(
                "this workflow is pinned to a codebase-owned M8M runtime; invoke the "
                "built skill's scripts/m8m_run.py instead of the Builder runtime"
            )
        raise RuntimeReleaseError(
            "this product harness has no codebase-owned M8M runtime release; "
            "run the complete m8m-harness-builder workflow and invoke the built "
            "skill's scripts/m8m_run.py"
        )
    manifest_path = Path(manifest_value).resolve()
    manifest = _runtime_for_binding(manifest_path)
    release_root = manifest_path.parent.resolve()
    expected_runtime_module = release_root / "scripts" / "runtime_release.py"
    if Path(__file__).resolve() != expected_runtime_module.resolve():
        raise RuntimeReleaseError(
            "the runtime manifest does not describe the M8M runtime code that is "
            "actually executing; invoke the built skill's codebase launcher"
        )
    if executing_entrypoint is not None:
        entrypoint = Path(executing_entrypoint).resolve()
        scripts_root = (release_root / "scripts").resolve()
        if entrypoint.parent != scripts_root or entrypoint.name not in {
            "run_flow.py",
            "run_goal.py",
        }:
            raise RuntimeReleaseError(
                "the executing M8M entrypoint is outside the pinned codebase runtime release"
            )
        declared_members = {
            str(item.get("path") or "")
            for item in manifest.get("members", [])
            if isinstance(item, dict)
        }
        if f"scripts/{entrypoint.name}" not in declared_members:
            raise RuntimeReleaseError(
                "the executing M8M entrypoint is not declared by the pinned runtime release"
            )
    executing_lock = _runtime_lock(manifest)
    assert_product_distribution_lock(product_distributions, manifest["dependencies"])
    if run_mode == "resume":
        if not run_lock_path.is_file():
            raise RuntimeReleaseError("resume is missing its pinned M8M runtime lock")
        pinned = _read_lock(run_lock_path, label="run runtime lock")
        if not _lock_matches(pinned, executing_lock):
            raise RuntimeReleaseError(
                "resume attempted with a runtime different from the run's pinned M8M runtime"
            )
        return executing_lock
    if harness_lock_path.is_file():
        declared = _read_lock(harness_lock_path, label="workflow runtime lock")
        if not _lock_matches(declared, executing_lock):
            raise RuntimeReleaseError(
                "fresh execution runtime does not match the workflow's pinned M8M runtime"
            )
    if run_lock_path.is_file():
        existing = _read_lock(run_lock_path, label="run runtime lock")
        if not _lock_matches(existing, executing_lock):
            raise RuntimeReleaseError("fresh run already has a different pinned M8M runtime")
    else:
        _write_json(run_lock_path, executing_lock)
    return executing_lock


def _argument_value(argv: Sequence[str], names: Iterable[str]) -> str | None:
    accepted = tuple(names)
    for index, value in enumerate(argv):
        for name in accepted:
            if value == name and index + 1 < len(argv):
                return argv[index + 1]
            prefix = f"{name}="
            if value.startswith(prefix):
                return value[len(prefix) :]
    return None


def resolve_runtime_manifest(harness: Path, argv: Sequence[str]) -> Path:
    root = harness.resolve()
    run_value = _argument_value(argv, ("--run-dir", "--output", "--goal-dir"))
    lock: dict[str, Any]
    if run_value and (Path(run_value).resolve() / RUNTIME_LOCK_FILENAME).is_file():
        lock = _read_lock(
            Path(run_value).resolve() / RUNTIME_LOCK_FILENAME,
            label="run runtime lock",
        )
    else:
        lock = _read_lock(root / "runtime" / "active.json", label="active runtime lock")
    runtime_id = str(lock.get("runtime_id") or "")
    manifest = root / "runtime" / "releases" / runtime_id / "runtime-manifest.json"
    if not manifest.is_file():
        raise RuntimeReleaseError(
            f"pinned M8M runtime release is not installed in the codebase harness: {runtime_id}"
        )
    return manifest


__all__ = [
    "RUNTIME_CLI_ABI",
    "RUNTIME_LOCK_FILENAME",
    "RUNTIME_MANIFEST_ENV",
    "RUNTIME_NAME",
    "RUNTIME_RELEASE_SCHEMA",
    "RUNTIME_VERSION",
    "RuntimeReleaseError",
    "bind_runtime_to_run",
    "resolve_runtime_manifest",
    "stage_runtime_release",
    "verify_harness_runtime",
    "verify_runtime_release",
]
