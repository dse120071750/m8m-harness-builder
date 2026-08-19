from __future__ import annotations

import os
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
EXAMPLE = SKILL_ROOT / "examples" / "text_pipeline"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def optional_product_repo() -> Path | None:
    raw = os.environ.get("M8M_PRODUCT_REPO")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


def optional_sample_skill() -> Path | None:
    raw = os.environ.get("M8M_SAMPLE_SKILL")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None
