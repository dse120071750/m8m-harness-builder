---
name: m8m-harness-builder
description: >
  M8M writer. Split a Codex or Claude skill into milestones, FlowSteps,
  and tools, then write one toolbox table and one flowchart. Each
  milestone is a harness checkpoint with a required asset. Use when
  the user wants to identify checkpoints, list tools per checkpoint, or
  scaffold a flow. Invoke as $m8m-harness-builder.
license: MIT
metadata:
  author: dse120071750
  version: "1.9"
---

# M8M harness builder

This skill **writes** a split. **Milestones are the harness.**
FlowSteps are a guide, like a normal skill.

```text
identify milestones
  → list FlowSteps inside each (atomic; prefer ONE tool)
  → develop that tool (existing / promote / generate-new stub)
  → write one FlowStep table + one milestone flowchart
     (markdown + humanized JPEG; for / judge / branch)
  → scaffold flow YAML and tool stubs
```

Three words:

```text
Milestone  = compulsory harness. previous.out is this.in.
             Required asset (file, image, json proof, data) or BLOCK.
FlowStep   = atomic goal inside that checkpoint.
             Prefers one tool. Tool is optional.
             Table order is the guide. If the tool fails, recover like
             a normal agent. Still must produce the milestone asset.
Tool       = Python at <repo>/flowsteps/tools/<id>/.
             Builder should develop it (fetch, MCP, crop, hash, …).
             Generate-new is a successful sketch.
```

## Do this

```powershell
python scripts/run_m8m.py --target <skill-or-flow-dir> --codebase <repo>
```

Or piecemeal:

```powershell
python scripts/audit_harness.py --target <skill-or-flow-dir>
python scripts/generate_harness.py --codebase <repo> --from-audit <skill>/planning/flowstep-audit.json
```

Deliverables (this is the product):

| File | What |
| --- | --- |
| `planning/flowstep-audit.md` | Proposed milestones, FlowSteps, tools |
| `planning/m8m-flowchart.md` | Milestone chart (harness) + FlowStep table (guide) + For/Judge tables |
| `planning/m8m-flowchart.jpg` | Portable audit JPEG. Humanizer names each milestone and FlowStep. Rewritten on generate and on every step edit. |
| `<repo>/flowsteps/flows/<id>/flow.yaml` | Scaffolded chain |
| `<repo>/flowsteps/tools/<id>/` | Seeded tools, or stubs marked generate-new |
| `<repo>/.agents/skills/<name>/SKILL.md` and `.claude/skills/<name>/SKILL.md` | Pointer skill |

A stub tool or a generate-new row is a successful sketch, not a failed
build. The **milestone output schema is not a sketch**: it names the
asset that must exist. Runtime BLOCKs if that schema does not PASS.

Inside a milestone, follow the FlowStep table as a **guide**: try the
preferred tool first. If it fails, find a way like a normal agent.
The flowchart is only the milestone canvas. The JPEG is the audit
copy: portable, easy to pass around, native when a person reviews
the split. After generate, and after every step edit (`write` /
`mark`), both `planning/m8m-flowchart.md` and
`planning/m8m-flowchart.jpg` are rewritten.

## For, judge, and branch

A **for loop** and a **judge loop** are canvas milestones. **Branch**
is after a milestone. Proceed is guarded by an **internal worker**: a
required repo tool that writes a closed receipt. Intelligence may
draft. It may not set `ok` or `branch`.

- **for:** previous asset is a **ledger** (typed array, `maxItems`). This
  milestone produces the item asset for each remaining row until
  `remaining == 0`.
- **judge:** do the work until the worker says ok. **Image generation**
  and **spatial alignment** always use this. `ok: false` and budget
  left → stay. Budget gone → BLOCK.
- **branch:** after this milestone’s asset PASSes, AI drafts which
  generation path to take. The worker writes `{ok, branch}`. Other
  path milestones are `skipped: true`. Skip is not BLOCK. Do not call
  this IF: IF is a rigid schema fork. URL vs text stays data.

```yaml
- id: images_bound
  loop: for
  ledger: { path: items, item_schema: schemas/image_item_v1.json, max_items: 32 }
  worker: ledger_receipt
- id: card_aligned
  loop: judge
  worker: alignment_judge
  intelligence: image
- id: intake_ready
  intelligence: completion
  branch:
    worker: branch_receipt
    default: direct
    paths:
      - { id: direct, then: restyle_direct }
      - { id: floorplan_source_case, then: floorplan_source_ready }
    join: restyle_ready
- id: restyle_direct
  on_path: direct
- id: floorplan_source_ready
  on_path: floorplan_source_case
```

Receipt `ok: true` **and** the milestone asset schema PASS → next
(or, for branch, the named path). Receipt cannot waive a missing asset.

## Session folder (address)

A run is a session tree. Generated images/files go in **slots**, never
Downloads, `/tmp`, or a new invented folder.

Default: `<repo>/flowsteps/runs/<flow_id>/<utc>_<id>/`

```text
<run>/
  request.json
  manifest.json
  milestones/<id>/out/files/asset.png     # required bytes
  milestones/<id>/out/asset.json          # envelope
  milestones/<id>/items/001/files/...     # for-ledger
  milestones/<id>/work/attempts/01/...    # judge retries
```

Tools receive `address.write_to`. Copy generated bytes there, then
`hash_bind`. `asset.path` must stay inside the run (address leak → BLOCK).
Audit later from `manifest.json`.

`validate_harness.py` is optional. `run_flow.py` is the harness:
tool fail → agent recovery; no asset → BLOCK; worker not ok / no receipt → stay or BLOCK.

## Response

```text
Outcome: writer
Audit: planning/flowstep-audit.md
Chart: planning/m8m-flowchart.md
JPEG:  planning/m8m-flowchart.jpg
Flow: <repo>/flowsteps/flows/<flow_id>
Tools: <repo>/flowsteps/tools/
Notes: (name hints, generate-new tools — never a refusal to draw)
```

Return the flowchart JPEG (human labels), then the markdown table, then the audit.
