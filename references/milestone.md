# Milestone nodes (M8M — milestone to milestone)

The builder writes the split. Milestones are the compulsory harness;
FlowSteps and tools live inside them. Every active path defaults to `loop: none`.
Complete from the actual named outputs and their schema. Do not add hashes,
revision chains, proof graphs, or automatic image reviews. Ordinary file paths
need no checksum companion. A separate judge is used only for an explicitly
requested review; image generation and milestone names do not imply one.

| Word | Meaning | Runtime role |
| --- | --- | --- |
| **Milestone** | One canvas node with a success rule and declared output ports. | Commits exactly one chosen output bundle or BLOCKS. |
| **FlowStep** | An atomic goal inside a milestone. | Generates or refines the current candidate. |
| **Tool** | The preferred implementation for one FlowStep. | May be retried or replaced during candidate work. |
| **Judge** | Optional milestone-specific semantic evaluator after admission. | In `loop: judge`, PASS commits the admitted candidate; rejection loops. |

```text
n8n: node = one action
M8M: node = one milestone
     FlowSteps = internal actions
     declared outputs = canvas ports
     chosen-output.json = accepted run-time port values
```

## Master prompt first

Every milestone starts with one complete master prompt at
`references/<milestone>.md`, selected by `gem`. Author the role, objective,
inputs and reference roles, domain instructions, constraints, and exact output
before choosing its FlowSteps. The whole document is the execution prompt;
Every milestone Markdown lists numbered FlowSteps with their actual tools,
then named outputs; these describe internal actions under the full prompt. See `master-prompts.md`.

Use stable IDs `milestone01`, `milestone02`, etc. for new milestones. Later
master prompts name their upstream producers and outputs; matching `inputs`
bindings resolve the actual values. See `master-prompts.md` for YAML examples.

## Milestone contract

A valid `flowstep_flow_v4` milestone declares all of:

```yaml
- id: milestone01
  gem: references/milestone01.md
  success: Seven rendered cards exist with the declared names and file paths.
  output_contract: cards_rendered_v1
  output_schema: milestones/milestone01/output.schema.json
  outputs:
    - id: cards
      name: Rendered cards
      kind: image
      cardinality: many
      required: true
    - id: render_receipt
      name: Render receipt
      kind: json
      cardinality: one
      required: true
  loop: none
  flowsteps:
    - { id: render_cards, tool: render_cards }
  tools: [render_cards]
  execution:
    candidate_executor:
      ref: handler.article_cards.milestone01@3.1.0
    tool_bindings:
      - { tool: render_cards, ref: render_cards@3.1.0 }
```

This is authored-agent syntax: each FlowStep names its local package. The
`tools` list and binding keys are FlowStep IDs. Compilation replaces each
executable `flowsteps[].tool` with the exact versioned binding ref; IDs remain
milestone-local slots.

Kinds are provider-neutral: `json`, `data`, `file`, `image`, `video`, or
`audio`. Cardinality is `one` or `many`. Every candidate collection item
has a unique filesystem-safe `id` and a non-empty display `name`.

A milestone is not complete merely because its handler returned. The four
authored fields `success`, `output_contract`, `output_schema`, and `outputs`
form one expectation. It is complete only when:

1. the current candidate satisfies its output schema;
2. every required output and member exists;
3. structural admission freezes file/media bytes into the run;
4. for `loop: judge`, the separate semantic judge accepts that same expectation;
5. for `loop: none`, no semantic judge is called; and
6. the runtime writes `out/chosen-output.json` last.

Only that chosen manifest is queryable downstream. Rejected attempts may
remain under `work/attempts`, but they are diagnostics—not candidates a
later milestone may select.

## Success authority and candidate loop

Authored `success` and the output declarations are the sole expectation
authority for machine completion. Every milestone has a complete master-prompt Gem; a legacy
`## Rule of success` section is accepted only when it exactly matches authored
`success`. Only `loop: judge` has a dedicated semantic judge, and that judge is
attached metadata rather than a second canvas node.

```text
read complete milestone master prompt + bind inputs → FlowStep candidate work
  → admit and freeze named outputs
  → loop:none: commit
  → loop:judge: typed judge request
       RETRY: keep working, then return a new current candidate
       PASS:  commit this current candidate as the chosen bundle
```

The judge receipt has no candidate hash or lock ID. The durable control shape
is `m8m.milestone_judge_receipt.v1`: milestone ID, actual attempt,
`PASS|RETRY|BLOCKED`, success rule, judge reference, reasons, blockers, and
maximum attempts. A strict worker returns exactly bounded
`{decision, reasons, blockers}`; compact `{ok}` is invalid. PASS is
immediately followed by materialization, so there is no separate “candidate
lock” entity. Missing outputs, duplicate IDs, invalid paths, missing bytes,
schema errors, exhausted attempts, or a missing chosen manifest BLOCK.

Only when a separate review was explicitly requested, author:

```yaml
loop: judge
worker: render_judge@3.1.0
judge_abi: m8m_milestone_judge_v1
receipt_schema: flowsteps/tools/render_judge/output.schema.json
```

The runtime invokes that worker separately on the admitted current candidate
with only `schema`, milestone/attempt fields, the derived `expectation`,
resolved `inputs`, and `candidate`. It does not accept a handler-supplied
top-level control receipt as the decision. An AI
candidate or AI judge must also declare its exact versioned executor/profile,
Gem and schema bindings, tools, capabilities, token budget, and timeout in the
milestone agent source. Deterministic milestones declare executor/judge refs
without model profiles.

## Output bindings

Bind all declared outputs:

```yaml
inputs:
  rendered_content: cards_rendered.cards_rendered_v1
```

Bind one port, preserving one/many cardinality:

```yaml
inputs:
  cards:
    from: cards_rendered.cards_rendered_v1
    output: cards
```

Bind one exact member:

```yaml
inputs:
  hero:
    from: cards_rendered.cards_rendered_v1
    output: cards
    member: hero_image
```

The canvas reads the static `outputs` declarations. The local frontend
reads `chosen-output.json` for chosen status, ordered members, paths, and
previews.

## FlowStep rules

- Classify every FlowStep as deterministic tool work or bounded intelligence
  before generation; record why a fixture can or cannot decide it.
- Keep atomic operations inside the milestone instead of multiplying
  canvas nodes.
- Prefer one tool per FlowStep. The sequence is a guide for producing the
  candidate, not a substitute for the milestone success rule.
- Product tools are declared in-process functions. A subprocess, shell, CLI,
  secondary workflow, or recursive execution-root discovery is not a
  FlowStep implementation.
- The runtime alone writes chosen manifests and control receipts. After an
  approval boundary, only one declared in-process `finalize` FlowStep may run.
- If a preferred tool fails, recover within the milestone when possible.
- Generated-new tools can be retained only as explicit
  `BUILD_REQUIRED/non_runnable` implementation sketches. They cannot satisfy
  builder validation, local installation, or a declared product milestone
  output.
- Intelligence may draft content but cannot set `ok`, `branch`, or
  `cycle`.

## Fresh context, resume, and repair

Fresh is the default invocation mode. It creates a new cache-off run with
`chat_history_allowed: false`. An `ACTION_REQUIRED` draft must come from the
fresh no-history worker described by its context capsule, using only the
capsule's allowlisted files. Never use prior chat or another case's assets as
an implicit input.

On resume, validate each chosen manifest and skip completed nodes without
calling their handler or judge. Start at the first node without a valid
chosen output. Resume names one exact run and reuses its state; it is not a
cache hit.

After an intentional workflow fix, `--continue-after-edit <id>` adopts only
changes owned by that milestone/downstream graph, preserves compatible
upstream chosen bundles, and invalidates the selected/downstream state.
Milestone reordering or incompatible preserved ports requires a fresh run.

Use `--replace-milestone <id>` only for intentional regeneration. It
clears that milestone and all transitively downstream chosen/work state,
then reruns from the selected node. The replacement is in-place: one
current chosen bundle, no revisions. External side effects retain their
existing idempotency contract.

For a repeated goal, create an outer goal ledger and one fresh cache-off
child run per row. The next row receives a new roster, milestone ledger, and
model context. Repair the current row in place, or explicitly abandon it to
create a new attempt; do not process all rows through one accumulating chat.

## Optional candidate cache

Cache is not milestone state. A milestone may opt in only with:

```yaml
cache: { reuse: candidate, ttl_seconds: 86400, side_effects: none }
```

The run must also enable cache. A hit is copied into this run, checked
against the current output schema, and passed to the current judge. It is
never directly chosen. Rejection or cache failure continues with the
normal FlowSteps and does not consume their attempt budget.

Resume of a chosen milestone never reads cache. Replacement bypasses cache
reads for the selected/downstream nodes. Wait, branch/cycle-control, side
effects, and legacy `loop: for` are ineligible. Cache status is attached
metadata under `work/cache-receipt.json`; it is not a canvas node or downstream input.

## Cycle, judge, branch, and wait

- **judge:** retries candidate production on this milestone until the
  named worker accepts it or the attempt budget is exhausted.
- **branch:** runs only after the current milestone has a chosen bundle;
  the selected path continues and other paths are skipped.
- **cycle:** walks a frozen item ledger. A pass preserves the completed
  item; a fail removes unfinished live residue and leaves it resumable.
- **wait:** is a normal milestone whose roster row becomes `waiting`.
  Resume supplies the reply, reruns its candidate/judge loop, and commits
  a chosen bundle only when accepted.

Roster and cycle ledger are state books, not canvas milestones and not
chosen-output substitutes.

## Session result

```text
milestones/<milestone_id>/out/
  chosen-output.json
  judge-receipt.json
  members/<member_id>/asset.<ext>
```

JSON and data are serialized as JSON assets. File/media members are
copied into their member folders. Member ordering follows the accepted
candidate. The manifest contains relative paths and no selection hashes,
lock identifiers, or revision chain.
