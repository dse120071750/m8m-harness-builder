"""Load and run pre-made toolbox functions from <repo>/flowsteps/tools/."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def _builder_runtime():
    """Load this skill's runtime by path so article scripts/flowstep_runtime.py cannot shadow it."""
    name = "flowstep_builder_runtime"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent / "flowstep_runtime.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import builder runtime: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
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


def tools_root(codebase: Path) -> Path:
    return Path(codebase).resolve() / FLOWSTEPS_DIRNAME / "tools"


def infer_codebase(harness_dir: Path) -> Path | None:
    harness_dir = harness_dir.resolve()
    if harness_dir.parent.name == "flows" and harness_dir.parent.parent.name == FLOWSTEPS_DIRNAME:
        return harness_dir.parent.parent.parent
    return None


def tool_dir(codebase: Path, tool_id: str) -> Path:
    return tools_root(codebase) / tool_id


def load_library_tool(codebase: Path, tool_id: str) -> Any:
    path = tool_dir(codebase, tool_id) / "tool.py"
    if not path.is_file():
        raise FlowError(f"missing toolbox tool: {path}")
    spec = importlib.util.spec_from_file_location(f"flowstep_lib_{tool_id}", path)
    if spec is None or spec.loader is None:
        raise FlowError(f"cannot import tool: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "run", None)):
        raise FlowError(f"{path} must define run(input_data, **kwargs)")
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
    for name in ("tool.py", "input.schema.json", "output.schema.json", "tests/test_tool.py"):
        if not (root / name).is_file():
            errors.append(f"{tool_id}: missing {name}")
    tool_py = root / "tool.py"
    if tool_py.is_file():
        errors.extend(
            inspect_tool_source(tool_py.read_text(encoding="utf-8"), step_id=tool_id, model="none")
        )
        try:
            load_library_tool(codebase, tool_id)
        except FlowError as exc:
            errors.append(str(exc))
    test_py = root / "tests" / "test_tool.py"
    if test_py.is_file():
        errors.extend(inspect_step_test(test_py.read_text(encoding="utf-8"), step_id=tool_id))
    output_schema = root / "output.schema.json"
    if output_schema.is_file():
        schema = read_json(output_schema)
        if is_stub_output_schema(schema):
            errors.append(f"{tool_id}: output schema is still the generated {{ok: boolean}} stub")
        errors.extend(lint_file_payload_schema(schema, label=f"{tool_id}.output.schema.json"))
    input_schema = root / "input.schema.json"
    if input_schema.is_file():
        read_json(input_schema)
    return errors
