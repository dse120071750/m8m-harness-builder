"""Delegate to flowstep-harness-builder so this skill does not fork the driver."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]


def _builder_run_flow() -> Path:
    env = os.environ.get("FLOWSTEP_BUILDER")
    if env:
        candidate = Path(env) / "scripts" / "run_flow.py"
        if candidate.is_file():
            return candidate
    sibling = Path(__file__).resolve().parents[2] / "flowstep-harness-builder" / "scripts" / "run_flow.py"
    if sibling.is_file():
        return sibling
    default = Path(r"__BUILDER_ROOT__") / "scripts" / "run_flow.py"
    if default.is_file():
        return default
    raise SystemExit("Cannot find flowstep-harness-builder. Set FLOWSTEP_BUILDER.")


def main() -> None:
    script = _builder_run_flow()
    forwarded: list[str] = []
    argv = sys.argv[1:]
    if "--skill-dir" not in argv:
        forwarded.extend(["--skill-dir", str(SKILL_DIR)])
    forwarded.extend(argv)
    sys.argv = [str(script), *forwarded]
    sys.path.insert(0, str(script.parent))
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
