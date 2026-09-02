"""Run the deterministic Builder toolbox-construction milestone."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


MILESTONE_ID = "toolbox_ready"
OUTPUT_IDS = ("toolbox_manifest",)
_STEPS: ModuleType | None = None


def _load_builder_steps() -> ModuleType:
    global _STEPS
    if _STEPS is not None:
        return _STEPS
    root = Path(__file__).resolve().parents[3]
    scripts = root / "scripts"
    path = scripts / "m8m_build_steps.py"
    if not path.is_file():
        raise RuntimeError(f"missing deterministic Builder handler: {path}")
    spec = importlib.util.spec_from_file_location("m8m_install_toolbox_steps", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load deterministic Builder handler: {path}")
    module = importlib.util.module_from_spec(spec)
    original_sys_path = list(sys.path)
    try:
        sys.path.insert(0, str(scripts))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_sys_path
    _STEPS = module
    return module


def _context(
    params: dict[str, Any] | None,
    run_dir: Path | str | None,
    task: dict[str, Any] | None,
) -> tuple[Path, dict[str, Any]]:
    if params is not None and not isinstance(params, dict):
        raise TypeError("params must be an object")
    values = params or {}
    resolved_run_dir = run_dir if run_dir is not None else values.get("run_dir")
    resolved_task = task if task is not None else values.get("task")
    if resolved_run_dir is None:
        raise ValueError("install_toolbox requires run_dir")
    if resolved_task is not None and not isinstance(resolved_task, dict):
        raise TypeError("task must be an object")
    effective_task = dict(resolved_task or {})
    declared_step = str(effective_task.get("step_id") or "")
    if declared_step and declared_step != MILESTONE_ID:
        raise ValueError(f"install_toolbox cannot run milestone {declared_step}")
    effective_task["step_id"] = MILESTONE_ID
    return Path(str(resolved_run_dir)).resolve(), effective_task


def run(
    input_data: dict[str, Any],
    params: dict[str, Any] | None = None,
    *,
    run_dir: Path | str | None = None,
    task: dict[str, Any] | None = None,
    draft: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    if not isinstance(input_data, dict):
        raise TypeError("input_data must be an object")
    if draft is not None:
        raise ValueError("install_toolbox does not accept drafts")
    resolved_run_dir, effective_task = _context(params, run_dir, task)
    result = _load_builder_steps().run(
        input_data,
        task=effective_task,
        run_dir=resolved_run_dir,
    )
    outputs = result.get("outputs") if isinstance(result, dict) else None
    if not isinstance(outputs, dict) or set(outputs) != set(OUTPUT_IDS):
        raise RuntimeError("install_toolbox handler returned unexpected output ports")
    return result
