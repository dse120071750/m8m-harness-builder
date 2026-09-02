from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


RUNTIME = Path(__file__).resolve().parents[1] / "scripts" / "flowstep_runtime.py"
sys.path.insert(0, str(RUNTIME.parent))
SPEC = importlib.util.spec_from_file_location("flowstep_runtime_tool_ref_test", RUNTIME)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ToolRefResolutionTests(unittest.TestCase):
    def test_previous_tool_namespace_remains_resumable(self) -> None:
        self.assertEqual(
            MODULE.local_tool_package_name(
                "tool.resolve_project_style_knowledge@1.0.0"
            ),
            "resolve_project_style_knowledge",
        )

    def test_native_safe_versioned_ref_maps_to_local_package(self) -> None:
        self.assertEqual(MODULE.local_tool_package_name("hash_bind@1.0.0"), "hash_bind")


if __name__ == "__main__":
    unittest.main()
