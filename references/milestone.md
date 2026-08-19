# Milestone nodes (M8M — milestone to milestone)

The builder **writes** the split (chart + table + stubs). **FlowSteps and
tools** may be sketches. **Milestones are the harness.**

Three words. Do not mix them.

| Word | Meaning | Path |
| --- | --- | --- |
| **Milestone** | Canvas node. Harness checkpoint. `this.in` **is** `previous.out`. Required asset or BLOCK. | `flowsteps/flows/<flow_id>/` |
| **FlowStep** | Small goal **inside** a milestone (bind five images). Not drawn on the canvas. May be a stub. | listed on that milestone |
| **Tool** | One premade Python way to do that goal. Input schema in, output schema out. Generate-new is a sketch. | `flowsteps/tools/<tool_id>/` |

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

## Milestone rules (the harness)

A milestone is a person doing one checkpoint in a workflow:
`source_ready`, `plan_frozen`, `assets_bound`, `cards_rendered`,
`release_decided`. You either produced the thing or you did not.

It is **not** `crop_4x5` or `fetch_record`. Those are FlowSteps (and
tools). A name like that is a **note** on the chart, not a reason to
refuse to draw.

Each milestone declares a **required asset**. Kind is one of:

| Kind | Proof |
| --- | --- |
| `file` | `asset.path` + `asset.sha256` |
| `image` | same file receipt (bytes of a picture) |
| `json` | closed JSON object with required fields (a proof, not an open bag) |
| `data` | same as json: typed required fields |

The output schema is closed (`additionalProperties: false`) and has
`required` fields. An empty passthrough object is not a milestone.

The next milestone starts **only** when this asset is produced (output
schema PASS). That payload **is** the next milestone’s input schema.
If the asset is missing or invalid: **BLOCK**. No semantic approval.
No “close enough.” Intelligence may draft until the schema PASSes or
the budget is exhausted; it may not skip the asset.

YAML:

```yaml
- id: plan_frozen
  asset:
    kind: json
  output_contract: plan_frozen_v1
  output_schema: schemas/plan_frozen_v1.json
```

`intelligence: none` — assemble only calls listed FlowSteps / tools.
`intelligence: completion|image|judge` — a draft is allowed. Tools run
**first**. On `on_tool_fail: need_model`, drafts may repeat until the
asset schema PASSes or `max_model_attempts` / the run budget is
exhausted. Then BLOCK.

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

Teaching contracts (`references/*.md` on a Codex or Claude skill) belong
on the flow: `<repo>/flowsteps/flows/<id>/references/`. Same ownership as
tools. The skill folder may point at them. It must not be the only copy.

A name like `crop_4x5` or `if_ready` is a **note** on the chart, not a
reason to refuse to draw.
