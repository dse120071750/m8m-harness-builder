"""Launch the builder's canonical v4 M8M workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from flowstep_runtime import FlowError
from m8m_factory import run_factory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True, help="Skill directory to turn into a milestone flow.")
    parser.add_argument("--codebase", type=Path, required=True, help="Project repo. Tools and flow are written here.")
    parser.add_argument("--flow-id")
    parser.add_argument("--skill-name")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--run-dir", type=Path, help="Resume one exact builder session")
    parser.add_argument("--replace-milestone", help="Replace one chosen builder milestone and its dependents")
    parser.add_argument(
        "--continue-after-edit",
        help="Adopt builder workflow edits and continue from this milestone using the same run-local assets",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_factory(
            args.target,
            args.codebase,
            flow_id=args.flow_id,
            skill_name=args.skill_name,
            overwrite=args.force,
            run_dir=args.run_dir,
            replace_milestone=args.replace_milestone,
            continue_after_edit=args.continue_after_edit,
        )
    except FlowError as exc:
        print(json.dumps({"status": "BLOCKED", "blockers": [str(exc)]}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result["status"] == "PASS":
        return 0
    return 3 if result["status"] == "FINDINGS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
