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
  version: "1.3"
---

# M8M harness builder

This skill **writes** a split. FlowSteps and tools may be sketches.
**Milestones are the harness.**

```text
identify milestones
  → list FlowSteps (small goals) inside each
  → list tools (existing / promote from a script / generate new)
  → write one table + one flowchart
  → scaffold flow YAML and tool stubs
```

Three words:

```text
Milestone  = harness checkpoint. previous.out is this.in.
             Must produce a declared asset (file, image, json proof, data).
             If it is not produced: BLOCK. Next milestone does not start.
FlowStep   = small goal inside that checkpoint. May be a stub.
Tool       = Python that does the goal, at <repo>/flowsteps/tools/<id>/.
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
| `planning/m8m-flowchart.md` | One mermaid chart + toolbox table |
| `<repo>/flowsteps/flows/<id>/flow.yaml` | Scaffolded chain |
| `<repo>/flowsteps/tools/<id>/` | Seeded tools, or stubs marked generate-new |
| `<repo>/.agents/skills/<name>/SKILL.md` and `.claude/skills/<name>/SKILL.md` | Pointer skill |

A stub tool or a generate-new row is a successful sketch, not a failed
build. The **milestone output schema is not a sketch**: it names the
asset that must exist. Runtime BLOCKs if that schema does not PASS.

`validate_harness.py` is an optional follow-up for filling in tools.
`run_flow.py` is the harness: no asset → BLOCK.

## Response

```text
Outcome: writer
Audit: planning/flowstep-audit.md
Chart: planning/m8m-flowchart.md
Flow: <repo>/flowsteps/flows/<flow_id>
Tools: <repo>/flowsteps/tools/
Notes: (name hints, generate-new tools — never a refusal to draw)
```

Return the flowchart markdown and the toolbox table. Then the audit.
