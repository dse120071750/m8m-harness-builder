from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tool.py"
_spec = importlib.util.spec_from_file_location("schema_validate_tool", TOOL)
assert _spec is not None and _spec.loader is not None
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class SchemaValidateTests(unittest.TestCase):
    def test_accepts_matching_object(self) -> None:
        result = tool.run(
            {
                "instance": {"ok": True},
                "schema": {
                    "type": "object",
                    "required": ["ok"],
                    "properties": {"ok": {"type": "boolean"}},
                },
            }
        )
        self.assertEqual(result, {"valid": True})

    def test_rejects_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            tool.run({"instance": {}, "schema": {"type": "object", "required": ["ok"]}})


if __name__ == "__main__":
    unittest.main()
