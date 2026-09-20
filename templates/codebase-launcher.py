"""Launch this workflow through the runtime release owned by its codebase."""

import sys


# Re-exec before importing any non-builtin module. In isolated mode the
# workflow harness cannot shadow hashlib/json/subprocess ahead of verification.
if not sys.flags.isolated:
    _platform = __import__("nt" if "nt" in sys.builtin_module_names else "posix")
    _arguments = [sys.executable, "-I", "-B", __file__, *sys.argv[1:]]
    if hasattr(_platform, "spawnv"):
        # Windows spawnv joins tokens without quoting. Preserve each argument
        # using CRT rules before importing any potentially shadowed module.
        def _windows_argument(value):
            quoted, backslashes = '"', 0
            for character in value:
                if character == "\\":
                    backslashes += 1
                elif character == '"':
                    quoted += "\\" * (backslashes * 2 + 1) + '"'
                    backslashes = 0
                else:
                    quoted += "\\" * backslashes + character
                    backslashes = 0
            return quoted + "\\" * (backslashes * 2) + '"'

        raise SystemExit(_platform.spawnv(
            0, sys.executable, [_windows_argument(value) for value in _arguments]
        ))
    _platform.execv(sys.executable, _arguments)
    raise SystemExit("M8M isolated bootstrap exec unexpectedly returned")

import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath


HARNESS_ROOT = Path(__file__).absolute().parent
M8M_CODEBASE_DISPATCH_ABI = "m8m_codebase_dispatch_v2"
_LOCK_FIELDS = {
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
_MANIFEST_FIELDS = {
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


def _is_unsafe_link(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(is_junction and is_junction())
    except OSError:
        return True


def _argument_value(names: tuple[str, ...]) -> str | None:
    argv = sys.argv[1:]
    for index, value in enumerate(argv):
        for name in names:
            if value == name and index + 1 < len(argv):
                return argv[index + 1]
            prefix = f"{name}="
            if value.startswith(prefix):
                return value[len(prefix) :]
    return None


def _has_flag(names: tuple[str, ...]) -> bool:
    return any(
        value == name or value.startswith(f"{name}=")
        for value in sys.argv[1:]
        for name in names
    )


def _canonical_bytes(value: object) -> bytes:
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


def _safe_relative(value: str) -> str:
    if not value or "\\" in value:
        raise SystemExit(f"Unsafe codebase runtime member path: {value!r}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise SystemExit(f"Unsafe codebase runtime member path: {value!r}")
    return pure.as_posix()


def _runtime_lock(*, require_run_lock: bool) -> dict[str, object]:
    run_value = _argument_value(("--run-dir", "--output", "--goal-dir"))
    lock_path = (
        Path(run_value).resolve() / "m8m-runtime-lock.json"
        if run_value
        else HARNESS_ROOT / "runtime" / "active.json"
    )
    if run_value and not lock_path.is_file():
        if require_run_lock:
            raise SystemExit(
                "Pinned M8M continuation is missing the exact run-local runtime lock"
            )
        lock_path = HARNESS_ROOT / "runtime" / "active.json"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read the codebase's pinned M8M runtime: {exc}") from exc
    if not isinstance(lock, dict) or set(lock) != _LOCK_FIELDS:
        raise SystemExit("The codebase's pinned M8M runtime lock has an invalid closed shape")
    if lock.get("schema") != "m8m.runtime_lock.v1":
        raise SystemExit("The codebase's pinned M8M runtime lock schema is invalid")
    runtime_id = str(lock.get("runtime_id") or "")
    if len(runtime_id) != 64 or any(
        character not in "0123456789abcdef" for character in runtime_id
    ):
        raise SystemExit("The codebase's pinned M8M runtime id is invalid")
    return lock


def _verify_release(lock: dict[str, object]) -> tuple[dict[str, object], Path]:
    runtime_id = str(lock["runtime_id"])
    release_root = HARNESS_ROOT / "runtime" / "releases" / runtime_id
    manifest_path = release_root / "runtime-manifest.json"
    if any(
        _is_unsafe_link(path)
        for path in (
            HARNESS_ROOT / "runtime",
            HARNESS_ROOT / "runtime" / "releases",
            release_root,
            manifest_path,
            HARNESS_ROOT,
            HARNESS_ROOT.parent,
            HARNESS_ROOT.parent.parent,
        )
    ):
        raise SystemExit("The codebase's pinned M8M runtime uses an unsafe link")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read the codebase runtime manifest: {exc}") from exc
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        raise SystemExit("The codebase runtime manifest has an invalid closed shape")
    if (
        manifest.get("schema") != "m8m.runtime_release.v1"
        or manifest.get("runtime_name") != "m8m-runtime"
        or manifest.get("cli_abi") != "m8m_runtime_cli_v1"
    ):
        raise SystemExit("The codebase runtime manifest identity is invalid")
    members = manifest.get("members")
    if not isinstance(members, list) or not members:
        raise SystemExit("The codebase runtime manifest has no members")
    dependencies = manifest.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        raise SystemExit("The codebase runtime manifest has no dependency lock")
    normalized_dependencies: list[dict[str, object]] = []
    names: set[str] = set()
    vendor_paths: set[str] = set()
    for item in dependencies:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "version",
            "vendor_path",
            "file_count",
            "files_digest",
        }:
            raise SystemExit("The codebase runtime dependency shape is invalid")
        name = str(item.get("name") or "")
        version = str(item.get("version") or "")
        vendor_path = _safe_relative(str(item.get("vendor_path") or ""))
        file_count = item.get("file_count")
        files_digest = str(item.get("files_digest") or "")
        if (
            not name
            or not version
            or name in names
            or vendor_path in vendor_paths
            or len(PurePosixPath(vendor_path).parts) != 2
            or PurePosixPath(vendor_path).parts[0] != "vendor"
            or not isinstance(file_count, int)
            or isinstance(file_count, bool)
            or file_count < 1
            or len(files_digest) != 71
            or not files_digest.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in files_digest[7:])
        ):
            raise SystemExit("The codebase runtime dependency lock is invalid")
        names.add(name)
        vendor_paths.add(vendor_path)
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
    if python_abi != str(sys.implementation.cache_tag or ""):
        raise SystemExit("The codebase runtime Python ABI does not match this interpreter")
    try:
        python_executable = Path(sys.executable).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SystemExit("The codebase runtime Python executable cannot be resolved") from exc
    if not python_executable.is_file() or _is_unsafe_link(python_executable):
        raise SystemExit("The codebase runtime Python executable is unavailable or unsafe")
    python_executable_digest = "sha256:" + _sha256_file(python_executable)
    if manifest.get("python_executable_digest") != python_executable_digest:
        raise SystemExit(
            "The codebase runtime Python executable does not match its release"
        )
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in members:
        if not isinstance(item, dict) or set(item) != {"path", "byte_count", "digest"}:
            raise SystemExit("The codebase runtime manifest member shape is invalid")
        relative = _safe_relative(str(item.get("path") or ""))
        if relative in seen:
            raise SystemExit(f"Duplicate codebase runtime member: {relative}")
        seen.add(relative)
        member = release_root.joinpath(*PurePosixPath(relative).parts)
        # Verify the stdlib-only bootstrap module here. It verifies every
        # member in the isolated child before any vendor/entrypoint import.
        # Other member digests below bind the manifest identity, not a claim
        # that those bytes have already been checked.
        digest, byte_count = item.get("digest"), item.get("byte_count")
        if relative == "scripts/runtime_release.py":
            if _is_unsafe_link(member) or not member.is_file():
                raise SystemExit(f"Missing codebase runtime member: {relative}")
            digest = "sha256:" + _sha256_file(member)
            byte_count = member.stat().st_size
        if digest != item.get("digest") or byte_count != item.get("byte_count"):
            raise SystemExit(f"Codebase runtime member digest mismatch: {relative}")
        normalized.append(
            {"path": relative, "byte_count": byte_count, "digest": digest}
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
            raise SystemExit(
                f"The vendored runtime dependency identity is invalid: {dependency['name']}"
            )
        covered_vendor_members.update(str(item["path"]) for item in dependency_members)
    all_vendor_members = {
        str(item["path"])
        for item in normalized
        if str(item["path"]).startswith("vendor/")
    }
    if all_vendor_members != covered_vendor_members:
        raise SystemExit("The codebase runtime has unowned vendor files")
    # Closed inventory and all file/link checks run once in the child verifier.
    payload_digest = _digest_bytes(
        _canonical_bytes(
            {
                "schema": manifest["schema"],
                "runtime_name": manifest["runtime_name"],
                "runtime_version": manifest["runtime_version"],
                "cli_abi": manifest["cli_abi"],
                "entrypoint": manifest["entrypoint"],
                "python_abi": python_abi,
                "python_executable_digest": python_executable_digest,
                "dependencies": normalized_dependencies,
                "members": normalized,
            }
        )
    )
    if (
        manifest.get("runtime_id") != runtime_id
        or manifest.get("payload_digest") != payload_digest
        or lock.get("payload_digest") != payload_digest
        or lock.get("manifest_digest") != _digest_bytes(_canonical_bytes(manifest))
        or lock.get("runtime_name") != manifest.get("runtime_name")
        or lock.get("runtime_version") != manifest.get("runtime_version")
        or lock.get("cli_abi") != manifest.get("cli_abi")
        or lock.get("entrypoint") != manifest.get("entrypoint")
        or lock.get("python_abi") != python_abi
        or lock.get("python_executable_digest") != python_executable_digest
        or lock.get("dependencies") != normalized_dependencies
    ):
        raise SystemExit("The codebase runtime release identity does not match its pin")
    if str(manifest.get("entrypoint") or "") not in seen:
        raise SystemExit("The codebase runtime entrypoint is not a declared member")
    if "scripts/runtime_release.py" not in seen:
        raise SystemExit("The verified codebase runtime has no launcher module")
    return manifest, release_root


forwarded = list(sys.argv[1:])
if _has_flag(("--codebase", "--skill-dir", "--harness-dir", "--flow-id")):
    raise SystemExit(
        "The workflow-specific codebase launcher does not accept harness location overrides"
    )
goal_mode = any(
    value == flag or value.startswith(f"{flag}=")
    for value in forwarded
    for flag in ("--goal", "--goal-dir", "--abandon-row")
)
cache_prune_mode = bool(forwarded and forwarded[0] == "cache-prune")
resume_requested = (
    _has_flag(("--resume", "--replace-milestone", "--continue-after-edit"))
    or _argument_value(("--run-mode",)) == "resume"
)
if resume_requested and not _argument_value(("--run-dir", "--goal-dir")):
    raise SystemExit(
        "Pinned M8M resume requires the exact --run-dir or --goal-dir at the codebase launcher boundary"
    )
require_run_lock = resume_requested or _argument_value(("--goal-dir",)) is not None
manifest, release_root = _verify_release(
    _runtime_lock(require_run_lock=require_run_lock)
)
entrypoint_relative = (
    "scripts/m8m_cache.py"
    if cache_prune_mode
    else "scripts/run_goal.py"
    if goal_mode
    else str(manifest["entrypoint"])
)
declared = {str(item["path"]) for item in manifest["members"] if isinstance(item, dict)}
if entrypoint_relative not in declared:
    raise SystemExit(f"The requested codebase runtime entrypoint is not declared: {entrypoint_relative}")
entrypoint = release_root.joinpath(*PurePosixPath(entrypoint_relative).parts)
if len(HARNESS_ROOT.parents) < 3 or HARNESS_ROOT.parent.name != "flows":
    raise SystemExit("Codebase launcher is not under flowsteps/flows/<flow_id>")
if cache_prune_mode:
    forwarded[0] = "prune"
    forwarded.extend(["--flow-id", HARNESS_ROOT.name])
else:
    forwarded.extend(
        ["--codebase", str(HARNESS_ROOT.parents[2]), "--flow-id", HARNESS_ROOT.name]
    )
environment = dict(os.environ)
environment["M8M_RUNTIME_MANIFEST"] = str(release_root / "runtime-manifest.json")
environment["PYTHONDONTWRITEBYTECODE"] = "1"
vendor_roots = [
    release_root.joinpath(*PurePosixPath(str(item["vendor_path"])).parts)
    for item in manifest["dependencies"]
    if isinstance(item, dict)
]


def _native_import_path(path: Path) -> str:
    """Preserve verified vendor paths for Win32 extension loaders beyond MAX_PATH."""
    value = str(path)
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


isolated_runtime_bootstrap = (
    "import hashlib,importlib.util,pathlib,runpy,sys;"
    "entrypoint=sys.argv[1];"
    "vendor_count=int(sys.argv[2]);"
    "vendor_roots=sys.argv[3:3+vendor_count];"
    "sys.argv=[entrypoint,*sys.argv[3+vendor_count:]];"
    "release=pathlib.Path(entrypoint).parent.parent;"
    "verifier=release/'scripts'/'runtime_release.py';"
    # Recheck exactly the module loaded across the process boundary. The
    # expected hash is an argument from the pin-checked launcher, not an env flag.
    "expected=sys.argv.pop(1);"
    "expected_manifest=sys.argv.pop(1);"
    "actual=hashlib.sha256(verifier.read_bytes()).hexdigest();"
    "None if actual == expected else sys.exit('runtime verifier changed before isolated import');"
    "spec=importlib.util.spec_from_file_location('runtime_release',verifier);"
    "module=importlib.util.module_from_spec(spec);"
    "spec.loader.exec_module(module);"
    "verified=module.verify_runtime_for_startup(release/'runtime-manifest.json');"
    "None if module._digest_bytes(module._canonical_bytes(verified)) == expected_manifest else sys.exit('runtime manifest changed across bootstrap');"
    "sys.modules['runtime_release']=module;"
    "sys.path[:0]=[str(pathlib.Path(entrypoint).parent),*vendor_roots];"
    "runpy.run_path(entrypoint,run_name='__main__')"
)
completed = subprocess.run(
    [
        sys.executable,
        "-I",
        "-S",
        "-B",
        "-c",
        isolated_runtime_bootstrap,
        str(entrypoint),
        str(len(vendor_roots)),
        *(_native_import_path(path) for path in vendor_roots),
        next(str(item['digest'])[7:] for item in manifest['members'] if item['path'] == 'scripts/runtime_release.py'),
        "sha256:" + hashlib.sha256(_canonical_bytes(manifest)).hexdigest(),
        *forwarded,
    ],
    env=environment,
    check=False,
)
raise SystemExit(completed.returncode)
