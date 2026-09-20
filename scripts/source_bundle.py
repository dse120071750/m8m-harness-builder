"""Deterministic Builder 3.1 workflow source-bundle compiler.

This module deliberately has no filesystem write, network, handoff, catalog,
release, or deployment authority.  It compiles one base-independent logical
package.  A separate authoring-IO client may later resolve its requirements,
but must preserve the proof's logical projection.

API assumptions for this foundation:

* Inputs other than ``source_root`` are JSON-native values.  Mapping keys must
  be strings; non-finite floats and Python-specific objects are rejected.
* ``contract_bundle`` is a previously computed exact lock.  This module checks
  its shape but cannot derive the normative contract digest without the
  contract bundle bytes.
* Resource inputs contain ``ref``, ``kind``, ``role``, ``source_path``, and
  ``media_type``.  Text is canonicalized to UTF-8, NFC, and LF before its
  optional ``byte_count``/``digest`` assertions are checked; binary resources
  remain byte-exact.
* Requirement payloads use closed provider-neutral shapes.  Their logical
  identities, implementation ABI, build state, and built-artifact proof are
  validated before they are canonicalized, ordered, and hash-bound.
* ``validation`` is reduced to a closed advisory summary.  Local prose and
  paths stay outside the source bundle, while the summary is digest-bound.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator, RefResolver
from jsonschema.exceptions import SchemaError


SOURCE_BUNDLE_SCHEMA = "m8m.workflow_source_bundle.v1"
SOURCE_BUNDLE_PROOF_SCHEMA = "m8m.workflow_source_bundle_proof.v1"
CONTRACT_BUNDLE_LOCK_SCHEMA = "m8m.contract_bundle_lock.v1"
FLOW_SCHEMA = "flowstep_flow_v4"
LOCAL_VALIDATION_SCHEMA = "m8m.builder_validation_summary.v1"
LOCAL_VALIDATION_BUILDER = "m8m-harness-builder/3.1"
LOCAL_VALIDATION_POLICY = "m8m-builder-validation/3"

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FLOW_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSIONED_REF_RE = re.compile(r"^[a-z][a-z0-9_.-]*@[0-9]+\.[0-9]+\.[0-9]+$")
_TERMINAL_FROM_RE = re.compile(
    r"^(?P<source>[a-z][a-z0-9_]*)\.(?P<contract>[A-Za-z][A-Za-z0-9_.-]*)$"
)
_SCHEMA_REF_GLOB_RE = re.compile(r"[*?\[]")
_PATH_FIELD_NAMES = {
    "artifact_root",
    "draft_schema",
    "gem",
    "handler",
    "implementation_dependencies",
    "input_schema",
    "operator_result_path",
    "output_schema",
    "path",
    "receipt_schema",
    "source_path",
}
_FORBIDDEN_IDENTITY_KEYS = {
    "agent_profile_id",
    "artifact_id",
    "authoring_session_id",
    "base_definition_hash",
    "base_revision",
    "base_workflow_revision_id",
    "binding_mode",
    "candidate_digest",
    "catalog_entry_id",
    "chat_id",
    "deployment_id",
    "execution_closure_id",
    "execution_id",
    "handoff_id",
    "idempotency_key",
    "implementation_bundle_id",
    "installed_reference_catalog_digest",
    "registry_policy",
    "release_id",
    "run_id",
    "session_id",
    "thread_id",
    "workflow_revision_id",
}
_FORBIDDEN_TIME_KEYS = {
    "created_at",
    "expires_at",
    "generated_at",
    "modified_at",
    "timestamp",
    "updated_at",
}
_FORBIDDEN_VALIDATION_KEYS = {
    "builder_validation",
    "validation_findings",
    "validation_prose",
}
_FORBIDDEN_SECRET_KEYS = {
    "access_key",
    "access_key_id",
    "access_token",
    "api_key",
    "api_secret",
    "api_token",
    "auth_token",
    "authorization",
    "authorization_header",
    "bearer",
    "client_secret",
    "credential",
    "credentials",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "secret",
    "secret_key",
    "service_account_key",
    "signing_key",
    "webhook_secret",
}
_RESOURCE_INPUT_KEYS = {
    "ref",
    "kind",
    "role",
    "source_path",
    "media_type",
    "byte_count",
    "digest",
}
_WORKFLOW_WIRING_KEYS = {
    "milestones",
    "entry_milestones",
    "terminal_milestones",
}
_ROOT_WORKFLOW_CONTRACT_KEYS = {
    "request_schema",
    "configuration_schema",
    "result_schema",
    "terminal_bindings",
}
_WORKFLOW_MILESTONE_KEYS = {
    "milestone_id",
    "input_schema",
    "inputs",
    "output_contract",
    "output_schema",
    "outputs",
}
_TERMINAL_BINDING_KEYS = {"name", "from", "output"}
_BUNDLE_KEYS = {
    "schema",
    "contract_bundle",
    "definition",
    "definition_digest",
    "workflow_contracts",
    "workflow_contracts_digest",
    "resources",
    "resource_requirements_digest",
    "implementation_requirements",
    "implementation_requirements_digest",
    "agent_profile_requirements",
    "agent_profile_requirements_digest",
    "capability_requirements",
    "capability_requirements_digest",
    "observer",
    "observer_digest",
    "local_validation",
    "local_validation_digest",
    "source_bundle_proof",
}
_PROOF_KEYS = {
    "schema",
    "contract_bundle_id",
    "contract_bundle_digest",
    "definition_digest",
    "workflow_contracts_digest",
    "resource_requirements_digest",
    "implementation_requirements_digest",
    "agent_profile_requirements_digest",
    "capability_requirements_digest",
    "observer_digest",
    "local_validation_digest",
    "source_bundle_digest",
}
_IMPLEMENTATION_KINDS = {
    "flowstep_tool",
    "milestone_handler",
    "milestone_judge",
    "shared_dependency",
}
_IMPLEMENTATION_ABIS = {
    "flowstep_tool": "m8m_flowstep_tool_v1",
    "milestone_handler": "m8m_milestone_handler_v1",
    "milestone_judge": "m8m_milestone_judge_v1",
    "shared_dependency": "m8m_shared_dependency_v1",
}
_BUILD_STATES = {"built", "BUILD_REQUIRED"}
_PROFILE_REASONING = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
}
_PROFILE_ROLES = {"candidate_executor", "ai_judge"}
_CAPABILITY_ACCESS = {"read", "write", "execute", "use"}
_CAPABILITY_SIDE_EFFECTS = {"none", "local", "external"}
_TEXTUAL_MEDIA_TYPES = {
    "application/json",
    "application/javascript",
    "application/toml",
    "application/xml",
    "application/x-httpd-php",
    "application/x-yaml",
    "application/yaml",
}
_CREDENTIAL_TEXT_PATTERNS = (
    (
        "private-key block",
        re.compile(
            r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY(?: BLOCK)?-----",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "Bearer token",
        re.compile(
            r"\bBearer[ \t]+[A-Za-z0-9._~+/=-]{16,}(?![A-Za-z0-9._~+/=-])",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "prefixed API token",
        re.compile(
            r"\bsk-(?:(?:proj|ant-api\d{2})-)?[A-Za-z0-9_-]{16,}\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "GitHub token",
        re.compile(
            r"\b(?:github_pat_[A-Za-z0-9_]{22,}|gh[pousr]_[A-Za-z0-9]{20,})\b"
        ),
    ),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "Slack token",
        re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{16,}\b", flags=re.IGNORECASE),
    ),
    (
        "Stripe secret key",
        re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}\b"),
    ),
)


class SourceBundleError(ValueError):
    """Fail-closed source compile or proof validation error."""


@lru_cache(maxsize=1)
def _flow_validator() -> Draft202012Validator:
    path = Path(__file__).resolve().parents[1] / "contracts" / "flowstep_flow_v4.schema.json"
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError) as exc:
        # A missing or unusable normative contract is itself a hard source
        # boundary failure.  Do not silently fall back to checking the schema
        # discriminator alone.
        raise SourceBundleError(f"cannot load normative {FLOW_SCHEMA} contract: {exc}") from exc
    return Draft202012Validator(schema)


def _validate_flow_definition(definition: Mapping[str, Any]) -> None:
    errors = sorted(
        _flow_validator().iter_errors(definition),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if not errors:
        return
    error = errors[0]
    location = ".".join(str(item) for item in error.absolute_path) or "$"
    raise SourceBundleError(
        f"definition is not valid {FLOW_SCHEMA} at {location}: {error.message}"
    )


@lru_cache(maxsize=1)
def _source_bundle_validator() -> Draft202012Validator:
    contracts = Path(__file__).resolve().parents[1] / "contracts"
    root_path = contracts / "m8m_workflow_source_bundle_v1.schema.json"
    store: dict[str, Any] = {}
    root_schema: dict[str, Any] | None = None
    try:
        for path in sorted(contracts.glob("*.schema.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            store[path.name] = schema
            store[path.as_uri()] = schema
            if isinstance(schema.get("$id"), str):
                store[str(schema["$id"])] = schema
            if path == root_path:
                root_schema = schema
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError) as exc:
        raise SourceBundleError(f"cannot load normative source-bundle contract: {exc}") from exc
    if root_schema is None:
        raise SourceBundleError("cannot load normative source-bundle contract")
    resolver = RefResolver(
        base_uri=root_path.as_uri(),
        referrer=root_schema,
        store=store,
    )
    return Draft202012Validator(root_schema, resolver=resolver)


def _validate_normative_source_bundle(bundle: Mapping[str, Any]) -> None:
    try:
        errors = sorted(
            _source_bundle_validator().iter_errors(bundle),
            key=lambda error: tuple(str(item) for item in error.absolute_path),
        )
    except Exception as exc:
        raise SourceBundleError(f"cannot resolve normative source-bundle contract: {exc}") from exc
    if not errors:
        return
    error = errors[0]
    location = ".".join(str(item) for item in error.absolute_path) or "$"
    raise SourceBundleError(
        f"source bundle violates m8m_workflow_source_bundle_v1 at {location}: {error.message}"
    )


@lru_cache(maxsize=2)
def _flow_component_validator(component: str) -> Draft202012Validator:
    """Return the normative v4 validator for one reusable component."""

    flow_schema = _flow_validator().schema
    if component not in {"binding", "output"}:
        raise SourceBundleError(f"unsupported {FLOW_SCHEMA} component: {component}")
    return Draft202012Validator(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": flow_schema["$defs"],
            "$ref": f"#/$defs/{component}",
        }
    )


def _validate_flow_component(value: Any, component: str, label: str) -> None:
    errors = sorted(
        _flow_component_validator(component).iter_errors(value),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if not errors:
        return
    error = errors[0]
    location = ".".join(str(item) for item in error.absolute_path)
    suffix = f" at {location}" if location else ""
    raise SourceBundleError(
        f"{label} is not valid {FLOW_SCHEMA} {component}{suffix}: {error.message}"
    )


def _binding_from(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("from") or "")
    return str(value or "")


def _derive_workflow_wiring(definition: Mapping[str, Any]) -> dict[str, Any]:
    """Project the one canonical v4 graph into portable milestone wiring."""

    milestones = list(definition.get("milestones") or [])
    rows = [
        {
            "milestone_id": str(item.get("id") or ""),
            "input_schema": str(item.get("input_schema") or ""),
            "inputs": dict(item.get("inputs") or {}),
            "output_contract": str(item.get("output_contract") or ""),
            "output_schema": str(item.get("output_schema") or ""),
            "outputs": list(item.get("outputs") or []),
        }
        for item in milestones
    ]
    referenced_sources = {
        source.split(".", 1)[0]
        for item in milestones
        for binding in (item.get("inputs") or {}).values()
        if (source := _binding_from(binding)) != "user.request" and "." in source
    }
    return {
        "milestones": rows,
        "entry_milestones": [
            str(item.get("id") or "")
            for item in milestones
            if all(
                _binding_from(binding) == "user.request"
                for binding in (item.get("inputs") or {}).values()
            )
        ],
        "terminal_milestones": [
            str(item.get("id") or "")
            for item in milestones
            if str(item.get("id") or "") not in referenced_sources
        ],
    }


def _require_flow_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _FLOW_ID_RE.fullmatch(value) is None:
        raise SourceBundleError(f"{label} must match ^[a-z][a-z0-9_]*$")
    return value


def _require_schema_ref(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if not isinstance(value, str) or not value or value != value.strip():
        raise SourceBundleError(f"{label} must be a non-empty trimmed schema ref")
    if "\\" in value or _SCHEMA_REF_GLOB_RE.search(value):
        raise SourceBundleError(f"{label} must be a safe relative .json schema ref")
    try:
        canonical = _safe_relative_path(value)
    except SourceBundleError as exc:
        raise SourceBundleError(
            f"{label} must be a safe relative .json schema ref"
        ) from exc
    if canonical != value or not canonical.endswith(".json"):
        raise SourceBundleError(f"{label} must be a safe relative .json schema ref")
    return value


def _require_nonempty_id_array(value: Any, label: str) -> list[str]:
    rows = _require_sequence(value, label)
    if not rows:
        raise SourceBundleError(f"{label} must not be empty")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(rows):
        identifier = _require_flow_id(item, f"{label}[{index}]")
        if identifier in seen:
            raise SourceBundleError(f"{label} contains duplicate milestone: {identifier}")
        seen.add(identifier)
        result.append(identifier)
    return result


def _normalize_workflow_contracts(
    value: Mapping[str, Any] | None,
    definition: Mapping[str, Any],
    *,
    derive_when_omitted: bool = False,
    allow_wiring_only: bool = False,
) -> dict[str, Any]:
    """Validate the closed workflow boundary and its exact v4 projection.

    Native Builder-3 callers must supply the complete public boundary.  The
    generic wiring-only projection exists only for an explicitly identified
    legacy-import staging call.
    """

    expected_wiring = _derive_workflow_wiring(definition)
    if value is None:
        if not derive_when_omitted:
            raise SourceBundleError(
                "workflow_contracts must be an object; omission requires explicit legacy-import staging"
            )
        contracts = _require_mapping(expected_wiring, "workflow_contracts", nonempty=True)
    else:
        contracts = _require_mapping(value, "workflow_contracts", nonempty=True)

    _require_allowed_and_required_keys(
        contracts,
        allowed=_WORKFLOW_WIRING_KEYS | _ROOT_WORKFLOW_CONTRACT_KEYS,
        required=_WORKFLOW_WIRING_KEYS,
        label="workflow_contracts",
    )
    present_root = set(contracts) & _ROOT_WORKFLOW_CONTRACT_KEYS
    if present_root and present_root != _ROOT_WORKFLOW_CONTRACT_KEYS:
        missing = _ROOT_WORKFLOW_CONTRACT_KEYS - present_root
        raise SourceBundleError(
            "workflow_contracts root contract group is missing fields: "
            + ", ".join(sorted(missing))
        )

    raw_milestones = _require_sequence(
        contracts.get("milestones"), "workflow_contracts.milestones"
    )
    if not raw_milestones:
        raise SourceBundleError("workflow_contracts.milestones must not be empty")
    milestones: list[dict[str, Any]] = []
    milestone_ids: set[str] = set()
    for index, raw in enumerate(raw_milestones):
        label = f"workflow_contracts.milestones[{index}]"
        row = _require_mapping(raw, label, nonempty=True)
        _require_exact_keys(row, _WORKFLOW_MILESTONE_KEYS, label)
        milestone_id = _require_flow_id(row.get("milestone_id"), f"{label}.milestone_id")
        if milestone_id in milestone_ids:
            raise SourceBundleError(
                f"workflow_contracts.milestones contains duplicate milestone: {milestone_id}"
            )
        milestone_ids.add(milestone_id)
        _require_schema_ref(row.get("input_schema"), f"{label}.input_schema", allow_empty=True)
        output_contract = _required_trimmed_string(row, "output_contract", label)
        if len(output_contract) < 3:
            raise SourceBundleError(f"{label}.output_contract must have at least 3 characters")
        _require_schema_ref(row.get("output_schema"), f"{label}.output_schema")

        inputs = _require_mapping(row.get("inputs"), f"{label}.inputs", nonempty=False)
        for name, binding in inputs.items():
            _validate_flow_component(binding, "binding", f"{label}.inputs.{name}")

        outputs = _require_sequence(row.get("outputs"), f"{label}.outputs")
        if not outputs:
            raise SourceBundleError(f"{label}.outputs must not be empty")
        output_ids: set[str] = set()
        normalized_outputs: list[dict[str, Any]] = []
        for output_index, raw_output in enumerate(outputs):
            output_label = f"{label}.outputs[{output_index}]"
            output = _require_mapping(raw_output, output_label, nonempty=True)
            _validate_flow_component(output, "output", output_label)
            output_id = str(output.get("id") or "")
            if output_id in output_ids:
                raise SourceBundleError(
                    f"{label}.outputs contains duplicate output id: {output_id}"
                )
            output_ids.add(output_id)
            normalized_outputs.append(output)

        milestones.append(
            {
                "milestone_id": milestone_id,
                "input_schema": row["input_schema"],
                "inputs": inputs,
                "output_contract": output_contract,
                "output_schema": row["output_schema"],
                "outputs": normalized_outputs,
            }
        )

    entry_milestones = _require_nonempty_id_array(
        contracts.get("entry_milestones"), "workflow_contracts.entry_milestones"
    )
    terminal_milestones = _require_nonempty_id_array(
        contracts.get("terminal_milestones"), "workflow_contracts.terminal_milestones"
    )
    for label, identifiers in (
        ("entry_milestones", entry_milestones),
        ("terminal_milestones", terminal_milestones),
    ):
        unknown = [identifier for identifier in identifiers if identifier not in milestone_ids]
        if unknown:
            raise SourceBundleError(
                f"workflow_contracts.{label} references unknown milestones: "
                + ", ".join(unknown)
            )

    actual_wiring = {
        "milestones": milestones,
        "entry_milestones": entry_milestones,
        "terminal_milestones": terminal_milestones,
    }
    if actual_wiring != expected_wiring:
        raise SourceBundleError(
            "workflow_contracts milestone wiring must exactly project definition"
        )

    if not present_root:
        if not allow_wiring_only:
            raise SourceBundleError(
                "workflow_contracts requires request, configuration, and result schemas "
                "plus terminal bindings; wiring-only contracts require explicit legacy-import staging"
            )
        return actual_wiring

    for field in ("request_schema", "configuration_schema", "result_schema"):
        _require_schema_ref(contracts.get(field), f"workflow_contracts.{field}")

    raw_bindings = _require_sequence(
        contracts.get("terminal_bindings"), "workflow_contracts.terminal_bindings"
    )
    if not raw_bindings:
        raise SourceBundleError("workflow_contracts.terminal_bindings must not be empty")
    by_id = {row["milestone_id"]: row for row in milestones}
    terminal_ids = set(terminal_milestones)
    names: set[str] = set()
    sources_and_outputs: set[tuple[str, str]] = set()
    terminal_bindings: list[dict[str, str]] = []
    for index, raw in enumerate(raw_bindings):
        label = f"workflow_contracts.terminal_bindings[{index}]"
        binding = _require_mapping(raw, label, nonempty=True)
        _require_exact_keys(binding, _TERMINAL_BINDING_KEYS, label)
        name = _require_flow_id(binding.get("name"), f"{label}.name")
        if name in names:
            raise SourceBundleError(f"{label} has duplicate name: {name}")
        names.add(name)
        source_ref = _required_trimmed_string(binding, "from", label)
        match = _TERMINAL_FROM_RE.fullmatch(source_ref)
        if match is None:
            raise SourceBundleError(
                f"{label}.from must be <milestone_id>.<output_contract>"
            )
        source = match.group("source")
        contract = match.group("contract")
        if source not in terminal_ids:
            raise SourceBundleError(
                f"{label}.from source must be a declared terminal milestone"
            )
        milestone = by_id[source]
        if contract != milestone["output_contract"]:
            raise SourceBundleError(
                f"{label}.from contract must match terminal milestone {source}"
            )
        output = _require_flow_id(binding.get("output"), f"{label}.output")
        if output not in {item["id"] for item in milestone["outputs"]}:
            raise SourceBundleError(
                f"{label}.output is not declared by terminal milestone {source}"
            )
        source_and_output = (source, output)
        if source_and_output in sources_and_outputs:
            raise SourceBundleError(
                f"{label} has duplicate terminal output: {source}.{output}"
            )
        sources_and_outputs.add(source_and_output)
        terminal_bindings.append(
            {"name": name, "from": source_ref, "output": output}
        )

    return {
        "request_schema": contracts["request_schema"],
        "configuration_schema": contracts["configuration_schema"],
        "result_schema": contracts["result_schema"],
        "terminal_bindings": terminal_bindings,
        **actual_wiring,
    }


def _validate_root_contract_resources(
    workflow_contracts: Mapping[str, Any],
    resources: Sequence[Mapping[str, Any]],
) -> None:
    """Bind every authored root schema ref to a digest-bound source resource."""

    if "request_schema" not in workflow_contracts:
        return
    for field in ("request_schema", "configuration_schema", "result_schema"):
        source_path = str(workflow_contracts[field])
        matches = [
            row
            for row in resources
            if row.get("source_path") == source_path
            and row.get("kind") in {"schema", "json_schema"}
            and str(row.get("media_type") or "").partition(";")[0].strip().casefold()
            == "application/schema+json"
        ]
        if not matches:
            raise SourceBundleError(
                f"workflow_contracts.{field} is not bound to an included JSON Schema resource"
            )


def canonical_json(value: Any) -> bytes:
    """Return this compiler's stable UTF-8 canonical JSON representation."""

    normalized = _normalize_json(value, trail="$", key_hint=None)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest_json(value: Any) -> str:
    """Return a namespaced SHA-256 digest of canonical JSON."""

    return _digest_bytes(canonical_json(value))


def compile_source_bundle(
    *,
    source_root: Path,
    contract_bundle: Mapping[str, Any],
    definition: Mapping[str, Any],
    workflow_contracts: Mapping[str, Any] | None = None,
    resources: Sequence[Mapping[str, Any]] = (),
    implementation_requirements: Sequence[Mapping[str, Any]] = (),
    agent_profile_requirements: Sequence[Mapping[str, Any]] = (),
    capability_requirements: Sequence[Mapping[str, Any]] = (),
    observer: Mapping[str, Any] | None = None,
    validation: Mapping[str, Any] | None = None,
    require_ready: bool = False,
    allow_legacy_wiring_only: bool = False,
) -> dict[str, Any]:
    """Compile one deterministic, base-independent workflow source bundle.

    ``require_ready=False`` permits a repairable package with
    ``BUILD_REQUIRED`` requirements.  ``require_ready=True`` asserts that the
    caller intends to use the package as ready source and therefore requires a
    ``SOURCE_VALID`` local validation result and no ``BUILD_REQUIRED`` value.
    """

    root = Path(source_root)
    try:
        resolved_root = root.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise SourceBundleError(f"source_root must be an existing directory: {root}") from exc
    if not resolved_root.is_dir():
        raise SourceBundleError(f"source_root must be a directory: {root}")

    lock = _normalize_contract_bundle(contract_bundle)
    normalized_definition = _require_mapping(definition, "definition", nonempty=True)
    _validate_flow_definition(normalized_definition)
    normalized_workflow_contracts = _normalize_workflow_contracts(
        workflow_contracts,
        normalized_definition,
        derive_when_omitted=allow_legacy_wiring_only,
        allow_wiring_only=allow_legacy_wiring_only,
    )
    normalized_observer = _require_mapping(observer or {}, "observer", nonempty=False)
    local_validation = _compile_local_validation(validation, lock)

    compiled_resources = _compile_resources(resolved_root, resources)
    _validate_schema_resource_closure(resolved_root, compiled_resources)
    _validate_root_contract_resources(
        normalized_workflow_contracts, compiled_resources
    )
    implementations = _ordered_requirements(
        implementation_requirements,
        "implementation_requirements",
        identity_fields=("ref",),
    )
    profiles = _ordered_requirements(
        agent_profile_requirements,
        "agent_profile_requirements",
        identity_fields=("ref",),
    )
    capabilities = _ordered_requirements(
        capability_requirements,
        "capability_requirements",
        identity_fields=("milestone_id", "id"),
    )
    _validate_requirement_closure(
        normalized_definition,
        compiled_resources,
        implementations,
        profiles,
        capabilities,
    )

    logical_parts = (
        normalized_definition,
        normalized_workflow_contracts,
        compiled_resources,
        implementations,
        profiles,
        capabilities,
        normalized_observer,
        local_validation,
    )
    for part in logical_parts:
        _reject_non_source_state(part)

    if require_ready:
        _assert_ready(
            validation,
            (implementations, profiles, capabilities),
            normalized_definition,
        )

    bundle: dict[str, Any] = {
        "schema": SOURCE_BUNDLE_SCHEMA,
        "contract_bundle": lock,
        "definition": normalized_definition,
        "definition_digest": digest_json(normalized_definition),
        "workflow_contracts": normalized_workflow_contracts,
        "workflow_contracts_digest": digest_json(normalized_workflow_contracts),
        "resources": compiled_resources,
        "resource_requirements_digest": digest_json(compiled_resources),
        "implementation_requirements": implementations,
        "implementation_requirements_digest": digest_json(implementations),
        "agent_profile_requirements": profiles,
        "agent_profile_requirements_digest": digest_json(profiles),
        "capability_requirements": capabilities,
        "capability_requirements_digest": digest_json(capabilities),
        "observer": normalized_observer,
        "observer_digest": digest_json(normalized_observer),
        "local_validation": local_validation,
        "local_validation_digest": digest_json(local_validation),
    }
    source_bundle_digest = digest_json(bundle)
    bundle["source_bundle_proof"] = {
        "schema": SOURCE_BUNDLE_PROOF_SCHEMA,
        "contract_bundle_id": lock["id"],
        "contract_bundle_digest": lock["digest"],
        "definition_digest": bundle["definition_digest"],
        "workflow_contracts_digest": bundle["workflow_contracts_digest"],
        "resource_requirements_digest": bundle["resource_requirements_digest"],
        "implementation_requirements_digest": bundle[
            "implementation_requirements_digest"
        ],
        "agent_profile_requirements_digest": bundle[
            "agent_profile_requirements_digest"
        ],
        "capability_requirements_digest": bundle[
            "capability_requirements_digest"
        ],
        "observer_digest": bundle["observer_digest"],
        "local_validation_digest": bundle["local_validation_digest"],
        "source_bundle_digest": source_bundle_digest,
    }
    _validate_normative_source_bundle(bundle)
    verify_source_bundle(bundle)
    return bundle


def verify_source_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Verify the logical proof of a compiled bundle and return that proof.

    Resource file bytes cannot be re-read without a source root; this function
    verifies the descriptors and all manifest-level component digests.  The
    compile step is the byte-verifying boundary.
    """

    normalized = _require_mapping(bundle, "source_bundle", nonempty=True)
    _require_exact_keys(normalized, _BUNDLE_KEYS, "source_bundle")
    if normalized.get("schema") != SOURCE_BUNDLE_SCHEMA:
        raise SourceBundleError(f"source_bundle.schema must be {SOURCE_BUNDLE_SCHEMA}")
    lock = _normalize_contract_bundle(normalized.get("contract_bundle"))

    definition = _require_mapping(normalized.get("definition"), "definition", nonempty=True)
    _validate_flow_definition(definition)
    workflow_contracts = _normalize_workflow_contracts(
        normalized.get("workflow_contracts"),
        definition,
        allow_wiring_only=True,
    )
    observer = _require_mapping(normalized.get("observer"), "observer", nonempty=False)
    local_validation = _verify_local_validation(
        normalized.get("local_validation"), lock
    )
    resources = _verify_compiled_resources(normalized.get("resources"))
    _validate_root_contract_resources(workflow_contracts, resources)
    implementations = _verify_ordered_requirements(
        normalized.get("implementation_requirements"),
        "implementation_requirements",
        identity_fields=("ref",),
    )
    profiles = _verify_ordered_requirements(
        normalized.get("agent_profile_requirements"),
        "agent_profile_requirements",
        identity_fields=("ref",),
    )
    capabilities = _verify_ordered_requirements(
        normalized.get("capability_requirements"),
        "capability_requirements",
        identity_fields=("milestone_id", "id"),
    )
    _validate_requirement_closure(
        definition,
        resources,
        implementations,
        profiles,
        capabilities,
    )

    components = {
        "definition_digest": definition,
        "workflow_contracts_digest": workflow_contracts,
        "resource_requirements_digest": resources,
        "implementation_requirements_digest": implementations,
        "agent_profile_requirements_digest": profiles,
        "capability_requirements_digest": capabilities,
        "observer_digest": observer,
        "local_validation_digest": local_validation,
    }
    for digest_field, value in components.items():
        _require_digest(normalized.get(digest_field), digest_field)
        actual = digest_json(value)
        if normalized[digest_field] != actual:
            raise SourceBundleError(f"{digest_field} mismatch")

    proof = _require_mapping(
        normalized.get("source_bundle_proof"), "source_bundle_proof", nonempty=True
    )
    _require_exact_keys(proof, _PROOF_KEYS, "source_bundle_proof")
    if proof.get("schema") != SOURCE_BUNDLE_PROOF_SCHEMA:
        raise SourceBundleError(
            f"source_bundle_proof.schema must be {SOURCE_BUNDLE_PROOF_SCHEMA}"
        )
    expected_proof = {
        "schema": SOURCE_BUNDLE_PROOF_SCHEMA,
        "contract_bundle_id": lock["id"],
        "contract_bundle_digest": lock["digest"],
        **{field: normalized[field] for field in components},
        "source_bundle_digest": digest_json(
            {key: value for key, value in normalized.items() if key != "source_bundle_proof"}
        ),
    }
    if proof != expected_proof:
        differing = sorted(key for key in _PROOF_KEYS if proof.get(key) != expected_proof.get(key))
        raise SourceBundleError(
            "source_bundle_proof mismatch: " + ", ".join(differing)
        )

    _reject_non_source_state(
        {key: value for key, value in normalized.items() if key != "source_bundle_proof"}
    )
    _validate_normative_source_bundle(normalized)
    return dict(proof)


def serialize_source_bundle(bundle: Mapping[str, Any]) -> bytes:
    """Verify and serialize a source bundle without adding a timestamp/newline."""

    verify_source_bundle(bundle)
    return canonical_json(bundle)


def _normalize_contract_bundle(value: Any) -> dict[str, str]:
    lock = _require_mapping(value, "contract_bundle", nonempty=True)
    _require_exact_keys(lock, {"schema", "id", "digest"}, "contract_bundle")
    if lock.get("schema") != CONTRACT_BUNDLE_LOCK_SCHEMA:
        raise SourceBundleError(
            f"contract_bundle.schema must be {CONTRACT_BUNDLE_LOCK_SCHEMA}"
        )
    identifier = lock.get("id")
    if not isinstance(identifier, str) or not identifier or identifier != identifier.strip():
        raise SourceBundleError("contract_bundle.id must be a non-empty trimmed string")
    _require_digest(lock.get("digest"), "contract_bundle.digest")
    _reject_non_source_state(lock)
    return {"schema": CONTRACT_BUNDLE_LOCK_SCHEMA, "id": identifier, "digest": lock["digest"]}


def _compile_local_validation(
    validation: Mapping[str, Any] | None,
    contract_bundle: Mapping[str, str],
) -> dict[str, Any]:
    if validation is None:
        source: Mapping[str, Any] = {}
    elif isinstance(validation, Mapping):
        source = validation
    else:
        raise SourceBundleError("validation must be an object when supplied")
    status = source.get("status", "BUILD_REQUIRED")
    if status not in {"SOURCE_VALID", "BUILD_REQUIRED"}:
        raise SourceBundleError(
            "validation.status must be SOURCE_VALID or BUILD_REQUIRED"
        )
    findings_count = source.get("findings_count", 0)
    if (
        isinstance(findings_count, bool)
        or not isinstance(findings_count, int)
        or findings_count < 0
    ):
        raise SourceBundleError("validation.findings_count must be a non-negative integer")
    return {
        "schema": LOCAL_VALIDATION_SCHEMA,
        "builder": LOCAL_VALIDATION_BUILDER,
        "policy_version": LOCAL_VALIDATION_POLICY,
        "contract_bundle_digest": contract_bundle["digest"],
        "status": status,
        "findings_count": findings_count,
        "advisory": True,
    }


def _verify_local_validation(
    value: Any,
    contract_bundle: Mapping[str, str],
) -> dict[str, Any]:
    summary = _require_mapping(value, "local_validation", nonempty=True)
    _require_exact_keys(
        summary,
        {
            "schema",
            "builder",
            "policy_version",
            "contract_bundle_digest",
            "status",
            "findings_count",
            "advisory",
        },
        "local_validation",
    )
    if summary.get("schema") != LOCAL_VALIDATION_SCHEMA:
        raise SourceBundleError(f"local_validation.schema must be {LOCAL_VALIDATION_SCHEMA}")
    if summary.get("builder") != LOCAL_VALIDATION_BUILDER:
        raise SourceBundleError(f"local_validation.builder must be {LOCAL_VALIDATION_BUILDER}")
    if summary.get("policy_version") != LOCAL_VALIDATION_POLICY:
        raise SourceBundleError(
            f"local_validation.policy_version must be {LOCAL_VALIDATION_POLICY}"
        )
    _require_digest(
        summary.get("contract_bundle_digest"),
        "local_validation.contract_bundle_digest",
    )
    if summary["contract_bundle_digest"] != contract_bundle["digest"]:
        raise SourceBundleError(
            "local_validation.contract_bundle_digest must match contract_bundle.digest"
        )
    if summary.get("status") not in {"SOURCE_VALID", "BUILD_REQUIRED"}:
        raise SourceBundleError(
            "local_validation.status must be SOURCE_VALID or BUILD_REQUIRED"
        )
    findings_count = summary.get("findings_count")
    if (
        isinstance(findings_count, bool)
        or not isinstance(findings_count, int)
        or findings_count < 0
    ):
        raise SourceBundleError(
            "local_validation.findings_count must be a non-negative integer"
        )
    if summary.get("advisory") is not True:
        raise SourceBundleError("local_validation.advisory must be true")
    return dict(summary)


def _reject_duplicate_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SourceBundleError(f"duplicate JSON key in schema resource: {key}")
        result[key] = value
    return result


def _iter_schema_refs(value: Any, *, trail: str = "$") -> Sequence[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_trail = f"{trail}.{key}"
            if key == "$ref":
                if not isinstance(child, str):
                    raise SourceBundleError(
                        f"{child_trail}: JSON Schema $ref must be a string"
                    )
                refs.append((child_trail, child))
            else:
                refs.extend(_iter_schema_refs(child, trail=child_trail))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            refs.extend(_iter_schema_refs(child, trail=f"{trail}[{index}]"))
    return refs


def _assert_schema_fragment(document: Any, fragment: str, *, label: str) -> None:
    if not fragment:
        return
    decoded = unquote(fragment)
    if decoded.startswith("/"):
        current = document
        for raw_token in decoded[1:].split("/"):
            token = raw_token.replace("~1", "/").replace("~0", "~")
            if isinstance(current, Mapping) and token in current:
                current = current[token]
            elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
                current = current[int(token)]
            else:
                raise SourceBundleError(f"{label}: unresolved JSON Schema fragment #{decoded}")
        return
    pending = [document]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            if current.get("$anchor") == decoded or current.get("$dynamicAnchor") == decoded:
                return
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    raise SourceBundleError(f"{label}: unresolved JSON Schema anchor #{decoded}")


def _schema_dependency_path(
    source_root: Path,
    source_path: str,
    raw_ref: str,
    *,
    label: str,
) -> tuple[str, str]:
    parsed = urlsplit(raw_ref)
    if parsed.scheme or parsed.netloc or parsed.query:
        raise SourceBundleError(f"{label}: external JSON Schema refs are forbidden")
    relative = unquote(parsed.path)
    if not relative:
        return source_path, parsed.fragment
    if "\\" in relative or relative.startswith("/") or _SCHEMA_REF_GLOB_RE.search(relative):
        raise SourceBundleError(f"{label}: JSON Schema ref must be a local relative .json path")
    source = source_root.joinpath(*PurePosixPath(source_path).parts)
    candidate = source.parent.joinpath(*PurePosixPath(relative).parts)
    if candidate.is_symlink():
        raise SourceBundleError(f"{label}: symbolic JSON Schema refs are forbidden")
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise SourceBundleError(f"{label}: referenced JSON Schema is missing: {relative}") from exc
    if not resolved.is_relative_to(source_root) or resolved == source_root:
        raise SourceBundleError(f"{label}: JSON Schema ref escapes source_root")
    if not resolved.is_file() or resolved.suffix.lower() != ".json":
        raise SourceBundleError(f"{label}: JSON Schema ref must resolve to a regular .json file")
    return resolved.relative_to(source_root).as_posix(), parsed.fragment


def _validate_schema_resource_closure(
    source_root: Path,
    resources: Sequence[Mapping[str, Any]],
) -> None:
    schema_rows = {
        str(row.get("source_path") or ""): row
        for row in resources
        if row.get("kind") in {"schema", "json_schema"}
        and str(row.get("media_type") or "").partition(";")[0].strip().casefold()
        == "application/schema+json"
    }
    documents: dict[str, dict[str, Any]] = {}

    def load_document(source_path: str) -> dict[str, Any]:
        if source_path in documents:
            return documents[source_path]
        path = source_root.joinpath(*PurePosixPath(source_path).parts)
        try:
            raw = path.read_text(encoding="utf-8", errors="strict")
            value = json.loads(raw, object_pairs_hook=_reject_duplicate_json_pairs)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SourceBundleError(
                f"JSON Schema resource is unreadable or invalid: {source_path}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise SourceBundleError(f"JSON Schema resource must be an object: {source_path}")
        try:
            Draft202012Validator.check_schema(value)
        except SchemaError as exc:
            raise SourceBundleError(f"invalid JSON Schema resource {source_path}: {exc}") from exc
        documents[source_path] = value
        return value

    pending = sorted(schema_rows)
    visited: set[str] = set()
    while pending:
        source_path = pending.pop(0)
        if source_path in visited:
            continue
        visited.add(source_path)
        document = load_document(source_path)
        for trail, raw_ref in _iter_schema_refs(document):
            target_path, fragment = _schema_dependency_path(
                source_root,
                source_path,
                raw_ref,
                label=f"schema resource {source_path} {trail}",
            )
            if target_path not in schema_rows:
                raise SourceBundleError(
                    f"schema resource {source_path} has undeclared dependency: {target_path}"
                )
            target = load_document(target_path)
            _assert_schema_fragment(
                target,
                fragment,
                label=f"schema resource {source_path} {trail}",
            )
            if target_path not in visited and target_path not in pending:
                pending.append(target_path)
                pending.sort()


def _compile_resources(
    source_root: Path, resources: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    rows = _require_sequence(resources, "resources")
    compiled: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for index, raw in enumerate(rows):
        label = f"resources[{index}]"
        row = _require_mapping(raw, label, nonempty=True)
        unknown = set(row) - _RESOURCE_INPUT_KEYS
        if unknown:
            raise SourceBundleError(f"{label} has unknown fields: {', '.join(sorted(unknown))}")
        ref = _required_trimmed_string(row, "ref", label)
        if ref in seen_refs:
            raise SourceBundleError(f"duplicate resource ref: {ref}")
        seen_refs.add(ref)
        kind = _required_trimmed_string(row, "kind", label)
        role = _required_trimmed_string(row, "role", label)
        media_type = _required_trimmed_string(row, "media_type", label)
        source_path = _safe_relative_path(_required_trimmed_string(row, "source_path", label))
        path = source_root.joinpath(*PurePosixPath(source_path).parts)
        try:
            resolved = path.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise SourceBundleError(f"resource file does not exist: {source_path}") from exc
        if not resolved.is_relative_to(source_root):
            raise SourceBundleError(f"resource path escapes source_root: {source_path}")
        if not resolved.is_file():
            raise SourceBundleError(f"resource must be a regular file: {source_path}")
        try:
            payload = resolved.read_bytes()
        except OSError as exc:
            raise SourceBundleError(f"resource is unreadable: {source_path}") from exc
        payload = _canonical_resource_payload(payload, media_type, source_path)
        byte_count = len(payload)
        digest = _digest_bytes(payload)
        expected_count = row.get("byte_count")
        if expected_count is not None and expected_count != byte_count:
            raise SourceBundleError(f"resource byte_count mismatch: {source_path}")
        expected_digest = row.get("digest")
        if expected_digest is not None and expected_digest != digest:
            raise SourceBundleError(f"resource digest mismatch: {source_path}")
        compiled.append(
            {
                "ref": ref,
                "kind": kind,
                "role": role,
                "source_path": source_path,
                "media_type": media_type,
                "byte_count": byte_count,
                "digest": digest,
            }
        )
    compiled.sort(key=canonical_json)
    return compiled


def _verify_compiled_resources(value: Any) -> list[dict[str, Any]]:
    rows = _require_sequence(value, "resources")
    normalized: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for index, raw in enumerate(rows):
        label = f"resources[{index}]"
        row = _require_mapping(raw, label, nonempty=True)
        _require_exact_keys(row, _RESOURCE_INPUT_KEYS, label)
        ref = _required_trimmed_string(row, "ref", label)
        if ref in seen_refs:
            raise SourceBundleError(f"duplicate resource ref: {ref}")
        seen_refs.add(ref)
        _required_trimmed_string(row, "kind", label)
        _required_trimmed_string(row, "role", label)
        _required_trimmed_string(row, "media_type", label)
        normalized_path = _safe_relative_path(_required_trimmed_string(row, "source_path", label))
        if normalized_path != row["source_path"]:
            raise SourceBundleError(f"resource path is not canonical: {row['source_path']}")
        if isinstance(row.get("byte_count"), bool) or not isinstance(row.get("byte_count"), int):
            raise SourceBundleError(f"{label}.byte_count must be a non-negative integer")
        if row["byte_count"] < 0:
            raise SourceBundleError(f"{label}.byte_count must be a non-negative integer")
        _require_digest(row.get("digest"), f"{label}.digest")
        normalized.append(dict(row))
    if normalized != sorted(normalized, key=canonical_json):
        raise SourceBundleError("resources must use canonical order")
    return normalized


def _ordered_requirements(
    value: Sequence[Mapping[str, Any]],
    label: str,
    *,
    identity_fields: tuple[str, ...],
    identity_any: bool = False,
) -> list[dict[str, Any]]:
    rows = _require_sequence(value, label)
    normalized: list[dict[str, Any]] = []
    identities: set[tuple[tuple[str, str], ...]] = set()
    for index, raw in enumerate(rows):
        row_label = f"{label}[{index}]"
        row = _require_mapping(raw, row_label, nonempty=True)
        _reject_non_source_state(row)
        identity = _require_requirement_identity(
            row, row_label, identity_fields, identity_any
        )
        if identity in identities:
            raise SourceBundleError(
                f"{label} contains duplicate logical identity: "
                + ", ".join(f"{field}={item}" for field, item in identity)
            )
        identities.add(identity)
        _validate_requirement_shape(row, row_label, label)
        normalized.append(row)
    ordered = sorted(normalized, key=canonical_json)
    encoded = [canonical_json(row) for row in ordered]
    if len(encoded) != len(set(encoded)):
        raise SourceBundleError(f"{label} contains a duplicate requirement")
    return ordered


def _verify_ordered_requirements(
    value: Any,
    label: str,
    *,
    identity_fields: tuple[str, ...],
    identity_any: bool = False,
) -> list[dict[str, Any]]:
    rows = _require_sequence(value, label)
    normalized: list[dict[str, Any]] = []
    identities: set[tuple[tuple[str, str], ...]] = set()
    for index, raw in enumerate(rows):
        row_label = f"{label}[{index}]"
        row = _require_mapping(raw, row_label, nonempty=True)
        _reject_non_source_state(row)
        identity = _require_requirement_identity(
            row, row_label, identity_fields, identity_any
        )
        if identity in identities:
            raise SourceBundleError(
                f"{label} contains duplicate logical identity: "
                + ", ".join(f"{field}={item}" for field, item in identity)
            )
        identities.add(identity)
        _validate_requirement_shape(row, row_label, label)
        normalized.append(row)
    if normalized != sorted(normalized, key=canonical_json):
        raise SourceBundleError(f"{label} must use canonical order")
    encoded = [canonical_json(row) for row in normalized]
    if len(encoded) != len(set(encoded)):
        raise SourceBundleError(f"{label} contains a duplicate requirement")
    return normalized


def _require_requirement_identity(
    row: Mapping[str, Any],
    label: str,
    identity_fields: tuple[str, ...],
    identity_any: bool,
) -> tuple[tuple[str, str], ...]:
    present = []
    for field in identity_fields:
        value = row.get(field)
        if value is not None:
            if not isinstance(value, str) or not value or value != value.strip():
                raise SourceBundleError(f"{label}.{field} must be a non-empty trimmed string")
            present.append(field)
    if identity_any:
        if len(present) != 1:
            raise SourceBundleError(
                f"{label} requires exactly one logical identity field: "
                f"{', '.join(identity_fields)}"
            )
    elif set(present) != set(identity_fields):
        raise SourceBundleError(f"{label} requires {', '.join(identity_fields)}")
    return tuple((field, str(row[field])) for field in identity_fields)


def _validate_requirement_shape(
    row: Mapping[str, Any],
    row_label: str,
    group: str,
) -> None:
    if group == "implementation_requirements":
        _validate_implementation_requirement(row, row_label)
        return
    if group == "agent_profile_requirements":
        _validate_agent_profile_requirement(row, row_label)
        return
    if group == "capability_requirements":
        _validate_capability_requirement(row, row_label)
        return
    raise SourceBundleError(f"unsupported requirement group: {group}")


def _validate_implementation_requirement(
    row: Mapping[str, Any], label: str
) -> None:
    base = {
        "ref",
        "kind",
        "role",
        "runtime_abi",
        "entrypoint",
        "build_state",
    }
    kind = _required_trimmed_string(row, "kind", label)
    if kind not in _IMPLEMENTATION_KINDS:
        raise SourceBundleError(
            f"{label}.kind must be one of: {', '.join(sorted(_IMPLEMENTATION_KINDS))}"
        )
    if kind == "flowstep_tool":
        size_field = "member_count"
        allowed = base | {"local_name", "digest", size_field}
    else:
        size_field = "byte_count"
        allowed = base | {"milestone_id", "digest", size_field}
    _require_allowed_and_required_keys(row, allowed=allowed, required=base, label=label)
    _required_trimmed_string(row, "ref", label)
    _required_trimmed_string(row, "role", label)
    runtime_abi = _required_trimmed_string(row, "runtime_abi", label)
    if runtime_abi != _IMPLEMENTATION_ABIS[kind]:
        raise SourceBundleError(
            f"{label}.runtime_abi must be {_IMPLEMENTATION_ABIS[kind]} for {kind}"
        )
    _required_trimmed_string(row, "entrypoint", label)
    if "milestone_id" in row:
        milestone_id = _required_trimmed_string(row, "milestone_id", label)
        if not _FLOW_ID_RE.fullmatch(milestone_id):
            raise SourceBundleError(f"{label}.milestone_id is invalid")
    if "local_name" in row:
        local_name = _required_trimmed_string(row, "local_name", label)
        if not _FLOW_ID_RE.fullmatch(local_name):
            raise SourceBundleError(f"{label}.local_name is invalid")
    state = _require_build_state(row, label)
    count = row.get(size_field)
    if count is not None and (
        isinstance(count, bool) or not isinstance(count, int) or count < 0
    ):
        raise SourceBundleError(f"{label}.{size_field} must be a non-negative integer")
    if state == "built":
        _require_digest(row.get("digest"), f"{label}.digest")
        if count is None:
            raise SourceBundleError(f"{label} is missing fields: {size_field}")
    elif row.get("digest") is not None:
        _require_digest(row.get("digest"), f"{label}.digest")


def _validate_agent_profile_requirement(
    row: Mapping[str, Any], label: str
) -> None:
    built_fields = {
        "executor_ref",
        "gem_ref",
        "schema_refs",
        "tool_refs",
        "capability_ids",
        "model_configuration",
        "token_budget",
        "timeout_seconds",
    }
    _require_allowed_and_required_keys(
        row,
        allowed={
            "ref",
            "milestone_id",
            "role",
            "build_state",
            "missing",
            *built_fields,
        },
        required={"ref", "milestone_id", "role", "build_state"},
        label=label,
    )
    _required_trimmed_string(row, "ref", label)
    milestone_id = _required_trimmed_string(row, "milestone_id", label)
    if not _FLOW_ID_RE.fullmatch(milestone_id):
        raise SourceBundleError(f"{label}.milestone_id is invalid")
    role = _required_trimmed_string(row, "role", label)
    if role not in _PROFILE_ROLES:
        raise SourceBundleError(
            f"{label}.role must be one of: {', '.join(sorted(_PROFILE_ROLES))}"
        )
    state = _require_build_state(row, label)
    if state == "BUILD_REQUIRED":
        missing = _require_sequence(row.get("missing"), f"{label}.missing")
        if not missing:
            raise SourceBundleError(f"{label}.missing must not be empty")
        for index, item in enumerate(missing):
            if not isinstance(item, str) or not item or item != item.strip():
                raise SourceBundleError(
                    f"{label}.missing[{index}] must be a non-empty trimmed string"
                )
        if len(missing) != len(set(missing)):
            raise SourceBundleError(f"{label}.missing must be unique")
        return

    absent = built_fields - set(row)
    if absent:
        raise SourceBundleError(
            f"{label} is missing fields: {', '.join(sorted(absent))}"
        )
    if "missing" in row:
        raise SourceBundleError(f"{label}.missing is forbidden for built profiles")
    for field in ("executor_ref", "gem_ref"):
        _required_trimmed_string(row, field, label)
    schema_refs = _require_mapping(
        row["schema_refs"], f"{label}.schema_refs", nonempty=True
    )
    _require_allowed_and_required_keys(
        schema_refs,
        allowed={"input_schema", "draft_schema", "output_schema", "receipt_schema"},
        required={"input_schema", "output_schema"},
        label=f"{label}.schema_refs",
    )
    for field in schema_refs:
        _required_trimmed_string(schema_refs, field, f"{label}.schema_refs")
    required_schema_fields = (
        {"input_schema", "draft_schema", "output_schema"}
        if role == "candidate_executor"
        else {"input_schema", "output_schema", "receipt_schema"}
    )
    if set(schema_refs) != required_schema_fields:
        raise SourceBundleError(
            f"{label}.schema_refs must exactly contain: "
            + ", ".join(sorted(required_schema_fields))
        )
    for field in ("tool_refs", "capability_ids"):
        values = _require_sequence(row[field], f"{label}.{field}")
        for index, item in enumerate(values):
            if not isinstance(item, str) or not item or item != item.strip():
                raise SourceBundleError(
                    f"{label}.{field}[{index}] must be a non-empty trimmed string"
                )
        if len(values) != len(set(values)):
            raise SourceBundleError(f"{label}.{field} must be unique")
    config = _require_mapping(
        row["model_configuration"], f"{label}.model_configuration", nonempty=True
    )
    _require_allowed_and_required_keys(
        config,
        allowed={"model", "reasoning", "temperature"},
        required={"model", "reasoning"},
        label=f"{label}.model_configuration",
    )
    _required_trimmed_string(config, "model", f"{label}.model_configuration")
    if config["reasoning"] not in _PROFILE_REASONING:
        raise SourceBundleError(
            f"{label}.model_configuration.reasoning is not supported"
        )
    if "temperature" in config:
        temperature = config["temperature"]
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not 0 <= temperature <= 2
        ):
            raise SourceBundleError(
                f"{label}.model_configuration.temperature must be between 0 and 2"
            )
    budget = _require_mapping(
        row["token_budget"], f"{label}.token_budget", nonempty=True
    )
    _require_allowed_and_required_keys(
        budget,
        allowed={"max_input_tokens", "max_output_tokens"},
        required={"max_input_tokens", "max_output_tokens"},
        label=f"{label}.token_budget",
    )
    for field in ("max_input_tokens", "max_output_tokens"):
        tokens = budget[field]
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 1:
            raise SourceBundleError(f"{label}.token_budget.{field} must be positive")
    timeout = row["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise SourceBundleError(f"{label}.timeout_seconds must be a positive integer")


def _validate_capability_requirement(
    row: Mapping[str, Any], label: str
) -> None:
    _require_allowed_and_required_keys(
        row,
        allowed={
            "id",
            "ref",
            "milestone_id",
            "access",
            "side_effects",
            "build_state",
            "phase_journal",
            "missing",
        },
        required={"id", "ref", "milestone_id", "access", "side_effects", "build_state"},
        label=label,
    )
    _required_trimmed_string(row, "id", label)
    capability_ref = _required_trimmed_string(row, "ref", label)
    if not _VERSIONED_REF_RE.fullmatch(capability_ref):
        raise SourceBundleError(f"{label}.ref must be a versioned capability ref")
    milestone_id = _required_trimmed_string(row, "milestone_id", label)
    if not _FLOW_ID_RE.fullmatch(milestone_id):
        raise SourceBundleError(f"{label}.milestone_id is invalid")
    if row.get("access") not in _CAPABILITY_ACCESS:
        raise SourceBundleError(f"{label}.access is not supported")
    if row.get("side_effects") not in _CAPABILITY_SIDE_EFFECTS:
        raise SourceBundleError(f"{label}.side_effects is not supported")
    state = _require_build_state(row, label)
    if row.get("side_effects") == "external" and (
        state == "built" or "phase_journal" in row
    ):
        journal = _require_mapping(
            row.get("phase_journal"), f"{label}.phase_journal", nonempty=True
        )
        _require_allowed_and_required_keys(
            journal,
            allowed={"path", "operator_result_path", "resume"},
            required={"path", "operator_result_path", "resume"},
            label=f"{label}.phase_journal",
        )
        for field in ("path", "operator_result_path"):
            _required_trimmed_string(journal, field, f"{label}.phase_journal")
        if journal["resume"] != "query_exact_operation":
            raise SourceBundleError(
                f"{label}.phase_journal.resume must be query_exact_operation"
            )
    elif "phase_journal" in row:
        raise SourceBundleError(
            f"{label}.phase_journal requires side_effects external"
        )
    if state == "BUILD_REQUIRED":
        missing = _require_sequence(row.get("missing"), f"{label}.missing")
        if not missing:
            raise SourceBundleError(f"{label}.missing must not be empty")
        normalized_missing = [
            _required_trimmed_string({"value": item}, "value", f"{label}.missing[{index}]")
            for index, item in enumerate(missing)
        ]
        if len(normalized_missing) != len(set(normalized_missing)):
            raise SourceBundleError(f"{label}.missing must be unique")
    elif "missing" in row:
        raise SourceBundleError(f"{label}.missing is forbidden when built")


def _require_allowed_and_required_keys(
    value: Mapping[str, Any],
    *,
    allowed: set[str],
    required: set[str],
    label: str,
) -> None:
    actual = set(value)
    unknown = actual - allowed
    missing = required - actual
    if unknown:
        raise SourceBundleError(f"{label} has unknown fields: {', '.join(sorted(unknown))}")
    if missing:
        raise SourceBundleError(f"{label} is missing fields: {', '.join(sorted(missing))}")


def _require_build_state(value: Mapping[str, Any], label: str) -> str:
    state = value.get("build_state")
    if state not in _BUILD_STATES:
        raise SourceBundleError(
            f"{label}.build_state must be one of: {', '.join(sorted(_BUILD_STATES))}"
        )
    return str(state)


def _validate_requirement_closure(
    definition: Mapping[str, Any],
    resources: Sequence[Mapping[str, Any]],
    implementations: Sequence[Mapping[str, Any]],
    profiles: Sequence[Mapping[str, Any]],
    capabilities: Sequence[Mapping[str, Any]],
) -> None:
    """Cross-check logical requirement rows against their owning milestone.

    Individual row validation is insufficient: a perfectly shaped profile that
    points at another milestone's Gem or an unrelated tool is not executable
    closure.  This check is deliberately source-only; it never resolves cloud
    IDs or claims that a requirement has been admitted by a registry.
    """

    milestones = {
        str(item.get("id")): item
        for item in definition.get("milestones") or []
        if isinstance(item, Mapping) and item.get("id")
    }
    resource_by_ref = {str(item.get("ref")): item for item in resources}
    implementation_by_ref = {str(item.get("ref")): item for item in implementations}
    capability_by_identity = {
        (str(item.get("milestone_id") or ""), str(item.get("id") or "")): item
        for item in capabilities
        if item.get("id")
    }
    profiles_by_milestone: dict[str, list[Mapping[str, Any]]] = {
        milestone_id: [] for milestone_id in milestones
    }
    capabilities_by_milestone: dict[str, list[Mapping[str, Any]]] = {
        milestone_id: [] for milestone_id in milestones
    }

    for capability in capabilities:
        if "id" not in capability:
            continue
        milestone_id = str(capability.get("milestone_id") or "")
        if milestone_id not in milestones:
            raise SourceBundleError(
                f"capability requirement {capability.get('id')} names unknown milestone {milestone_id}"
            )
        capabilities_by_milestone[milestone_id].append(capability)

    for profile in profiles:
        milestone_id = str(profile.get("milestone_id") or "")
        milestone = milestones.get(milestone_id)
        if milestone is None:
            raise SourceBundleError(
                f"agent profile {profile.get('ref')} names unknown milestone {milestone_id}"
            )
        profiles_by_milestone[milestone_id].append(profile)
        if profile.get("build_state") != "built":
            continue

        role = str(profile.get("role") or "")
        executor_ref = str(profile.get("executor_ref") or "")
        executor = implementation_by_ref.get(executor_ref)
        expected_kind = "milestone_handler" if role == "candidate_executor" else "milestone_judge"
        if executor is None or executor.get("kind") != expected_kind:
            raise SourceBundleError(
                f"agent profile {profile.get('ref')} executor_ref does not bind a {expected_kind}"
            )
        if expected_kind == "milestone_handler":
            if executor.get("milestone_id") != milestone_id:
                raise SourceBundleError(
                    f"agent profile {profile.get('ref')} executor belongs to another milestone"
                )
        else:
            execution = (
                milestone.get("execution")
                if isinstance(milestone.get("execution"), Mapping)
                else {}
            )
            judge_binding = (
                execution.get("judge")
                if isinstance(execution.get("judge"), Mapping)
                else {}
            )
            if str(judge_binding.get("ref") or "") != executor_ref:
                raise SourceBundleError(
                    f"agent profile {profile.get('ref')} executor_ref does not bind "
                    "the milestone's exact execution.judge ref"
                )
            if executor.get("runtime_abi") != milestone.get("judge_abi"):
                raise SourceBundleError(
                    f"agent profile {profile.get('ref')} executor_ref does not bind "
                    "the milestone's exact judge ABI"
                )
        if executor.get("build_state") != "built":
            raise SourceBundleError(
                f"agent profile {profile.get('ref')} executor_ref is BUILD_REQUIRED"
            )

        gem_ref = str(profile.get("gem_ref") or "")
        gem = resource_by_ref.get(gem_ref)
        if (
            gem is None
            or gem.get("kind") != "gem"
            or gem.get("source_path") != milestone.get("gem")
        ):
            raise SourceBundleError(
                f"agent profile {profile.get('ref')} gem_ref does not bind its exact milestone Gem"
            )

        schema_refs = profile.get("schema_refs") or {}
        for field, ref in schema_refs.items():
            resource = resource_by_ref.get(str(ref))
            if (
                resource is None
                or resource.get("kind") not in {"json_schema", "schema"}
                or resource.get("source_path") != milestone.get(field)
            ):
                raise SourceBundleError(
                    f"agent profile {profile.get('ref')} {field} does not bind its exact schema"
                )

        tool_refs = list(profile.get("tool_refs") or [])
        for ref in tool_refs:
            requirement = implementation_by_ref.get(str(ref))
            if requirement is None or requirement.get("kind") != "flowstep_tool":
                raise SourceBundleError(
                    f"agent profile {profile.get('ref')} tool_refs contains an unknown FlowStep tool"
                )
            if requirement.get("build_state") != "built":
                raise SourceBundleError(
                    f"agent profile {profile.get('ref')} tool_ref {ref} is BUILD_REQUIRED"
                )
        if role == "candidate_executor":
            execution = (
                milestone.get("execution")
                if isinstance(milestone.get("execution"), Mapping)
                else {}
            )
            bindings = execution.get("tool_bindings") or []
            expected_refs = list(
                dict.fromkeys(
                    str(item.get("ref") or "")
                    for item in bindings
                    if isinstance(item, Mapping)
                )
            )
            if tool_refs != expected_refs:
                raise SourceBundleError(
                    f"agent profile {profile.get('ref')} tool_refs do not exactly bind "
                    "the milestone's ordered execution.tool_bindings refs"
                )
        elif tool_refs:
            raise SourceBundleError(
                f"AI judge profile {profile.get('ref')} must not bind candidate FlowStep tools"
            )

        for capability_id in profile.get("capability_ids") or []:
            capability = capability_by_identity.get(
                (milestone_id, str(capability_id))
            )
            if capability is None:
                raise SourceBundleError(
                    f"agent profile {profile.get('ref')} capability_ids contains an unowned capability"
                )
            if capability.get("build_state") != "built":
                raise SourceBundleError(
                    f"agent profile {profile.get('ref')} capability {capability_id} is BUILD_REQUIRED"
                )

    for milestone_id, milestone in milestones.items():
        rows = profiles_by_milestone[milestone_id]
        roles = [str(item.get("role") or "") for item in rows]
        if len(roles) != len(set(roles)):
            raise SourceBundleError(
                f"milestone {milestone_id} has duplicate agent profile roles"
            )
        intelligence = str(milestone.get("intelligence") or "none")
        if intelligence != "none" and "candidate_executor" not in roles:
            raise SourceBundleError(
                f"milestone {milestone_id} requires a candidate_executor profile requirement"
            )
        if intelligence == "judge" and "ai_judge" not in roles:
            raise SourceBundleError(
                f"milestone {milestone_id} requires a separate ai_judge profile requirement"
            )
        if str(milestone.get("loop") or "none") == "judge":
            execution = (
                milestone.get("execution")
                if isinstance(milestone.get("execution"), Mapping)
                else {}
            )
            judge_binding = (
                execution.get("judge")
                if isinstance(execution.get("judge"), Mapping)
                else {}
            )
            judge_ref = str(judge_binding.get("ref") or "")
            judge = implementation_by_ref.get(judge_ref)
            if (
                judge is None
                or judge.get("kind") != "milestone_judge"
                or judge.get("runtime_abi") != milestone.get("judge_abi")
            ):
                raise SourceBundleError(
                    f"milestone {milestone_id} requires one exact milestone_judge requirement"
                )
        milestone_caps = capabilities_by_milestone[milestone_id]
        milestone_external = milestone.get("side_effects") == "external"
        capability_external = any(
            item.get("side_effects") == "external" for item in milestone_caps
        )
        if milestone_external and not capability_external:
            raise SourceBundleError(
                f"milestone {milestone_id} requires an external capability requirement"
            )
        if capability_external and not milestone_external:
            raise SourceBundleError(
                f"milestone {milestone_id} has an external capability but side_effects is not external"
            )
        if rows and all(item.get("build_state") == "built" for item in rows):
            bound_capabilities = {
                str(item)
                for profile in rows
                if profile.get("build_state") == "built"
                for item in profile.get("capability_ids") or []
            }
            declared_capabilities = {
                str(item.get("id"))
                for item in milestone_caps
                if item.get("id")
            }
            if bound_capabilities != declared_capabilities:
                raise SourceBundleError(
                    f"milestone {milestone_id} profiles do not exactly bind declared capabilities"
                )


def _assert_ready(
    validation: Mapping[str, Any] | None,
    requirement_groups: Sequence[Sequence[Mapping[str, Any]]],
    definition: Mapping[str, Any],
) -> None:
    if validation is None or not isinstance(validation, Mapping):
        raise SourceBundleError("ready compile requires local validation status SOURCE_VALID")
    if validation.get("status") != "SOURCE_VALID":
        raise SourceBundleError("ready compile requires local validation status SOURCE_VALID")
    findings_count = validation.get("findings_count", 0)
    if isinstance(findings_count, bool) or not isinstance(findings_count, int) or findings_count != 0:
        raise SourceBundleError("ready compile requires findings_count 0")
    for group in requirement_groups:
        for requirement in group:
            state = requirement.get("build_state")
            if state != "built":
                identity = requirement.get("ref") or requirement.get("id") or "requirement"
                raise SourceBundleError(
                    f"ready compile requires build_state built; {identity} is {state}"
                )

    implementations = list(requirement_groups[0]) if requirement_groups else []
    for milestone in definition.get("milestones") or []:
        if not isinstance(milestone, Mapping):
            continue
        milestone_id = str(milestone.get("id") or "")
        handlers = [
            row
            for row in implementations
            if row.get("kind") == "milestone_handler"
            and row.get("milestone_id") == milestone_id
        ]
        if len(handlers) != 1:
            raise SourceBundleError(
                f"ready compile requires one exact milestone_handler for {milestone_id}"
            )
        if handlers[0].get("role") != "candidate_executor":
            raise SourceBundleError(
                f"ready compile handler role does not match milestone {milestone_id}"
            )

        judge_loop = str(milestone.get("loop") or "none") == "judge"
        flowsteps = [
            item
            for item in milestone.get("flowsteps") or []
            if isinstance(item, Mapping)
        ]
        execution = (
            milestone.get("execution")
            if isinstance(milestone.get("execution"), Mapping)
            else {}
        )
        bindings = [
            item
            for item in execution.get("tool_bindings") or []
            if isinstance(item, Mapping)
        ]
        slots = [str(item.get("id") or "") for item in flowsteps]
        if [str(item) for item in milestone.get("tools") or []] != slots:
            raise SourceBundleError(
                f"ready compile requires milestone tools to equal FlowStep ids for {milestone_id}"
            )
        if len(bindings) != len(flowsteps):
            raise SourceBundleError(
                f"ready compile requires one exact tool binding per FlowStep for {milestone_id}"
            )
        for flowstep, binding in zip(flowsteps, bindings):
            slot = str(flowstep.get("id") or "")
            exact_ref = str(binding.get("ref") or "")
            if str(binding.get("tool") or "") != slot or exact_ref != str(
                flowstep.get("tool") or ""
            ):
                raise SourceBundleError(
                    f"ready compile FlowStep binding does not match slot {slot} for {milestone_id}"
                )
            matches = [
                row
                for row in implementations
                if row.get("kind") == "flowstep_tool"
                and row.get("ref") == exact_ref
            ]
            if len(matches) != 1:
                raise SourceBundleError(
                    f"ready compile requires one exact FlowStep implementation {exact_ref} "
                    f"for slot {slot} on {milestone_id}"
                )

        judges = [
            row
            for row in implementations
            if row.get("kind") == "milestone_judge"
            and row.get("milestone_id") == milestone_id
        ]
        if judge_loop and len(judges) != 1:
            raise SourceBundleError(
                f"ready compile requires one exact milestone_judge for {milestone_id}"
            )


def _reject_non_source_state(value: Any, *, trail: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = _normalized_policy_key(key)
            if lowered in _FORBIDDEN_IDENTITY_TOKENS:
                raise SourceBundleError(f"cloud/run identity is forbidden at {trail}.{key}")
            if (
                lowered in _FORBIDDEN_TIME_TOKENS
                or lowered.endswith("timestamp")
            ):
                raise SourceBundleError(f"timestamp metadata is forbidden at {trail}.{key}")
            if lowered in _FORBIDDEN_VALIDATION_TOKENS:
                raise SourceBundleError(f"validation prose is forbidden at {trail}.{key}")
            if lowered in _FORBIDDEN_SECRET_TOKENS:
                raise SourceBundleError(f"credential-bearing field is forbidden at {trail}.{key}")
            _reject_non_source_state(item, trail=f"{trail}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_non_source_state(item, trail=f"{trail}[{index}]")
    elif isinstance(value, str) and _looks_like_unsafe_path(value):
        raise SourceBundleError(f"absolute or unsafe local path is forbidden at {trail}")


def _normalized_policy_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


_FORBIDDEN_IDENTITY_TOKENS = frozenset(
    _normalized_policy_key(item) for item in _FORBIDDEN_IDENTITY_KEYS
)
_FORBIDDEN_TIME_TOKENS = frozenset(
    _normalized_policy_key(item) for item in _FORBIDDEN_TIME_KEYS
)
_FORBIDDEN_VALIDATION_TOKENS = frozenset(
    _normalized_policy_key(item) for item in _FORBIDDEN_VALIDATION_KEYS
)
_FORBIDDEN_SECRET_TOKENS = frozenset(
    _normalized_policy_key(item) for item in _FORBIDDEN_SECRET_KEYS
)


def _is_textual_media_type(media_type: str) -> bool:
    normalized = media_type.partition(";")[0].strip().casefold()
    return (
        normalized.startswith("text/")
        or normalized in _TEXTUAL_MEDIA_TYPES
        or normalized.endswith("+json")
        or normalized.endswith("+xml")
        or normalized.endswith("+yaml")
    )


def _canonical_resource_payload(
    payload: bytes,
    media_type: str,
    source_path: str,
) -> bytes:
    if not _is_textual_media_type(media_type):
        return payload
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceBundleError(
            f"text resource must be valid UTF-8: {source_path}"
        ) from exc
    canonical = (
        unicodedata.normalize("NFC", text)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    credential_scan = unicodedata.normalize("NFKC", canonical)
    for label, pattern in _CREDENTIAL_TEXT_PATTERNS:
        if pattern.search(credential_scan):
            raise SourceBundleError(
                f"credential material ({label}) is forbidden in text resource: {source_path}"
            )
    return canonical.encode("utf-8")


def source_bundle_resource_bytes(
    source_root: Path | str,
    resource: Mapping[str, Any],
) -> bytes:
    """Read and re-verify one source-bundle resource from its closed root.

    Transport packaging is deliberately separate from source compilation, so
    it must detect any resource drift between those two operations.  Text uses
    the same UTF-8/NFC/LF canonicalization as ``compile_source_bundle``;
    binaries remain byte-exact.
    """

    root = Path(source_root).resolve()
    if not root.is_dir():
        raise SourceBundleError("source bundle resource root is unavailable")
    row = _require_mapping(resource, "resource", nonempty=True)
    source_path = _safe_relative_path(
        _required_trimmed_string(row, "source_path", "resource")
    )
    media_type = _required_trimmed_string(row, "media_type", "resource")
    source = root.joinpath(*PurePosixPath(source_path).parts).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise SourceBundleError(
            f"source bundle resource escapes its root: {source_path}"
        ) from exc
    if not source.is_file():
        raise SourceBundleError(
            f"source bundle resource is missing or not a regular file: {source_path}"
        )
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise SourceBundleError(
            f"cannot read source bundle resource: {source_path}"
        ) from exc
    canonical = _canonical_resource_payload(payload, media_type, source_path)
    byte_count = row.get("byte_count")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int):
        raise SourceBundleError(
            f"resource byte_count is invalid: {source_path}"
        )
    if len(canonical) != byte_count:
        raise SourceBundleError(
            f"source bundle resource byte count changed: {source_path}"
        )
    digest = _require_digest(row.get("digest"), f"resource digest {source_path}")
    if _digest_bytes(canonical) != digest:
        raise SourceBundleError(
            f"source bundle resource digest changed: {source_path}"
        )
    return canonical


def _looks_like_unsafe_path(value: str) -> bool:
    raw = value.strip()
    if not raw:
        return False
    lowered = raw.casefold()
    if lowered.startswith("file://"):
        return True
    windows = PureWindowsPath(raw)
    if windows.is_absolute() or bool(windows.drive) or raw.startswith(("\\\\", "//")):
        return True
    if PurePosixPath(raw).is_absolute():
        return True
    if ("/" in raw or "\\" in raw) and ".." in PurePosixPath(raw.replace("\\", "/")).parts:
        return True
    return False


def _safe_relative_path(value: str) -> str:
    raw = unicodedata.normalize("NFC", value).replace("\\", "/")
    if _looks_like_unsafe_path(raw):
        raise SourceBundleError(f"unsafe resource path: {value}")
    path = PurePosixPath(raw)
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SourceBundleError(f"unsafe resource path: {value}")
    canonical = path.as_posix()
    if ":" in canonical or canonical.startswith("/"):
        raise SourceBundleError(f"unsafe resource path: {value}")
    return canonical


def _normalize_json(value: Any, *, trail: str, key_hint: str | None) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SourceBundleError(f"non-finite number is forbidden at {trail}")
        return value
    if isinstance(value, str):
        text = unicodedata.normalize("NFC", value)
        if key_hint in _PATH_FIELD_NAMES or (key_hint or "").endswith("_path"):
            text = text.replace("\\", "/")
        return text
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise SourceBundleError(f"mapping key must be a string at {trail}")
            key = unicodedata.normalize("NFC", raw_key)
            if key in normalized:
                raise SourceBundleError(f"duplicate key after Unicode normalization at {trail}.{key}")
            normalized[key] = _normalize_json(
                item,
                trail=f"{trail}.{key}",
                key_hint=key,
            )
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _normalize_json(item, trail=f"{trail}[{index}]", key_hint=key_hint)
            for index, item in enumerate(value)
        ]
    raise SourceBundleError(f"non-JSON value {type(value).__name__} is forbidden at {trail}")


def _require_mapping(value: Any, label: str, *, nonempty: bool) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceBundleError(f"{label} must be an object")
    normalized = _normalize_json(value, trail=label, key_hint=None)
    if nonempty and not normalized:
        raise SourceBundleError(f"{label} must not be empty")
    return normalized


def _require_sequence(value: Any, label: str) -> list[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise SourceBundleError(f"{label} must be an array")
    return list(value)


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise SourceBundleError(f"{label} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise SourceBundleError(f"{label} has unknown fields: {', '.join(sorted(unknown))}")


def _required_trimmed_string(value: Mapping[str, Any], field: str, label: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item or item != item.strip():
        raise SourceBundleError(f"{label}.{field} must be a non-empty trimmed string")
    return item


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SourceBundleError(f"{label} must be sha256:<64 lowercase hex>")
    return value


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


__all__ = [
    "CONTRACT_BUNDLE_LOCK_SCHEMA",
    "FLOW_SCHEMA",
    "SOURCE_BUNDLE_PROOF_SCHEMA",
    "SOURCE_BUNDLE_SCHEMA",
    "SourceBundleError",
    "canonical_json",
    "compile_source_bundle",
    "digest_json",
    "serialize_source_bundle",
    "source_bundle_resource_bytes",
    "verify_source_bundle",
]
