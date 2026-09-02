# M8M harness builder 3.2

This skill writes a minimalist M8M milestone workflow. Milestones are the
canvas nodes. FlowSteps and their preferred tools run inside each node;
structural admission is universal and semantic judging is optional.

```text
derive expectation from success + output contract/schema + output ports
  → FlowSteps produce/refine current candidate against that expectation
  → structural admission freezes and validates the named outputs
  → loop:none commits without a semantic-judge call
  → loop:judge sends one closed request to the separate current judge
      reject → another candidate attempt
      PASS   → commit the admitted current candidate
  → one chosen output bundle
  → downstream named binding
```

The chosen bundle is a logical session result stored at
`milestones/<id>/out/chosen-output.json`. It is not a cryptographic lock.
Do not invent lock IDs, candidate hashes, revision histories, or judge
digest bindings.

Workflow state is not cross-run cache. Chosen outputs, attempts, roster,
ledger, branch, wait, resume, and replacement always belong to one run.
Cross-run reuse is an optional candidate optimization and is off by default.

Every flow uses `context_policy: isolated`. The orchestration chat is not
workflow input. For any `ACTION_REQUIRED`, launch a fresh no-history worker
from the emitted `m8m_context_capsule_v1`; that worker may read only the
listed files and may write only the listed draft. Never draft a milestone
from remembered chat, another run, or an earlier goal row.

Making an existing skill into an M8M workflow is the same work as building
one from scratch. Audit only adds context: the task, reusable milestones,
FlowSteps, tools, Gems, and repo-structure gaps. Mixed leftovers are normal.
Do not refuse a missing canvas or a Builder `run_flow.py` pointer. Reuse what
already exists; build or move the rest into the current ownership. Missing
tools become closed `candidate_bind` packages so the dialect can compile and
install a codebase-owned runtime. This does not claim equivalence with the
old files. Lossless v4 equivalence still uses `scripts/import_flow_v4.py`.

Builder 3.1 has one authored source and a closed local execution boundary:

```text
SKILL.md + agents/*.yaml + references/<milestone>.md + every declared schema/source resource
  → deterministic builder compile
  → canonical flowstep_flow_v4 JSON + m8m.workflow_source_bundle.v1
  → codebase-owned immutable runtime release + codebase launcher
  → built skill pointer to that launcher
```

`flow.yaml` and flowcharts are generated review artifacts. Do not maintain
them as a second editable graph. The source bundle is base-independent and
contains no host, chat, deployment, or remote catalog identity, but it does
declare the deterministic runtime archive, workflow runtime lock, and portable
codebase launcher needed to close local execution. This builder never pushes,
publishes, deploys, activates Time Machine, or performs provider side effects.

The product runtime is never the installed Builder. Local installation writes
`flowsteps/flows/<flow_id>/launch.py` and digest-addressed releases under that
same codebase harness. The product skill receives only
`scripts/m8m_run.py`, which points to the codebase launcher. Fresh runs pin the
selected release in `m8m-runtime-lock.json`; resume loads that pin rather than
the current active release. Builder upgrades therefore affect future builds
only. If a product launcher names `m8m-harness-builder/scripts/run_flow.py`,
the product must be regenerated and locally installed; do not edit its
milestones to satisfy the new Builder.

Each release vendors and hashes the transitive Python distributions used by the
runtime and launches them with system site-packages disabled. The host Python
ABI/executable is pinned separately. Product handlers must declare every
repository-local imported module or package initializer through
`implementation_dependencies`; complete bound tool packages are frozen as one
closure. Unprovable dynamic code loading is rejected before handler import.

`agents/openai.yaml` must declare the authoring-only `workflow_contracts`
boundary: closed Draft 2020-12 request, configuration, and result schemas plus
exact named bindings from success-terminal output ports. The compiler includes
those schema resources and bindings in the source bundle, while deliberately
omitting them from `flowstep_flow_v4`; milestone runtime input and chosen-state
semantics do not change.

Read [references/architecture.md](references/architecture.md) when compiling or
running a workflow, [references/milestone.md](references/milestone.md) when
designing node boundaries, [references/flowstep-development.md](references/flowstep-development.md)
before classifying or implementing FlowSteps, and
[planning/upgrade.md](planning/upgrade.md) for the Builder 3.1 source-dialect
and cutover rules.

## Three words

| Word | Meaning |
| --- | --- |
| **Milestone** | Compulsory canvas checkpoint whose expectation is `success` + output contract/schema + named ports. It may use structural admission only or an additional semantic judge. No chosen manifest means BLOCKED. |
| **FlowStep** | Atomic candidate-generation or refinement goal inside a milestone. Prefer one tool; recover within the milestone when needed. |
| **Tool** | Reusable Python implementation at `<repo>/flowsteps/tools/<id>/`. A generated-new tool may be a sketch; a chosen milestone result may not. |

Tool-heavy means declared in-process product functions, not shell commands.
The Builder must dissect every proposed FlowStep into deterministic tool work
or bounded intelligence before generation. Closed product FlowStep packages
may not launch subprocess workflows, recursively discover the execution root,
author M8M control manifests, split stability/receipt reconstruction into
extra commands, or perform post-approval work other than one declared
`finalize` tool. These are build and install admission rules.

## Builder workflow

Run the builder through its own canonical v4 workflow:

```powershell
python scripts/run_m8m.py --target <skill-or-flow-dir> --codebase <repo> --harness-root C:\NisanRuntime
```

Fresh Builder 3.1 sessions use `flows/m8m_build_v2.yaml`. Its five compulsory
milestones are:

```text
source audit
  → toolbox construction
  → staged flow/skill generation
  → harness validation
  → local skill installation
```

Each milestone consumes the prior chosen bundle. Generated files are
staged inside the builder session; do not write the target skill until
the validation milestone has a chosen output. The installation milestone
performs a local copy only. Never deploy.

All five Builder milestones use an explicit deterministic candidate executor
with `loop: none`. Their substantive tools already fail closed and their
closed stage-specific schemas plus named ports provide deterministic admission,
so the runtime commits them after structural validation with zero semantic
judge calls. The Builder source bundle therefore carries no redundant judge
implementation requirements for these five nodes.

## Expectation-first execution

Do not author an `expectation:` object and do not create a flow v5. The four
existing milestone fields are one logical expectation:

```text
success + output_contract + output_schema + outputs
  → m8m.milestone_expectation.v1 (derived execution view)
```

New Gems contain FlowStep guidance only; `success` in the milestone source is
the sole semantic authority. During migration, a legacy `## Rule of success`
section is optional, but when present its normalized text must exactly match
`success`. Before its first FlowStep, a candidate worker receives the derived
expectation. It returns only `{outputs: {...}}`; candidate or draft `ok` values
cannot approve the milestone.

The runtime validates the candidate schema, named ports, required members,
cardinality, member IDs/names, paths, and file/media bytes before semantic
judgment. A `loop: none` milestone commits after that deterministic admission
and never invokes a semantic judge. A `loop: judge` milestone invokes its
separate worker with only `m8m.milestone_judge_request.v1`:

```text
schema, milestone_id, attempt, max_attempts, expectation, inputs, candidate
```

The judge returns exactly bounded `{decision, reasons, blockers}` with
`decision` equal to `PASS`, `RETRY`, or `BLOCKED`; compact `{ok}` is rejected.
The runtime remains the only writer of `m8m.milestone_judge_receipt.v1`. A business receipt belongs in a
named JSON output port; it is not the control receipt.

The five chosen ports are `source_audit`, `toolbox_manifest`, then two distinct
generation ports (`workflow_source_bundle` and its linked run-local
`staged_harness`), followed by `validation_report` and
`installation_receipt`. The source bundle carries only a digest-bound advisory
validation summary; the validation milestone still runs the current full
validator and is the authority required before local installation. The final
installation receipt repeats the verified bundle path, byte digest, portable
bundle digest, and staged resource root so a caller has one explicit local
workflow-package handoff without treating it as a push or release.

Builder 2 runs are not resumed or installed by Builder 3. Export their staged
members only as untrusted import material, then start a fresh Builder 3 session.
`m8m_build_v1.yaml` is not a Builder 3 runtime option.

An existing, fully specified `flowstep_flow_v4` product that must prove
lossless equivalence enters Builder 3 only through the explicit staging
importer. Ordinary “make this an M8M workflow” requests use `from_context`:
same new-workflow authoring, with audit inventory as the starting context.

```powershell
python scripts/import_flow_v4.py inspect --source-root <legacy-flow-dir> --out <inspection.json>
python scripts/import_flow_v4.py accept --inspection <inspection.json> --request <acceptance-request.json> --source-root <legacy-flow-dir> --project-root <repo> --out <acceptance.json>
python scripts/import_flow_v4.py stage --inspection <inspection.json> --acceptance <acceptance.json> --source-root <legacy-flow-dir> --project-root <repo> --stage-dir <new-empty-stage>
python scripts/import_flow_v4.py verify --inspection <inspection.json> --acceptance <acceptance.json> --stage-dir <new-empty-stage>
```

The operator must supply source-only facts absent from legacy YAML: closed root
workflow contracts, exact versioned executors/tools/judges, independent AI
profiles, budgets, timeouts, and capabilities. The importer binds source and
tool-package bytes, recompiles the staged skill, and writes a root-independent
equivalence proof under `planning/import-flow-v4/`. It never installs or
deploys. Lossy routing, non-strict judge loops, missing Gems or FlowSteps,
unsafe paths, and source drift remain blocked.

Piecemeal audit and scaffold repair remain available, but their output is
deliberately non-runnable:

```powershell
python scripts/audit_harness.py --target <skill-or-flow-dir>
python scripts/generate_harness.py --codebase <repo> --from-audit <skill>/planning/flowstep-audit.json
python scripts/validate_harness.py --codebase <repo> --flow-id <id>
```

`generate_harness.py` writes `BUILD_REQUIRED_RUNTIME`; direct execution and
validation fail until the complete five-stage `run_m8m.py` build packages and
installs the codebase-owned runtime release. Piecemeal generation is never an
alternate shared-Builder runtime path.

## Canonical contracts

Write only `flowstep_flow_v4`. Runtime envelopes are
`flowstep_output_v3`; chosen manifests are `m8m_chosen_output_v1`.
Reject v1/v2/v3 workflows with: “regenerate with
m8m-harness-builder 3.1”. Every Builder 3 package also pins the exact
contract-bundle ID and digest; do not dispatch on `flowstep_flow_v4` alone or
offer a legacy execution mode.

Declare every shared runtime file imported outside milestone handlers and
tool public contracts with project-relative `implementation_dependencies`.
The field may be set at flow level (owned by every milestone) or on one
milestone (owned only by that milestone). These files are hash-bound into the
implementation lock, so a plain resume fails closed on shared-code drift and
`--continue-after-edit` invalidates every owning milestone.

Every milestone must declare:

- `success`: the exact goal its judge accepts;
- `output_contract` and `output_schema`;
- `outputs`: one or more ports with `id`, display `name`, provider-neutral
  `kind`, `cardinality: one|many`, and `required`; at least one business
  output must have `required: true`.

Supported kinds are `json`, `data`, `file`, `image`, `video`, and
`audio`. Collection items need unique filesystem-safe IDs and non-empty
names.

## Exact execution requirements

The skill source must close every execution fact that a later server would
otherwise have to guess. In `agents/<milestone>.yaml`, the candidate binding
is exactly `handler.<flow_id>.<milestone_id>@3.1.0`; arbitrary or unknown
candidate refs are rejected. That canonical slot plus the implementation
fingerprint closes the handler path and bytes that local `run_flow` invokes.
Changing only the release ref therefore cannot silently relabel unchanged
behavior. A judge loop also declares a distinct
`execution.judge.ref`, `worker`, a closed `receipt_schema`, and
`judge_abi: m8m_milestone_judge_v1`. Bind every candidate FlowStep
tool through `execution.tool_bindings` and declare provider-neutral
capabilities explicitly.

The authored and executable views intentionally differ at one field. In an
authored milestone agent, `flowsteps[].tool` is the local package name. The
milestone `tools` list contains FlowStep IDs, and every binding uses that
milestone-local ID as `tool` plus the exact versioned implementation as
`ref`. Compilation projects the exact binding ref into executable
`flowsteps[].tool`; executable `flowsteps[].tool` and the binding `ref` must
then be byte-for-byte equal. Local lookup may strip `@semver`, but emitted
runtime identity never does. Reusing the same FlowStep ID in another
milestone does not couple their bindings.

Standalone `agents/<milestone>_judge.yaml` is rejected in Builder 3 until it
has its own closed typed contract. The exact judge binding/profile lives in
the milestone's `execution.judge`; this avoids a second, unvalidated source of
judge authority.

An AI candidate or AI judge additionally owns an immutable profile containing
its model and reasoning configuration, input/output token budget, timeout,
exact tool list, and exact capability list. Candidate and AI-judge profiles
must be separate. Deterministic `loop:none` milestones need an executor
binding but no judge or model profile. A semantic `loop:judge` milestone also
needs its exact judge binding. Missing required executor, judge, Gem, schema,
tool, capability, budget, or timeout produces `BUILD_REQUIRED`; the compiler
never invents a built profile from `intelligence` or a worker name.

Audit inventories the task and reusable pieces, then proposes milestones.
For `authoring_mode: from_context`, that inventory is the input to the same
current dialect as a new workflow: canvas, Gems, FlowSteps, repo tools, and
a codebase-owned runtime. Missing product tools are synthesized as closed
`candidate_bind` packages so the dialect can run; replace them with the real
in-process tools after install.

These declarations compile into source-bundle implementation, profile, and
capability requirements. They are not cloud closure claims: a server-side
admission layer must still resolve those exact requirements before execution.

Cross-runtime parity is proved on the shared common linear executable profile:
the Builder and native engine must derive the same expectations and make the
same admission/judge decisions from byte-identical fixtures. Contract-union
fixtures may describe capabilities owned by only one runtime; branch, cycle,
or explicit-DAG support is never inferred from schema acceptance, and an
unsupported topology must be rejected explicitly.

Every compiled source bundle also reports the advisory observer fields
`platform_common_profile: compatible|unsupported` and
`platform_unsupported_features`. The list is deterministic and names each
unsupported milestone topology control. This is author feedback only: it does
not alter local execution validity, grant a capability, admit a package, or
make Builder depend on the platform. Platform admission independently
revalidates the same profile.

The Builder and platform must compare locked Builder 3.1 contract files as
bytes, not as parsed JSON. Run:

```text
python scripts/verify_platform_contract_parity.py --platform-schema-root <platform>/schemas
```

Any member, order, digest, aggregate bundle identity, or byte difference is a
hard alignment failure.

```yaml
- id: render
  success: The accepted visual set satisfies the brief.
  output_contract: render_v1
  output_schema: milestones/render/output.schema.json
  outputs:
    - id: images
      name: Accepted images
      kind: image
      cardinality: many
      required: true
    - id: receipt
      name: Render receipt
      kind: json
      cardinality: one
      required: true
  loop: judge
  worker: render_judge@3.1.0
  flowsteps:
    - { id: generate, tool: image_generate }
    - { id: compare, tool: visual_compare }
    - { id: refine, tool: image_edit }
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
```

The workflow JSON/YAML exposes these output ports for the canvas. The
chosen manifest exposes run-time status, previews, member ordering, and
query paths.

## Success authority and judge loop

Each milestone owns a Gem at `references/<id>.md`. Each
`## <flowstep>` section is that FlowStep's prompt. The judge receives the
runtime-derived expectation from the milestone's authored `success` and output
declarations; it does not read the Gem through its closed ABI.

The gem and judge are attached metadata, not separate canvas nodes.
Develop a milestone-specific worker instead of defaulting every quality
gate to shared `ok_receipt`. Intelligence may draft; it may not set
`ok`, `branch`, or `cycle`.

Branch and cycle control never create a candidate exception. The candidate
executor still returns exactly `{"outputs": {...}}`; top-level `receipt`,
`ok`, `branch`, or `cycle` is rejected before control runs. A branch/cycle
milestone declares its control worker as a distinct milestone-local FlowStep
ID with one exact `execution.tool_bindings` ref. Generated candidate handlers
reserve that ID and do not invoke it. After structural admission, the runtime
alone sends the bound worker a bounded closed request containing only the
resolved inputs, admitted candidate, and declared branch/cycle limits. Its
validated result is private control evidence inside the durable runtime judge
receipt; the branch/cycle driver consumes that evidence after the chosen
bundle commit. Candidate bytes never carry a control receipt.

After judge PASS, the runtime immediately commits the current FlowStep
return:

```text
milestones/<id>/out/
  chosen-output.json
  judge-receipt.json
  members/<member_id>/asset.<ext>
```

JSON/data values become JSON assets. Files and media are copied into the
member folder. Write the chosen manifest last. Rejected attempts may stay
under `work/attempts` for diagnostics but are never downstream-readable.

Persist only the standardized `m8m.milestone_judge_receipt.v1` control
receipt. It records the milestone, actual attempt, `PASS|RETRY|BLOCKED`,
success rule, judge reference, reasons, blockers, and maximum attempts. Raw
semantic worker result is a bounded decision, never the durable receipt
contract. For `judge_abi: m8m_milestone_judge_v1`, the runtime invokes the
declared worker on the current candidate and ignores any handler-supplied
top-level control receipt.
For an image-producing milestone, the provider output must physically resolve
to the execution volume. A C: junction whose target is on D: is invalid. Copy
an accepted provider result immediately into the current run's attempt slot
before admitting it as a chosen member.

Missing required outputs, duplicate member IDs, unsafe paths, empty
names, missing bytes, schema failures, exhausted attempts, or no chosen
manifest leave the milestone BLOCKED.

Tool-only milestones must never ask a model to repair deterministic or
infrastructure failure. Declare `on_tool_fail: retryable` with a bounded
`max_tool_attempts` when an unchanged tool call may safely be resumed, or
`on_tool_fail: BLOCKED` when it may not. `retryable` emits a typed
`retry_tool_then_resume` action with `model: none`; it does not fabricate a
draft. Any milestone that can plan, stage, commit, publish, delete, or patch
external state must also keep a run-local phase journal. Persist the durable
operation ID and plan hash before staging, persist the operator result before
readback, and on resume query that exact operation before another commit.
Declare such a milestone with `side_effects: external` and:

```yaml
phase_journal:
  path: publication/phase-journal.json
  operator_result_path: publication/operator-result.json
  resume: query_exact_operation
```

The runtime refuses to choose that milestone unless the journal proves the
ordered phases `plan_frozen → operation_queried → operator_persisted →
readback_verified`, and the bound operator/readback artifacts match the frozen
operation ID and plan hash. The audit treats an external writer script outside
the linked flow as P0 unless the script explicitly declares a separate owning
skill with `M8M_EXTERNAL_SIDE_EFFECT_OWNER`.

## Input bindings

Whole bundle:

```yaml
inputs:
  rendered_content: render.render_v1
```

One output port or one exact member:

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

Omitting `member` returns one value or an ordered array according to the
port cardinality. Downstream milestones query only chosen manifests.

## Fresh rerun, resume, and repair

### Execution storage boundary

Source code, flow definitions, gems, schemas, and tools stay in the source
repository. Mutable runtime state does not. On Windows, new runs use
`%SystemDrive%\NisanRuntime` by default (`C:\NisanRuntime` on this host), or
the same-volume path passed by `--harness-root` / `M8M_HARNESS_ROOT`.

```text
C:\NisanRuntime\runs\<run-id>\   # run, goal, builder staging, retries, media
C:\NisanRuntime\cache\v1\...    # optional candidate cache
D:\...\source-repo\              # code/specification authority only
```

Every new run freezes `m8m_run_context_v2` with a versioned storage contract:
source root, execution root, active run directory, and cache root. Request
`file_ref_v2` bytes are copied once into `inputs/source-assets`, hash-verified,
deduplicated, recorded in `source-assets-manifest.json`, and rewritten to
run-local paths before any milestone runs. Never create runs, goals, caches,
checkpoints, or milestone media below the source repository.

An exact initialized legacy run may be resumed or repaired at its recorded
location. Default discovery never scans the source repository, and the
runtime never migrates, rewrites, or deletes legacy D-drive runs. Generic M8M
does not export final packages; a product runtime remains the sole authority
for any verified final delivery to source-owned output storage.

An ordinary invocation and an explicit **rerun** start fresh by default:

```powershell
python <installed-skill>/scripts/m8m_run.py --harness-root C:\NisanRuntime --request <request.json>
```

This creates a new run folder, freezes `run-context.json`, uses
`context_policy: isolated`, and defaults cross-run cache to `off`. Supplying
an existing folder in fresh mode is an error. A fresh run may use only its
exact `request.json`, current flow/gems/schemas, and chosen outputs created
inside that run. Previous Codex/Claude chat, prior run folders, and remembered
assets are not implicit inputs. If the new invocation omits a required input,
ask for it; do not reconstruct it from earlier chat.

Resume is explicit and names the same run:

```powershell
python <installed-skill>/scripts/m8m_run.py --run-mode resume --run-dir <exact-run>
```

Plain resume validates chosen manifests, skips completed milestones, and
starts at the first unfinished node. It makes no handler or judge call
for a valid chosen milestone. This is run-local state reuse, not cache.
Run budgets count active `run_flow` execution only; time parked for a model,
user response, or infrastructure repair does not consume the budget.

If a defect required editing the workflow, intentionally adopt the edit:

```powershell
python <installed-skill>/scripts/m8m_run.py --run-dir <exact-run> --continue-after-edit <id>
```

The runtime verifies that changed code belongs only to the selected
milestone or its downstream graph, preserves compatible upstream chosen
outputs, adopts a new implementation lock, and clears the selected node
plus its dependents. Flow-level changes, milestone reordering, unowned code
changes, or changed preserved output ports require a fresh run. Do not use
cross-run cache to implement continuation.

When a side-effecting milestone already has a valid frozen external phase
journal, `--continue-after-edit` on that external milestone may preserve
compatible changed upstream outputs as historical operation evidence. It must
clear only the external milestone and recover by querying the exact frozen
operation; it may not replay consumed preimages or plan/commit another write.

Intentional regeneration uses:

```powershell
python <installed-skill>/scripts/m8m_run.py --run-dir <exact-run> --replace-milestone <id>
```

Clear the selected milestone plus all transitively downstream chosen and
working state, then rerun from the selected node. Keep one current chosen
bundle per milestone; never create revisions. Replacement affects session
artifacts only. Existing side-effecting tools retain their idempotency
responsibilities.

## Repeated-goal ledger

For a goal such as ten interior cases, use one outer goal ledger and one
fresh child M8M session per row:

```powershell
python <installed-skill>/scripts/m8m_run.py --harness-root C:\NisanRuntime --goal <goal.json>
```

Each row gets its own `run-context.json`, roster, milestone state, cycle
ledger, request, and folder under `rows/<row>/attempt-NNN/run`. Every child
is cache-off and context-isolated, so row 10 cannot inherit the conversation
or assets from rows 1–9. The outer `goal-ledger.json` records pending,
running, done, and blocked rows.

After repairing the workflow, continue the current child in place with
`--goal-dir <goal> --continue-after-edit <id>`. Compatible upstream chosen
assets remain. Use `--abandon-row` only when the current attempt should be
left behind and a new clean attempt created. Never execute all rows as ten
reuses of one growing chat context.

## Optional cross-run candidate cache

Short tasks and all existing workflows remain uncached. Cache requires
double opt-in: the milestone declares eligibility and the run enables it.

```yaml
cache:
  reuse: candidate
  ttl_seconds: 86400
  side_effects: none
```

```powershell
python <installed-skill>/scripts/m8m_run.py ... --cache-mode read-write --cache-namespace <tenant-or-local>
```

Supported cache modes are `off`, `read`, `write`, and `read-write`; new runs
default to `off` and freeze their mode and namespace. Goal children always
force `off`. Platform launchers
must use the tenant ID as the namespace. A cache hit is copied into the
new run as a candidate, validated, and judged again. It never marks a
milestone complete and never supplies a judge receipt. Only the new run's
judge PASS may write `chosen-output.json`.

Cache entries live outside run state at
`<harness-root>/cache/v1/<flow>/<namespace-digest>/<milestone>/<key>/`.
Run-local cache status lives under `work/cache-receipt.json` (or
`items/<row>/work/cache-receipt.json` for cycle rows). Expired, corrupt,
rejected, missing, or unwritable
cache is a miss/warning, never a workflow blocker. Judge retries look up at
most once. `--replace-milestone` bypasses cache reads for the selected and
downstream subgraph. `--cache-mode write` forces fresh work and refreshes
the cache.

Only side-effect-free candidate work may declare cache. Wait, branch
decision, cycle-control, and legacy `loop: for` milestones may not. An
ordinary selected-path or cycle-row work milestone may cache its semantic
input. Cache digests stay in cache contracts; do not add them to chosen
manifests or judge receipts. Prune explicitly with:

```powershell
python <installed-skill>/scripts/m8m_run.py cache-prune --harness-root C:\NisanRuntime
```

## Cycle, branch, and wait

- Judge loops candidate work on the same milestone.
- Branch occurs only after chosen output commit; unselected paths skip.
- Cycle walks a frozen ledger; pass preserves a completed row, fail
  purges unfinished live residue and leaves the row resumable.
- Wait is a normal milestone whose roster row becomes `waiting`; resume
  supplies a draft and re-enters its judge loop.

Roster and cycle ledger are state books, not canvas nodes and not chosen
output substitutes.

Cache receipts are optimization metadata, not state books or canvas nodes.

## Deliverables

| File | Purpose |
| --- | --- |
| `planning/flowstep-audit.md` | Proposed milestones, FlowSteps, and toolbox |
| `planning/m8m-flowchart.md` | Canvas chart with declared output ports and FlowStep guide |
| `planning/m8m-flowchart.jpg` | Portable human review image |
| `<skill>/agents/openai.yaml` | Sole authored graph, ordered roster, required closed root workflow contracts, terminal bindings, and observer source |
| `<skill>/agents/<id>.yaml` | One authored milestone with inputs, success, outputs, FlowSteps, and bounded intelligence |
| `<skill>/references/<id>.md` | One authored Gem per milestone |
| `<repo>/flowsteps/flows/<id>/flow.yaml` | Generated review snapshot of `flowstep_flow_v4` |
| `<run>/work/builder/.../source-bundle.json` | Deterministic `m8m.workflow_source_bundle.v1` with declared runtime archive, lock, and launcher resources |
| `<run>/work/builder/packages/<flow_id>.m8mpkg` | Deterministic import envelope chosen only after full harness validation; never uploaded automatically |
| `<repo>/flowsteps/flows/<id>/launch.py` | Codebase-owned product launcher; never imports the Builder |
| `<repo>/flowsteps/flows/<id>/runtime/releases/<runtime_id>/` | Immutable digest-addressed runtime source closure with a locked Python ABI/dependency set |
| `<repo>/flowsteps/flows/<id>/m8m-runtime-lock.json` | Runtime release selected for new runs of this workflow build |
| `<repo>/flowsteps/flows/<id>/references/<id>.md` | FlowStep worker prompts |
| `<repo>/flowsteps/tools/<id>/` | Reusable tools or explicit `BUILD_REQUIRED` non-runnable sketches |
| `<repo>/.agents/skills/<name>/SKILL.md` | Local product skill pointer |
| `<repo>/.agents/skills/<name>/scripts/m8m_run.py` | Thin pointer to the codebase launcher; contains no runtime |

Generated flowcharts must teach:

```text
expectation → FlowSteps → structural admission → optional semantic judge → chosen reply → downstream milestone
```

Return the local outcome, exact portable source-bundle path, staged resource
root, digest, and installed runtime-release identity, plus the audit,
chart/JPEG, flow path, toolbox path, and any `BUILD_REQUIRED` notes. The
bundle, resource root, and codebase runtime form the local executable package;
they are not a cloud submission or deployment. A sketch is a repairable
finding, never validation PASS or installation authority. Do not push or
deploy.
