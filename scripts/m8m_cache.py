"""Explicit maintenance for the optional M8M cross-run candidate cache."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from candidate_cache import prune_expired
from session_layout import default_harness_root, validate_fresh_harness_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prune = sub.add_parser("prune", help="Remove expired or invalid cache entries")
    prune.add_argument(
        "--harness-root",
        type=Path,
        help="Mutable execution root (default: M8M_HARNESS_ROOT or %SystemDrive%\\NisanRuntime)",
    )
    prune.add_argument(
        "--codebase",
        type=Path,
        help="Explicit legacy cache root selector; never used for default discovery",
    )
    prune.add_argument("--flow-id", help="Limit pruning to one workflow")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.codebase is not None and args.harness_root is not None:
        raise SystemExit("choose --harness-root or explicit legacy --codebase, not both")
    if args.codebase is not None:
        root = args.codebase.resolve() / "flowsteps" / "cache" / "v1"
    else:
        harness_root = validate_fresh_harness_root(args.harness_root or default_harness_root())
        root = harness_root / "cache" / "v1"
    if args.flow_id:
        root = root / str(args.flow_id)
    result = prune_expired(root)
    print(json.dumps({"schema": "m8m_cache_prune_v1", "root": str(root), **result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
