"""Launch the Builder 3 canonical v4 M8M workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from flowstep_runtime import FlowError
from m8m_factory import BUILDER_FLOW, BUILDER_FLOW_ID, run_factory


def _assert_builder3_run(run_dir: Path | None) -> None:
    if run_dir is None:
        return
    record_path = run_dir.resolve() / "flow-execution-record.json"
    if not record_path.is_file():
        return
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FlowError(f"cannot read existing builder execution record: {exc}") from exc
    flow_id = str(record.get("flow_id") or "")
    if flow_id != BUILDER_FLOW_ID:
        raise FlowError(
            "Builder 3 cannot resume or execute a Builder 2 run; start a fresh "
            "m8m_build_v2 session and import the old staged source as untrusted input"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True, help="Skill directory to turn into a milestone flow.")
    parser.add_argument("--codebase", type=Path, required=True, help="Project repo. Tools and flow are written here.")
    parser.add_argument(
        "--harness-root",
        type=Path,
        help="Mutable execution root (default: M8M_HARNESS_ROOT or %%SystemDrive%%\\NisanRuntime)",
    )
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
        _assert_builder3_run(args.run_dir)
        result = run_factory(
            args.target,
            args.codebase,
            flow_id=args.flow_id,
            skill_name=args.skill_name,
            overwrite=args.force,
            run_dir=args.run_dir,
            replace_milestone=args.replace_milestone,
            continue_after_edit=args.continue_after_edit,
            harness_root=args.harness_root,
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
