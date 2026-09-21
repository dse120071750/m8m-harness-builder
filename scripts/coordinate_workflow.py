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
from teaching_contracts import write_milestone_gems
from validate_harness import validate_harness


def prepare_workflow(target: Path, codebase: Path, *, flow_id: str | None = None) -> dict[str, Any]:
    """Validate in place, then refresh the flow snapshot and milestone outlines.

    Authored prompts and existing implementations remain intact.
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
    packaged = (target / "m8m-runtime-lock.json").exists()
    prompt_files: list[str] = []
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
        else:
            # An already authored v4 flow remains usable; no forced dialect migration.
            validation = validate_harness(target, update_instruction=False, scope="workflow")
            if target.name != validation["flow_id"] or (flow_id and flow_id != validation["flow_id"]):
                raise FlowError("flow_id must match the harness directory and --flow-id")
            definition = yaml.safe_load(snapshot.read_text(encoding="utf-8-sig"))
        # Installed packages keep their immutable source. Their build path writes
        # these outlines before packaging; coordination never edits their pins.
        if not packaged:
            docs = []
            for milestone in definition["milestones"]:
                item = dict(milestone)
                if (target / "agents" / "openai.yaml").is_file():
                    agent = yaml.safe_load((target / "agents" / f"{item['id']}.yaml").read_text(encoding="utf-8-sig"))
                    item["observer"] = agent.get("observer") or {}
                docs.append(item)
            prompt_files = write_milestone_gems(target, docs)
        if temporary is not None and (not snapshot.is_file() or snapshot.read_bytes() != temporary.read_bytes()):
            os.replace(temporary, snapshot)
            temporary = None
    except (SkillSourceError, OSError) as exc:
        raise FlowError(str(exc)) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    runner = ([sys.executable, str(target / "launch.py")] if packaged else
              [sys.executable, str(Path(__file__).with_name("run_flow.py")),
               "--execution-mode", "coordination", "--skill-dir", str(target)])
    return {
        "status": "PASS", "mode": "coordinate", "flow_id": validation["flow_id"],
        "harness_dir": str(target), "flow_path": str(snapshot),
        "milestones": validation["steps"], "validation_scope": "workflow",
        "updated_milestone_documents": prompt_files,
        "runtime": "existing_packaged_launcher" if packaged else "installed_coordination_runner",
        "run_command": runner,
    }
