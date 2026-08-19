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
  version: "1.4"
---

# M8M harness builder

This skill **writes** a split. **Milestones are the harness.**
FlowSteps are a guide, like a normal skill.

```text
identify milestones
  → list FlowSteps inside each (atomic; prefer ONE tool)
  → develop that tool (existing / promote / generate-new stub)
  → write one FlowStep table + one milestone flowchart
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
| `planning/m8m-flowchart.md` | Milestone chart (harness) + FlowStep table (guide) |
| `<repo>/flowsteps/flows/<id>/flow.yaml` | Scaffolded chain |
| `<repo>/flowsteps/tools/<id>/` | Seeded tools, or stubs marked generate-new |
| `<repo>/.agents/skills/<name>/SKILL.md` and `.claude/skills/<name>/SKILL.md` | Pointer skill |

A stub tool or a generate-new row is a successful sketch, not a failed
build. The **milestone output schema is not a sketch**: it names the
asset that must exist. Runtime BLOCKs if that schema does not PASS.

Inside a milestone, follow the FlowStep table as a **guide**: try the
preferred tool first. If it fails, find a way like a normal agent.
The flowchart is only the milestone canvas.

`validate_harness.py` is optional. `run_flow.py` is the harness:
tool fail → agent recovery; no asset → BLOCK.

## Response

```text
Outcome: writer
Audit: planning/flowstep-audit.md
Chart: planning/m8m-flowchart.md
Flow: <repo>/flowsteps/flows/<flow_id>
Tools: <repo>/flowsteps/tools/
Notes: (name hints, generate-new tools — never a refusal to draw)
```

Return the flowchart markdown and the FlowStep table. Then the audit.
