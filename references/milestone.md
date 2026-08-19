# Milestone nodes (M8M — milestone to milestone)

Three words. Do not mix them.

| Word | Meaning | Path |
| --- | --- | --- |
| **Milestone** | Canvas node. Human checkpoint. `this.in` **is** `previous.out`. | `flowsteps/flows/<flow_id>/` |
| **FlowStep** | Tool-heavy unit **inside** a milestone. Not drawn on the canvas. | listed on that milestone |
| **Tool** | Premade Python a FlowStep runs. Input schema in, output schema out. | `flowsteps/tools/<tool_id>/` |

n8n’s canvas is too stiff: every HTTP call and crop is its own node.
Keep n8n’s good parts (typed units, reusable pieces, AI does not invent
IO) and invert the grain:

```text
n8n:     node = one action
M8M:     node = one milestone
         next.in = previous.out
         FlowSteps = tool-heavy units *inside* that milestone
         Tool = the Python those FlowSteps run
```

The driver advances milestone → milestone. Crop/hash stay FlowSteps
inside a milestone. n8n IF/loop are **schema gates** (`next.when` /
`foreach`), not canvas nodes and not intelligence.

Intelligence is optional *on* a milestone (`NEED_MODEL`). It is not a
third canvas node.

## Milestone rules

A milestone is something you would stop and inspect: `source_ready`,
`plan_frozen`, `assets_bound`, `cards_rendered`, `release_decided`.

It is **not** `crop_4x5` or `fetch_record`. Those are FlowSteps (and
tools).

Each milestone lists the only FlowSteps it may run. How many times, in
what order, on which pages — that stays inside the milestone. The next
milestone starts only when this one’s output schema PASSes. That payload
**is** the next milestone’s input schema.

`intelligence: none` — assemble only calls listed FlowSteps / tools.
`intelligence: completion|image|judge` — a draft is allowed, then the
tools must still produce a schema-valid payload.

## Toolbox rules

A **tool** is the Python package. A **FlowStep** is that tool used
inside a milestone. Adding a missing capability means adding
`flowsteps/tools/<id>/`, not drawing another milestone. See
`references/tool-vs-intelligence.md`.

## Schema gates (if/else and loop)

Criterion is JSON Schema validity, never a model.

- `next[].when` + `else`: exclusive branch. First gate schema that
  validates `this.out` wins. `else: BLOCKED` is allowed.
- `foreach.path` + `item_schema` + `max_items`: loop over a typed array
  already declared on the previous output schema (`maxItems` required).
- Join after a branch: `join: [url_ready, file_ready]` binds the PASS
  branch. Downstream input is still a schema (`oneOf` / open object).

Audit infers `next` from `enum`/`const` fields that match later milestone
ids, and `foreach` from a previous array that already declares
`maxItems`. Generate writes the gate schemas. The model does not approve
the branch.

The one chart is `planning/m8m-flowchart.md` (mermaid + gate table +
foreach table). Audit and generate both write that file. It is not
embedded in the audit report or the instruction.

Teaching contracts (`references/*.md` on a Codex skill) belong on the
flow: `<repo>/flowsteps/flows/<id>/references/`. Same ownership as tools.
The skill folder may point at them. It must not be the only copy.

Forbidden: `if_*` / `loop_*` milestone ids, unbounded while, intelligence
choosing `then`, “repeat until it looks good”.
