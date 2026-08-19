# M8M architecture

Protocol reference. The skill file owns the engineering method. This
file owns YAML, the tool protocol, and the driver.

```text
Milestone  = canvas node. this.in = previous.out
FlowStep   = tool-heavy unit inside a milestone
Tool       = premade Python at flowsteps/tools/<id>/
```

v3 product **milestones** live at `<codebase>/flowsteps/flows/<flow_id>/`.
Reusable **tools** live at `<codebase>/flowsteps/tools/<tool_id>/`.
See `milestone.md`.

## Control plane

```text
driver
  -> bind typed inputs (previous data or user.request)
  -> validate input.schema.json
  -> steps/<id>/tool.py run()
  -> validate output.schema.json
  -> wrap flowstep_output_v2
  -> next step

if preferred FlowStep tool fails
  -> NEED_MODEL (agent recovery, like a normal skill)
  -> write work/<id>/model_request.json
  -> agent writes work/<id>/draft.json
  -> assemble runs again with draft
  -> milestone asset schema still must PASS or BLOCK
```

There is one execution mode: `tool`. `class` is `tool` or `intelligence`
(see `tool-vs-intelligence.md`). `model` is only set on intelligence.
A name like `crop_*` / `fetch_*` is a **note** (looks like a tool);
`judge_*` / `choose_*` looks like intelligence. The writer still draws.

Every **milestone** output schema is a required asset (file, image, json
proof, or data), closed, with `required` fields. Runtime BLOCKs if that
schema does not PASS. Next milestone does not start.

A generated **tool** stub is a successful sketch. Fill in `tool.py` later.
`validate_harness.py` is optional (tools). `run_flow.py` is the harness.

Do not set `max_run_repair_cycles`. A BLOCKED run stays BLOCKED; start a
new run after changing tools.

## Flow YAML

```yaml
schema: flowstep_flow_v2
flow_id: text_pipeline_v1
version: 1
max_run_seconds: 3600
artifact_root: artifacts
steps:
  - id: ingest
    kind: source.ingest
    class: tool
    handler: steps/ingest/tool.py
    model: none
    inputs:
      request: user.request
    output_contract: ingest_v1
    input_schema: steps/ingest/input.schema.json
    output_schema: steps/ingest/output.schema.json
  - id: segment
    kind: text.segment
    class: tool
    handler: steps/segment/tool.py
    model: none
    inputs:
      ingest: ingest.ingest_v1
    output_contract: segment_v1
    input_schema: steps/segment/input.schema.json
    output_schema: steps/segment/output.schema.json
  - id: label
    kind: text.label
    class: intelligence
    handler: steps/label/tool.py
    model: completion
    model_justification: semantic class is not derivable from punctuation alone
    draft_schema: steps/label/draft.schema.json
    inputs:
      segment: segment.segment_v1
    output_contract: label_v1
    input_schema: steps/label/input.schema.json
    output_schema: steps/label/output.schema.json
```

`inputs` values are `user.request` or `<earlier_step>.<output_contract>`.
The driver puts the upstream `data` object under the input name. The input
schema for `segment` therefore requires `ingest` and `$ref`s
`../ingest/output.schema.json`.

v1 fields are rejected: `execution_mode`, `assigned_agent`,
`persistent_worker`, `max_subagent_roles`.

## Tool protocol

```python
def run(input_data: dict, draft: dict | None = None, **kwargs) -> dict:
    ...
```

Return values:

| Return | Driver |
| --- | --- |
| payload object | validate against `output.schema.json`, write artifact, continue |
| `{'_flowstep':'NEED_MODEL','model':...,'model_request':{...}}` | stop with `ACTION_REQUIRED` |
| `{'_flowstep':'BLOCKED','blockers':[...]}` | write BLOCKED artifact and stop |
| exception | BLOCKED |

`model: none` tools may not return `NEED_MODEL`. The payload must not use the
key `_flowstep`.

## Envelope

The driver, not the tool, writes:

```json
{
  "schema": "<output_contract>",
  "artifact_id": "<contract>:<run_id>:<step_id>",
  "run_id": "<run_id>",
  "flow_id": "<flow_id>",
  "flow_version": 1,
  "step_id": "<step_id>",
  "status": "PASS",
  "data": {},
  "evidence": {
    "handler": "steps/<id>/tool.py",
    "model": "none",
    "attempt": 1,
    "input_artifacts": [],
    "implementation_fingerprint_sha256": "<sha256>",
    "blockers": []
  },
  "created_at": "<iso8601>"
}
```

`data` is the output schema. A file is a `file_ref_v2` object, not a bare
path. Required fields are `path` and `sha256`. Add `content_schema` when the
bytes are JSON that the next step must understand. `$ref` the shared
`contracts/file_ref_v2.schema.json`. A `*_path` property without a sibling
`sha256` fails `validate_harness.py`.

## Run layout

```text
<run-dir>/
  request.json
  implementation-lock.json
  flow-execution-record.json
  progress.json
  runtime-tasks/<step>.json
  work/<step>/input.json
  work/<step>/model_request.json
  work/<step>/draft.json
  artifacts/<step>.<contract>.json
  materialized/<step>.runtime_step_result.json
```

The implementation lock hashes the flow YAML plus every step `tool.py` and
schema. Changing those files mid-run is terminal; start a new run.

Consecutive `model: none` steps execute in one `run_flow.py` invocation.
Repair is not implicit. A BLOCKED run stays BLOCKED.

## Shared commands

All product skills call this skill’s scripts. Do not fork `run_flow.py`.

```powershell
python <builder>/scripts/audit_harness.py --target <skill-or-flow>
python <builder>/scripts/generate_harness.py --codebase <repo> --tool crop_4x5
python <builder>/scripts/generate_harness.py --codebase <repo> --flow-id <id> --milestone source_ready --milestone assets_bound --tools hash_bind,crop_4x5 --intelligence assets_bound
python <builder>/scripts/flowstep_instruction.py mark --codebase <repo> --flow-id <id> --step <milestone> --status DONE
python <builder>/scripts/validate_harness.py --codebase <repo> --flow-id <id>
python <builder>/scripts/run_flow.py --codebase <repo> --flow-id <id> --run-dir <run> --request <request.json>
```

`--skill-dir` remains only for this skill’s `examples/text_pipeline` fixture.
