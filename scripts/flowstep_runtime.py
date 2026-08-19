"""Shared FlowStep v2 runtime: one step is one Python tool plus I/O schemas."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, RefResolver


FLOW_SCHEMA = "flowstep_flow_v2"
FLOW_SCHEMA_V3 = "flowstep_flow_v3"
ENVELOPE_SCHEMA = "flowstep_output_v2"
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
)
STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
FLOW_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
FLOWSTEPS_DIRNAME = "flowsteps"
NEED_MODEL = "NEED_MODEL"
BUILDER_ROOT = Path(__file__).resolve().parents[1]
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
    if path.exists() and not overwrite:
        raise FlowError(f"immutable output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def load_yaml(path: Path) -> Any:
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


def is_under_home_skills(path: Path) -> bool:
    """True for Codex or Claude home/project skill folders. Product tools must not live there."""
    text = f"/{path.resolve().as_posix().lower()}/"
    if any(marker in text for marker in HOME_SKILL_MARKERS):
        return True
    stripped = path.resolve().as_posix().lower().rstrip("/")
    return any(stripped.endswith(suffix) for suffix in HOME_SKILL_SUFFIXES)


def is_under_codex_skills(path: Path) -> bool:
    return is_under_home_skills(path)


def is_builder_fixture(path: Path) -> bool:
    text = path.resolve().as_posix().replace("\\", "/").lower()
    return (
        "flowstep-harness-builder/examples/" in text
        or "m8m-harness-builder/examples/" in text
    )


def assert_product_harness_location(path: Path) -> None:
    resolved = path.resolve()
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
        return Path(harness_dir).resolve()
    if codebase:
        if not flow_id:
            raise FlowError("--flow-id is required with --codebase")
        if not FLOW_ID_RE.match(str(flow_id)):
            raise FlowError(f"invalid flow_id: {flow_id}")
        root = Path(codebase).resolve()
        if is_under_home_skills(root):
            raise FlowError("--codebase must be the repo root, not ~/.codex/skills or ~/.claude/skills")
        return (root / FLOWSTEPS_DIRNAME / "flows" / flow_id).resolve()
    if skill_dir:
        return Path(skill_dir).resolve()
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


def _load_flow_v3(skill_dir: Path, path: Path, raw: dict[str, Any]) -> dict[str, Any]:
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
    steps: list[dict[str, Any]] = []
    ids: list[str] = []
    previous: dict[str, Any] | None = None
    for index, item in enumerate(milestones):
        if not isinstance(item, dict):
            raise FlowError(f"milestone {index} must be a mapping")
        _require(item, {"id", "output_contract"}, f"milestone {index}")
        step_id = item["id"]
        if not isinstance(step_id, str) or not STEP_ID_RE.match(step_id):
            raise FlowError(f"invalid milestone id: {step_id}")
        if step_id in ids:
            raise FlowError(f"duplicate milestone id: {step_id}")
        if any(step_id.startswith(prefix) for prefix in ("if_", "loop_", "switch_", "when_", "else_")):
            raise FlowError(f"{step_id}: if/loop/switch are schema gates, not milestones")
        if step_class_hint(step_id) == "tool":
            raise FlowError(
                f"{step_id}: this name is a toolbox function, not a milestone "
                "(see references/milestone.md)"
            )
        ids.append(step_id)
        intel = item.get("intelligence") or "none"
        if intel not in ("none", *MODELS[1:]):
            raise FlowError(f"{step_id}.intelligence must be none|completion|image|judge")
        tools = item.get("tools")
        if not isinstance(tools, list) or not tools or not all(isinstance(t, str) and t for t in tools):
            raise FlowError(f"{step_id}: tools must be a non-empty list of toolbox ids")
        on_tool_fail = str(item.get("on_tool_fail") or "BLOCKED")
        if on_tool_fail not in {"BLOCKED", "need_model"}:
            raise FlowError(f"{step_id}.on_tool_fail must be BLOCKED or need_model")
        if on_tool_fail == "need_model" and intel == "none":
            raise FlowError(f"{step_id}: on_tool_fail need_model requires intelligence completion|image|judge")
        if intel != "none" and not str(item.get("model_justification") or "").strip():
            raise FlowError(f"{step_id}: intelligence {intel} requires model_justification")
        item.setdefault("output_schema", f"schemas/{item['output_contract']}.json")
        item.setdefault("draft_schema", f"milestones/{step_id}/draft.schema.json")
        item.setdefault("handler", f"milestones/{step_id}/assemble.py")
        inputs = item.get("inputs")
        if not inputs:
            if previous is None:
                inputs = {"request": "user.request"}
            else:
                inputs = {previous["id"]: f"{previous['id']}.{previous['output_contract']}"}
        next_edges = item.get("next") or []
        if next_edges:
            if not isinstance(next_edges, list):
                raise FlowError(f"{step_id}: next must be a list of {{when, then}}")
            for edge in next_edges:
                if not isinstance(edge, dict) or not edge.get("when") or not edge.get("then"):
                    raise FlowError(f"{step_id}: each next edge needs when (schema) and then (milestone id)")
            if not item.get("else"):
                raise FlowError(f"{step_id}: next requires else (milestone id or BLOCKED)")
        foreach = item.get("foreach")
        if foreach is not None:
            if not isinstance(foreach, dict):
                raise FlowError(f"{step_id}: foreach must be a mapping")
            for key in ("path", "item_schema", "tools", "max_items"):
                if key not in foreach:
                    raise FlowError(f"{step_id}: foreach.{key} is required")
        join = item.get("join")
        if join is not None and (not isinstance(join, list) or not join):
            raise FlowError(f"{step_id}: join must be a non-empty list of milestone ids")
        step = {
            "id": step_id,
            "kind": "milestone",
            "class": "tool" if intel == "none" else "intelligence",
            "handler": item["handler"],
            "model": "none" if intel == "none" else intel,
            "intelligence": intel,
            "tools": list(tools),
            "inputs": inputs,
            "output_contract": item["output_contract"],
            "input_schema": item.get("input_schema", f"milestones/{step_id}/input.schema.json"),
            "output_schema": item["output_schema"],
            "test": item.get("test", f"milestones/{step_id}/tests/test_assemble.py"),
            "params": item.get("params") or {},
            "on_tool_fail": on_tool_fail,
            "next": list(next_edges),
            "else": item.get("else"),
            "foreach": foreach,
            "join": list(join) if isinstance(join, list) else None,
        }
        if intel != "none":
            step["draft_schema"] = item["draft_schema"]
            step["model_justification"] = item.get("model_justification")
        steps.append(step)
        previous = step
    raw["steps"] = steps
    raw["_skill_dir"] = skill_dir
    raw["_flow_path"] = path
    raw["_v3"] = True
    return raw


def load_flow(skill_dir: Path, flow_path: Path | None = None) -> dict[str, Any]:
    skill_dir = skill_dir.resolve()
    path = flow_path or find_flow_path(skill_dir)
    raw = load_yaml(path)
    if not isinstance(raw, dict):
        raise FlowError(f"flow must be a mapping: {path}")
    schema = raw.get("schema")
    if schema == "flowstep_flow_v1":
        raise FlowError(
            "flowstep_flow_v1 is rejected: every step needs handler + input/output schemas "
            "(flowstep_flow_v2). Do not use execution_mode local/in_process/subagent."
        )
    if schema == FLOW_SCHEMA_V3:
        return _load_flow_v3(skill_dir, path, raw)
    _require(raw, {"schema", "flow_id", "version", "steps"}, "flow")
    if raw["schema"] != FLOW_SCHEMA:
        raise FlowError(f"flow schema must be {FLOW_SCHEMA}")
    if not isinstance(raw["flow_id"], str) or not FLOW_ID_RE.match(raw["flow_id"]):
        raise FlowError(f"invalid flow_id: {raw.get('flow_id')}")
    if not isinstance(raw["version"], int) or raw["version"] < 1:
        raise FlowError("flow version must be a positive integer")
    if not isinstance(raw["steps"], list) or not raw["steps"]:
        raise FlowError("flow must declare at least one step")
    raw.setdefault("max_run_seconds", 3600)
    raw.setdefault("artifact_root", "artifacts")
    if int(raw.get("max_run_repair_cycles") or 0) != 0:
        raise FlowError("max_run_repair_cycles is forbidden; a BLOCKED run stays BLOCKED")
    ids: list[str] = []
    for index, step in enumerate(raw["steps"]):
        if not isinstance(step, dict):
            raise FlowError(f"step {index} must be a mapping")
        if step.get("execution_mode") or step.get("assigned_agent") or step.get("params", {}).get("execution_mode"):
            raise FlowError(
                f"step {step.get('id', index)} uses v1 agent modes; replace with handler + model"
            )
        _require(step, {"id", "handler", "inputs", "output_contract"}, f"step {index}")
        step_id = step["id"]
        if not isinstance(step_id, str) or not STEP_ID_RE.match(step_id):
            raise FlowError(f"invalid step id: {step_id}")
        if step_id in ids:
            raise FlowError(f"duplicate step id: {step_id}")
        ids.append(step_id)
        step.setdefault("kind", "step")
        step.setdefault("model", "none")
        step.setdefault("class", "tool" if step["model"] == "none" else "intelligence")
        step.setdefault("input_schema", f"steps/{step_id}/input.schema.json")
        step.setdefault("output_schema", f"steps/{step_id}/output.schema.json")
        step.setdefault("test", f"steps/{step_id}/tests/test_tool.py")
        step.setdefault("params", {})
        if step["model"] not in MODELS:
            raise FlowError(f"{step_id}.model must be one of {MODELS}")
        if step["class"] not in STEP_CLASSES:
            raise FlowError(f"{step_id}.class must be one of {STEP_CLASSES}")
        if step["class"] == "tool" and step["model"] != "none":
            raise FlowError(
                f"{step_id}: class tool forbids model {step['model']}; "
                "fetch/crop/hash/render/package belong in a codebase tool"
            )
        if step["class"] == "intelligence" and step["model"] == "none":
            raise FlowError(f"{step_id}: class intelligence requires model completion|image|judge")
        hint = step_class_hint(step_id)
        if hint == "tool" and step["class"] == "intelligence":
            raise FlowError(
                f"{step_id}: name is a structured transform/IO; class must be tool "
                "(see references/tool-vs-intelligence.md)"
            )
        if hint == "intelligence" and step["class"] == "tool":
            raise FlowError(
                f"{step_id}: name is judgment; class must be intelligence "
                "(see references/tool-vs-intelligence.md)"
            )
        if step["model"] == "none":
            if step.get("draft_schema"):
                raise FlowError(f"{step_id}: draft_schema is only valid when model is not none")
        else:
            step.setdefault("draft_schema", f"steps/{step_id}/draft.schema.json")
            if not str(step.get("model_justification") or "").strip():
                raise FlowError(f"{step_id}: model {step['model']} requires model_justification")
        if not isinstance(step["inputs"], dict) or not step["inputs"]:
            raise FlowError(f"{step_id}.inputs must be a non-empty mapping")
        if not str(step["handler"]).replace("\\", "/").endswith("tool.py"):
            raise FlowError(f"{step_id}.handler must point at tool.py")
    raw["_skill_dir"] = skill_dir
    raw["_flow_path"] = path
    return raw


def skill_rel(skill_dir: Path, value: str | Path) -> Path:
    path = Path(value)
    resolved = (path if path.is_absolute() else skill_dir / path).resolve()
    root = skill_dir.resolve()
    if resolved != root and root not in resolved.parents:
        raise FlowError(f"path escapes skill directory: {value}")
    return resolved


def run_rel(run_dir: Path, value: str | Path) -> Path:
    path = Path(value)
    resolved = (path if path.is_absolute() else run_dir / path).resolve()
    root = run_dir.resolve()
    if resolved != root and root not in resolved.parents:
        raise FlowError(f"path escapes run directory: {value}")
    return resolved


def relative_to(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def implementation_files(skill_dir: Path, flow: dict[str, Any]) -> list[Path]:
    files = [flow["_flow_path"]]
    for step in flow["steps"]:
        files.append(skill_rel(skill_dir, step["handler"]))
        files.append(skill_rel(skill_dir, step["input_schema"]))
        files.append(skill_rel(skill_dir, step["output_schema"]))
        if step["model"] != "none" and step.get("draft_schema"):
            files.append(skill_rel(skill_dir, step["draft_schema"]))
    return sorted({path.resolve() for path in files if path.is_file()}, key=lambda item: item.as_posix().lower())


def implementation_lock(skill_dir: Path, flow: dict[str, Any]) -> dict[str, Any]:
    entries = {
        relative_to(skill_dir, path): sha256_file(path)
        for path in implementation_files(skill_dir, flow)
    }
    return {
        "schema": "flowstep_implementation_lock_v2",
        "skill": skill_dir.name,
        "flow_id": flow["flow_id"],
        "flow_version": flow["version"],
        "files": entries,
        "fingerprint_sha256": sha256_bytes(canonical_json(entries)),
    }


def assert_implementation_lock(run_dir: Path, skill_dir: Path, flow: dict[str, Any]) -> dict[str, Any]:
    lock_path = run_dir / "implementation-lock.json"
    if not lock_path.is_file():
        raise FlowError("missing implementation lock")
    frozen = read_json(lock_path)
    current = implementation_lock(skill_dir, flow)
    if frozen.get("fingerprint_sha256") != current["fingerprint_sha256"]:
        raise FlowError("implementation drift detected; start a fresh run")
    return frozen


def validate_against_schema(instance: Any, schema_path: Path) -> None:
    if not schema_path.is_file():
        raise FlowError(f"schema not found: {schema_path}")
    schema = read_json(schema_path)
    resolver = RefResolver(base_uri=schema_path.resolve().as_uri(), referrer=schema)
    errors = sorted(
        Draft202012Validator(schema, resolver=resolver).iter_errors(instance),
        key=lambda item: list(item.path),
    )
    if errors:
        err = errors[0]
        location = "$" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in err.path)
        raise FlowError(f"schema {schema_path.name} failed at {location}: {err.message}")


def envelope_schema_path() -> Path:
    return CONTRACTS_DIR / "flowstep_output_v2.schema.json"


def expected_artifact_path(run_dir: Path, flow: dict[str, Any], step: dict[str, Any]) -> Path:
    return run_dir / flow["artifact_root"] / f"{step['id']}.{step['output_contract']}.json"


def work_dir(run_dir: Path, step_id: str) -> Path:
    return run_dir / "work" / step_id


def load_tool(skill_dir: Path, step: dict[str, Any]) -> Any:
    path = skill_rel(skill_dir, step["handler"])
    if not path.is_file():
        raise FlowError(f"missing tool: {path}")
    spec = importlib.util.spec_from_file_location(f"flowstep_tool_{step['id']}", path)
    if spec is None or spec.loader is None:
        raise FlowError(f"cannot import tool: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "run", None)):
        raise FlowError(f"{path} must define run(input_data, draft=None, **kwargs)")
    return module


def bind_inputs(run_dir: Path, flow: dict[str, Any], step: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    by_id = {item["id"]: item for item in flow["steps"]}
    payload: dict[str, Any] = {}
    bindings: list[dict[str, Any]] = []
    join = step.get("join") or []
    if join:
        for source_id in join:
            if source_id not in by_id:
                raise FlowError(f"{step['id']} join references unknown milestone {source_id}")
            source = by_id[source_id]
            path = expected_artifact_path(run_dir, flow, source)
            if not path.is_file():
                continue
            artifact = read_json(path)
            if artifact.get("status") != "PASS":
                continue
            payload[source_id] = artifact.get("data")
            bindings.append(
                {
                    "input_name": source_id,
                    "source_step_id": source_id,
                    "artifact_path": relative_to(run_dir, path),
                    "artifact_id": artifact.get("artifact_id"),
                    "artifact_sha256": sha256_file(path),
                    "contract": source["output_contract"],
                    "status": "PASS",
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
                    "artifact_path": relative_to(run_dir, path),
                    "artifact_id": "user.request",
                    "artifact_sha256": sha256_file(path),
                    "contract": "user.request",
                    "status": "PASS",
                }
            )
            continue
        if not isinstance(reference, str) or "." not in reference:
            raise FlowError(f"{step['id']}.inputs.{name} must be user.request or <step>.<contract>")
        source_id, contract = reference.split(".", 1)
        if source_id not in by_id:
            raise FlowError(f"{step['id']} input {name} references unknown step {source_id}")
        source = by_id[source_id]
        if source["output_contract"] != contract:
            raise FlowError(f"{step['id']} input {name} contract mismatch: expected {source['output_contract']}")
        path = expected_artifact_path(run_dir, flow, source)
        if not path.is_file():
            raise FlowError(f"missing upstream artifact: {source_id}")
        artifact = read_json(path)
        if artifact.get("status") != "PASS":
            raise FlowError(f"upstream step is not PASS: {source_id}")
        payload[name] = artifact.get("data")
        bindings.append(
            {
                "input_name": name,
                "source_step_id": source_id,
                "artifact_path": relative_to(run_dir, path),
                "artifact_id": artifact.get("artifact_id"),
                "artifact_sha256": sha256_file(path),
                "contract": contract,
                "status": "PASS",
            }
        )
    return payload, bindings


def invoke_tool(
    skill_dir: Path,
    step: dict[str, Any],
    input_data: dict[str, Any],
    draft: dict[str, Any] | None,
    task: dict[str, Any],
) -> dict[str, Any]:
    module = load_tool(skill_dir, step)
    return module.run(input_data, draft=draft, task=task)


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
) -> dict[str, Any]:
    return {
        "schema": step["output_contract"],
        "artifact_id": f"{step['output_contract']}:{run_id}:{step['id']}",
        "run_id": run_id,
        "flow_id": flow["flow_id"],
        "flow_version": flow["version"],
        "step_id": step["id"],
        "status": status,
        "data": data,
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


def _returns_draft(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) and node.id == "draft":
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
        return any(_returns_draft(arg) for arg in node.args) and not node.keywords
    if isinstance(node, ast.Dict):
        return any(key is None and _returns_draft(value) for key, value in zip(node.keys, node.values))
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
    for child in ast.walk(run_fn):
        if isinstance(child, ast.Return) and child.value is not None and _returns_draft(child.value):
            issues.append(f"{step_id}: identity tool (return draft) is forbidden")
            break
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


def lint_file_payload_schema(schema: dict[str, Any], *, label: str) -> list[str]:
    issues: list[str] = []

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            keys = set(properties)
            for key in keys:
                if key == "path" or key.endswith("_path"):
                    hash_key = "sha256" if key == "path" else f"{key[:-5]}_sha256"
                    if "sha256" not in keys and hash_key not in keys:
                        issues.append(f"{label}{path}.{key} needs a sibling sha256 or {hash_key}")
            for key, child in properties.items():
                walk(child, f"{path}.{key}")
        for key in ("items", "additionalProperties"):
            if isinstance(node.get(key), dict):
                walk(node[key], f"{path}.{key}")
        for key in ("allOf", "anyOf", "oneOf"):
            for index, child in enumerate(node.get(key) or []):
                walk(child, f"{path}.{key}[{index}]")
        if "$ref" in node and isinstance(node["$ref"], str) and node["$ref"].endswith("file_ref_v2.schema.json"):
            return

    walk(schema, "")
    return issues


def file_ref(path: str, digest: str, *, content_schema: str | None = None) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise FlowError(f"file_ref sha256 must be 64 lowercase hex: {path}")
    payload = {"path": path, "sha256": digest}
    if content_schema:
        payload["content_schema"] = content_schema
    return payload
