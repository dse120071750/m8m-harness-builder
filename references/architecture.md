# M8M architecture

M8M is a milestone workflow engine. A milestone is the canvas node;
FlowSteps and their preferred tools execute inside it.

Builder 3.1 is the local, deterministic compiler and packager. It is not a
product runtime:

```text
skill-native source
  → canonical flowstep_flow_v4 JSON
  → m8m.workflow_source_bundle.v1
  → immutable m8m-runtime release under the owning codebase harness
  → codebase-owned launch.py + active runtime pin
  → built skill scripts/m8m_run.py pointer
```

The source bundle is base-independent and includes the deterministic runtime
archive, workflow runtime lock, and portable codebase launcher as declared
resources. Local installation expands the same verified payload into
`flowsteps/flows/<flow_id>/runtime/releases/<runtime_id>/`. Builder 3 does not
claim remote admission or deployment, but it does close local execution: once
built, the workflow never imports the mutable Builder installation.

## Runtime ownership boundary

The target codebase owns execution. The installed product skill is an
invocation pointer only:

```text
<product skill>/scripts/m8m_run.py
  → <codebase>/flowsteps/flows/<flow_id>/launch.py
  → runtime/releases/<runtime_id>/scripts/run_flow.py
  → product flow, Gems, schemas, handlers, and tools
```

Every fresh run writes the selected release identity to
`<run>/m8m-runtime-lock.json` before milestone work. Resume selects that exact
release even if a later build changes `runtime/active.json`. New runtime
releases install side by side; installation must not erase a release still
named by an existing run. A missing or mismatched release fails before a
handler or judge runs.

The v1 product pointer and dispatcher may be transactionally cut over once to
the hardened v2 bootstrap. The v2 routing ABI is then immutable in place: a
future bootstrap must install side by side. Runtime releases are independently
immutable and remain side by side, so old runs still select their run-local pin.
The product pointer pins the exact dispatcher SHA-256 and refuses to start it
after any out-of-band mutation.

The release manifest content-addresses every execution identity field: runtime
schema/name/version, CLI ABI, entrypoint, Python ABI and executable digest,
runtime source/contracts, and the exact file inventory of every transitive
Python dependency. Those dependencies are copied under the immutable release's
`vendor/` roots. The dispatcher verifies the closed directory, re-execs with
`-I -S -B`, and adds only the verified release scripts and vendor roots. Product
files, `PYTHONPATH`, system site-packages, another release, and the Builder
process therefore cannot supply runtime modules. The only host prerequisite is
the exact pinned Python interpreter ABI/executable; a mismatch fails before
workflow work.

`m8m-harness-builder` is build-time provenance only. Upgrading it may change a
future compiled release, but it never changes the accepted ABI of an existing
run, never requires an authored product workflow edit, and is never a product
implementation dependency. A launcher that calls the Builder's
`scripts/run_flow.py` directly is a P0 legacy artifact and must be regenerated
and locally installed.

Product implementation identity is closed as well. Repository-local modules
must be named by `implementation_dependencies` (or live inside a complete bound
tool package), and all package initializers/helpers/resources in that closure
are frozen. Undeclared local imports and unprovable dynamic code imports fail
before a handler is loaded. The build also lints every UTF-8 execution-closure
member, including configuration resources, for a mutable Builder launcher
binding. This deterministic lint prevents architectural dependency regressions;
it is not presented as a hostile-code operating-system sandbox.

```text
named inputs + derived milestone expectation
  → FlowSteps generate/refine the current candidate
  → structural admission validates and freezes named outputs
  → loop:none: commit without a semantic-judge call
  → loop:judge: separate typed judge evaluates the same expectation
       RETRY → another attempt under work/attempts
       PASS   → commit the admitted current candidate
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
m8m-harness-builder 3.1”. Every Builder 3 source bundle pins an exact
contract-bundle ID and digest. There is no legacy execution mode and no
builder-2 cloud admission.

## Authored source and generated representations

The sole editable source is:

```text
SKILL.md                              invocation/pointer only
agents/openai.yaml                    graph, roster, root contracts, terminals, observer
agents/<milestone>.yaml               one milestone node
references/<milestone>.md             one Gem
schemas/**                            closed workflow/milestone contracts
```

The milestone's closed `execution.judge` binding is the judge source. Builder
3 rejects standalone `agents/<milestone>_judge.yaml` until a separate typed
judge-agent contract exists.

Builder 3 compiles deterministic canonical JSON, a review-only `flow.yaml`,
resource/implementation/profile/capability requirements, and a source-bundle
proof. A mismatch between authored source and generated output is P0. Charts,
timestamps, run folders, local absolute paths, validation prose, and handoff
catalogs do not participate in source identity.

The canvas must own `workflow_contracts`: safe relative Draft
2020-12 request/configuration/result schema references and exact named bindings
from success-terminal output ports. These are authoring/source-bundle data,
not `flowstep_flow_v4` runtime fields. The corresponding schemas are immutable
resource requirements, so a separate consumer can inspect the exact public
workflow boundary without reconstructing it from chat or milestone
implementation details.

Each milestone must declare:

- `success`: the goal evaluated by its judge
- `output_contract` and `output_schema`
- at least one `outputs` entry with `id`, display `name`, `kind`,
  `cardinality: one|many`, and `required: true|false`; at least one port is
  required

Provider-neutral member kinds are `json`, `data`, `file`, `image`,
`video`, and `audio`.

Those four existing fields are one logical Milestone Expectation. The runtime
derives `m8m.milestone_expectation.v1` from them; authors do not add an
`expectation:` object, a new node, or a flow v5. New Gems contain FlowStep
guidance only. A legacy Gem may retain `## Rule of success` during migration,
but the compiler accepts it only when it exactly matches `success`.

## Execution closure in authored source

`agents/<milestone>.yaml` is also the local execution-requirement authority.
It declares a versioned candidate executor, exact FlowStep tool bindings, and
explicit capabilities. Judge loops declare a separate versioned judge, a
closed receipt schema, and `judge_abi: m8m_milestone_judge_v1`, so the runtime evaluates
the current candidate through that worker instead of trusting a handler's
top-level receipt.

AI executors carry closed profiles: model/reasoning configuration, input and
output token budgets, timeout, exact tools, and exact capabilities. Candidate
and AI-judge profiles are distinct. Deterministic nodes carry executor/judge
requirements without a model profile. These authored fields compile into the
source bundle and do not become extra canvas nodes or fields inferred from
chat. Any unresolved executor, judge, Gem, schema, tool, capability, budget,
or timeout is `BUILD_REQUIRED`, never an inferred built requirement.

Authored milestone agents name a local package in `flowsteps[].tool`, list
FlowStep IDs in `tools`, and bind each ID to an exact versioned ref. The
compiler emits an executable flow where both `flowsteps[].tool` and the
corresponding binding `ref` are that exact ref. FlowStep IDs are
milestone-local binding slots; package names are filesystem lookup details,
not workflow binding keys.

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
    worker: render_judge@3.1.0
    flowsteps:
      - { id: generate, tool: image_generate@3.1.0 }
      - { id: compare, tool: visual_compare@3.1.0 }
      - { id: refine, tool: image_edit@3.1.0 }
    tools: [generate, compare, refine]
    execution:
      candidate_executor:
        ref: handler.content_post_v1.render@3.1.0
      judge:
        ref: render_judge@3.1.0
      tool_bindings:
        - { tool: generate, ref: image_generate@3.1.0 }
        - { tool: compare, ref: visual_compare@3.1.0 }
        - { tool: refine, ref: image_edit@3.1.0 }

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
    flowsteps:
      - { id: assemble_package, tool: assemble_package@3.1.0 }
    tools: [assemble_package]
    execution:
      candidate_executor:
        ref: handler.content_post_v1.package@3.1.0
      tool_bindings:
        - { tool: assemble_package, ref: assemble_package@3.1.0 }
```

The workflow JSON/YAML is the design-time canvas representation. Its
`outputs` entries are visible output ports. `chosen-output.json` is the
run-time representation used for status, previews, and queries.

## Local/native conformance scope

Builder 3.1 and the native engine pin byte-identical contract-union fixtures,
then execute the same frozen two-milestone linear profile for expectation
projection, candidate admission, judge input, and durable receipts. This is a
common executable-profile guarantee, not a claim that the two runtimes expose
identical topology engines. Explicit DAG scheduling and legacy branch/cycle
control remain capability-scoped: a runtime must reject a topology it does
not implement instead of admitting it under approximate semantics.

## Candidate protocol

Handlers return only the current candidate in the shape validated by the
milestone `output_schema`:

```json
{
  "outputs": {
    "images": [
      {"id": "hero_image", "name": "Hero image", "path": "..."},
      {"id": "detail_image", "name": "Detail image", "path": "..."}
    ],
    "receipt": {"id": "render_receipt", "name": "Render receipt", "value": {}}
  }
}
```

Each collection member needs a unique filesystem-safe `id` and non-empty
`name`. A one-cardinality output is one member; a many-cardinality output
is an ordered member array. Candidate `ok`, `branch`, or `cycle` values are not
semantic approval. A business receipt is a named JSON output such as the
example `receipt`; it is distinct from the runtime control receipt.

Before any semantic judge call, structural admission validates the output
schema, declarations, required members, cardinality, unique safe member IDs,
names, paths, and bytes. File/media bytes are copied into the current attempt,
and the judge sees that frozen run-local candidate. Admission failure BLOCKS
or retries without calling the judge. Rejected attempts are diagnostic only
and are never queryable.

`loop: none` is deterministic structural mode: admission is the complete gate
and no semantic judge is invoked. `loop: judge` requires a separate
`m8m_milestone_judge_v1` worker. That worker receives the closed
`m8m.milestone_judge_request.v1` fields only: schema, milestone ID, attempt,
maximum attempts, expectation, resolved inputs, and admitted candidate. Gem
paths and chat guidance are not part of this ABI.

Strict semantic judges return exactly `{decision, reasons, blockers}`; compact
`{ok}` decisions are rejected by the Builder 3.1 ABI. The runtime persists
only `m8m.milestone_judge_receipt.v1`, with the
actual attempt, decision, success rule, judge reference, reasons, blockers,
and maximum attempts. `RETRY` receipts remain under the attempt workspace;
only a final `PASS` receipt may accompany a chosen manifest, while terminal
`BLOCKED` has a receipt and envelope but no chosen manifest.

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

### Storage contract

The source repository owns the immutable runtime release and workflow source;
it is not a mutable execution folder.
On Windows a fresh invocation resolves `--harness-root`, then
`M8M_HARNESS_ROOT`, then `%SystemDrive%\NisanRuntime`. The resolved root must
be on the system volume. New run, goal, and builder directories live under
`<harness-root>/runs/`; the optional candidate cache lives under
`<harness-root>/cache/v1/`.

`m8m_run_context_v2` freezes `m8m_run_storage_contract_v1`, including the
source-code root, execution root, active run directory, and cache root.
Request `file_ref_v2` inputs are copied exactly once to
`inputs/source-assets`, verified against their declared SHA-256, deduplicated,
and rewritten to those run-local paths. The provenance list is frozen in
`source-assets-manifest.json`.

Image provider output must physically resolve to the execution volume. A
junction or symlink whose visible C: path resolves to D: fails before chosen
output commit. Valid provider output is copied into the current attempt slot
before it can become a chosen member.

Only an exact initialized legacy run may retain a repository-local location.
Default paused-run discovery never searches the source repository, and no
legacy run is automatically migrated, rewritten, or removed. M8M itself has
no generic final exporter; product runtimes own any final archive delivery.

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
python <installed-skill>/scripts/m8m_run.py --run-dir <run> --continue-after-edit render
```

The adoption gate compares the frozen flow snapshot and implementation lock
with current code. It permits changes owned by `render` and its transitive
downstream graph, verifies preserved chosen port compatibility, updates the
lock, then clears and reruns the affected graph. Flow-level changes,
milestone identity/order changes, unowned implementation changes, or edits
to preserved milestones fail closed and require a fresh run.

Handlers and tool contracts are discovered automatically. Shared product
modules outside those files must be named with project-relative
`implementation_dependencies` in `flow.yaml`. Flow-level dependencies belong
to every milestone; milestone-level dependencies belong to that milestone.
Both are included in `implementation-lock.json` and in adoption ownership.
Every bound tool package is frozen as a complete runtime package, including
sibling helpers and package-local resources while excluding only generated
caches. Because product implementation is loaded from the live codebase, every
execution boundary rediscovers package membership and rehashes the frozen
closure. A verification receipt is audit evidence only and never suppresses
that check; changing a helper or adding a package member is implementation
drift just like changing the public `tool.py` entrypoint.

Intentional regeneration is explicit:

```powershell
python <installed-skill>/scripts/m8m_run.py --run-dir <run> --replace-milestone render
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
`<harness-root>/cache/v1/<flow>/<namespace-digest>/<milestone>/<key>/`.
The key covers canonical semantic inputs plus milestone handler, FlowStep
tools, schemas, authored success expectation, FlowStep Gem, judge, model configuration, output ports, and
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
  run-context.json                       # isolated plus frozen storage contract
  source-assets-manifest.json            # deduplicated file_ref_v2 provenance
  inputs/source-assets/<sha256>.<ext>     # hash-verified run-local source bytes
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
  semantic judge returns `PASS` or attempts are exhausted.
- A branch is evaluated after the milestone has committed its chosen
  output. Unselected paths are skipped, not BLOCKED.
- A cycle wraps milestones over a frozen ledger. Pass preserves the item;
  fail removes unfinished live output and leaves the row resumable.
- A wait is a normal milestone using roster state. It pauses without
  manufacturing a chosen output, then resumes and judges the reply.

Intelligence may propose content. It may not set `ok`, `branch`, or
`cycle`; deterministic workers own those receipts.

## Builder dogfooding

`flows/m8m_build_v2.yaml` is itself `flowstep_flow_v4` and runs through
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

These five nodes are deterministic `loop: none` milestones. Their tools do
the fail-closed audit/build/validation/install work, and the runtime admits
their exact typed named outputs before committing. They make zero semantic
judge calls and publish no redundant Builder judge requirements.

Installation builds complete target-adjacent managed-root snapshots, verifies
every declared member, writes a run-local `PREPARED` journal, then promotes by
directory-level atomic replacement. `COMMITTED` is written only after all
roots pass readback. Handled failures roll every root back; a hard interruption
recovers the same PREPARED transaction forward. A committed retry verifies and
returns the same receipt instead of copying again.

The five nodes use closed stage-specific contracts and explicit named outputs:
`source_audit`, `toolbox_manifest`, the paired `workflow_source_bundle` and
`staged_harness` generation outputs, `validation_report`, and
`installation_receipt`. The portable bundle never embeds the run-local staged
report. Their digest/path linkage is rechecked by validation and installation.
The terminal local-install receipt exposes that verified bundle path/digest,
its staged resource root, and the installed codebase-owned runtime release.
It performs no submission itself, and this repository makes no guarantee
about any remote consumer.
A `BUILD_REQUIRED` tool or judge sketch may remain in
staging, but it blocks validation and local installation. Builder-2 runs may be
exported only as untrusted import material; Builder 3 never resumes or installs
them.

## Explicit v4 import

When the target is not already closed skill-native source, Builder uses
`authoring_mode: from_context`: the same current dialect as a new workflow,
authored from audit inventory. That path does not claim equivalence. A fully specified legacy `flowstep_flow_v4` that must
prove lossless equivalence is inspected, bound to explicit operator
acceptance, staged, and recompiled through `scripts/import_flow_v4.py`. The root-independent
inspection binds resources and external tool-package bytes; acceptance supplies
closed root workflow contracts plus exact execution/profile/capability facts;
verification compares normalized runtime projections and freezes an auditable
proof under `planning/import-flow-v4/`. The importer writes only a new empty
stage and never installs or deploys. Any semantic difference is a migration,
not an equivalence claim.

## Commands

```powershell
python scripts/run_m8m.py --target <skill-or-flow-dir> --codebase <repo> --harness-root C:\NisanRuntime
python scripts/audit_harness.py --target <skill-or-flow-dir>
python scripts/generate_harness.py --codebase <repo> --from-audit <skill>/planning/flowstep-audit.json
python scripts/validate_harness.py --codebase <repo> --flow-id <id>
python <installed-skill>/scripts/m8m_run.py --harness-root C:\NisanRuntime --request <request.json>
python scripts/import_flow_v4.py inspect --source-root <legacy-flow-dir> --out <inspection.json>
```

No command in this builder deploys a generated workflow.
