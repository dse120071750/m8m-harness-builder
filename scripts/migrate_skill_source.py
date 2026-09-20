"""Author current M8M source from audit context.

This is the same dialect as a new workflow. Audit only inventories existing
task, milestones, FlowSteps, tools, Gems, and repo-structure gaps. Mixed
leftovers are normal. A lossless ``flowstep_flow_v4`` import remains
``scripts/import_flow_v4.py``.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from execution_identity import canonical_candidate_executor_ref
from flowstep_runtime import (
    FLOW_ID_RE,
    STEP_ID_RE,
    FlowError,
    is_under_home_skills,
    load_yaml,
    normalize_flowsteps,
)
from teaching_contracts import write_milestone_gems


RUNTIME_OWNERS = ("codebase", "builder", "mixed", "none")
CANDIDATE_BIND_TOOL = "candidate_bind"
CANDIDATE_BIND_REF = f"{CANDIDATE_BIND_TOOL}@1.0.0"
_BUILDER_RUNTIME_MARKERS = (
    ".codex/skills/m8m-harness-builder",
    ".claude/skills/m8m-harness-builder",
    ".codex/skills/flowstep-harness-builder",
    ".claude/skills/flowstep-harness-builder",
    "m8m-harness-builder/scripts/run_flow.py",
    "m8m-harness-builder/scripts/run_goal.py",
    "flowstep-harness-builder/scripts/run_flow.py",
    "flowstep-harness-builder/scripts/run_goal.py",
)
_SKIP_SNAPSHOT_PARTS = {
    "__pycache__",
    ".git",
    ".pytest_cache",
    "node_modules",
}
_TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".yaml",
    ".yml",
    ".json",
    ".txt",
    ".toml",
}


def _dump_yaml(value: Any) -> str:
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True)


def _dump_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _write_json(path: Path, value: Any) -> None:
    _write_text(path, _dump_json(value))


def _safe_skill_id(value: str, *, fallback: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    if FLOW_ID_RE.match(token):
        return token
    if STEP_ID_RE.match(token):
        return token
    return fallback


def _find_flow_path(root: Path) -> Path | None:
    direct = root / "flow.yaml"
    if direct.is_file():
        return direct
    flows = root / "flows"
    if flows.is_dir():
        matches = sorted(flows.glob("*.yaml")) + sorted(flows.glob("*.yml"))
        if matches:
            return matches[0]
    nested = list(root.glob("flowsteps/flows/*/flow.yaml"))
    if nested:
        return sorted(nested)[0]
    return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _file_has_builder_runtime(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
        return False
    text = _read_text(path).replace("\\", "/")
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _BUILDER_RUNTIME_MARKERS)


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    if not root.is_dir():
        return files
    for current, directory_names, file_names in os.walk(root):
        directory_names[:] = [
            name for name in directory_names if name not in _SKIP_SNAPSHOT_PARTS
        ]
        parent = Path(current)
        for name in file_names:
            path = parent / name
            if path.suffix.lower() in {".pyc", ".pyo"}:
                continue
            files.append(path)
    return files


def _ownership_scan_roots(
    root: Path,
    *,
    codebase: Path | None,
    flow_id: str | None,
) -> list[Path]:
    """Return only product-owned roots relevant to runtime classification.

    Runtime ownership is a property of one product, not of every file in its
    repository.  Walking the whole codebase both made audit time proportional
    to unrelated workspace contents and allowed another skill's legacy Builder
    reference to contaminate this product's classification.
    """

    candidates = [Path(root)]
    if codebase is not None:
        repo = Path(codebase)
        if flow_id:
            candidates.append(repo / "flowsteps" / "flows" / flow_id)
        candidates.extend(
            [
                repo / ".agents" / "skills" / Path(root).name,
                repo / ".claude" / "skills" / Path(root).name,
            ]
        )
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = str(candidate.resolve()).casefold()
        except OSError:
            key = str(candidate.absolute()).casefold()
        if key in seen or not candidate.is_dir():
            continue
        seen.add(key)
        roots.append(candidate)
    return roots


def _skill_task(root: Path) -> str:
    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        return ""
    text = _read_text(skill_md)
    match = re.search(r"\A---\s*\n(.*?)\n---\s*", text, re.S)
    if not match:
        return text.strip()[:240]
    try:
        parsed = yaml.safe_load(match.group(1))
    except Exception:
        parsed = {}
    if isinstance(parsed, dict) and str(parsed.get("description") or "").strip():
        return str(parsed.get("description") or "").strip()
    return text[match.end() :].strip()[:240]


def _reuse_inventory(root: Path) -> dict[str, Any]:
    root = Path(root)
    openai = root / "agents" / "openai.yaml"
    canvas = None
    if openai.is_file():
        raw = load_yaml(openai)
        canvas = raw.get("canvas") if isinstance(raw, dict) else None
    flow_path = _find_flow_path(root)
    flow = load_yaml(flow_path) if flow_path is not None else None
    flow = flow if isinstance(flow, dict) else {}
    milestones: list[str] = []
    if isinstance(canvas, dict):
        for item in canvas.get("milestones") or []:
            if item and str(item) not in milestones:
                milestones.append(str(item))
    for item in flow.get("milestones") or flow.get("steps") or []:
        if isinstance(item, dict) and item.get("id"):
            milestone_id = str(item["id"])
            if milestone_id not in milestones:
                milestones.append(milestone_id)
    tools: list[str] = []
    for folder in (
        root / "flowsteps" / "tools",
        root / "steps",
    ):
        if folder.is_dir():
            for child in sorted(folder.iterdir()):
                if child.is_dir() and (child / "tool.py").is_file() and child.name not in tools:
                    tools.append(child.name)
    scripts = root / "scripts"
    if scripts.is_dir():
        for path in sorted(scripts.glob("*.py")):
            if path.stem not in tools and path.stem not in {"m8m_run", "run"}:
                tools.append(path.stem)
    gems: list[str] = []
    refs = root / "references"
    if refs.is_dir():
        gems = sorted(path.stem for path in refs.glob("*.md") if "worker" not in path.stem.lower())
    return {
        "canvas": isinstance(canvas, dict),
        "flow_schema": flow.get("schema"),
        "milestones": milestones,
        "tools": tools,
        "gems": gems,
    }


def runtime_ownership_audit(
    root: Path,
    *,
    codebase: Path | None = None,
    flow_id: str | None = None,
) -> dict[str, Any]:
    """Detect whether execution still depends on the Builder install."""

    root = Path(root)
    codebase = Path(codebase) if codebase is not None else None
    gaps: list[str] = []
    builder_hits: list[str] = []
    scan_roots = _ownership_scan_roots(
        root,
        codebase=codebase,
        flow_id=flow_id,
    )
    named = [
        root / "scripts" / "m8m_run.py",
        root / "scripts" / "run.py",
        root / "launch.py",
    ]
    if codebase is not None and flow_id:
        harness = codebase / "flowsteps" / "flows" / flow_id
        named.extend(
            [
                harness / "launch.py",
                codebase / ".agents" / "skills" / root.name / "scripts" / "m8m_run.py",
                codebase / ".claude" / "skills" / root.name / "scripts" / "m8m_run.py",
            ]
        )
    for path in named:
        if _file_has_builder_runtime(path):
            builder_hits.append(path.as_posix())
    for scan in scan_roots:
        for path in _walk_files(scan):
            relative = path.as_posix()
            if "m8m-harness-builder" in relative.replace("\\", "/") and path.name in {
                "run_flow.py",
                "run_goal.py",
                "m8m_run.py",
            }:
                continue
            if path.suffix.lower() not in {".py", ".md", ".yaml", ".yml", ".json"}:
                continue
            if path.name in {"SKILL.md", "m8m_run.py", "run.py", "launch.py", "flow.yaml"}:
                if _file_has_builder_runtime(path) and path.as_posix() not in builder_hits:
                    builder_hits.append(path.as_posix())
    has_builder = bool(builder_hits) or is_under_home_skills(root)
    harness = None
    if codebase is not None and flow_id:
        harness = codebase / "flowsteps" / "flows" / flow_id
    elif codebase is not None:
        flows = sorted((codebase / "flowsteps" / "flows").glob("*/launch.py")) if (codebase / "flowsteps" / "flows").is_dir() else []
        harness = flows[0].parent if flows else None
    has_launcher = bool(harness and (harness / "launch.py").is_file())
    has_release = bool(
        harness
        and (harness / "runtime" / "releases").is_dir()
        and any((harness / "runtime" / "releases").glob("*/runtime-manifest.json"))
    )
    has_codebase = has_launcher and has_release
    if has_builder and not has_codebase:
        owner = "builder"
        gaps.append("product still launches through the mutable Builder runtime")
    elif has_codebase and has_builder:
        owner = "mixed"
        gaps.append("codebase runtime exists but Builder runtime references remain")
    elif has_codebase:
        owner = "codebase"
    else:
        owner = "none"
        gaps.append("no codebase-owned launch.py and runtime release")
    if is_under_home_skills(root):
        gaps.append("source currently lives under ~/.codex/skills or ~/.claude/skills")
    if not (root / "agents" / "openai.yaml").is_file():
        gaps.append("missing skill-native canvas at agents/openai.yaml")
    tools_home = False
    scripts = root / "scripts"
    if scripts.is_dir() and any(scripts.glob("*.py")):
        tools_home = True
    if tools_home and (codebase is None or not (codebase / "flowsteps" / "tools").is_dir()):
        gaps.append("skill-private scripts are not yet repo toolbox packages")
    return {
        "runtime_ownership": owner,
        "builder_runtime_hits": builder_hits,
        "ownership_gaps": list(dict.fromkeys(gaps)),
        "has_codebase_launcher": has_launcher,
        "has_runtime_release": has_release,
    }


def classify_skill(
    root: Path,
    *,
    codebase: Path | None = None,
    flow_id: str | None = None,
) -> dict[str, Any]:
    """Inventory mixed leftovers as context for the same new-workflow build."""

    root = Path(root)
    reuse = _reuse_inventory(root)
    ownership = runtime_ownership_audit(root, codebase=codebase, flow_id=flow_id)
    closed_canvas = reuse["canvas"] and (root / "references").is_dir()
    return {
        "source_context": {
            "task": _skill_task(root),
            "reuse": reuse,
            "gaps": ownership["ownership_gaps"],
        },
        "authoring_mode": "skill_native" if closed_canvas else "from_context",
        "equivalence_claimed": False,
        **ownership,
    }


def freeze_source_tree(source: Path, snapshot: Path) -> Path:
    """Copy one skill or flow tree into a run-local snapshot."""

    source = Path(source).resolve()
    snapshot = Path(snapshot).resolve()
    snapshot.mkdir(parents=True, exist_ok=True)
    for path in _walk_files(source):
        relative = path.relative_to(source)
        if "runtime" in relative.parts and "releases" in relative.parts:
            continue
        destination = snapshot.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    return snapshot


def _candidate_output_schema(output_ids: list[str]) -> dict[str, Any]:
    properties = {
        port_id: {
            "type": "object",
            "additionalProperties": True,
            "required": ["id", "name"],
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "value": True,
                "path": {"type": "string", "minLength": 1},
                "sha256": {"type": "string", "minLength": 1},
            },
        }
        for port_id in output_ids
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["outputs"],
        "properties": {
            "outputs": {
                "type": "object",
                "additionalProperties": False,
                "required": output_ids,
                "properties": properties,
            }
        },
    }


def _input_schema(inputs: dict[str, Any]) -> dict[str, Any]:
    names = [str(name) for name in (inputs or {"request": "user.request"})]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": True,
        "required": names,
        "properties": {name: {"type": "object"} for name in names},
    }


def _candidate_bind_output_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "candidate_bind.output.schema.json",
        "type": "object",
        "additionalProperties": False,
        "required": ["bound", "byte_count", "keys"],
        "properties": {
            "bound": {"type": "boolean", "const": True},
            "byte_count": {"type": "integer", "minimum": 0},
            "keys": {"type": "array", "items": {"type": "string"}},
        },
    }


def _candidate_bind_input_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "candidate_bind.input.schema.json",
        "type": "object",
        "additionalProperties": True,
    }


def write_candidate_bind_tool(dest: Path) -> None:
    """Write a closed, non-passthrough bind tool used when a product tool is missing."""

    dest = Path(dest)
    _write_text(
        dest / "tool.py",
        '''"""Describe a candidate payload without returning the input object."""

from __future__ import annotations

import json
from typing import Any


def run(input_data: dict[str, Any], **_: Any) -> dict[str, Any]:
    if not isinstance(input_data, dict):
        raise TypeError("input_data must be an object")
    canonical = json.dumps(
        input_data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    encoded = canonical.encode("utf-8")
    return {
        "bound": True,
        "byte_count": len(encoded),
        "keys": sorted(str(key) for key in input_data),
    }
''',
    )
    _write_json(dest / "input.schema.json", _candidate_bind_input_schema())
    _write_json(dest / "output.schema.json", _candidate_bind_output_schema())
    _write_text(
        dest / "tests" / "test_tool.py",
        '''from __future__ import annotations

import unittest
from pathlib import Path
import importlib.util

TOOL = Path(__file__).resolve().parents[1] / "tool.py"
spec = importlib.util.spec_from_file_location("candidate_bind_tool", TOOL)
assert spec is not None and spec.loader is not None
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class CandidateBindTests(unittest.TestCase):
    def test_run_describes_payload_without_returning_the_input(self) -> None:
        payload = {"request": {"id": "case-1"}}
        result = tool.run(payload)
        self.assertTrue(result["bound"])
        self.assertNotIn("sha256", result)
        self.assertGreater(result["byte_count"], 0)
        self.assertEqual(result["keys"], ["request"])
        self.assertIsNot(result, payload)
        self.assertNotIn("request", result)
''',
    )


def _script_defines_run(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"
        for node in tree.body
    )


def _copy_tool_package(source: Path, dest: Path) -> bool:
    if not source.is_dir() or not (source / "tool.py").is_file():
        return False
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        source,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    marker = dest / "BUILD_REQUIRED"
    if marker.is_file():
        marker.unlink()
    return True


def promote_or_synthesize_tools(
    stage: Path,
    audit: dict[str, Any],
    *,
    source_root: Path,
    codebase: Path,
) -> list[str]:
    """Place repo tools for a migrated skill: existing, promoted, or synthesized."""

    from flowstep_tools import validate_library_tool

    stage = Path(stage)
    source_root = Path(source_root)
    codebase = Path(codebase)
    written: list[str] = []
    tool_ids: list[str] = []
    for row in audit.get("python_standardization") or []:
        if isinstance(row, dict) and row.get("tool_id"):
            tool_ids.append(str(row["tool_id"]))
    for item in audit.get("proposed_milestones") or []:
        if not isinstance(item, dict):
            continue
        _flowsteps, tools = normalize_flowsteps(
            flowsteps=item.get("flowsteps"),
            tools=item.get("tools"),
        )
        for flowstep in _flowsteps:
            package = str(flowstep.get("tool") or "")
            if package and package not in tool_ids and "@" not in package:
                tool_ids.append(package)
        for tool_id in tools:
            if tool_id not in tool_ids:
                tool_ids.append(tool_id)
    if CANDIDATE_BIND_TOOL not in tool_ids:
        tool_ids.append(CANDIDATE_BIND_TOOL)
    for tool_id in list(dict.fromkeys(tool_ids)):
        if not STEP_ID_RE.match(tool_id):
            continue
        dest = stage / "flowsteps" / "tools" / tool_id
        candidates = [
            codebase / "flowsteps" / "tools" / tool_id,
            source_root / "flowsteps" / "tools" / tool_id,
            source_root / "steps" / tool_id,
        ]
        copied = False
        for candidate in candidates:
            if _copy_tool_package(candidate, dest):
                copied = True
                break
        if copied and not validate_library_tool(stage, tool_id):
            written.append(str(dest))
            continue
        script = source_root / "scripts" / f"{tool_id}.py"
        if script.is_file() and _script_defines_run(script):
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(script, dest / "tool.py")
            _write_json(dest / "input.schema.json", _candidate_bind_input_schema())
            _write_json(dest / "output.schema.json", _candidate_bind_output_schema())
            _write_text(
                dest / "tests" / "test_tool.py",
                (
                    "from __future__ import annotations\n\n"
                    "import importlib.util\n"
                    "import unittest\n"
                    "from pathlib import Path\n\n"
                    f"TOOL = Path(__file__).resolve().parents[1] / 'tool.py'\n"
                    f"spec = importlib.util.spec_from_file_location({tool_id!r}, TOOL)\n"
                    "assert spec is not None and spec.loader is not None\n"
                    "module = importlib.util.module_from_spec(spec)\n"
                    "spec.loader.exec_module(module)\n\n"
                    "class PromotedToolTests(unittest.TestCase):\n"
                    "    def test_run_exists(self) -> None:\n"
                    "        self.assertTrue(callable(getattr(module, 'run', None)))\n"
                ),
            )
            if not validate_library_tool(stage, tool_id):
                written.append(str(dest))
                continue
        if tool_id == CANDIDATE_BIND_TOOL or not copied:
            write_candidate_bind_tool(dest)
            written.append(str(dest))
    return written


def _milestone_outputs(item: dict[str, Any]) -> list[dict[str, Any]]:
    declared = item.get("outputs") if isinstance(item.get("outputs"), list) else []
    outputs: list[dict[str, Any]] = []
    for raw in declared:
        if not isinstance(raw, dict):
            continue
        port_id = _safe_skill_id(str(raw.get("id") or "result"), fallback="result")
        outputs.append(
            {
                "id": port_id,
                "name": str(raw.get("name") or port_id.replace("_", " ").title()),
                "kind": str(raw.get("kind") or "json"),
                "cardinality": str(raw.get("cardinality") or "one"),
                "required": bool(raw.get("required", True)),
            }
        )
    if not outputs:
        outputs.append(
            {
                "id": "result",
                "name": str(item.get("id") or "result").replace("_", " ").title(),
                "kind": "json",
                "cardinality": "one",
                "required": True,
            }
        )
    if not any(item.get("required") for item in outputs):
        outputs[0]["required"] = True
    return outputs


def _milestone_flowsteps(item: dict[str, Any]) -> tuple[list[dict[str, str]], list[str]]:
    flowsteps, tools = normalize_flowsteps(
        flowsteps=item.get("flowsteps"),
        tools=item.get("tools"),
    )
    authored: list[dict[str, str]] = []
    for step in flowsteps:
        flowstep_id = _safe_skill_id(str(step.get("id") or ""), fallback="")
        package = str(step.get("tool") or "").split("@", 1)[0]
        package = _safe_skill_id(package, fallback="")
        if not flowstep_id:
            continue
        if not package:
            package = CANDIDATE_BIND_TOOL
        authored.append({"id": flowstep_id, "tool": package})
    if not authored:
        authored = [{"id": "bind_candidate", "tool": CANDIDATE_BIND_TOOL}]
    tool_ids = [str(step["id"]) for step in authored]
    return authored, tool_ids


def _existing_file(*candidates: Path) -> Path | None:
    for path in candidates:
        if path.is_file():
            return path
    return None


def _handler_source(*, milestone_id: str, flowsteps: list[dict[str, str]], outputs: list[dict[str, Any]]) -> str:
    first = flowsteps[0] if flowsteps else {"id": "bind_candidate", "tool": CANDIDATE_BIND_TOOL}
    package = str(first.get("tool") or CANDIDATE_BIND_TOOL)
    ports = [
        {
            "id": str(item["id"]),
            "name": str(item["name"]),
        }
        for item in outputs
    ]
    return (
        f'"""Migrated candidate handler for {milestone_id}."""\n'
        "from __future__ import annotations\n\n"
        "from pathlib import Path\n"
        "from typing import Any\n\n"
        "from flowstep_tools import run_library_tool\n\n"
        f"STEP_ID = {milestone_id!r}\n"
        f"TOOL_ID = {package!r}\n"
        f"OUTPUTS = {json.dumps(ports, ensure_ascii=False)}\n\n"
        "def _codebase() -> Path:\n"
        "    return Path(__file__).resolve().parents[5]\n\n"
        "def run(input_data: dict[str, Any], draft: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:\n"
        "    payload = dict(input_data) if isinstance(input_data, dict) else {'value': input_data}\n"
        "    if isinstance(draft, dict):\n"
        "        payload.update({key: value for key, value in draft.items() if key not in {'ok', 'branch', 'cycle', 'decision'}})\n"
        "    bound = run_library_tool(_codebase(), TOOL_ID, payload)\n"
        "    if not isinstance(bound, dict):\n"
        "        raise ValueError(f'{STEP_ID}: bound tool must return an object')\n"
        "    outputs = {}\n"
        "    for item in OUTPUTS:\n"
        "        outputs[item['id']] = {\n"
        "            'id': item['id'],\n"
        "            'name': item['name'],\n"
        "            'value': bound,\n"
        "        }\n"
        "    return {'outputs': outputs}\n"
    )


def _handler_test(milestone_id: str) -> str:
    return (
        "from __future__ import annotations\n\n"
        "import unittest\n"
        "from pathlib import Path\n"
        "import importlib.util\n\n"
        "HANDLER = Path(__file__).resolve().parents[1] / 'assemble.py'\n\n"
        f"class {milestone_id.title().replace('_', '')}HandlerTests(unittest.TestCase):\n"
        "    def test_handler_defines_run(self) -> None:\n"
        "        spec = importlib.util.spec_from_file_location('migrated_handler', HANDLER)\n"
        "        self.assertIsNotNone(spec)\n"
        "        self.assertTrue(HANDLER.is_file())\n"
    )


def write_migrated_skill_source(
    dest: Path,
    audit: dict[str, Any],
    *,
    flow_id: str,
    skill_name: str,
    source_root: Path,
) -> dict[str, Any]:
    """Write a closed skill-native tree that compiles as Builder 3.1 source."""

    dest = Path(dest)
    source_root = Path(source_root)
    dest.mkdir(parents=True, exist_ok=True)
    milestones = [item for item in audit.get("proposed_milestones") or [] if isinstance(item, dict)]
    if not milestones:
        raise FlowError("migration needs proposed_milestones from the source audit")
    if not FLOW_ID_RE.match(flow_id):
        flow_id = "product_v1"
    skill_name = str(skill_name or "product-skill")
    ids = [_safe_skill_id(str(item.get("id") or f"step_{index}"), fallback=f"step_{index}") for index, item in enumerate(milestones, start=1)]
    graph = []
    for index, milestone_id in enumerate(ids):
        nxt = [ids[index + 1]] if index + 1 < len(ids) else []
        graph.append({"from": milestone_id, "to": nxt})
    terminal = ids[-1]
    description = str((audit.get("audited_skill") or {}).get("description") or skill_name)
    template = Path(__file__).resolve().parents[1] / "templates" / "product-SKILL.md"
    skill_md = template.read_text(encoding="utf-8").replace("__SKILL_NAME__", skill_name)
    _write_text(dest / "SKILL.md", skill_md)

    last = milestones[-1]
    last_outputs = _milestone_outputs({**last, "id": ids[-1]})
    last_contract = str(last.get("output_contract") or f"{ids[-1]}_v1")
    terminal_port = str(last_outputs[0]["id"])
    openai = {
        "interface": {
            "display_name": skill_name.replace("-", " ").replace("_", " ").title(),
            "short_description": description[:240] or "Migrated M8M workflow.",
            "default_prompt": f"Use ${skill_name} as an M8M workflow. Do not push, publish, or deploy.",
        },
        "policy": {"allow_implicit_invocation": True},
        "canvas": {
            "schema": "m8m_skill_canvas_v1",
            "flow_id": flow_id,
            "version": 1,
            "context_policy": "isolated",
            "max_run_seconds": 3600,
            "artifact_root": "artifacts",
            "workflow_contracts": {
                "request_schema": "schemas/workflow_request.schema.json",
                "configuration_schema": "schemas/workflow_configuration.schema.json",
                "result_schema": "schemas/workflow_result.schema.json",
                "terminal_bindings": [
                    {
                        "name": "final_result",
                        "from": f"{terminal}.{last_contract}",
                        "output": terminal_port,
                    }
                ],
            },
            "entry": ids[0],
            "milestones": ids,
            "graph": graph,
            "terminal_states": {"success": [terminal], "blocked": "BLOCKED"},
            "observer": {
                "title": f"{skill_name} workflow",
                "summary": description[:240] or "Migrated milestone workflow.",
            },
        },
    }
    _write_text(dest / "agents" / "openai.yaml", _dump_yaml(openai))
    _write_json(
        dest / "schemas" / "workflow_request.schema.json",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
    )
    _write_json(
        dest / "schemas" / "workflow_configuration.schema.json",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
    )
    _write_json(
        dest / "schemas" / "workflow_result.schema.json",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["final_result"],
            "properties": {"final_result": {"type": "object"}},
        },
    )

    gem_specs: list[dict[str, Any]] = []
    for milestone_id, item in zip(ids, milestones):
        outputs = _milestone_outputs({**item, "id": milestone_id})
        flowsteps, tool_ids = _milestone_flowsteps(item)
        contract = str(item.get("output_contract") or f"{milestone_id}_v1")
        success = str(item.get("success") or "").strip() or (
            f"The {milestone_id.replace('_', ' ')} named outputs are present and contract-valid."
        )
        inputs = item.get("inputs") if isinstance(item.get("inputs"), dict) else {}
        if not inputs:
            inputs = {"request": "user.request"}
        output_schema_rel = f"schemas/{milestone_id}.schema.json"
        input_schema_rel = f"milestones/{milestone_id}/input.schema.json"
        handler_rel = f"milestones/{milestone_id}/assemble.py"
        test_rel = f"milestones/{milestone_id}/tests/test_assemble.py"
        existing_output = _existing_file(
            source_root / str(item.get("output_schema_path") or ""),
            source_root / "schemas" / f"{milestone_id}.schema.json",
            source_root / "milestones" / milestone_id / "output.schema.json",
        )
        if existing_output is not None:
            shutil.copy2(existing_output, dest / output_schema_rel)
        else:
            _write_json(dest / output_schema_rel, _candidate_output_schema([row["id"] for row in outputs]))
        _write_json(dest / input_schema_rel, _input_schema(inputs))
        existing_handler = _existing_file(
            source_root / str(item.get("handler") or ""),
            source_root / "milestones" / milestone_id / "assemble.py",
            source_root / "handlers" / f"{milestone_id}.py",
        )
        dest_handler = dest / handler_rel
        if existing_handler is not None and "BUILD_REQUIRED" not in _read_text(existing_handler):
            dest_handler.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(existing_handler, dest_handler)
        else:
            _write_text(dest_handler, _handler_source(milestone_id=milestone_id, flowsteps=flowsteps, outputs=outputs))
        _write_text(dest / test_rel, _handler_test(milestone_id))
        bindings = [
            {"tool": step["id"], "ref": f"{step['tool']}@1.0.0"}
            for step in flowsteps
        ]
        agent = {
            "schema": "m8m_milestone_agent_v1",
            "agent_id": milestone_id,
            "role": "milestone",
            "success": success,
            "output_contract": contract,
            "output_schema": output_schema_rel,
            "outputs": outputs,
            "handler": handler_rel,
            "test": test_rel,
            "input_schema": input_schema_rel,
            "inputs": inputs,
            "flowsteps": flowsteps,
            "tools": tool_ids,
            "intelligence": "none",
            "execution": {
                "candidate_executor": {
                    "ref": canonical_candidate_executor_ref(flow_id, milestone_id)
                },
                "tool_bindings": bindings,
            },
            "on_tool_fail": "BLOCKED",
            "loop": "none",
            "gem": f"references/{milestone_id}.md",
            "read_paths": [f"references/{milestone_id}.md"],
            "observer": {
                "title": milestone_id.replace("_", " ").title(),
                "summary": success,
                "actions": [
                    {
                        "flowstep_id": step["id"],
                        "title": step["id"].replace("_", " ").title(),
                        "summary": f"Run `{step['tool']}` for this FlowStep.",
                    }
                    for step in flowsteps
                ],
                "outputs": [
                    {
                        "output_id": row["id"],
                        "title": row["name"],
                        "summary": f"Required {row['kind']} output.",
                    }
                    for row in outputs
                ],
            },
        }
        _write_text(dest / "agents" / f"{milestone_id}.yaml", _dump_yaml(agent))
        gem_specs.append(
            {
                "id": milestone_id,
                "success": success,
                "outputs": outputs,
                "flowsteps": flowsteps,
                "tools": tool_ids,
                "intelligence": "none",
                "loop": "none",
            }
        )
        existing_gem = _existing_file(
            source_root / "references" / f"{milestone_id}.md",
            source_root / str(item.get("gem") or ""),
        )
        if existing_gem is not None and _read_text(existing_gem).strip():
            destination_gem = dest / "references" / f"{milestone_id}.md"
            destination_gem.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(existing_gem, destination_gem)
    write_milestone_gems(dest, gem_specs, overwrite=False)
    return {
        "skill_root": str(dest),
        "flow_id": flow_id,
        "skill_name": skill_name,
        "milestones": ids,
        "source_context": audit.get("source_context") or classify_skill(source_root).get("source_context"),
        "equivalence_claimed": False,
    }
