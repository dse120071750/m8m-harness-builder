from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ASSEMBLE = Path(__file__).resolve().parents[1] / "assemble.py"
_spec = importlib.util.spec_from_file_location("__STEP_ID___assemble", ASSEMBLE)
assert _spec is not None and _spec.loader is not None
assemble = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(assemble)


class AssembleTests(unittest.TestCase):
    def test_run_returns_object(self) -> None:
        if assemble.INTELLIGENCE != "none":
            result = assemble.run({"request": {"text": "x"}})
            self.assertEqual(result.get("_flowstep"), "NEED_MODEL")
            return
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "asset.txt"
            path.write_text("ok\n", encoding="utf-8")
            result = assemble.run({"request": {"path": str(path)}})
        self.assertIsInstance(result, dict)
        if assemble.ASSET_KIND in {"file", "image", "video", "audio"}:
            self.assertIn("outputs", result)
            output_id = assemble.OUTPUTS[0]["id"]
            self.assertTrue(result["outputs"][output_id]["asset"]["path"])


if __name__ == "__main__":
    unittest.main()
