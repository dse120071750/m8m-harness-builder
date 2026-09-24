"""Shared M8M v4 runtime: FlowSteps work inside chosen-output milestones."""

from __future__ import annotations

import ast
from contextlib import nullcontext
from copy import deepcopy
import hashlib
from functools import wraps
import importlib.util
import json
import os
import re
import secrets
import sys
import time
import tokenize
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from execution_identity import canonical_candidate_executor_ref



FLOW_SCHEMA = "flowstep_flow_v4"
FLOW_SCHEMA_V3 = "flowstep_flow_v3"
ENVELOPE_SCHEMA = "flowstep_output_v3"
ACTION_SCHEMA = "flow_sequence_action_v2"
MODELS = ("none", "completion", "image", "judge")
STEP_CLASSES = ("tool", "intelligence")
TOOL_ID_HINTS = (
    "fetch",
    "crop",
    "hash",
    "render",
    "package",
    "resize",
    "normalize",
    "query",
    "upload",
    "download",
    "parse",
    "letterbox",
)
INTEL_ID_HINTS = ("judge", "choose", "draft", "decide", "review", "caption")
MILESTONE_SUFFIXES = (
    "_ready",
    "_frozen",
    "_bound",
    "_rendered",
    "_packaged",
    "_decided",
    "_admitted",
    "_verified",
    "_checked",
    "_written",
    "_complete",
)
STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
VERSIONED_RUNTIME_REF_RE = re.compile(
    r"^[a-z][a-z0-9_.-]*@[0-9]+\.[0-9]+\.[0-9]+$"
)
OUTPUT_KINDS = ("json", "data", "file", "image", "video", "audio")
OUTPUT_CARDINALITIES = ("one", "many")
CACHE_SIDE_EFFECT_TOKENS = {
    "apply",
    "commit",
    "delete",
    "deploy",
    "install",
    "patch",
    "publish",
    "register",
    "remove",
    "rotate",
    "send",
    "update",
    "upload",
}
READ_ONLY_PATCH_TOKENS = {
    "build",
    "compose",
    "derive",
    "prepare",
}


def runtime_package_name(runtime_ref: str, *, label: str = "runtime ref") -> str:
    """Map an exact versioned runtime ref to its local package directory name."""

    value = str(runtime_ref or "").strip()
    if not VERSIONED_RUNTIME_REF_RE.fullmatch(value):
        raise FlowError(f"{label} must be an exact versioned ref")
    package = value.rsplit("@", 1)[0]
    if not STEP_ID_RE.fullmatch(package):
        raise FlowError(f"{label} does not resolve to a safe local runtime package")
    return package


def local_tool_package_name(runtime_ref: str, *, label: str = "tool ref") -> str:
    """Resolve one exact tool implementation ref to its local package name.

    ``flowstep.tool`` and ``execution.tool_bindings[*].ref`` keep the exact
    versioned identity used by native admission. Local filesystem lookup
    accepts both native refs and the immediately preceding ``tool.`` namespace
    so an in-flight frozen run remains resumable across the 3.1 cutover.
    """

    value = str(runtime_ref or "").strip()
    if not VERSIONED_RUNTIME_REF_RE.fullmatch(value):
        raise FlowError(f"{label} must be an exact versioned ref")
    package = value.rsplit("@", 1)[0].removeprefix("tool.")
    if not STEP_ID_RE.fullmatch(package):
        raise FlowError(f"{label} does not resolve to a safe local runtime package")
    return package


def cache_side_effect_risk(step: dict[str, Any]) -> str | None:
    """Return a conservative identifier when declared work may mutate external state."""
    identifiers = [str(step.get("id") or ""), Path(str(step.get("handler") or "")).stem]
    identifiers.extend(str(item) for item in (step.get("tools") or []) if item)
    for flowstep in step.get("flowsteps") or []:
        if isinstance(flowstep, str):
            identifiers.append(flowstep)
        elif isinstance(flowstep, dict):
            identifiers.extend(str(flowstep.get(key) or "") for key in ("id", "tool"))
    for identifier in identifiers:
        tokens = {item for item in re.split(r"[^a-z0-9]+", identifier.lower()) if item}
        risky_tokens = tokens & CACHE_SIDE_EFFECT_TOKENS
        # A patch *manifest* builder is deterministic preparation, not the
        # external patch operation. Keep the conservative gate for identifiers
        # that also name a real mutator (commit/apply/register/etc.).
        if risky_tokens == {"patch"} and tokens & READ_ONLY_PATCH_TOKENS:
            continue
        # The builder's install_toolbox step materializes local generated
        # source under the selected codebase; it is not a remote operation.
        if risky_tokens == {"install"} and tokens & {"skill", "toolbox"}:
            continue
        if risky_tokens:
            return identifier
    return None


def normalize_flowsteps(
    *,
    flowsteps: Any = None,
    tools: Any = None,
) -> tuple[list[dict[str, str]], list[str]]:
    """Guide sequence inside a milestone. Each FlowStep prefers at most one tool."""
    steps: list[dict[str, str]] = []
    if isinstance(flowsteps, list) and flowsteps:
        for index, raw in enumerate(flowsteps):
            if isinstance(raw, str) and raw.strip():
                fid = raw.strip()
                tool = fid
            elif isinstance(raw, dict):
                tool = str(raw.get("tool") or "").strip()
                fid = str(raw.get("id") or tool or f"step_{index + 1}").strip()
            else:
                continue
            if not fid:
                continue
            entry = {"id": fid}
            if tool:
                entry["tool"] = tool
            steps.append(entry)
    elif isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, str) and tool.strip():
                steps.append({"id": tool.strip(), "tool": tool.strip()})
    tool_ids: list[str] = []
    for item in steps:
        flowstep_id = item.get("id") or ""
        if flowstep_id and flowstep_id not in tool_ids:
            tool_ids.append(flowstep_id)
    return steps, tool_ids


def recovery_model(step: dict[str, Any]) -> str:
    model = str(step.get("model") or step.get("intelligence") or "none")
    if model in {None, "", "none"}:
        return "completion"
    return model
FLOW_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
FLOWSTEPS_DIRNAME = "flowsteps"
NEED_MODEL = "NEED_MODEL"
# Keep startup lexical. ``Path.resolve()`` performs filesystem probes for every
# path component on Windows and this module is imported by every flow command.
BUILDER_ROOT = Path(os.path.abspath(__file__)).parents[1]
CONTRACTS_DIR = BUILDER_ROOT / "contracts"


class FlowError(RuntimeError):
    """Fail-closed harness or tool error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise FlowError(f"UTF-8 required: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FlowError(f"invalid JSON: {path}: {exc}") from exc


def write_json(path: Path, value: Any, *, overwrite: bool = True) -> None:
    """Durably publish one JSON document without sharing a static temp path.

    A static ``<name>.tmp`` lets concurrent/resumed writers clobber each
    other's staging bytes. Immutable outputs additionally need an atomic
    create-if-absent operation; a pre-flight ``exists()`` check is racy.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    try:
        with temp.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if not overwrite:
            try:
                # Atomic create-if-absent on NTFS and POSIX filesystems.
                os.link(temp, path)
            except FileExistsError as exc:
                raise FlowError(f"immutable output already exists: {path}") from exc
            except OSError as exc:
                if os.name != "nt" or getattr(exc, "winerror", None) not in {1, 50}:
                    raise
                # Some Windows volumes reject hard links (ERROR_INVALID_FUNCTION
                # / ERROR_NOT_SUPPORTED). os.rename is still atomic there and,
                # on Windows, refuses to replace an existing destination.
                for attempt in range(20):
                    try:
                        os.rename(temp, path)
                        return
                    except FileExistsError as exists:
                        raise FlowError(
                            f"immutable output already exists: {path}"
                        ) from exists
                    except OSError as rename_error:
                        if (
                            getattr(rename_error, "winerror", None)
                            not in {5, 32, 1450}
                            or attempt == 19
                        ):
                            raise
                        time.sleep(0.05 * (attempt + 1))
            return
        for attempt in range(20):
            try:
                os.replace(temp, path)
                return
            except OSError as exc:
                # Windows can report ERROR_ACCESS_DENIED (5) for the same
                # brief antivirus/indexer handle race that otherwise appears
                # as sharing violation (32) or insufficient resources (1450).
                if getattr(exc, "winerror", None) not in {5, 32, 1450} or attempt == 19:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            # The destination is already durable. A scanner retaining the
            # unique temp handle must not invalidate the committed document.
            pass


def load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def step_class_hint(step_id: str) -> str | None:
    lowered = step_id.lower()
    # Outcome names (cards_rendered, release_packaged) are milestones.
    # Action names (render_html_shell, crop_4x5) are tools.
    if any(lowered.endswith(suffix) for suffix in MILESTONE_SUFFIXES):
        return None
    tokens = set(lowered.split("_"))
    if tokens & set(TOOL_ID_HINTS):
        return "tool"
    if tokens & set(INTEL_ID_HINTS):
        return "intelligence"
    if any(lowered.startswith(prefix) for prefix in ("if_", "loop_", "switch_", "when_", "else_")):
        return "tool"
    return None


HOME_SKILL_MARKERS = ("/.codex/skills/", "/.claude/skills/")
HOME_SKILL_SUFFIXES = ("/.codex/skills", "/.claude/skills")


def lexical_abs(path: Path | str) -> Path:
    """Absolute lexical path without filesystem realpath/stat traversal."""
    return Path(os.path.abspath(str(path)))


def is_under_home_skills(path: Path) -> bool:
    """True for Codex or Claude home/project skill folders. Product tools must not live there."""
    normalized = lexical_abs(path).as_posix().lower()
    text = f"/{normalized}/"
    if any(marker in text for marker in HOME_SKILL_MARKERS):
        return True
    stripped = normalized.rstrip("/")
    return any(stripped.endswith(suffix) for suffix in HOME_SKILL_SUFFIXES)


def is_under_codex_skills(path: Path) -> bool:
    return is_under_home_skills(path)


def is_builder_fixture(path: Path) -> bool:
    text = lexical_abs(path).as_posix().replace("\\", "/").lower()
    return (
        "flowstep-harness-builder/examples/" in text
        or "m8m-harness-builder/examples/" in text
    )


def assert_product_harness_location(path: Path) -> None:
    resolved = lexical_abs(path)
    if is_under_home_skills(resolved) and not is_builder_fixture(resolved):
        raise FlowError(
            "product tools must live in the codebase at flowsteps/<flow_id>, "
            "not under ~/.codex/skills or ~/.claude/skills"
        )


def resolve_harness_dir(
    *,
    codebase: Path | str | None = None,
    flow_id: str | None = None,
    skill_dir: Path | str | None = None,
    harness_dir: Path | str | None = None,
) -> Path:
    if harness_dir:
        return lexical_abs(harness_dir)
    if codebase:
        if not flow_id:
            raise FlowError("--flow-id is required with --codebase")
        if not FLOW_ID_RE.match(str(flow_id)):
            raise FlowError(f"invalid flow_id: {flow_id}")
        root = lexical_abs(codebase)
        if is_under_home_skills(root):
            raise FlowError("--codebase must be the repo root, not ~/.codex/skills or ~/.claude/skills")
        return lexical_abs(root / FLOWSTEPS_DIRNAME / "flows" / flow_id)
    if skill_dir:
        return lexical_abs(skill_dir)
    raise FlowError("pass --codebase and --flow-id, or --skill-dir for the builder fixture")


def add_harness_location_args(parser: Any) -> None:
    parser.add_argument("--codebase", type=Path, help="Repo root. Tools are written to <codebase>/flowsteps/<flow_id>.")
    parser.add_argument("--flow-id", dest="flow_id_flag")
    parser.add_argument("--skill-dir", type=Path, help="Harness dir. Fixture only; product work uses --codebase.")
    parser.add_argument("--harness-dir", type=Path)


def harness_dir_from_args(args: Any, *, require_existing: bool = False) -> Path:
    flow_id = getattr(args, "flow_id_flag", None) or getattr(args, "flow_id", None)
    path = resolve_harness_dir(
        codebase=getattr(args, "codebase", None),
        flow_id=flow_id,
        skill_dir=getattr(args, "skill_dir", None),
        harness_dir=getattr(args, "harness_dir", None),
    )
    if require_existing and not path.is_dir():
        raise FlowError(f"harness directory not found: {path}")
    return path


def find_flow_path(skill_dir: Path, flow: str | None = None) -> Path:
    root = skill_dir / "flow.yaml"
    if flow is None and root.is_file():
        return root
    flows = skill_dir / "flows"
    if flow:
        path = Path(flow)
        if not path.is_absolute():
            path = flows / flow if path.parent == Path(".") else skill_dir / path
        if not path.is_file():
            raise FlowError(f"flow not found: {path}")
        return path
    matches = sorted(flows.glob("*.yaml")) + sorted(flows.glob("*.yml"))
    if root.is_file():
        matches = [root, *matches]
    if len(matches) != 1:
        names = ", ".join(item.name for item in matches) or "(none)"
        raise FlowError(f"pass --flow; expected exactly one YAML in {skill_dir}: {names}")
    return matches[0]


def _require(mapping: dict[str, Any], keys: set[str], label: str) -> None:
    missing = sorted(keys - set(mapping))
    if missing:
        raise FlowError(f"{label} missing keys: {missing}")


def _normalize_cycle(raw: Any, *, step_id: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw:
        return None
    start = str(raw.get("start") or step_id).strip()
    if start and not STEP_ID_RE.match(start):
        raise FlowError(f"{step_id}: invalid cycle.start: {start}")
    join = str(raw.get("join") or "").strip() or None
    worker = str(raw.get("worker") or "cycle_receipt").strip()
    max_rounds = raw.get("max_rounds") if raw.get("max_rounds") is not None else raw.get("max_attempts") or 8
    if not isinstance(max_rounds, int) or max_rounds < 1:
        raise FlowError(f"{step_id}: cycle.max_rounds must be a positive integer")
    cid = str(raw.get("id") or raw.get("name") or "").strip()
    return {
        "id": cid or start or step_id,
        "worker": worker,
        "start": start,
        "join": join,
        "ledger": str(raw.get("ledger") or "").strip() or None,
        "pass": str(raw.get("pass") or "").strip() or None,
        "max_rounds": max_rounds,
        "receipt_schema": str(raw.get("receipt_schema") or f"schemas/{step_id}_cycle_v1.json"),
    }


def _normalize_branch(raw: Any, *, step_id: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw:
        return None
    paths_raw = raw.get("paths")
    if not isinstance(paths_raw, list) or len(paths_raw) < 2:
        raise FlowError(f"{step_id}: branch requires at least two paths")
    paths: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(paths_raw):
        if isinstance(item, str) and item.strip():
            pid = item.strip()
            then = pid
        elif isinstance(item, dict) and item.get("id"):
            pid = str(item["id"]).strip()
            then = str(item.get("then") or pid).strip()
        else:
            raise FlowError(f"{step_id}: branch path {index} needs id")
        if not STEP_ID_RE.match(pid):
            raise FlowError(f"{step_id}: invalid branch path id: {pid}")
        if pid in seen:
            raise FlowError(f"{step_id}: duplicate branch path {pid}")
        seen.add(pid)
        paths.append({"id": pid, "then": then})
    default = str(raw.get("default") or paths[0]["id"])
    if default not in seen:
        raise FlowError(f"{step_id}: branch.default {default} is not a path")
    join = str(raw.get("join") or "").strip()
    worker = str(raw.get("worker") or "branch_receipt").strip()
    return {
        "worker": worker,
        "default": default,
        "paths": paths,
        "join": join or None,
        "receipt_schema": str(raw.get("receipt_schema") or f"schemas/{step_id}_branch_v1.json"),
    }


def _normalize_outputs(raw: Any, *, step_id: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise FlowError(f"{step_id}.outputs must be a non-empty list")
    outputs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise FlowError(f"{step_id}.outputs[{index}] must be a mapping")
        _require(item, {"id", "name", "kind", "cardinality", "required"}, f"{step_id}.outputs[{index}]")
        output_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        kind = str(item.get("kind") or "").strip().lower()
        cardinality = str(item.get("cardinality") or "").strip().lower()
        required = item.get("required")
        if not STEP_ID_RE.match(output_id):
            raise FlowError(f"{step_id}.outputs[{index}].id is invalid: {output_id}")
        if output_id in seen:
            raise FlowError(f"{step_id}: duplicate output id {output_id}")
        if not name:
            raise FlowError(f"{step_id}.outputs[{index}].name must not be empty")
        if kind not in OUTPUT_KINDS:
            raise FlowError(f"{step_id}.outputs[{index}].kind must be one of {OUTPUT_KINDS}")
        if cardinality not in OUTPUT_CARDINALITIES:
            raise FlowError(
                f"{step_id}.outputs[{index}].cardinality must be one of {OUTPUT_CARDINALITIES}"
            )
        if not isinstance(required, bool):
            raise FlowError(f"{step_id}.outputs[{index}].required must be boolean")
        seen.add(output_id)
        outputs.append(
            {
                "id": output_id,
                "name": name,
                "kind": kind,
                "cardinality": cardinality,
                "required": required,
            }
        )
    if not any(item["required"] for item in outputs):
        raise FlowError(f"{step_id}.outputs must declare at least one required business output")
    return outputs


def _load_flow_v4(
    skill_dir: Path,
    path: Path,
    raw: dict[str, Any],
    *,
    allow_unbound_import: bool = False,
) -> dict[str, Any]:
    validate_against_schema(raw, flow_schema_path())
    if "runtime_distributions" in raw:
        from runtime_release import validate_product_distributions, RuntimeReleaseError
        try:
            validate_product_distributions(raw["runtime_distributions"])
        except RuntimeReleaseError as exc:
            raise FlowError(str(exc)) from exc
    if "codebase_import_roots" in raw:
        from project_imports import validate_roots, ProjectImportError
        try:
            validate_roots(raw["codebase_import_roots"])
        except ProjectImportError as exc:
            raise FlowError(str(exc)) from exc
    if raw.get("graph") is not None:
        raise FlowError(
            "flowstep_flow_v4 graph is contract-valid, but the Builder local runtime "
            "does not execute explicit DAGs; use the canonical M8M platform runtime "
            "or compile the graph to Builder branch/cycle controls"
        )
    _require(raw, {"schema", "flow_id", "version", "milestones"}, "flow")
    if not isinstance(raw["flow_id"], str) or not FLOW_ID_RE.match(raw["flow_id"]):
        raise FlowError(f"invalid flow_id: {raw.get('flow_id')}")
    if not isinstance(raw["version"], int) or raw["version"] < 1:
        raise FlowError("flow version must be a positive integer")
    if int(raw.get("max_run_repair_cycles") or 0) != 0:
        raise FlowError("max_run_repair_cycles is forbidden; a BLOCKED run stays BLOCKED")
    milestones = raw.get("milestones")
    if not isinstance(milestones, list) or not milestones:
        raise FlowError("flow must declare at least one milestone")
    raw.setdefault("max_run_seconds", 3600)
    raw.setdefault("artifact_root", "artifacts")
    raw.setdefault("context_policy", "isolated")
    steps: list[dict[str, Any]] = []
    ids: list[str] = []
    previous: dict[str, Any] | None = None
    for index, item in enumerate(milestones):
        if not isinstance(item, dict):
            raise FlowError(f"milestone {index} must be a mapping")
        _require(
            item,
            {"id", "success", "output_contract", "output_schema", "outputs"},
            f"milestone {index}",
        )
        step_id = item["id"]
        if not isinstance(step_id, str) or not STEP_ID_RE.match(step_id):
            raise FlowError(f"invalid milestone id: {step_id}")
        if step_id in ids:
            raise FlowError(f"duplicate milestone id: {step_id}")
        ids.append(step_id)
        success = str(item.get("success") or "").strip()
        if not success:
            raise FlowError(f"{step_id}.success must not be empty")
        outputs = _normalize_outputs(item.get("outputs"), step_id=step_id)
        intel = item.get("intelligence") or "none"
        if intel not in ("none", *MODELS[1:]):
            raise FlowError(f"{step_id}.intelligence must be none|completion|image|judge")
        flowsteps, tools = normalize_flowsteps(flowsteps=item.get("flowsteps"), tools=item.get("tools"))
        if item.get("tools") is not None and list(item.get("tools") or []) != tools:
            raise FlowError(
                f"{step_id}.tools must equal first-use FlowStep id order"
            )
        on_tool_fail = str(
            item.get("on_tool_fail")
            or ("need_model" if intel != "none" else "BLOCKED")
        )
        if on_tool_fail not in {"BLOCKED", "need_model", "retryable"}:
            raise FlowError(
                f"{step_id}.on_tool_fail must be BLOCKED, retryable, or need_model"
            )
        max_model_attempts = item.get("max_model_attempts")
        if max_model_attempts is None:
            max_model_attempts = (item.get("params") or {}).get("max_model_attempts")
        if max_model_attempts is None:
            max_model_attempts = 8
        if not isinstance(max_model_attempts, int) or max_model_attempts < 1:
            raise FlowError(f"{step_id}.max_model_attempts must be a positive integer")
        max_tool_attempts = item.get("max_tool_attempts")
        if max_tool_attempts is None:
            max_tool_attempts = (item.get("params") or {}).get("max_tool_attempts")
        if max_tool_attempts is None:
            max_tool_attempts = 3
        if not isinstance(max_tool_attempts, int) or max_tool_attempts < 1:
            raise FlowError(f"{step_id}.max_tool_attempts must be a positive integer")
        if intel != "none" and not str(item.get("model_justification") or "").strip():
            item["model_justification"] = "optional; writer sketch"
        item.setdefault("draft_schema", f"milestones/{step_id}/draft.schema.json")
        item.setdefault("handler", f"milestones/{step_id}/assemble.py")
        inputs = item.get("inputs")
        if not inputs:
            if previous is None:
                inputs = {"request": "user.request"}
            else:
                inputs = {previous["id"]: f"{previous['id']}.{previous['output_contract']}"}
        loop = str(item.get("loop") or "none").strip().lower()
        if loop in {"foreach", "for"}:
            loop = "for"
        if loop in {"if", "judge"}:
            loop = "judge"
        if loop == "branch":
            raise FlowError(f"{step_id}: branch is after the milestone, not loop=branch")
        if loop == "cycle":
            raise FlowError(f"{step_id}: cycle wraps milestones, not loop=cycle")
        if loop not in {"none", "for", "judge"}:
            raise FlowError(f"{step_id}.loop must be none|for|judge")
        ledger = item.get("ledger") if isinstance(item.get("ledger"), dict) else None
        foreach = item.get("foreach") if isinstance(item.get("foreach"), dict) else None
        if loop == "for" and ledger is None and foreach:
            ledger = {
                "path": foreach.get("path") or "items",
                "item_schema": foreach.get("item_schema") or f"schemas/{step_id}_item_v1.json",
                "max_items": foreach.get("max_items") or 8,
            }
        if loop == "for":
            if not isinstance(ledger, dict):
                raise FlowError(f"{step_id}: loop=for requires ledger")
            ledger = dict(ledger)
            ledger.setdefault("path", "items")
            ledger.setdefault("item_schema", f"schemas/{step_id}_item_v1.json")
            ledger.setdefault("max_items", 8)
            if not isinstance(ledger.get("max_items"), int) or ledger["max_items"] < 1:
                raise FlowError(f"{step_id}: ledger.max_items must be a positive integer")
        else:
            ledger = None
        worker = str(item.get("worker") or "").strip()
        judge_abi = str(item.get("judge_abi") or "").strip() or None
        if judge_abi and loop != "judge":
            raise FlowError(f"{step_id}: judge_abi is valid only for loop=judge")
        if judge_abi and not worker:
            raise FlowError(f"{step_id}: strict judge ABI requires an explicit worker")
        if judge_abi and not str(item.get("receipt_schema") or "").strip():
            raise FlowError(f"{step_id}: strict judge ABI requires an explicit receipt_schema")
        if loop == "judge" and judge_abi != "m8m_milestone_judge_v1":
            raise FlowError(
                f"{step_id}: semantic judge loops require judge_abi "
                "m8m_milestone_judge_v1 and a separate typed judge"
            )
        execution = item.get("execution")
        unbound_import = not isinstance(execution, dict) and allow_unbound_import
        cutover = "; regenerate with m8m-harness-builder 3.1"
        if not isinstance(execution, dict):
            if not unbound_import:
                raise FlowError(
                    f"{step_id}: missing closed execution binding{cutover}"
                )
            execution = {
                "candidate_executor": {
                    "ref": f"legacy_import.{step_id}@0.0.0"
                },
                "tool_bindings": [
                    {
                        "tool": flowstep["id"],
                        "ref": (
                            flowstep.get("tool")
                            if VERSIONED_RUNTIME_REF_RE.fullmatch(
                                str(flowstep.get("tool") or "")
                            )
                            else f"legacy_import.{flowstep['id']}@0.0.0"
                        ),
                    }
                    for flowstep in flowsteps
                ],
            }
            if loop == "judge":
                execution["judge"] = {
                    "ref": worker
                    if VERSIONED_RUNTIME_REF_RE.fullmatch(worker)
                    else f"{worker or step_id + '_judge'}@0.0.0"
                }
        candidate_executor = execution.get("candidate_executor")
        if not isinstance(candidate_executor, dict) or not str(
            candidate_executor.get("ref") or ""
        ).strip():
            raise FlowError(
                f"{step_id}: execution.candidate_executor.ref is required{cutover}"
            )
        candidate_ref = str(candidate_executor.get("ref") or "")
        expected_candidate_ref = canonical_candidate_executor_ref(
            str(raw["flow_id"]), step_id
        )
        if not unbound_import and candidate_ref != expected_candidate_ref:
            raise FlowError(
                f"{step_id}: execution.candidate_executor.ref must be "
                f"{expected_candidate_ref}; regenerate with m8m-harness-builder 3.1"
            )
        bindings = execution.get("tool_bindings")
        if not isinstance(bindings, list):
            raise FlowError(
                f"{step_id}: execution.tool_bindings must be an exact list{cutover}"
            )
        binding_ids = [
            str(binding.get("tool") or "")
            for binding in bindings
            if isinstance(binding, dict)
        ]
        if len(binding_ids) != len(bindings) or len(set(binding_ids)) != len(binding_ids):
            raise FlowError(
                f"{step_id}: execution.tool_bindings must bind each FlowStep tool once{cutover}"
            )
        if binding_ids != tools:
            missing = sorted(set(tools) - set(binding_ids))
            unknown = sorted(set(binding_ids) - set(tools))
            raise FlowError(
                f"{step_id}: execution.tool_bindings differ from FlowStep ids; "
                f"missing={missing}, unknown={unknown}{cutover}"
            )
        flowstep_tools = {
            str(flowstep["id"]): str(flowstep.get("tool") or "")
            for flowstep in flowsteps
        }
        for binding in bindings:
            flowstep_id = str(binding["tool"])
            tool_ref = str(binding.get("ref") or "")
            expected_ref = flowstep_tools[flowstep_id]
            if not unbound_import and tool_ref != expected_ref:
                raise FlowError(
                    f"{step_id}: execution.tool_bindings[{flowstep_id}].ref must "
                    f"exactly equal flowstep.tool {expected_ref!r}{cutover}"
                )
            if not unbound_import:
                local_tool_package_name(
                    tool_ref,
                    label=f"{step_id}.execution.tool_bindings[{flowstep_id}].ref",
                )
        judge_binding = execution.get("judge")
        if loop == "judge":
            if not isinstance(judge_binding, dict) or not str(
                judge_binding.get("ref") or ""
            ).strip():
                raise FlowError(
                    f"{step_id}: loop=judge requires execution.judge.ref{cutover}"
                )
            judge_ref = str(judge_binding["ref"])
            if not unbound_import and worker != judge_ref:
                raise FlowError(
                    f"{step_id}: worker must exactly equal execution.judge.ref{cutover}"
                )
            if not unbound_import:
                runtime_package_name(
                    judge_ref,
                    label=f"{step_id}.execution.judge.ref",
                )
            tool_refs = {
                str(binding.get("ref") or "")
                for binding in bindings
                if isinstance(binding, dict)
            }
            if judge_ref == candidate_ref or judge_ref in tool_refs:
                raise FlowError(
                    f"{step_id}: execution.judge.ref must be distinct from candidate and tool refs{cutover}"
                )
        elif judge_binding is not None:
            raise FlowError(
                f"{step_id}: execution.judge is valid only for loop=judge{cutover}"
            )
        branch = _normalize_branch(item.get("branch"), step_id=step_id)
        on_path = str(item.get("on_path") or "").strip()
        on_cycle = str(item.get("on_cycle") or "").strip()
        cycle = _normalize_cycle(item.get("cycle"), step_id=step_id)
        if branch and cycle:
            raise FlowError(
                f"{step_id}: one milestone cannot own both branch and cycle control"
            )
        control_spec = branch or cycle
        control_kind = "branch" if branch else "cycle" if cycle else ""
        control_worker_ref: str | None = None
        if control_spec:
            control_worker = str(control_spec.get("worker") or "").strip()
            if not STEP_ID_RE.fullmatch(control_worker):
                raise FlowError(
                    f"{step_id}.{control_kind}.worker must name one milestone-local "
                    "FlowStep id"
                )
            control_bindings = [
                binding
                for binding in bindings
                if isinstance(binding, dict)
                and str(binding.get("tool") or "") == control_worker
            ]
            if len(control_bindings) != 1:
                raise FlowError(
                    f"{step_id}.{control_kind}.worker {control_worker} must have "
                    "one exact execution.tool_bindings entry"
                )
            control_worker_ref = str(control_bindings[0].get("ref") or "")
            if not unbound_import:
                local_tool_package_name(
                    control_worker_ref,
                    label=(
                        f"{step_id}.execution.tool_bindings[{control_worker}].ref"
                    ),
                )
            control_spec["worker"] = control_worker
        side_effects = str(item.get("side_effects") or "none").strip().lower()
        if side_effects not in {"none", "external"}:
            raise FlowError(f"{step_id}.side_effects must be none or external")
        phase_journal = dict(item["phase_journal"]) if isinstance(item.get("phase_journal"), dict) else None
        declared_side_effect_risk = cache_side_effect_risk(item)
        if declared_side_effect_risk and side_effects != "external":
            raise FlowError(
                f"{step_id}: {declared_side_effect_risk} may have external side effects; "
                "declare side_effects: external and a phase_journal"
            )
        if side_effects == "external":
            required_journal = {"path", "operator_result_path", "resume"}
            if not isinstance(phase_journal, dict) or not required_journal.issubset(phase_journal):
                raise FlowError(
                    f"{step_id}: external side effects require phase_journal with "
                    "path, operator_result_path, and resume"
                )
            if any(not str(phase_journal.get(key) or "").strip() for key in ("path", "operator_result_path")):
                raise FlowError(f"{step_id}: phase_journal paths must not be empty")
            if phase_journal.get("resume") != "query_exact_operation":
                raise FlowError(f"{step_id}: phase_journal.resume must be query_exact_operation")
        elif phase_journal is not None:
            raise FlowError(f"{step_id}: phase_journal is valid only when side_effects is external")
        cache = dict(item["cache"]) if isinstance(item.get("cache"), dict) else None
        if cache and (branch or cycle or loop == "for"):
            raise FlowError(f"{step_id}: branch/cycle control milestones cannot use cross-run cache")
        wait_tokens = {"response", "reply", "confirm", "wait"}
        if cache and set(step_id.lower().replace("-", "_").split("_")) & wait_tokens:
            raise FlowError(f"{step_id}: wait milestones cannot use cross-run cache")
        side_effect_risk = cache_side_effect_risk(item) if cache else None
        if side_effect_risk:
            raise FlowError(
                f"{step_id}: cache is unsafe because {side_effect_risk} may have external side effects"
            )
        if loop == "for" and cycle is None:
            cycle = {
                "worker": worker or "cycle_receipt",
                "start": step_id,
                "join": None,
                "max_rounds": int(item.get("max_attempts") or max_model_attempts or 8),
                "ledger": previous["id"] if previous else None,
                "pass": "legacy loop:for; rewrite as cycle over a frozen ledger",
                "receipt_schema": str(item.get("receipt_schema") or f"schemas/{step_id}_cycle_v1.json"),
                "id": on_cycle or step_id,
            }
            on_cycle = on_cycle or step_id
            loop = "none"
            ledger = None
        if loop == "for" and not worker:
            worker = "ledger_receipt"
        if control_spec and loop != "judge":
            # Preserve the legacy display field while execution uses the exact
            # bound control ref captured above. A semantic judge, when present,
            # continues to own step.worker independently.
            worker = str(control_spec.get("worker") or "")
        if judge_abi and worker in tools:
            raise FlowError(
                f"{step_id}: strict judge worker {worker} must not be a candidate FlowStep tool"
            )
        if control_spec and str(control_spec.get("worker") or "") not in tools:
            raise FlowError(
                f"{step_id}.{control_kind}.worker must be separately declared and "
                "exactly bound; the runtime will not synthesize a candidate FlowStep"
            )
        receipt_schema = item.get("receipt_schema")
        if loop in {"for", "judge"}:
            receipt_schema = receipt_schema or f"schemas/{step_id}_receipt_v1.json"
        if branch:
            receipt_schema = receipt_schema or str(branch.get("receipt_schema") or f"schemas/{step_id}_branch_v1.json")
            branch["receipt_schema"] = receipt_schema
            branch["worker"] = str(branch.get("worker") or "branch_receipt")
        if cycle:
            receipt_schema = receipt_schema or str(cycle.get("receipt_schema") or f"schemas/{step_id}_cycle_v1.json")
            cycle["receipt_schema"] = receipt_schema
            cycle["worker"] = str(cycle.get("worker") or "cycle_receipt")
        max_attempts = item.get("max_attempts")
        if max_attempts is None and loop != "judge":
            max_attempts = max_model_attempts
        if max_attempts is None:
            if loop != "judge":
                raise FlowError(f"{step_id}.max_attempts must be a positive integer")
        elif not isinstance(max_attempts, int) or max_attempts < 1:
            raise FlowError(f"{step_id}.max_attempts must be a positive integer")
        primary_output = outputs[0]
        asset = item.get("asset") if isinstance(item.get("asset"), dict) else {}
        asset_kind = str(asset.get("kind") or primary_output["kind"]).strip().lower()
        step = {
            "id": step_id,
            "kind": "milestone",
            "class": "tool" if intel == "none" else "intelligence",
            "handler": item["handler"],
            "implementation_dependencies": list(item.get("implementation_dependencies") or []),
            "model": "none" if intel == "none" else intel,
            "intelligence": intel,
            "tools": list(tools),
            "flowsteps": flowsteps,
            "inputs": inputs,
            "output_contract": item["output_contract"],
            "output_schema": item["output_schema"],
            "test": item.get("test", f"milestones/{step_id}/tests/test_assemble.py"),
            "params": item.get("params") or {},
            "on_tool_fail": on_tool_fail,
            "max_model_attempts": max_model_attempts,
            "max_tool_attempts": max_tool_attempts,
            "max_attempts": max_attempts,
            "loop": loop,
            "ledger": ledger,
            "worker": worker or None,
            "judge_abi": judge_abi,
            "receipt_schema": receipt_schema,
            "foreach": foreach,
            "branch": branch,
            "on_path": on_path or None,
            "cycle": cycle,
            "on_cycle": on_cycle or None,
            "success": success,
            "gem": str(item.get("gem") or "").strip() or None,
            "next": [],
            "else": None,
            "join": None,
            "asset": {"kind": asset_kind} if asset_kind else dict(asset),
            "outputs": outputs,
            "cache": cache,
            "side_effects": side_effects,
            "phase_journal": phase_journal,
        }
        if intel != "none" or on_tool_fail == "need_model":
            step["draft_schema"] = item.get("draft_schema") or f"milestones/{step_id}/draft.schema.json"
        if intel != "none":
            step["model_justification"] = item.get("model_justification")
        if not unbound_import:
            step["execution"] = deepcopy(execution)
        if control_worker_ref:
            # Execution-only projection. It is derived from the canonical exact
            # binding and is never an authored flow field.
            step["_control_worker_ref"] = control_worker_ref
        steps.append(step)
        previous = step
    raw["steps"] = steps
    raw["_skill_dir"] = skill_dir
    raw["_flow_path"] = path
    raw["_v4"] = True
    return raw


def load_flow(
    skill_dir: Path,
    flow_path: Path | None = None,
    *,
    allow_unbound_import: bool = False,
) -> dict[str, Any]:
    skill_dir = lexical_abs(skill_dir)
    path = flow_path or find_flow_path(skill_dir)
    raw = load_yaml(path)
    if not isinstance(raw, dict):
        raise FlowError(f"flow must be a mapping: {path}")
    schema = raw.get("schema")
    if schema in {"flowstep_flow_v1", "flowstep_flow_v2", "flowstep_flow_v3"}:
        raise FlowError(
            f"{schema} is rejected; regenerate with m8m-harness-builder 3.1 "
            f"to produce {FLOW_SCHEMA} chosen-output milestones"
        )
    if schema == FLOW_SCHEMA:
        return _load_flow_v4(
            skill_dir,
            path,
            raw,
            allow_unbound_import=allow_unbound_import,
        )
    raise FlowError(f"flow schema must be {FLOW_SCHEMA}; regenerate with m8m-harness-builder 3.1")


def skill_rel(skill_dir: Path, value: str | Path) -> Path:
    path = Path(value)
    root = Path(os.path.abspath(str(skill_dir)))
    resolved = Path(os.path.abspath(str(path if path.is_absolute() else root / path)))
    if resolved != root and root not in resolved.parents:
        raise FlowError(f"path escapes skill directory: {value}")
    return resolved


def run_rel(run_dir: Path, value: str | Path) -> Path:
    path = Path(value)
    root = Path(os.path.abspath(str(run_dir)))
    resolved = Path(os.path.abspath(str(path if path.is_absolute() else root / path)))
    if resolved != root and root not in resolved.parents:
        raise FlowError(f"path escapes run directory: {value}")
    return resolved


def assert_external_phase_journal(run_dir: Path, step: dict[str, Any]) -> None:
    """Fail closed before choosing output from a mutating milestone.

    The writer owns the journal contents. The runtime owns the release gate:
    the exact operation must have been queried, its operator result persisted,
    and live readback verified before the milestone can become chosen.
    """

    if str(step.get("side_effects") or "none") != "external":
        return
    spec = step.get("phase_journal") if isinstance(step.get("phase_journal"), dict) else {}
    journal_path = run_rel(run_dir, str(spec.get("path") or ""))
    operator_path = run_rel(run_dir, str(spec.get("operator_result_path") or ""))
    if not journal_path.is_file():
        raise FlowError(f"{step['id']}: external phase journal is missing: {journal_path}")
    journal = read_json(journal_path)
    if not isinstance(journal, dict) or journal.get("schema") != "m8m_external_phase_journal_v1":
        raise FlowError(f"{step['id']}: external phase journal schema is invalid")
    if str(journal.get("milestone_id") or "") != str(step["id"]):
        raise FlowError(f"{step['id']}: external phase journal milestone binding drifted")
    operation_id = str(journal.get("operation_id") or "")
    plan_sha256 = str(journal.get("plan_sha256") or "")
    if not operation_id or not re.fullmatch(r"[0-9a-f]{64}", plan_sha256):
        raise FlowError(f"{step['id']}: phase journal lacks the durable operation ID or plan hash")
    if journal.get("exact_operation_queried") is not True:
        raise FlowError(f"{step['id']}: exact operation was not queried before commit/readback")
    phases = [
        str(item.get("phase") or "")
        for item in (journal.get("phases") or [])
        if isinstance(item, dict)
    ]
    required = ("plan_frozen", "operation_queried", "operator_persisted", "readback_verified")
    positions: list[int] = []
    for phase in required:
        if phase not in phases:
            raise FlowError(f"{step['id']}: phase journal is missing {phase}")
        positions.append(phases.index(phase))
    if positions != sorted(positions):
        raise FlowError(f"{step['id']}: phase journal order is invalid")
    if journal.get("status") != "readback_verified":
        raise FlowError(f"{step['id']}: live readback has not been verified")
    if not operator_path.is_file():
        raise FlowError(f"{step['id']}: operator result is missing: {operator_path}")
    operator = read_json(operator_path)
    if (
        not isinstance(operator, dict)
        or operator.get("status") != "completed"
        or str(operator.get("operation_id") or "") != operation_id
        or str(operator.get("plan_sha256") or "") != plan_sha256
    ):
        raise FlowError(f"{step['id']}: persisted operator result does not match the frozen operation")
    readback_path = run_rel(run_dir, str(journal.get("readback_path") or ""))
    if not readback_path.is_file():
        raise FlowError(f"{step['id']}: live readback artifact is missing")
    readback = read_json(readback_path)
    if (
        not isinstance(readback, dict)
        or readback.get("status") != "PASS"
        or str(readback.get("operation_id") or "") != operation_id
    ):
        raise FlowError(f"{step['id']}: live readback does not match the frozen operation")


def relative_to(root: Path, path: Path) -> str:
    normalized_root = Path(os.path.abspath(str(root)))
    normalized_path = Path(os.path.abspath(str(path)))
    return normalized_path.relative_to(normalized_root).as_posix()


def _is_unsafe_implementation_link(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(is_junction and is_junction())
    except OSError:
        return True


def _assert_safe_implementation_chain(base: Path, target: Path, *, label: str) -> None:
    try:
        relative = target.relative_to(base)
    except ValueError as exc:
        raise FlowError(f"{label} escapes its implementation root: {target}") from exc
    cursor = base
    if _is_unsafe_implementation_link(cursor):
        raise FlowError(f"{label} uses an unsafe link: {cursor}")
    for part in relative.parts:
        cursor = cursor / part
        if _is_unsafe_implementation_link(cursor):
            raise FlowError(f"{label} uses an unsafe link: {cursor}")


_DYNAMIC_MODULE_IMPORTS = {
    "__import__",
    "importlib.import_module",
    "runpy.run_module",
}
_DYNAMIC_FILE_IMPORT_ARGUMENT = {
    "importlib.util.spec_from_file_location": 1,
    "importlib.machinery.SourceFileLoader": 1,
    "importlib.machinery.SourcelessFileLoader": 1,
    "runpy.run_path": 0,
}
_TRUSTED_RUNTIME_DYNAMIC_LOADERS = {"flowstep_runtime.py", "flowstep_tools.py", "project_imports.py"}


def _implementation_project(skill_dir: Path) -> tuple[Path, Path]:
    root = Path(os.path.abspath(str(skill_dir)))
    project = (
        root.parent.parent.parent
        if root.parent.name == "flows" and root.parent.parent.name == "flowsteps"
        else root
    )
    return root, project


def _is_within(path: Path, root: Path) -> bool:
    try:
        Path(os.path.abspath(str(path))).relative_to(root)
    except ValueError:
        return False
    return True


def _python_tree(path: Path) -> ast.AST:
    try:
        with tokenize.open(path) as source_file:
            source = source_file.read()
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise FlowError(f"cannot inspect Python implementation {path}: {exc}") from exc
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise FlowError(f"Python implementation is not parseable: {path}: {exc}") from exc


def _call_name(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        owner = _call_name(node.value, aliases)
        return f"{owner}.{node.attr}" if owner else node.attr
    return ""


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                local = item.asname or item.name.split(".", 1)[0]
                aliases[local] = item.name if item.asname else local
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                if item.name == "*":
                    continue
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def _static_strings(tree: ast.AST) -> dict[str, str]:
    values: dict[str, str] = {}
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    for _ in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            value_node = node.value
            value: str | None = None
            if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
                value = value_node.value
            elif isinstance(value_node, ast.Name):
                value = values.get(value_node.id)
            elif isinstance(value_node, ast.BinOp) and isinstance(value_node.op, ast.Add):
                left = (
                    value_node.left.value
                    if isinstance(value_node.left, ast.Constant)
                    and isinstance(value_node.left.value, str)
                    else values.get(value_node.left.id)
                    if isinstance(value_node.left, ast.Name)
                    else None
                )
                right = (
                    value_node.right.value
                    if isinstance(value_node.right, ast.Constant)
                    and isinstance(value_node.right.value, str)
                    else values.get(value_node.right.id)
                    if isinstance(value_node.right, ast.Name)
                    else None
                )
                if left is not None and right is not None:
                    value = left + right
            if value is None:
                continue
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
            )
            for target in targets:
                if isinstance(target, ast.Name) and values.get(target.id) != value:
                    values[target.id] = value
                    changed = True
        if not changed:
            break
    return values


def _static_string(node: ast.AST | None, values: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return values.get(node.id)
    return None


def _static_string_options(node: ast.AST | None, values: dict[str, str]) -> set[str]:
    value = _static_string(node, values)
    if value is not None:
        return {value}
    if isinstance(node, ast.IfExp):
        return {
            *_static_string_options(node.body, values),
            *_static_string_options(node.orelse, values),
        }
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string_options(node.left, values)
        right = _static_string_options(node.right, values)
        return {prefix + suffix for prefix in left for suffix in right}
    return set()


def _static_path_value(
    node: ast.AST | None,
    *,
    source: Path,
    aliases: dict[str, str],
    values: dict[str, Path],
    strings: dict[str, str],
) -> Path | None:
    if isinstance(node, ast.Name):
        if node.id == "__file__":
            return source
        return values.get(node.id)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return Path(node.value)
    if isinstance(node, ast.Call):
        name = _call_name(node.func, aliases)
        if name in {"Path", "pathlib.Path"} and node.args:
            return _static_path_value(
                node.args[0],
                source=source,
                aliases=aliases,
                values=values,
                strings=strings,
            )
        if name in {"str", "os.fspath"} and node.args:
            return _static_path_value(
                node.args[0],
                source=source,
                aliases=aliases,
                values=values,
                strings=strings,
            )
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "absolute",
            "resolve",
        }:
            return _static_path_value(
                node.func.value,
                source=source,
                aliases=aliases,
                values=values,
                strings=strings,
            )
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        owner = _static_path_value(
            node.value,
            source=source,
            aliases=aliases,
            values=values,
            strings=strings,
        )
        return owner.parent if owner is not None else None
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
    ):
        owner = _static_path_value(
            node.value.value,
            source=source,
            aliases=aliases,
            values=values,
            strings=strings,
        )
        index = node.slice.value if isinstance(node.slice, ast.Constant) else None
        if owner is not None and isinstance(index, int) and index >= 0:
            try:
                return owner.parents[index]
            except IndexError:
                return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        owner = _static_path_value(
            node.left,
            source=source,
            aliases=aliases,
            values=values,
            strings=strings,
        )
        child = _static_string(node.right, strings)
        if owner is not None and child is not None:
            return owner / child
    return None


def _static_paths(
    tree: ast.AST,
    *,
    source: Path,
    aliases: dict[str, str],
    strings: dict[str, str],
) -> dict[str, Path]:
    values: dict[str, Path] = {}
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    for _ in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            value = _static_path_value(
                node.value,
                source=source,
                aliases=aliases,
                values=values,
                strings=strings,
            )
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and values.get(target.id) != value:
                    values[target.id] = value
                    changed = True
        if not changed:
            break
    return values


def _package_files(base: Path, parts: list[str], *, project: Path) -> list[Path]:
    if not parts or any(not part or part in {".", ".."} for part in parts):
        return []
    files: list[Path] = []
    cursor = base
    for part in parts:
        cursor = cursor / part
        init = cursor / "__init__.py"
        if init.is_file() and _is_within(init, project):
            files.append(Path(os.path.abspath(str(init))))
    module = base.joinpath(*parts).with_suffix(".py")
    if module.is_file() and _is_within(module, project):
        files.append(Path(os.path.abspath(str(module))))
    return files


def _local_module_files(
    project: Path,
    source: Path,
    module: str,
    *,
    level: int = 0,
    imported_names: list[str] | None = None,
    search_roots: list[Path] | None = None,
) -> list[Path]:
    parts = [part for part in module.split(".") if part]
    bases: list[Path] = []
    if level:
        base = source.parent
        for _ in range(max(0, level - 1)):
            base = base.parent
        bases.append(base)
    else:
        # Handlers and bound tools are loaded through importlib specs. Python
        # does not implicitly add a spec-loaded module's containing directory
        # to sys.path, so treating source.parent as an absolute-import root can
        # select a staged harness copy that runtime execution would never see.
        # Absolute imports use the repository root, explicitly declared
        # implementation roots, bound package roots, and source-declared
        # sys.path additions only. Relative imports remain source-relative.
        bases.extend((*(search_roots or []), project))
        if parts and parts[0] == project.name:
            bases.append(project.parent)
    unique_bases: list[Path] = []
    for base in bases:
        normalized = Path(os.path.abspath(str(base)))
        if normalized not in unique_bases:
            unique_bases.append(normalized)

    discovered: list[Path] = []
    for base in unique_bases:
        discovered.extend(_package_files(base, parts, project=project))
        package = base.joinpath(*parts) if parts else base
        if package.is_dir():
            for imported in imported_names or []:
                if imported == "*":
                    continue
                discovered.extend(
                    _package_files(package, imported.split("."), project=project)
                )
    return sorted(set(discovered), key=lambda item: item.as_posix().lower())


def _static_import_roots(
    tree: ast.AST,
    *,
    project: Path,
    source: Path,
    aliases: dict[str, str],
    paths: dict[str, Path],
    strings: dict[str, str],
) -> tuple[list[Path], list[str]]:
    """Resolve explicit ``sys.path`` additions that can change local imports."""

    roots: list[Path] = []
    problems: list[str] = []
    argument_by_call = {
        "sys.path.insert": 1,
        "sys.path.append": 0,
        "site.addsitedir": 0,
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call = _call_name(node.func, aliases)
        if call not in argument_by_call:
            continue
        argument = argument_by_call[call]
        argument_node = node.args[argument] if len(node.args) > argument else None
        raw = _static_path_value(
            argument_node,
            source=source,
            aliases=aliases,
            values=paths,
            strings=strings,
        )
        if raw is None:
            problems.append(
                f"dynamic import search path for {call} is not statically provable"
            )
            continue
        candidates = [raw] if raw.is_absolute() else [source.parent / raw, project / raw]
        for candidate in candidates:
            normalized = Path(os.path.abspath(str(candidate)))
            if normalized.is_dir() and _is_within(normalized, project) and normalized not in roots:
                roots.append(normalized)
    return roots, problems


def _trusted_runtime_dynamic_loader(source: Path) -> bool:
    if source.name not in _TRUSTED_RUNTIME_DYNAMIC_LOADERS:
        return False
    expected = Path(__file__).with_name(source.name)
    try:
        return expected.is_file() and sha256_file(source) == sha256_file(expected)
    except OSError:
        return False


def _assert_closed_repository_imports(
    project: Path,
    implementation_paths: list[Path],
    *,
    repository_import_roots: list[Path] | None = None,
) -> None:
    """Reject product Python whose repository-local imports are not frozen."""

    project = Path(os.path.abspath(str(project)))
    frozen = {Path(os.path.abspath(str(path))) for path in implementation_paths}
    problems: list[str] = []

    def source_label(path: Path) -> str:
        try:
            return path.relative_to(project).as_posix()
        except ValueError:
            return path.as_posix()

    def require_targets(source: Path, reason: str, targets: list[Path]) -> None:
        for target in targets:
            normalized = Path(os.path.abspath(str(target)))
            _assert_safe_implementation_chain(
                project,
                normalized,
                label=f"repository-local import {reason}",
            )
            if normalized not in frozen:
                problems.append(
                    f"{source_label(source)}: {reason} resolves to repository-local "
                    f"{source_label(normalized)} outside the frozen implementation closure; "
                    f"declare {source_label(normalized)} in implementation_dependencies"
                )

    for source in sorted(
        (path for path in frozen if path.suffix.lower() == ".py" and path.is_file()),
        key=lambda item: item.as_posix().lower(),
    ):
        tree = _python_tree(source)
        aliases = _import_aliases(tree)
        string_values = _static_strings(tree)
        path_values = _static_paths(
            tree,
            source=source,
            aliases=aliases,
            strings=string_values,
        )
        import_roots, import_root_problems = _static_import_roots(
            tree,
            project=project,
            source=source,
            aliases=aliases,
            paths=path_values,
            strings=string_values,
        )
        import_roots = [
            *(
                Path(os.path.abspath(str(root)))
                for root in (repository_import_roots or [])
            ),
            *import_roots,
        ]
        problems.extend(
            f"{source_label(source)}: {problem}; dynamic local imports are forbidden"
            for problem in import_root_problems
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    require_targets(
                        source,
                        f"import {item.name}",
                        _local_module_files(
                            project,
                            source,
                            item.name,
                            search_roots=import_roots,
                        ),
                    )
            elif isinstance(node, ast.ImportFrom):
                module = str(node.module or "")
                names = [item.name for item in node.names]
                display = "." * int(node.level or 0) + module
                require_targets(
                    source,
                    f"from {display or '.'} import {', '.join(names)}",
                    _local_module_files(
                        project,
                        source,
                        module,
                        level=int(node.level or 0),
                        imported_names=names,
                        search_roots=import_roots,
                    ),
                )
            elif isinstance(node, ast.Call):
                call = _call_name(node.func, aliases)
                if call in _DYNAMIC_MODULE_IMPORTS:
                    targets = (
                        _static_string_options(node.args[0], string_values)
                        if node.args
                        else set()
                    )
                    if not targets:
                        problems.append(
                            f"{source_label(source)}: dynamic import target for {call} "
                            "is not statically provable; dynamic local imports are forbidden"
                        )
                        continue
                    for target in sorted(targets):
                        if target.startswith("."):
                            package_node = next(
                                (
                                    item.value
                                    for item in node.keywords
                                    if item.arg == "package"
                                ),
                                None,
                            )
                            package = _static_string(package_node, string_values)
                            if not package:
                                problems.append(
                                    f"{source_label(source)}: relative dynamic import {target!r} "
                                    "has no statically provable package"
                                )
                                continue
                            try:
                                target = importlib.util.resolve_name(target, package)
                            except (ImportError, ValueError) as exc:
                                problems.append(
                                    f"{source_label(source)}: invalid dynamic import {target!r}: {exc}"
                                )
                                continue
                        require_targets(
                            source,
                            f"dynamic import {target}",
                            _local_module_files(
                                project,
                                source,
                                target,
                                search_roots=import_roots,
                            ),
                        )
                elif call in _DYNAMIC_FILE_IMPORT_ARGUMENT:
                    argument = _DYNAMIC_FILE_IMPORT_ARGUMENT[call]
                    argument_node = (
                        node.args[argument] if len(node.args) > argument else None
                    )
                    static_path = _static_path_value(
                        argument_node,
                        source=source,
                        aliases=aliases,
                        values=path_values,
                        strings=string_values,
                    )
                    target = _static_string(argument_node, string_values)
                    if static_path is None and target is None:
                        if not _trusted_runtime_dynamic_loader(source):
                            problems.append(
                                f"{source_label(source)}: dynamic execution path for {call} "
                                "is not statically provable; dynamic local imports are forbidden"
                            )
                        continue
                    raw = static_path if static_path is not None else Path(str(target))
                    candidates = [raw] if raw.is_absolute() else [source.parent / raw, project / raw]
                    existing: list[Path] = []
                    for candidate in candidates:
                        candidate = Path(os.path.abspath(str(candidate)))
                        if candidate.is_dir() and (candidate / "__main__.py").is_file():
                            candidate = candidate / "__main__.py"
                        if candidate.is_file() and _is_within(candidate, project):
                            existing.append(candidate)
                    if not existing and not _trusted_runtime_dynamic_loader(source):
                        problems.append(
                            f"{source_label(source)}: dynamic execution path {str(raw)!r} for {call} "
                            "does not resolve to a frozen repository file"
                        )
                        continue
                    require_targets(source, f"dynamic execution {raw}", existing)

    if problems:
        raise FlowError("implementation import closure is open:\n- " + "\n- ".join(sorted(set(problems))))


def implementation_files(skill_dir: Path, flow: dict[str, Any]) -> list[Path]:
    # `Path.resolve()` calls Windows realpath/stat for every component. On a
    # busy filtered filesystem that can serialize for tens of seconds per
    # file, making an 80-file implementation check appear hung. These paths
    # come from the already-validated frozen flow, so lexical normalization is
    # sufficient here and preserves the same canonical absolute labels.
    root, project = _implementation_project(skill_dir)

    def implementation_path(value: str | Path) -> Path:
        path = Path(value)
        normalized = Path(os.path.abspath(str(path if path.is_absolute() else root / path)))
        try:
            normalized.relative_to(root)
        except ValueError as exc:
            raise FlowError(f"path escapes skill directory: {value}") from exc
        _assert_safe_implementation_chain(
            root, normalized, label="implementation path"
        )
        return normalized

    def dependency_path(value: str | Path) -> Path:
        """Resolve an explicitly declared runtime dependency from project root."""
        path = Path(value)
        normalized = Path(os.path.abspath(str(path if path.is_absolute() else project / path)))
        try:
            normalized.relative_to(project)
        except ValueError as exc:
            raise FlowError(f"implementation dependency escapes project directory: {value}") from exc
        _assert_safe_implementation_chain(
            project, normalized, label="implementation dependency"
        )
        return normalized

    flow_path = Path(os.path.abspath(str(flow["_flow_path"])))
    _assert_safe_implementation_chain(root, flow_path, label="flow definition")
    files = [flow_path]
    declared_dependencies = [
        dependency_path(value)
        for value in (flow.get("implementation_dependencies") or [])
    ]
    files.extend(declared_dependencies)
    for step in flow["steps"]:
        step_dependencies = [
            dependency_path(value)
            for value in (step.get("implementation_dependencies") or [])
        ]
        declared_dependencies.extend(step_dependencies)
        files.extend(step_dependencies)
        for field in ("handler", "output_schema"):
            if step.get(field):
                files.append(implementation_path(step[field]))
        # These paths may be inferred defaults for recovery/judge modes even
        # when the optional file is not materialized by the flow.
        for field in ("draft_schema", "receipt_schema", "gem"):
            if step.get(field):
                candidate = implementation_path(step[field])
                if candidate.is_file():
                    files.append(candidate)
    tool_ids = {
        local_tool_package_name(
            str(binding.get("ref") or ""),
            label=(
                f"{step['id']}.execution.tool_bindings"
                f"[{binding.get('tool')}].ref"
            ),
        )
        for step in flow["steps"]
        for binding in (step.get("execution") or {}).get("tool_bindings") or []
        if isinstance(binding, dict) and binding.get("ref")
    }
    tool_ids.update(
        runtime_package_name(str(step["worker"]), label=f"{step['id']}.worker")
        for step in flow["steps"]
        if step.get("judge_abi") and step.get("worker")
    )
    repository_import_roots = {path.parent for path in declared_dependencies}
    for tool_id in sorted(tool_ids):
        tool_root = project / "flowsteps" / "tools" / tool_id
        repository_import_roots.add(tool_root)
        _assert_safe_implementation_chain(
            project, tool_root, label=f"tool package {tool_id}"
        )
        # A toolbox package may execute sibling helpers or read package-local
        # resources.  Freezing only tool.py plus the two public schemas leaves
        # those executable bytes outside the run identity.  Freeze the whole
        # runtime package while excluding only cache/VCS trees. Test-named
        # helpers remain frozen because executable package code can import them.
        excluded_directories = {"__pycache__", ".git", ".pytest_cache"}
        for candidate in sorted(tool_root.rglob("*"), key=lambda item: item.as_posix().lower()):
            relative = candidate.relative_to(tool_root)
            if any(part in excluded_directories for part in relative.parts[:-1]):
                continue
            if _is_unsafe_implementation_link(candidate):
                raise FlowError(f"tool package contains an unsafe link: {candidate}")
            if candidate.is_file():
                files.append(candidate)
    implementation_paths = sorted(
        {Path(os.path.abspath(str(path))) for path in files},
        key=lambda item: item.as_posix().lower(),
    )
    if flow.get("codebase_import_roots"):
        from project_imports import module_map, ProjectImportError
        try:
            module_map(project, flow["codebase_import_roots"], implementation_paths)
        except ProjectImportError as exc:
            raise FlowError(str(exc)) from exc
        repository_import_roots.update(project / value for value in flow["codebase_import_roots"])
    _assert_closed_repository_imports(
        project,
        implementation_paths,
        repository_import_roots=sorted(
            repository_import_roots,
            key=lambda item: item.as_posix().lower(),
        ),
    )
    return implementation_paths


def implementation_lock(skill_dir: Path, flow: dict[str, Any]) -> dict[str, Any]:
    root, project = _implementation_project(skill_dir)
    entries: dict[str, str] = {}
    for path in implementation_files(skill_dir, flow):
        try:
            digest = sha256_file(path)
        except FileNotFoundError:
            # Missing generated-tool contracts are an audit finding, not
            # implementation bytes that can be frozen.
            continue
        try:
            label = f"skill:{path.relative_to(root).as_posix()}"
        except ValueError:
            try:
                label = f"project:{path.relative_to(project).as_posix()}"
            except ValueError:
                label = f"external:{path.as_posix()}"
        entries[label] = digest
    return {
        "schema": "flowstep_implementation_lock_v2",
        "skill": skill_dir.name,
        "flow_id": flow["flow_id"],
        "flow_version": flow["version"],
        "files": entries,
        "fingerprint_sha256": sha256_bytes(canonical_json(entries)),
    }


def implementation_lock_from_frozen_files(
    skill_dir: Path,
    flow: dict[str, Any],
    frozen: dict[str, Any],
) -> dict[str, Any]:
    """Re-hash the exact frozen implementation set without rediscovery."""
    root = Path(os.path.abspath(str(skill_dir)))
    project = root.parent.parent.parent if root.parent.name == "flows" and root.parent.parent.name == "flowsteps" else root
    frozen_files = frozen.get("files") if isinstance(frozen.get("files"), dict) else {}
    entries: dict[str, str] = {}
    for label in sorted(frozen_files):
        if label.startswith("skill:"):
            path = root / label.removeprefix("skill:")
        elif label.startswith("project:"):
            path = project / label.removeprefix("project:")
        elif label.startswith("external:"):
            path = Path(label.removeprefix("external:"))
        else:
            raise FlowError(f"invalid implementation lock label: {label}")
        entries[label] = sha256_file(path)
    return {
        "schema": "flowstep_implementation_lock_v2",
        "skill": skill_dir.name,
        "flow_id": flow["flow_id"],
        "flow_version": flow["version"],
        "files": entries,
        "fingerprint_sha256": sha256_bytes(canonical_json(entries)),
    }


def implementation_metadata_from_frozen_files(
    skill_dir: Path,
    frozen: dict[str, Any],
) -> dict[str, dict[str, int]]:
    """Cheaply detect whether a cached verification still describes current files."""
    root = Path(os.path.abspath(str(skill_dir)))
    project = root.parent.parent.parent if root.parent.name == "flows" and root.parent.parent.name == "flowsteps" else root
    frozen_files = frozen.get("files") if isinstance(frozen.get("files"), dict) else {}
    paths_by_parent: dict[Path, list[tuple[str, Path]]] = {}
    for label in sorted(frozen_files):
        if label.startswith("skill:"):
            path = root / label.removeprefix("skill:")
        elif label.startswith("project:"):
            path = project / label.removeprefix("project:")
        elif label.startswith("external:"):
            path = Path(label.removeprefix("external:"))
        else:
            raise FlowError(f"invalid implementation lock label: {label}")
        paths_by_parent.setdefault(path.parent, []).append((label, path))

    # On Windows removable/exFAT volumes, one Path.stat call per file can turn
    # every short resume into minutes of metadata latency.  scandir retrieves
    # the same size/mtime identity from one directory enumeration and keeps the
    # drift check exact while avoiding the per-file round trips.
    metadata: dict[str, dict[str, int]] = {}
    for parent, requested in paths_by_parent.items():
        with os.scandir(parent) as directory:
            entries = {entry.name.casefold(): entry for entry in directory}
        for label, path in requested:
            entry = entries.get(path.name.casefold())
            if entry is None:
                raise FileNotFoundError(path)
            stat = entry.stat(follow_symlinks=True)
            metadata[label] = {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}
    return metadata


def assert_implementation_lock(run_dir: Path, skill_dir: Path, flow: dict[str, Any]) -> dict[str, Any]:
    lock_path = run_dir / "implementation-lock.json"
    if not lock_path.is_file():
        raise FlowError("missing implementation lock")
    frozen = read_json(lock_path)
    # The product implementation is loaded from the live codebase, not copied
    # into the run.  A run-local verification receipt therefore cannot safely
    # suppress source verification for any amount of time.  Rediscover the
    # complete current closure on every execution boundary so changed, removed,
    # and newly added package members all fail before product work is loaded.
    verification_path = run_dir / "implementation-verification.json"
    current = implementation_lock(skill_dir, flow)
    if (
        frozen.get("flow_id") != current.get("flow_id")
        or frozen.get("flow_version") != current.get("flow_version")
        or frozen.get("files") != current.get("files")
        or frozen.get("fingerprint_sha256") != current.get("fingerprint_sha256")
    ):
        raise FlowError(
            "implementation drift detected; start a fresh run or use "
            "--continue-after-edit <milestone> to preserve compatible upstream chosen outputs"
        )
    write_json(
        verification_path,
        {
            "schema": "flowstep_implementation_verification_v1",
            "fingerprint_sha256": current["fingerprint_sha256"],
            "verified_at_epoch": time.time(),
            "ttl_seconds": 0,
            "files": implementation_metadata_from_frozen_files(skill_dir, current),
        },
        overwrite=True,
    )
    return frozen


def validate_against_schema(instance: Any, schema_path: Path) -> None:
    # jsonschema's Draft 2020 dependency graph is large on Windows. Import it
    # only for commands that actually cross a schema boundary; module import,
    # --help, and lexical session discovery must not pay this cost.
    from jsonschema import Draft202012Validator, RefResolver
    from jsonschema.exceptions import SchemaError

    if not schema_path.is_file():
        raise FlowError(f"schema not found: {schema_path}")
    schema = read_json(schema_path)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise FlowError(f"invalid schema {schema_path}: {exc.message}") from exc
    # Build the resolver URI lexically. Path.resolve() performs a physical
    # realpath/stat walk on Windows, which can stall every validation when a
    # schema lives on a degraded or removable volume. The schema existence
    # check and read above already establish the file target.
    resolver_store: dict[str, Any] = {}
    # All workflow schemas are closed local inputs. Register sibling contracts
    # under their authored $id, lexical filename, and file URI so a relative
    # $ref can never fall through RefResolver.resolve_remote(). This applies to
    # both m8m.local IDs and legacy filename IDs such as
    # m8m_milestone_expectation_v1.schema.json.
    serialized_schema = json.dumps(schema, ensure_ascii=True)
    if '"$ref"' in serialized_schema:
        schema_parent_uri = Path(os.path.abspath(str(schema_path.parent))).as_uri().rstrip("/") + "/"
        for sibling in schema_path.parent.glob("*.schema.json"):
            try:
                linked = read_json(sibling)
            except FlowError:
                continue
            sibling_name = sibling.name
            sibling_uri = schema_parent_uri + sibling_name
            resolver_store[sibling_name] = linked
            resolver_store[sibling_uri] = linked
            schema_id = linked.get("$id") if isinstance(linked, dict) else None
            if isinstance(schema_id, str) and schema_id:
                resolver_store[schema_id] = linked
    resolver = RefResolver(
        base_uri=Path(os.path.abspath(str(schema_path))).as_uri(),
        referrer=schema,
        store=resolver_store,
    )
    errors = sorted(
        Draft202012Validator(schema, resolver=resolver).iter_errors(instance),
        key=lambda item: list(item.path),
    )
    if errors:
        err = errors[0]
        location = "$" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in err.path)
        raise FlowError(f"schema {schema_path.name} failed at {location}: {err.message}")


def envelope_schema_path() -> Path:
    return CONTRACTS_DIR / "flowstep_output_v3.schema.json"


def action_schema_path() -> Path:
    return CONTRACTS_DIR / "flow_sequence_action_v2.schema.json"


def flow_schema_path() -> Path:
    return CONTRACTS_DIR / "flowstep_flow_v4.schema.json"


def chosen_output_schema_path() -> Path:
    return CONTRACTS_DIR / "m8m_chosen_output_v1.schema.json"


def candidate_cache_schema_path() -> Path:
    return CONTRACTS_DIR / "m8m_candidate_cache_entry_v1.schema.json"


def cache_receipt_schema_path() -> Path:
    return CONTRACTS_DIR / "m8m_cache_receipt_v1.schema.json"


def run_context_schema_path(schema: str = "m8m_run_context_v2") -> Path:
    if schema == "m8m_run_context_v1":
        return CONTRACTS_DIR / "m8m_run_context_v1.schema.json"
    if schema == "m8m_run_context_v2":
        return CONTRACTS_DIR / "m8m_run_context_v2.schema.json"
    raise FlowError(f"unsupported run context schema: {schema}")


def source_asset_manifest_schema_path() -> Path:
    return CONTRACTS_DIR / "m8m_source_asset_manifest_v1.schema.json"


def run_storage_contract_schema_path() -> Path:
    return CONTRACTS_DIR / "m8m_run_storage_contract_v1.schema.json"


def context_capsule_schema_path() -> Path:
    return CONTRACTS_DIR / "m8m_context_capsule_v1.schema.json"


def goal_ledger_schema_path() -> Path:
    return CONTRACTS_DIR / "m8m_goal_ledger_v1.schema.json"


def expected_artifact_path(run_dir: Path, flow: dict[str, Any], step: dict[str, Any]) -> Path:
    return run_dir / flow["artifact_root"] / f"{step['id']}.{step['output_contract']}.json"


def work_dir(run_dir: Path, step_id: str) -> Path:
    return run_dir / "work" / step_id


def _project_import_binding(skill_dir: Path, flow: dict[str, Any] | None = None):
    """Use only explicitly authored roots and the frozen implementation closure."""
    if flow is None or not flow.get("codebase_import_roots"):
        return None
    from project_imports import import_context, ProjectImportError
    _, project = _implementation_project(skill_dir)
    try:
        return import_context(project, flow["codebase_import_roots"], implementation_files(skill_dir, flow))
    except ProjectImportError as exc:
        raise FlowError(str(exc)) from exc


def project_import_context(skill_dir: Path, flow: dict[str, Any] | None = None):
    binding = _project_import_binding(skill_dir, flow)
    return binding.activate() if binding is not None else nullcontext()


def load_tool(skill_dir: Path, step: dict[str, Any], *, flow: dict[str, Any] | None = None) -> Any:
    path = skill_rel(skill_dir, step["handler"])
    if not path.is_file():
        raise FlowError(f"missing tool: {path}")
    spec = importlib.util.spec_from_file_location(f"flowstep_tool_{step['id']}", path)
    if spec is None or spec.loader is None:
        raise FlowError(f"cannot import tool: {path}")
    module = importlib.util.module_from_spec(spec)
    # Compile the current bytes instead of consulting __pycache__. Intentional
    # continue-after-edit can replace a handler with same-size source inside one
    # filesystem timestamp tick; normal import bytecode validation may otherwise
    # execute the implementation that the run has just superseded.
    source = path.read_bytes()
    original_sys_path = list(sys.path)
    import_binding = _project_import_binding(skill_dir, flow)
    def import_scope():
        return import_binding.activate() if import_binding is not None else nullcontext()
    try:
        with import_scope():
            exec(compile(source, str(path), "exec"), module.__dict__)
    finally:
        # Product handlers may add their repo root temporarily for imports.
        # Imported modules stay bound in sys.modules/module globals; retaining
        # that root globally makes every later import stat the workspace disk.
        sys.path[:] = original_sys_path
    if not callable(getattr(module, "run", None)):
        raise FlowError(f"{path} must define run(input_data, draft=None, **kwargs)")
    implementation = module.run
    @wraps(implementation)
    def run_with_project_imports(*args, **kwargs):
        with import_scope():
            return implementation(*args, **kwargs)
    module.run = run_with_project_imports
    return module


def _last_pass_source(
    run_dir: Path, flow: dict[str, Any], step: dict[str, Any]
) -> tuple[dict[str, Any], Path, dict[str, Any]] | None:
    by_id = {item["id"]: item for item in flow["steps"]}
    ids = [item["id"] for item in flow["steps"]]
    if step["id"] not in ids:
        return None
    skipped: set[str] = set()
    record_path = Path(run_dir) / "flow-execution-record.json"
    if record_path.is_file():
        record = read_json(record_path)
        skipped = {str(item.get("step_id") or "") for item in (record.get("skipped") or [])}
    for prev_id in reversed(ids[: ids.index(step["id"])]):
        if prev_id in skipped:
            continue
        source = by_id.get(prev_id)
        if not source:
            continue
        path = expected_artifact_path(run_dir, flow, source)
        if not path.is_file():
            continue
        artifact = read_json(path)
        if artifact.get("status") == "PASS":
            return source, path, artifact
    return None


def _binding_reference(reference: Any, *, step_id: str, input_name: str) -> tuple[str, str, str | None, str | None]:
    if isinstance(reference, str):
        if "." not in reference:
            raise FlowError(
                f"{step_id}.inputs.{input_name} must be user.request, <milestone>.<contract>, "
                "or a member binding"
            )
        source_id, contract = reference.split(".", 1)
        return source_id, contract, None, None
    if not isinstance(reference, dict):
        raise FlowError(f"{step_id}.inputs.{input_name} must be a string or member binding")
    source_ref = str(reference.get("from") or "")
    if "." not in source_ref:
        raise FlowError(f"{step_id}.inputs.{input_name}.from must be <milestone>.<contract>")
    source_id, contract = source_ref.split(".", 1)
    output_id = str(reference.get("output") or "").strip() or None
    member_id = str(reference.get("member") or "").strip() or None
    if member_id and not output_id:
        raise FlowError(f"{step_id}.inputs.{input_name}.member requires output")
    return source_id, contract, output_id, member_id


def bind_inputs(run_dir: Path, flow: dict[str, Any], step: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from session_layout import chosen_output_path, load_chosen_output, resolve_chosen_output

    by_id = {item["id"]: item for item in flow["steps"]}
    pinned_skill_dir = Path(flow["_skill_dir"])
    payload: dict[str, Any] = {}
    bindings: list[dict[str, Any]] = []
    join = step.get("join") or []
    if join:
        for source_id in join:
            if source_id not in by_id:
                raise FlowError(f"{step['id']} join references unknown milestone {source_id}")
            source = by_id[source_id]
            path = chosen_output_path(run_dir, source_id)
            if not path.is_file():
                continue
            payload[source_id] = resolve_chosen_output(
                run_dir,
                source_id,
                step=source,
                skill_dir=pinned_skill_dir,
            )
            bindings.append(
                {
                    "input_name": source_id,
                    "source_step_id": source_id,
                    "chosen_output_path": relative_to(run_dir, path),
                    "contract": source["output_contract"],
                    "status": "chosen",
                }
            )
            return payload, bindings
        raise FlowError(f"{step['id']}: join found no PASS branch among {join}")
    for name, reference in step["inputs"].items():
        if reference == "user.request":
            path = run_dir / "request.json"
            if not path.is_file():
                raise FlowError("missing request.json")
            payload[name] = read_json(path)
            bindings.append(
                {
                    "input_name": name,
                    "source_step_id": "user",
                    "chosen_output_path": relative_to(run_dir, path),
                    "contract": "user.request",
                    "status": "chosen",
                }
            )
            continue
        source_id, contract, output_id, member_id = _binding_reference(
            reference, step_id=step["id"], input_name=name
        )
        if source_id not in by_id:
            raise FlowError(f"{step['id']} input {name} references unknown milestone {source_id}")
        source = by_id[source_id]
        if source["output_contract"] != contract:
            raise FlowError(f"{step['id']} input {name} contract mismatch: expected {source['output_contract']}")
        path = chosen_output_path(run_dir, source_id)
        # Explicitly aliased inputs may reference every arm of a branch join.
        # An unselected arm is intentionally skipped and therefore has no
        # chosen-output.json.  Only tolerate that absence for on_path steps;
        # non-branch dependencies remain mandatory and fail closed below.
        if source.get("on_path") and not path.is_file():
            continue
        payload[name] = resolve_chosen_output(
            run_dir,
            source_id,
            output_id=output_id,
            member_id=member_id,
            step=source,
            skill_dir=pinned_skill_dir,
        )
        binding = {
            "input_name": name,
            "source_step_id": source_id,
            "chosen_output_path": relative_to(run_dir, path),
            "contract": contract,
            "status": "chosen",
        }
        if output_id:
            binding["output"] = output_id
        if member_id:
            binding["member"] = member_id
        bindings.append(binding)
    branch_source = next(
        (
            item
            for item in flow["steps"]
            if isinstance(item.get("branch"), dict)
            and str(item["branch"].get("join") or "") == step["id"]
        ),
        None,
    )
    # Explicit branch aliases already supply the chosen arm. Otherwise expose
    # its terminal output by milestone ID, without consulting an input schema.
    if branch_source is not None and not any(
        by_id.get(binding["source_step_id"], {}).get("on_path")
        for binding in bindings
    ):
        record_path = run_dir / "flow-execution-record.json"
        record = read_json(record_path) if record_path.is_file() else {}
        active_branch = str(record.get("active_branch") or "")
        step_index = next(
            index for index, candidate in enumerate(flow["steps"])
            if str(candidate.get("id") or "") == str(step["id"])
        )
        # One branch arm may contain several milestone checkpoints.  The join
        # consumes the terminal chosen output from the active arm; earlier
        # chosen outputs remain available to intermediate milestones and as
        # durable audit evidence, but are not competing join candidates.
        candidates = [
            item
            for item in flow["steps"][:step_index]
            if str(item.get("on_path") or "") == active_branch
            and chosen_output_path(run_dir, item["id"]).is_file()
        ]
        if not candidates:
            raise FlowError(
                f"{step['id']}: branch join found no terminal PASS output "
                f"for active branch {active_branch or '(empty)'}"
            )
        selected = candidates[-1]
        selected_id = str(selected["id"])
        selected_path = chosen_output_path(run_dir, selected_id)
        load_chosen_output(
            run_dir,
            selected_id,
            step=selected,
            skill_dir=pinned_skill_dir,
        )
        payload[selected_id] = resolve_chosen_output(
            run_dir,
            selected_id,
            step=selected,
            skill_dir=pinned_skill_dir,
        )
        bindings.append(
            {
                "input_name": selected_id,
                "source_step_id": selected_id,
                "chosen_output_path": relative_to(run_dir, selected_path),
                "contract": selected["output_contract"],
                "status": "chosen",
            }
        )
    return payload, bindings


def invoke_tool(
    skill_dir: Path,
    step: dict[str, Any],
    input_data: dict[str, Any],
    draft: dict[str, Any] | None,
    task: dict[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    root = Path(os.path.abspath(str(skill_dir)))
    project = (
        root.parent.parent.parent
        if root.parent.name == "flows" and root.parent.parent.name == "flowsteps"
        else root
    )
    for binding in (step.get("execution") or {}).get("tool_bindings") or []:
        if not isinstance(binding, dict):
            raise FlowError(f"{step['id']}: malformed bound FlowStep tool")
        flowstep_id = str(binding.get("tool") or "")
        package = local_tool_package_name(
            str(binding.get("ref") or ""),
            label=f"{step['id']}.execution.tool_bindings[{flowstep_id}].ref",
        )
        tool_root = project / "flowsteps" / "tools" / package
        missing = [
            name
            for name in ("tool.py", "input.schema.json", "output.schema.json")
            if not (tool_root / name).is_file()
        ]
        if missing:
            raise FlowError(
                f"{step['id']}: bound FlowStep tool {binding['ref']} is unavailable; "
                f"missing {', '.join(missing)}"
            )
    module = load_tool(skill_dir, step)
    return module.run(input_data, draft=draft, task=task, run_dir=run_dir)


def make_envelope(
    *,
    flow: dict[str, Any],
    step: dict[str, Any],
    run_id: str,
    attempt: int,
    status: str,
    data: dict[str, Any],
    bindings: list[dict[str, Any]],
    fingerprint: str,
    blockers: list[str],
    chosen_output: str | None,
) -> dict[str, Any]:
    return {
        "schema": ENVELOPE_SCHEMA,
        "artifact_id": f"{step['output_contract']}:{run_id}:{step['id']}",
        "run_id": run_id,
        "flow_id": flow["flow_id"],
        "flow_version": flow["version"],
        "step_id": step["id"],
        "output_contract": step["output_contract"],
        "status": status,
        "data": data,
        "chosen_output": chosen_output,
        "evidence": {
            "handler": step["handler"].replace("\\", "/"),
            "model": step["model"],
            "attempt": attempt,
            "input_artifacts": bindings,
            "implementation_fingerprint_sha256": fingerprint,
            "blockers": blockers,
        },
        "created_at": utc_now(),
    }


def render_flowstep_table(flow: dict[str, Any]) -> str:
    lines = [
        f"# {flow['flow_id']} FlowStep table",
        "",
        "| # | Step | Class | Handler | Model | Why model | Inputs | Output contract | Output schema |",
        "| ---: | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, step in enumerate(flow["steps"], start=1):
        inputs = ", ".join(f"{name}={ref}" for name, ref in step["inputs"].items())
        why = step.get("model_justification") or "none"
        lines.append(
            f"| {index} | `{step['id']}` | `{step.get('class', 'tool')}` | `{step['handler']}` | `{step['model']}` | "
            f"{why} | {inputs} | `{step['output_contract']}` | `{step['output_schema']}` |"
        )
    lines.append("")
    lines.append(
        "This table is generated from the flow YAML. The Python tool and schemas are the runtime."
    )
    lines.append("")
    return "\n".join(lines)


def is_stub_output_schema(schema: dict[str, Any]) -> bool:
    required = schema.get("required") or []
    properties = schema.get("properties") or {}
    return list(required) == ["ok"] and set(properties) <= {"ok"}


ASSET_KINDS = ("file", "image", "video", "audio", "json", "data")


def is_passthrough_schema(schema: dict[str, Any] | None) -> bool:
    """True when a milestone output would accept anything — not a harness asset."""
    if not isinstance(schema, dict):
        return True
    if is_stub_output_schema(schema):
        return True
    required = [str(item) for item in (schema.get("required") or []) if item]
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if not required:
        return True
    if schema.get("additionalProperties") is True and not properties:
        return True
    return False


def is_harness_asset_schema(schema: dict[str, Any] | None) -> bool:
    return not is_passthrough_schema(schema)


def infer_asset_kind(schema: dict[str, Any] | None, *, fallback: str = "file") -> str:
    kind = fallback if fallback in ASSET_KINDS else "file"
    if not isinstance(schema, dict):
        return kind
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    names = " ".join([*props, *[str(item) for item in (schema.get("required") or [])]]).lower()
    asset = props.get("asset") if isinstance(props.get("asset"), dict) else {}
    asset_props = asset.get("properties") if isinstance(asset.get("properties"), dict) else {}
    image_tokens = ("image", "png", "jpg", "jpeg", "webp", "card", "render", "screenshot")
    if "path" in asset_props or "sha256" in asset_props:
        return "image" if any(tok in names for tok in image_tokens) else "file"
    if any(tok in names for tok in image_tokens):
        return "image"
    if any(key in props for key in ("path", "sha256")) or any(
        str(key).endswith("_sha256") or str(key).endswith("_path") for key in props
    ):
        return "file"
    if props:
        return "json"
    return kind


def file_asset_schema(step_id: str) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{step_id}.output.schema.json",
        "type": "object",
        "additionalProperties": False,
        "required": ["asset"],
        "properties": {
            "asset": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "slot": {"type": "string", "minLength": 1},
                    "mime": {"type": "string", "minLength": 1},
                },
            }
        },
    }


def harness_output_schema(
    schema: dict[str, Any] | None,
    *,
    step_id: str,
    kind: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Closed required proof for a milestone. Never a passthrough object."""
    if is_harness_asset_schema(schema) and isinstance(schema, dict):
        closed = dict(schema)
        closed.setdefault("$schema", "https://json-schema.org/draft/2020-12/schema")
        closed.setdefault("$id", f"{step_id}.output.schema.json")
        closed["type"] = "object"
        closed["additionalProperties"] = False
        if not closed.get("required"):
            closed["required"] = [str(key) for key in (closed.get("properties") or {})]
        return closed, infer_asset_kind(closed, fallback=kind or "json")
    resolved = kind if kind in ASSET_KINDS else "file"
    if resolved in {"file", "image", "video", "audio"}:
        return file_asset_schema(step_id), resolved
    return (
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{step_id}.output.schema.json",
            "type": "object",
            "additionalProperties": False,
            "required": ["proof"],
            "properties": {"proof": {"type": "object", "minProperties": 1}},
        },
        resolved,
    )


def _returns_reference(node: ast.AST, names: set[str]) -> bool:
    """True for an unmodified direct reference or transparent container copy."""

    if isinstance(node, ast.Name) and node.id in names:
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
        return any(_returns_reference(arg, names) for arg in node.args) and not node.keywords
    if isinstance(node, ast.Dict):
        return (
            len(node.keys) == 1
            and node.keys[0] is None
            and _returns_reference(node.values[0], names)
        )
    return False


def _returns_draft(node: ast.AST) -> bool:
    return _returns_reference(node, {"draft"})


def _simple_input_alias_return(run_fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Detect a straight-line ``alias = input_data; return alias`` passthrough.

    The analysis intentionally stops at the first non-trivial statement. This
    catches generated identity wrappers without guessing whether control flow
    or an in-place mutation performed meaningful work.
    """

    positional = [*run_fn.args.posonlyargs, *run_fn.args.args]
    input_name = positional[0].arg if positional else "input_data"
    aliases = {input_name}
    for statement in run_fn.body:
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if isinstance(target, ast.Name) and isinstance(statement.value, ast.Name):
                if statement.value.id in aliases:
                    aliases.add(target.id)
                    continue
            return False
        if isinstance(statement, ast.AnnAssign):
            if (
                isinstance(statement.target, ast.Name)
                and isinstance(statement.value, ast.Name)
                and statement.value.id in aliases
            ):
                aliases.add(statement.target.id)
                continue
            return False
        if isinstance(statement, ast.Return):
            return statement.value is not None and _returns_reference(statement.value, aliases)
        if isinstance(statement, ast.Pass):
            continue
        return False
    return False


def inspect_tool_source(source: str, *, step_id: str, model: str) -> list[str]:
    issues: list[str] = []
    if re.search(r"raise\s+NotImplementedError", source):
        issues.append(f"{step_id}: tool is still a generated stub")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"{step_id}: tool.py is not valid Python: {exc}"]
    run_fn = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"
        ),
        None,
    )
    if run_fn is None:
        issues.append(f"{step_id}: tool.py must define run()")
        return issues
    positional = [*run_fn.args.posonlyargs, *run_fn.args.args]
    input_name = positional[0].arg if positional else "input_data"
    for child in ast.walk(run_fn):
        if isinstance(child, ast.Return) and child.value is not None and _returns_draft(child.value):
            issues.append(f"{step_id}: identity tool (return draft) is forbidden")
            break
    if any(
        isinstance(child, ast.Return)
        and child.value is not None
        and _returns_reference(child.value, {input_name})
        for child in ast.walk(run_fn)
    ) or _simple_input_alias_return(run_fn):
        issues.append(f"{step_id}: identity tool / input passthrough is forbidden")
    if model == "none" and "NEED_MODEL" in source:
        issues.append(f"{step_id}: model is none but the tool returns NEED_MODEL")
    if model != "none" and "NEED_MODEL" not in source:
        issues.append(f"{step_id}: model is {model} but the tool never returns NEED_MODEL")
    return issues


def inspect_step_test(source: str, *, step_id: str) -> list[str]:
    issues: list[str] = []
    if not re.search(r"def\s+test_\w+", source):
        issues.append(f"{step_id}: tests/test_tool.py must define at least one test_* function")
    if re.search(r"raise\s+NotImplementedError", source) and not re.search(
        r"assertRaises(?:Regex)?\(\s*NotImplementedError", source
    ):
        issues.append(f"{step_id}: tests/test_tool.py is still a generated stub")
    return issues


def lint_file_payload_schema(
    schema: dict[str, Any], *, label: str,
    named_media_inputs: dict[str, str] | None = None,
    image_path_draft: bool = False,
) -> list[str]:
    """Compatibility hook: file paths do not imply checksum requirements.

    Authored JSON Schemas validate payloads; runtime admission checks declared
    file/media outputs. Keep the old call signature for existing tool callers.
    Checksums are required only by an explicitly authored domain contract.
    """
    return []


def file_ref(path: str, digest: str, *, content_schema: str | None = None) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise FlowError(f"file_ref sha256 must be 64 lowercase hex: {path}")
    payload = {"path": path, "sha256": digest}
    if content_schema:
        payload["content_schema"] = content_schema
    return payload
