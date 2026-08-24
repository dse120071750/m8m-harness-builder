# Milestone nodes (M8M — milestone to milestone)

The builder writes the split. Milestones are the compulsory harness;
FlowSteps and tools live inside them.

| Word | Meaning | Runtime role |
| --- | --- | --- |
| **Milestone** | One canvas node with a success rule and declared output ports. | Commits exactly one chosen output bundle or BLOCKS. |
| **FlowStep** | An atomic goal inside a milestone. | Generates or refines the current candidate. |
| **Tool** | The preferred implementation for one FlowStep. | May be retried or replaced during candidate work. |
| **Judge** | The milestone-specific success evaluator. | PASS commits the current candidate; rejection loops. |

```text
n8n: node = one action
M8M: node = one milestone
     FlowSteps = internal actions
     declared outputs = canvas ports
     chosen-output.json = accepted run-time port values
```

## Milestone contract

A valid `flowstep_flow_v4` milestone declares all of:

```yaml
- id: cards_rendered
  success: Seven approved cards satisfy the layout and source-grounding rules.
  output_contract: cards_rendered_v1
  output_schema: milestones/cards_rendered/output.schema.json
  outputs:
    - id: cards
      name: Approved cards
      kind: image
      cardinality: many
      required: true
    - id: render_receipt
      name: Render receipt
      kind: json
      cardinality: one
      required: true
  loop: judge
  worker: cards_rendered_judge
  flowsteps:
    - { id: render_cards, tool: render_cards }
    - { id: compare_layout, tool: compare_layout }
    - { id: refine_cards, tool: image_edit }
```

Kinds are provider-neutral: `json`, `data`, `file`, `image`, `video`, or
`audio`. Cardinality is `one` or `many`. Every candidate collection item
has a unique filesystem-safe `id` and a non-empty display `name`.

A milestone is not complete merely because its handler returned. It is
complete only when:

1. the current candidate satisfies its output schema;
2. every required output and member exists;
3. the judge accepts the declared `success` rule; and
4. the runtime writes `out/chosen-output.json` last.

Only that chosen manifest is queryable downstream. Rejected attempts may
remain under `work/attempts`, but they are diagnostics—not candidates a
later milestone may select.

## Rule of success and candidate loop

Every milestone has a gem and a dedicated worker that reads its Rule of
success. The gem and judge sit on the milestone; neither is a second
canvas node.

```text
FlowStep candidate work
  → validate named outputs
  → judge Rule of success
       not ok: keep working, then return a new current candidate
       ok:     commit this current candidate as the chosen bundle
```

The judge receipt has no candidate hash or lock ID. PASS is immediately
followed by materialization, so there is no separate “candidate lock”
entity. Missing outputs, duplicate IDs, invalid paths, missing bytes,
schema errors, exhausted attempts, or a missing chosen manifest BLOCK.

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

- Keep atomic operations inside the milestone instead of multiplying
  canvas nodes.
- Prefer one tool per FlowStep. The sequence is a guide for producing the
  candidate, not a substitute for the milestone success rule.
- If a preferred tool fails, recover within the milestone when possible.
- Generated-new tools can be implementation sketches; declared milestone
  outputs cannot be sketches.
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
