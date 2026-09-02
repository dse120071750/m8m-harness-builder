"""Verify Builder 3.1 contract bytes against a platform-vendored schema root."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Sequence

from m8m_build_steps import (
    BUILDER_ROOT,
    CONTRACT_BUNDLE_FILES,
    _contract_bundle_lock,
)


class ContractParityError(RuntimeError):
    pass


def verify_platform_contract_parity(platform_schema_root: Path) -> dict[str, object]:
    root = Path(platform_schema_root).resolve()
    lock_path = root / "m8m.contract_bundle.builder-3.1.lock.json"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractParityError(f"platform contract lock is unreadable: {lock_path}") from exc
    if lock.get("schema") != "m8m.contract_bundle_lock_manifest.v1":
        raise ContractParityError("platform contract lock schema is invalid")
    if lock.get("contract_bundle") != _contract_bundle_lock():
        raise ContractParityError("platform contract bundle identity differs from Builder 3.1")
    declared = lock.get("contracts")
    if not isinstance(declared, list):
        raise ContractParityError("platform contract lock members are invalid")
    by_name = {
        str(item.get("name") or ""): str(item.get("digest") or "")
        for item in declared
        if isinstance(item, dict)
    }
    if tuple(by_name) != CONTRACT_BUNDLE_FILES:
        raise ContractParityError("platform contract member order differs from Builder 3.1")
    for name in CONTRACT_BUNDLE_FILES:
        builder_bytes = (BUILDER_ROOT / "contracts" / name).read_bytes()
        try:
            platform_bytes = (root / name).read_bytes()
        except OSError as exc:
            raise ContractParityError(f"platform contract member is missing: {name}") from exc
        digest = "sha256:" + hashlib.sha256(builder_bytes).hexdigest()
        if by_name[name] != digest:
            raise ContractParityError(f"platform contract lock digest differs: {name}")
        if platform_bytes != builder_bytes:
            raise ContractParityError(f"platform contract bytes differ: {name}")
    return {
        "schema": "m8m.builder_platform_contract_parity.v1",
        "status": "PASS",
        "contract_bundle": _contract_bundle_lock(),
        "member_count": len(CONTRACT_BUNDLE_FILES),
        "platform_schema_root": str(root),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-schema-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = verify_platform_contract_parity(args.platform_schema_root)
    except ContractParityError as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
