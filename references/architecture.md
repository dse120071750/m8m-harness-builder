# M8M architecture

M8M is a milestone workflow engine. A milestone is the canvas node;
FlowSteps and their preferred tools execute inside it.

```text
named inputs
  → FlowSteps generate/refine the current candidate
  → milestone judge evaluates success
       not ok → another attempt under work/attempts
       PASS   → commit the current candidate
  → out/chosen-output.json
  → downstream named input binding
```

The session directory is the run-state authority. Selection is logical,
not cryptographic: there are no lock IDs, candidate hashes, revisions, or
judge-digest bindings.

## Hard-cutover contracts

The canonical runtime accepts only:

- `flowstep_flow_v4` — workflow and declared output ports
- `flowstep_output_v3` — execution envelope
- `m8m_chosen_output_v1` — the one consumable milestone result

`flowstep_flow_v1`, v2, and v3 fail with “regenerate with
m8m-harness-builder 2.0”. There is no legacy execution mode.

Each milestone must declare:

- `success`: the goal evaluated by its judge
- `output_contract` and `output_schema`
- at least one `outputs` entry with `id`, display `name`, `kind`,
  `cardinality: one|many`, and `required: true|false`

Provider-neutral member kinds are `json`, `data`, `file`, `image`,
`video`, and `audio`.

## Canonical flow YAML

```yaml
schema: flowstep_flow_v4
flow_id: content_post_v1
version: 1
milestones:
  - id: render
    success: The accepted hero and supporting images satisfy the brief.
    output_contract: render_v1
    output_schema: milestones/render/output.schema.json
    outputs:
      - id: images
        name: Accepted images
        kind: image
        cardinality: many
        required: true
      - id: receipt
        name: Render data
        kind: json
        cardinality: one
        required: true
    handler: milestones/render/assemble.py
    input_schema: milestones/render/input.schema.json
    inputs:
      request: user.request
    loop: judge
    worker: render_judge
    flowsteps:
      - { id: generate, tool: image_generate }
      - { id: compare, tool: visual_compare }
      - { id: refine, tool: image_edit }

  - id: package
    success: The post package is complete.
    output_contract: package_v1
    output_schema: milestones/package/output.schema.json
    outputs:
      - id: package
        name: Content package
        kind: json
        cardinality: one
        required: true
    handler: milestones/package/assemble.py
    input_schema: milestones/package/input.schema.json
    inputs:
      all_rendered:
        from: render.render_v1
        output: images
      hero:
        from: render.render_v1
        output: images
        member: hero_image
```

The workflow JSON/YAML is the design-time canvas representation. Its
`outputs` entries are visible output ports. `chosen-output.json` is the
run-time representation used for status, previews, and queries.

## Candidate protocol

Handlers return the current candidate in the shape validated by the
milestone `output_schema`:

```json
{
  "outputs": {
    "images": [
      {"id": "hero_image", "name": "Hero image", "path": "..."},
      {"id": "detail_image", "name": "Detail image", "path": "..."}
    ],
    "receipt": {"id": "render_receipt", "name": "Render receipt", "value": {}}
  },
  "receipt": {"ok": true}
}
```

Each collection member needs a unique filesystem-safe `id` and non-empty
`name`. A one-cardinality output is one member; a many-cardinality output
is an ordered member array. The judge may reject the candidate and repeat
FlowSteps. Rejected attempts are diagnostic only and are never queryable.

On PASS the runtime immediately commits the current return value:

```text
milestones/<milestone_id>/out/
  chosen-output.json
  judge-receipt.json
  members/<member_id>/asset.<ext>
```

JSON/data values are stored as JSON assets. File and media bytes are
copied into the member directory. The chosen manifest contains the
ordered member list, output IDs, names, kinds, relative paths, and
`status: chosen`. It contains no candidate hash or lock ID and is written
last, after every declared member and the judge receipt are valid.

Only `out/chosen-output.json` makes a milestone consumable. Missing
required outputs, duplicate or unsafe IDs, empty names, missing files,
schema failures, exhausted judge attempts, or a missing chosen manifest
leave the milestone BLOCKED.

## Input queries

Whole milestone binding remains valid:

```yaml
inputs:
  rendered_content: render.render_v1
```

It returns a mapping of declared output IDs. Explicit output and member
bindings are:

```yaml
inputs:
  images:
    from: render.render_v1
    output: images
  hero:
    from: render.render_v1
    output: images
    member: hero_image
```

Omitting `member` returns the declared output as one value or an ordered
array according to cardinality. Downstream code never reads attempts,
execution envelopes, or unchosen candidate files.

## Execution context and run modes

`context_policy: isolated` is the v4 default. The runtime writes
`run-context.json` with `chat_history_allowed: false`. Deterministic handlers
receive only resolved run inputs. Model work returns `ACTION_REQUIRED` with
an `m8m_context_capsule_v1` containing a closed file allowlist and one draft
destination. The platform adapter must launch a fresh no-history worker for
that capsule; using the orchestration chat to produce the draft violates the
flow contract.

Fresh is the CLI default. It creates a new folder, defaults cross-run cache
to `off`, and refuses an existing execution record. Only the exact request,
current implementation, gems/schemas, and chosen outputs created inside that
folder are available. `--run-mode resume --run-dir <exact-run>` is explicit
same-run continuation. A missing fresh input is an error or clarification;
the adapter must not fill it from prior conversation memory.

Provider-neutral Python cannot erase a caller's model memory. The boundary
is therefore contractual and machine-readable: the caller must create the
no-history worker described by the capsule. Generated product skills make
this compulsory.

## Resume, edited-workflow continuation, and replacement

Plain resume validates each chosen manifest, skips completed milestones,
and starts at the first unfinished node. It does not invoke a handler or
judge for a milestone with a valid chosen output. It reuses run state, not a
cross-run cache entry.

When a defect is repaired during a paused or blocked workflow, use:

```powershell
python scripts/run_flow.py ... --run-dir <run> --continue-after-edit render
```

The adoption gate compares the frozen flow snapshot and implementation lock
with current code. It permits changes owned by `render` and its transitive
downstream graph, verifies preserved chosen port compatibility, updates the
lock, then clears and reruns the affected graph. Flow-level changes,
milestone identity/order changes, unowned implementation changes, or edits
to preserved milestones fail closed and require a fresh run.

Intentional regeneration is explicit:

```powershell
python scripts/run_flow.py ... --replace-milestone render
```

Replacement removes the chosen and working state of that milestone and
every transitively downstream milestone, then runs from the selected
milestone. There is only one current chosen result per milestone; no
revision is created. This changes session artifacts only. Side-effecting
tools retain their existing idempotency responsibilities.

## Repeated-goal isolation

`run_goal.py` is the outer orchestration layer for repeated work. Its
`m8m_goal_ledger_v1` freezes row IDs and tracks row status, while each row
uses a distinct child folder:

```text
<goal>/
  goal-ledger.json
  implementation-lock.json
  rows/<row>/attempt-001/
    request.json
    run/
      run-context.json
      roster.json
      milestones/...
      cycles/...
```

Every child has origin `goal_child`, `chat_history_allowed: false`, and cache
mode `off`. Completed rows are not carried into the next child's context;
only their status/path remains in the outer ledger. `--continue-after-edit`
repairs the current child in place and preserves compatible upstream state.
`--abandon-row` retains the old attempt and starts a new clean attempt. This
prevents a ten-case goal from becoming one progressively drifting model
conversation.

## Workflow state versus cross-run cache

The run directory is always the workflow-state authority. Its chosen
outputs, attempts, roster, ledger, branch, wait, resume, and replacement
records are never cache entries. The implementation lock prevents code
drift inside that run; it is not a cache key.

Cross-run cache is optional and requires both a milestone declaration and
an enabled run mode:

```yaml
cache:
  reuse: candidate
  ttl_seconds: 86400
  side_effects: none
```

```text
--cache-mode off|read|write|read-write   # new-run default: off
--cache-namespace <tenant-or-local>
```

Entries live at
`<repo>/flowsteps/cache/v1/<flow>/<namespace-digest>/<milestone>/<key>/`.
The key covers canonical semantic inputs plus milestone handler, FlowStep
tools, schemas, success gem, judge, model configuration, output ports, and
contract. It excludes run paths, IDs, attempts, timestamps, roster, and
cycle-control state. File and media input identity uses byte digests.

On hit, copy the entry into the current run's `work/cache-candidate`,
validate it, and run the current judge. Rejection falls through to fresh
FlowSteps without spending their attempt budget or looking up again.
PASS creates a normal run-local chosen bundle and current judge receipt.
Cache entries never contain chosen status or judge receipts, and chosen
manifests never point outside the run.

Cache faults and writes are non-blocking. Replacement bypasses reads for
the entire invalidated subgraph. Write-only mode is the explicit refresh
path. Wait, branch-decision, cycle-control, side-effecting, and legacy
`loop: for` milestones cannot declare cache. Ordinary cycle-row work uses
one lookup receipt per semantic row.

## Session layout

```text
<run>/
  request.json
  run-context.json                       # isolated; chat history forbidden
  manifest.json
  roster.json
  flow-execution-record.json
  runtime-tasks/<id>.context.json        # allowlist for a fresh model worker
  milestones/<id>/
    out/
      chosen-output.json
      judge-receipt.json
      members/<member_id>/asset.<ext>
    work/
      cache-receipt.json                # optional lookup/write metadata
      cache-candidate/...               # imported bytes; never downstream address
      candidate/files/...
      attempts/01/...
    items/001/work/cache-receipt.json   # optional cycle-row cache metadata
    items/001/...                       # completed cycle item, when used
  cycles/<id>/ledger.json
```

The roster tracks milestone progress. A cycle ledger tracks rows inside a
cycle. Neither is a canvas node and neither replaces chosen outputs.

## Control semantics

- FlowSteps are the preferred internal sequence; their tools may recover
  like a normal agent, but the declared chosen bundle is compulsory.
- `loop: judge` repeats current-candidate production until its dedicated
  worker says `ok: true` or attempts are exhausted.
- A branch is evaluated after the milestone has committed its chosen
  output. Unselected paths are skipped, not BLOCKED.
- A cycle wraps milestones over a frozen ledger. Pass preserves the item;
  fail removes unfinished live output and leaves the row resumable.
- A wait is a normal milestone using roster state. It pauses without
  manufacturing a chosen output, then resumes and judges the reply.

Intelligence may propose content. It may not set `ok`, `branch`, or
`cycle`; deterministic workers own those receipts.

## Builder dogfooding

`flows/m8m_build_v1.yaml` is itself `flowstep_flow_v4` and runs through
the canonical runtime. All five milestones are compulsory:

```text
source audit
  → toolbox construction
  → staged flow/skill generation
  → harness validation
  → local skill installation
```

Every stage consumes the preceding chosen output. Generated target files
remain inside the builder session until the validation milestone has a
valid chosen manifest. Only the installation milestone copies the
validated stage into the local target. `run_m8m.py` is a thin launcher;
it does not maintain a second pseudo-milestone engine.

## Commands

```powershell
python scripts/run_m8m.py --target <skill-or-flow-dir> --codebase <repo>
python scripts/audit_harness.py --target <skill-or-flow-dir>
python scripts/generate_harness.py --codebase <repo> --from-audit <skill>/planning/flowstep-audit.json
python scripts/validate_harness.py --codebase <repo> --flow-id <id>
python scripts/run_flow.py --codebase <repo> --flow-id <id> --run-dir <run> --request <request.json>
```

No command in this builder deploys a generated workflow.
