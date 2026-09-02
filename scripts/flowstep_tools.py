"""Load and run pre-made toolbox functions from <repo>/flowsteps/tools/."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any


def _builder_runtime():
    """Load this skill's runtime by path so article scripts/flowstep_runtime.py cannot shadow it."""
    path = Path(os.path.abspath(__file__)).parent / "flowstep_runtime.py"
    public = sys.modules.get("flowstep_runtime")
    public_path = Path(str(getattr(public, "__file__", ""))).resolve() if public is not None else None
    if public is not None and public_path == path.resolve():
        sys.modules.setdefault("flowstep_local_runtime", public)
        return public
    name = "flowstep_runtime" if public is None else "flowstep_local_runtime"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import builder runtime: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.modules.setdefault("flowstep_local_runtime", module)
    spec.loader.exec_module(module)
    return module


_rt = _builder_runtime()
FLOWSTEPS_DIRNAME = _rt.FLOWSTEPS_DIRNAME
FlowError = _rt.FlowError
inspect_step_test = _rt.inspect_step_test
inspect_tool_source = _rt.inspect_tool_source
is_stub_output_schema = _rt.is_stub_output_schema
lint_file_payload_schema = _rt.lint_file_payload_schema
read_json = _rt.read_json
validate_against_schema = _rt.validate_against_schema


_TOOL_CODE_CACHE: dict[str, Any] = {}


def tools_root(codebase: Path) -> Path:
    return Path(os.path.abspath(str(codebase))) / FLOWSTEPS_DIRNAME / "tools"


def infer_codebase(harness_dir: Path) -> Path | None:
    harness_dir = Path(os.path.abspath(str(harness_dir)))
    if harness_dir.parent.name == "flows" and harness_dir.parent.parent.name == FLOWSTEPS_DIRNAME:
        return harness_dir.parent.parent.parent
    return None


def tool_dir(codebase: Path, tool_id: str) -> Path:
    return tools_root(codebase) / tool_id


def load_library_tool(codebase: Path, tool_id: str) -> Any:
    root = tool_dir(codebase, tool_id)
    marker = root / "BUILD_REQUIRED"
    if marker.is_file():
        raise FlowError(f"{tool_id} is BUILD_REQUIRED and non-runnable")
    path = root / "tool.py"
    spec = importlib.util.spec_from_file_location(f"flowstep_lib_{tool_id}", path)
    if spec is None or spec.loader is None:
        raise FlowError(f"cannot import tool: {path}")
    module = importlib.util.module_from_spec(spec)
    cache_key = str(path)
    compiled = _TOOL_CODE_CACHE.get(cache_key)
    if compiled is None:
        try:
            compiled = compile(path.read_bytes(), str(path), "exec")
        except FileNotFoundError as exc:
            raise FlowError(f"missing toolbox tool: {path}") from exc
        _TOOL_CODE_CACHE[cache_key] = compiled
    original_sys_path = list(sys.path)
    try:
        exec(compiled, module.__dict__)
    finally:
        sys.path[:] = original_sys_path
    if not callable(getattr(module, "run", None)):
        raise FlowError(f"{path} must define run(input_data, **kwargs)")
    if getattr(module, "M8M_BUILD_STATUS", None) == "BUILD_REQUIRED" or getattr(
        module, "M8M_RUNNABLE", None
    ) is False:
        raise FlowError(f"{tool_id} declares BUILD_REQUIRED/non-runnable")
    return module


def run_library_tool(
    codebase: Path,
    tool_id: str,
    input_data: dict[str, Any],
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    module = load_library_tool(codebase, tool_id)
    input_schema = tool_dir(codebase, tool_id) / "input.schema.json"
    output_schema = tool_dir(codebase, tool_id) / "output.schema.json"
    validate_against_schema(input_data, input_schema)
    result = module.run(input_data, params=params or {})
    if not isinstance(result, dict):
        raise FlowError(f"{tool_id} must return a JSON object")
    validate_against_schema(result, output_schema)
    return result


def validate_library_tool(codebase: Path, tool_id: str) -> list[str]:
    errors: list[str] = []
    root = tool_dir(codebase, tool_id)
    if (root / "BUILD_REQUIRED").is_file():
        errors.append(f"{tool_id}: BUILD_REQUIRED marker is present")
    contents: dict[str, bytes] = {}
    for name in ("tool.py", "input.schema.json", "output.schema.json", "tests/test_tool.py"):
        try:
            contents[name] = (root / name).read_bytes()
        except FileNotFoundError:
            errors.append(f"{tool_id}: missing {name}")
    tool_py = root / "tool.py"
    if "tool.py" in contents:
        source = contents["tool.py"].decode("utf-8")
        errors.extend(
            inspect_tool_source(source, step_id=tool_id, model="none")
        )
        try:
            _TOOL_CODE_CACHE[str(tool_py)] = compile(source, str(tool_py), "exec")
        except (SyntaxError, UnicodeDecodeError) as exc:
            errors.append(str(exc))
    test_py = root / "tests" / "test_tool.py"
    if "tests/test_tool.py" in contents:
        errors.extend(
            inspect_step_test(
                contents["tests/test_tool.py"].decode("utf-8"), step_id=tool_id
            )
        )
    output_schema = root / "output.schema.json"
    if "output.schema.json" in contents:
        schema = __import__("json").loads(contents["output.schema.json"].decode("utf-8"))
        if is_stub_output_schema(schema):
            errors.append(f"{tool_id}: output schema is still the generated {{ok: boolean}} stub")
        errors.extend(lint_file_payload_schema(schema, label=f"{tool_id}.output.schema.json"))
    if "input.schema.json" in contents:
        __import__("json").loads(contents["input.schema.json"].decode("utf-8"))
    return errors
