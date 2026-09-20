from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from jsonschema import Draft202012Validator

from flowstep_runtime import lint_file_payload_schema
from flowstep_tools import run_library_tool, validate_library_tool
from validate_harness import _lint_step_file_payload_schema
from audit_harness import proposed_schema_object
from tool_vs_intelligence import from_audit


TEXT = {"type": "string", "minLength": 1}
HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


class FilePayloadSchemaContextTests(unittest.TestCase):
    def test_audit_defaults_use_actual_paths_and_schema_validation(self):
        schema = proposed_schema_object(step_id="image_generated", kind="output")
        self.assertTrue(Draft202012Validator(schema).is_valid({"path": "image.png"}))
        self.assertFalse(Draft202012Validator(schema).is_valid({}))
        self.assertNotIn("hash_bind", [row["id"] for row in from_audit({})["rows"]])

    def test_paths_need_no_hash_in_any_payload_role(self):
        schemas = [obj({"path": TEXT}), obj({"provider_image_path": TEXT}),
                   obj({"source": obj({"local_path": TEXT})}),
                   obj({"files": {"type": "array", "items": obj({"path": TEXT})}})]
        for schema in schemas:
            for role in ("input_schema", "draft_schema", "output_schema"):
                with self.subTest(schema=schema, role=role):
                    self.assertEqual(lint_file_payload_schema(schema, label=role), [])
                    self.assertEqual(_lint_step_file_payload_schema(
                        schema, flow={}, step={"id": "image_generated"}, index=0, schema_key=role), [])

    def test_tool_can_validate_and_return_plain_file_path(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            tool = repo / "flowsteps/tools/file_output"
            (tool / "tests").mkdir(parents=True)
            (tool / "tool.py").write_text(
                "def run(input_data, **kwargs):\n"
                "    return {'path': input_data['source_path']}\n", encoding="utf-8")
            (tool / "input.schema.json").write_text(json.dumps(obj({"source_path": TEXT})))
            (tool / "output.schema.json").write_text(json.dumps(obj({"path": TEXT})))
            (tool / "tests/test_tool.py").write_text("def test_output():\n    assert True\n")
            source = repo / "source.txt"
            source.write_text("actual output", encoding="utf-8")
            self.assertEqual(validate_library_tool(repo, "file_output"), [])
            result = run_library_tool(repo, "file_output", {"source_path": str(source)})
            self.assertEqual(result, {"path": str(source)})
            self.assertEqual(Path(result["path"]).read_text(), "actual output")

    def test_explicit_checksum_contract_still_validates_its_own_payload(self):
        schema = obj({"path": TEXT, "sha256": HASH})
        self.assertEqual(lint_file_payload_schema(schema, label="explicit"), [])
        validator = Draft202012Validator(schema)
        self.assertFalse(validator.is_valid({"path": "asset.png"}))
        self.assertFalse(validator.is_valid({"path": "asset.png", "sha256": "bad"}))
        self.assertTrue(validator.is_valid({"path": "asset.png", "sha256": "a" * 64}))


if __name__ == "__main__":
    unittest.main()
