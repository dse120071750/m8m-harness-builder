# Milestone nodes (M8M — milestone to milestone)

The builder **writes** the split (chart + table + stubs). **FlowSteps and
tools** may be sketches. **Milestones are the harness.**

Three words. Do not mix them.

| Word | Meaning | Path |
| --- | --- | --- |
| **Milestone** | Canvas node. Harness checkpoint. `this.in` **is** `previous.out`. Required asset or BLOCK. | `flowsteps/flows/<flow_id>/` |
| **FlowStep** | Atomic goal **inside** a milestone. Prefers one tool. Table order is a guide, not a compulsory path. If the tool fails, recover like a normal agent. | listed on that milestone |
| **Tool** | Preferred Python for that FlowStep. Optional. Builder should develop it. Generate-new is a sketch. | `flowsteps/tools/<tool_id>/` |

n8n’s canvas is too stiff: every HTTP call and crop is its own node.
Keep n8n’s good parts (typed units, reusable pieces, AI does not invent
IO) and invert the grain:

```text
n8n:     node = one action
M8M:     node = one milestone (harness)
         next.in = previous.out
         FlowSteps = atomic goals *inside* that milestone (guide)
         Tool = the one preferred Python for a FlowStep (optional)
```

The driver advances milestone → milestone. Crop/hash stay FlowSteps
inside a milestone. A **for loop** and a **judge loop** **are**
milestones. **Branch** is after a milestone: AI drafts the path, a
worker writes `{ok, branch}`, the other path is skipped. An internal
repo worker writes the receipt. Intelligence may draft; it may not
set `ok` or `branch`. Do not call branch IF.

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
- id: source_ready
  asset:
    kind: file
  output_contract: source_ready_v1
  flowsteps:
    - id: fetch_record
      tool: fetch_record
    - id: hash_bind
      tool: hash_bind
  on_tool_fail: need_model
```

`flowsteps` is the **guide**. Try `fetch_record`, then `hash_bind`. Either
tool may be missing or fail — then recover like a normal agent. The
**file asset** is still compulsory.

Default `on_tool_fail` is `need_model` (agent recovery). Set `BLOCKED`
only when a tool fail must stop the run before the asset check.
Missing the asset is always BLOCK, even after recovery.

`intelligence` on a milestone is optional judgment for producing the
asset. It is not required for tool-fail recovery, and it must not skip
the preferred tool.

## FlowStep rules (guide, not harness)

- One FlowStep prefers **one** tool. The builder should develop that
  tool (MCP fetch, table read, crop, hash, …).
- Sequence comes from the table. Proceed in that order.
- The tool is optional. How the FlowStep reaches the milestone goal is
  like a normal skill.
- If the tool fails: find a way (draft / retry / another approach).
  Still aimed at the milestone asset.
- Do not draw a FlowStep as a canvas node.

## Toolbox rules

A **tool** is the Python package. A **FlowStep** is the atomic goal that
*prefers* that tool. Adding a missing capability means adding
`flowsteps/tools/<id>/`, not drawing another milestone. See
`references/tool-vs-intelligence.md`.

## For (ledger), judge, and branch

For and judge are canvas milestones. Branch is **after** a checkpoint.
No exclusive `next.when`. Skip is not BLOCK.

- **for:** previous.out is a ledger (array + `maxItems` + item schema).
  This milestone walks remaining items and produces each asset until
  `remaining == 0`. One canvas box, not one node per item.
- **judge:** retry until the worker receipt is `ok: true`. Image
  generation and spatial alignment always use this.
- **branch:** after this milestone’s asset PASSes, AI drafts which
  generation path to take. The worker writes `{ok, branch}`. Milestones
  with `on_path` not equal to that id are `skipped: true`. Join has no
  `on_path`. Example: intake_ready → `direct` (default, case_type is
  not source_case) or `floorplan_source_case` (source record + floor
  plan, freeze the source title).
- **worker:** required Python at `flowsteps/tools/<id>/`. Writes a closed
  receipt. Missing receipt or `ok: false` → BLOCK. `ok: true` still
  needs the milestone asset. The model must not set `ok` or `branch`.

```yaml
- id: images_bound
  loop: for
  ledger:
    path: items
    item_schema: schemas/image_item_v1.json
    max_items: 32
  worker: ledger_receipt
- id: card_aligned
  loop: judge
  worker: alignment_judge
  intelligence: image
```

Audit infers `loop: for` from a previous array with `maxItems`, and
`loop: judge` from image/align/generate names or `intelligence: image|judge`.
The model does not approve proceed.

The one chart is `planning/m8m-flowchart.md` plus
`planning/m8m-flowchart.jpg`. The JPEG is the audit copy: humanizer
names each milestone and FlowStep; it is portable; a person can review
it without mermaid. Generate writes both. Every step edit (`write` /
`mark`) rewrites both.

Teaching contracts (`references/*.md` on a Codex or Claude skill) belong
on the flow: `<repo>/flowsteps/flows/<id>/references/`. Same ownership as
tools. The skill folder may point at them. It must not be the only copy.

A name like `crop_4x5` or `if_ready` is a **note** on the chart, not a
reason to refuse to draw.
