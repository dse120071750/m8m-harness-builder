from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import support  # noqa: F401

from m8m_build_steps import (
    BUILDER_ROOT,
    CONTRACT_BUNDLE_FILES,
    _contract_bundle_lock,
    _platform_common_profile_metadata,
)
from verify_platform_contract_parity import (
    ContractParityError,
    verify_platform_contract_parity,
)


def test_platform_common_profile_reports_compatible_dag() -> None:
    report = _platform_common_profile_metadata(
        {
            "milestones": [
                {"id": "source_ready", "loop": "none"},
                {"id": "result_ready", "loop": "judge"},
            ]
        }
    )
    assert report == {
        "platform_common_profile": "compatible",
        "platform_unsupported_features": [],
    }


def test_platform_common_profile_lists_exact_unsupported_features() -> None:
    report = _platform_common_profile_metadata(
        {
            "milestones": [
                {
                    "id": "rows_complete",
                    "loop": "for",
                    "ledger": {"schema": "example"},
                    "on_cycle": "retry_cycle",
                }
            ]
        }
    )
    assert report == {
        "platform_common_profile": "unsupported",
        "platform_unsupported_features": [
            "milestone:rows_complete:ledger",
            "milestone:rows_complete:loop:for",
            "milestone:rows_complete:on_cycle",
        ],
    }


def _write_platform_snapshot(root: Path) -> None:
    contracts = []
    for name in CONTRACT_BUNDLE_FILES:
        payload = (BUILDER_ROOT / "contracts" / name).read_bytes()
        (root / name).write_bytes(payload)
        contracts.append(
            {"name": name, "digest": "sha256:" + hashlib.sha256(payload).hexdigest()}
        )
    (root / "m8m.contract_bundle.builder-3.1.lock.json").write_text(
        json.dumps(
            {
                "schema": "m8m.contract_bundle_lock_manifest.v1",
                "contract_bundle": _contract_bundle_lock(),
                "contracts": contracts,
            }
        ),
        encoding="utf-8",
    )


def test_platform_contract_parity_checks_exact_bytes(tmp_path: Path) -> None:
    _write_platform_snapshot(tmp_path)
    report = verify_platform_contract_parity(tmp_path)
    assert report["status"] == "PASS"
    assert report["member_count"] == len(CONTRACT_BUNDLE_FILES)


def test_platform_contract_parity_rejects_byte_drift(tmp_path: Path) -> None:
    _write_platform_snapshot(tmp_path)
    member = tmp_path / CONTRACT_BUNDLE_FILES[0]
    member.write_bytes(member.read_bytes() + b"\n")
    with pytest.raises(ContractParityError, match="bytes differ"):
        verify_platform_contract_parity(tmp_path)
