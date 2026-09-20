"""Compile workflow edits against existing tools without creating a runtime release."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from flowstep_runtime import FlowError
from flowstep_tools import infer_codebase
from skill_source import SkillSourceError, compile_skill_source
from validate_harness import validate_harness


def prepare_workflow(target: Path, codebase: Path, *, flow_id: str | None = None) -> dict[str, Any]:
    """Validate in place, then replace only the generated flow snapshot.

    Source and existing implementations stay where their owners maintain them.
    A temporary sibling allows validation before changing the runnable snapshot.
    Packaging and provider operations are deliberately absent from this path.
    """
    target, codebase = target.resolve(), codebase.resolve()
    if infer_codebase(target) != codebase:
        raise FlowError(
            "coordinate --target must be the authored harness at "
            "<codebase>/flowsteps/flows/<flow_id>; keep existing tools in place "
            "and add only the required workflow bindings there"
        )
    temporary: Path | None = None
    snapshot = target / "flow.yaml"
    try:
        if (target / "agents" / "openai.yaml").is_file():
            definition = compile_skill_source(target, check_snapshot=False)
            if target.name != definition["flow_id"] or (flow_id and flow_id != definition["flow_id"]):
                raise FlowError("authored flow_id must match the harness directory and --flow-id")
            payload = yaml.safe_dump(definition, sort_keys=False, allow_unicode=True).encode("utf-8")
            if snapshot.is_symlink():
                raise FlowError("generated flow.yaml must not be a symbolic link")
            with tempfile.NamedTemporaryFile(dir=target, prefix=".flow-", suffix=".yaml", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
            validation = validate_harness(target, str(temporary), update_instruction=False, scope="workflow")
            if not snapshot.is_file() or snapshot.read_bytes() != payload:
                os.replace(temporary, snapshot)
                temporary = None
        else:
            # An already authored v4 flow remains usable; no forced dialect migration.
            validation = validate_harness(target, update_instruction=False, scope="workflow")
            if target.name != validation["flow_id"] or (flow_id and flow_id != validation["flow_id"]):
                raise FlowError("flow_id must match the harness directory and --flow-id")
    except (SkillSourceError, OSError) as exc:
        raise FlowError(str(exc)) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    packaged = (target / "m8m-runtime-lock.json").exists()
    runner = ([sys.executable, str(target / "launch.py")] if packaged else
              [sys.executable, str(Path(__file__).with_name("run_flow.py")),
               "--execution-mode", "coordination", "--skill-dir", str(target)])
    return {
        "status": "PASS", "mode": "coordinate", "flow_id": validation["flow_id"],
        "harness_dir": str(target), "flow_path": str(snapshot),
        "milestones": validation["steps"], "validation_scope": "workflow",
        "runtime": "existing_packaged_launcher" if packaged else "installed_coordination_runner",
        "run_command": runner,
    }
