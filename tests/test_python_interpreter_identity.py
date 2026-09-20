import hashlib
import sys

import pytest

import support  # noqa: F401
from runtime_release import _python_identity, RuntimeReleaseError


def test_interpreter_alias_uses_resolved_bytes_and_detects_retargeting(tmp_path, monkeypatch):
    first = tmp_path / "python3.12"
    second = tmp_path / "python3.13"
    first.write_bytes(b"first interpreter")
    second.write_bytes(b"second interpreter")
    alias = tmp_path / "python"
    try:
        alias.symlink_to(first)
    except OSError:
        pytest.skip("host cannot create symbolic links")
    monkeypatch.setattr(sys, "executable", str(alias))
    abi, digest = _python_identity()
    assert abi == sys.implementation.cache_tag
    assert digest == "sha256:" + hashlib.sha256(first.read_bytes()).hexdigest()
    alias.unlink()
    alias.symlink_to(second)
    assert _python_identity()[1] != digest


def test_missing_interpreter_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "missing"))
    with pytest.raises(RuntimeReleaseError, match="cannot resolve"):
        _python_identity()
