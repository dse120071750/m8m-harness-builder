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
  version: "1.11"
---

# M8M harness builder

This skill **writes** a split. **Milestones are the harness.**
FlowSteps are a guide, like a normal skill.

```text
identify milestones
  → list FlowSteps inside each (atomic; prefer ONE tool)
  → develop that tool (existing / promote / generate-new stub)
  → write one FlowStep table + one milestone flowchart
     (markdown + humanized JPEG; cycle / judge / branch)
  → write each milestone’s gem (rule of success)
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
| `<repo>/flowsteps/flows/<id>/flow.yaml` | Scaffolded chain (`success:` on every milestone) |
| `<repo>/flowsteps/flows/<id>/references/<id>.md` | Gem: rule of success for that checkpoint |
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

## Rule of success (gem), then cycle / judge / branch

Every milestone has a **rule of success**. The meaning lives on that
milestone’s **gem**: `flowsteps/flows/<id>/references/<milestone_id>.md`.
The harness still only gates on the **asset**. Do not add a shared
judge module and drop it onto cycle, asset, quality, and branch.

Two layers. Do not merge them.

1. **Exist (harness).** Source is ready must produce a file (path +
   sha256). Missing → BLOCK. Schema cannot be waived by a model, a
   gem, or a judge.
2. **Good (gem).** How a person would pass this checkpoint. Teaching,
   like a normal skill. `loop: judge` only when exist is not enough
   (image, alignment, “does it teach”). Worker writes `{ok}`.
   Intelligence may draft. It may not set `ok`.

Self-looping FlowSteps when it is not working is already this
milestone: `on_tool_fail: need_model`, and `loop: judge` when quality
is the gate. Do not add a new canvas loop that re-runs the FlowStep
table as a program.

Proceed is guarded by an **internal worker**. Intelligence may draft.
It may not set `ok`, `branch`, or `cycle`. Do not use FOR or IF.

- **cycle:** freeze a **ledger** first (a normal milestone asset). Then
  wrap milestones around each unfinished row. After the last wrap
  asset PASSes, AI drafts pass|fail. `cycle_receipt` writes the
  receipt **and updates the ledger**. Pass → preserve `items/NNN`.
  Fail → purge live residue; the row stays unfinished (resumable).
  `remaining == 0` is data, not the gate. Not a judge module.
- **judge:** do the work on *this* milestone until the worker says ok.
  Image generation and spatial alignment always use this. Rare.
- **branch:** after this milestone’s asset PASSes, AI drafts which
  path to take. The worker writes `{ok, branch}`. Skip is not BLOCK.
  Not a judge module.

```yaml
- id: pages_ledger_frozen
  asset: { kind: json }
  success: "Pages ledger is frozen — must produce a json proof."
- id: page_bound
  on_cycle: pages
- id: page_rendered
  on_cycle: pages
  cycle:
    worker: cycle_receipt
    ledger: pages_ledger_frozen
    start: page_bound
    join: release_packaged
    pass: "current row has a bound image (path + sha256)"
- id: card_aligned
  loop: judge
  worker: alignment_judge
  intelligence: image
  success: "Card is aligned — must produce an image (path + sha256). Retry until the worker receipt is ok."
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
  milestones/<id>/items/001/files/...     # finished cycle round (preserved)
  cycles/<id>/ledger.json                 # resumable cycle ledger
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
