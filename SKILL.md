---
name: m8m-harness-builder
description: >
  M8M 2.0 workflow writer. Split a Codex or Claude skill into milestone
  canvas nodes, internal FlowSteps, reusable tools, declared output ports,
  milestone judges, and chosen output bundles. Use when the user wants to
  design, scaffold, validate, or install an M8M workflow. Invoke as
  $m8m-harness-builder.
license: MIT
metadata:
  author: dse120071750
  version: "2.0"
---

# M8M harness builder 2.0

This skill writes an n8n-style milestone workflow. Milestones are the
canvas nodes. FlowSteps and their preferred tools run inside each node.

```text
FlowSteps produce/refine current candidate
  → milestone judge reads Rule of success
      reject → another candidate attempt
      PASS   → commit current candidate
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

## Three words

| Word | Meaning |
| --- | --- |
| **Milestone** | Compulsory canvas checkpoint with `success`, a judge, an output contract/schema, and one or more named output ports. No chosen manifest means BLOCKED. |
| **FlowStep** | Atomic candidate-generation or refinement goal inside a milestone. Prefer one tool; recover within the milestone when needed. |
| **Tool** | Reusable Python implementation at `<repo>/flowsteps/tools/<id>/`. A generated-new tool may be a sketch; a chosen milestone result may not. |

## Builder workflow

Run the builder through its own canonical v4 workflow:

```powershell
python scripts/run_m8m.py --target <skill-or-flow-dir> --codebase <repo>
```

`flows/m8m_build_v1.yaml` has five compulsory milestones:

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

Piecemeal authoring remains available:

```powershell
python scripts/audit_harness.py --target <skill-or-flow-dir>
python scripts/generate_harness.py --codebase <repo> --from-audit <skill>/planning/flowstep-audit.json
python scripts/validate_harness.py --codebase <repo> --flow-id <id>
```

## Canonical contracts

Write only `flowstep_flow_v4`. Runtime envelopes are
`flowstep_output_v3`; chosen manifests are `m8m_chosen_output_v1`.
Reject v1/v2/v3 workflows with: “regenerate with
m8m-harness-builder 2.0”. Do not offer a legacy execution mode.

Every milestone must declare:

- `success`: the exact goal its judge accepts;
- `output_contract` and `output_schema`;
- `outputs`: one or more ports with `id`, display `name`, provider-neutral
  `kind`, `cardinality: one|many`, and `required`.

Supported kinds are `json`, `data`, `file`, `image`, `video`, and
`audio`. Collection items need unique filesystem-safe IDs and non-empty
names.

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
  worker: render_judge
  flowsteps:
    - { id: generate, tool: image_generate }
    - { id: compare, tool: visual_compare }
    - { id: refine, tool: image_edit }
```

The workflow JSON/YAML exposes these output ports for the canvas. The
chosen manifest exposes run-time status, previews, member ordering, and
query paths.

## Rule of success and judge loop

Each milestone owns a gem at `references/<id>.md`:

- **Rule of success** is the judge instruction.
- Each `## <flowstep>` section is that FlowStep’s prompt.

The gem and judge are attached metadata, not separate canvas nodes.
Develop a milestone-specific worker instead of defaulting every quality
gate to shared `ok_receipt`. Intelligence may draft; it may not set
`ok`, `branch`, or `cycle`.

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

Missing required outputs, duplicate member IDs, unsafe paths, empty
names, missing bytes, schema failures, exhausted attempts, or no chosen
manifest leave the milestone BLOCKED.

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

An ordinary invocation and an explicit **rerun** start fresh by default:

```powershell
python scripts/run_flow.py --codebase <repo> --flow-id <id> --request <request.json>
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
python scripts/run_flow.py ... --run-mode resume --run-dir <exact-run>
```

Plain resume validates chosen manifests, skips completed milestones, and
starts at the first unfinished node. It makes no handler or judge call
for a valid chosen milestone. This is run-local state reuse, not cache.

If a defect required editing the workflow, intentionally adopt the edit:

```powershell
python scripts/run_flow.py ... --run-dir <exact-run> --continue-after-edit <id>
```

The runtime verifies that changed code belongs only to the selected
milestone or its downstream graph, preserves compatible upstream chosen
outputs, adopts a new implementation lock, and clears the selected node
plus its dependents. Flow-level changes, milestone reordering, unowned code
changes, or changed preserved output ports require a fresh run. Do not use
cross-run cache to implement continuation.

Intentional regeneration uses:

```powershell
python scripts/run_flow.py ... --replace-milestone <id>
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
python scripts/run_goal.py --codebase <repo> --flow-id <id> --goal <goal.json>
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
python scripts/run_flow.py ... --cache-mode read-write --cache-namespace <tenant-or-local>
```

Supported cache modes are `off`, `read`, `write`, and `read-write`; new runs
default to `off` and freeze their mode and namespace. Goal children always
force `off`. Platform launchers
must use the tenant ID as the namespace. A cache hit is copied into the
new run as a candidate, validated, and judged again. It never marks a
milestone complete and never supplies a judge receipt. Only the new run's
judge PASS may write `chosen-output.json`.

Cache entries live outside run state at
`<repo>/flowsteps/cache/v1/<flow>/<namespace-digest>/<milestone>/<key>/`.
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
python scripts/m8m_cache.py prune --codebase <repo> [--flow-id <id>]
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
| `<repo>/flowsteps/flows/<id>/flow.yaml` | `flowstep_flow_v4` workflow |
| `<repo>/flowsteps/flows/<id>/references/<id>.md` | Rule of success and FlowStep prompts |
| `<repo>/flowsteps/tools/<id>/` | Reusable tools or explicit generate-new stubs |
| `<repo>/.agents/skills/<name>/SKILL.md` | Local product skill pointer |

Generated flowcharts must teach:

```text
FlowSteps → milestone judge loop → chosen output bundle → downstream milestone
```

Return the local outcome, audit, chart/JPEG, flow path, toolbox path, and
any generate-new notes. Do not deploy.
