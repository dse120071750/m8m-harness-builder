"""Make the Builder test support module importable from a plain root pytest run."""

from __future__ import annotations

import sys
from pathlib import Path


TESTS = Path(__file__).resolve().parent / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
