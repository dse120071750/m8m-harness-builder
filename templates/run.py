"""Point this built skill to its codebase-owned M8M launcher."""

import sys


# A normal script launch prepends this product skill's scripts directory to
# sys.path. Re-exec through the platform builtin before importing any non-builtin
# module so a sibling hashlib.py/subprocess.py cannot run before verification.
if not sys.flags.isolated:
    _platform = __import__("nt" if "nt" in sys.builtin_module_names else "posix")
    _arguments = [sys.executable, "-I", "-B", __file__, *sys.argv[1:]]
    if hasattr(_platform, "spawnv"):
        # Windows spawnv joins tokens without quoting. Preserve each argument
        # using CRT rules before importing any potentially shadowed module.
        def _windows_argument(value):
            quoted, backslashes = '"', 0
            for character in value:
                if character == "\\":
                    backslashes += 1
                elif character == '"':
                    quoted += "\\" * (backslashes * 2 + 1) + '"'
                    backslashes = 0
                else:
                    quoted += "\\" * backslashes + character
                    backslashes = 0
            return quoted + "\\" * (backslashes * 2) + '"'

        raise SystemExit(_platform.spawnv(
            0, sys.executable, [_windows_argument(value) for value in _arguments]
        ))
    _platform.execv(sys.executable, _arguments)
    raise SystemExit("M8M isolated bootstrap exec unexpectedly returned")

import hashlib
import subprocess
from pathlib import Path


M8M_SKILL_POINTER_ABI = "m8m_skill_pointer_v2"
launcher = Path(r"__CODEBASE_LAUNCHER__").absolute()
expected_launcher_sha256 = "__CODEBASE_LAUNCHER_SHA256__"
if not launcher.is_file():
    raise SystemExit(
        "The built skill's codebase M8M launcher is unavailable: "
        f"{launcher}. Rebuild/install the skill from its owning codebase."
    )
digest = hashlib.sha256(launcher.read_bytes()).hexdigest()
if digest != expected_launcher_sha256:
    raise SystemExit(
        "The built skill's codebase M8M launcher failed its pinned digest check. "
        "Rebuild/install the skill from its owning codebase."
    )
completed = subprocess.run(
    [sys.executable, "-I", "-B", str(launcher), *sys.argv[1:]],
    check=False,
)
raise SystemExit(completed.returncode)
