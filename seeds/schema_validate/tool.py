"""Validate a JSON object against a JSON Schema document."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, RefResolver


def run(input_data: dict[str, Any], params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    del params
    schema = input_data.get("schema")
    if schema is None:
        schema_path = Path(str(input_data["schema_path"]))
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    instance = input_data["instance"]
    resolver = RefResolver.from_schema(schema)
    errors = sorted(
        Draft202012Validator(schema, resolver=resolver).iter_errors(instance),
        key=lambda item: list(item.path),
    )
    if errors:
        raise ValueError(errors[0].message)
    return {"valid": True}
