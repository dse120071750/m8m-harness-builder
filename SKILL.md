---
name: m8m-harness-builder
description: >
  M8M writer. Split a Codex or Claude skill into milestones, FlowSteps,
  and tools, then write one toolbox table and one flowchart. Use when
  the user wants to identify checkpoints, list tools per checkpoint, or
  scaffold a flow. Invoke as $m8m-harness-builder.
license: MIT
metadata:
  author: dse120071750
  version: "1.2"
---

# M8M harness builder

This skill **writes** a split. It is not a production runtime and not a
guardrail.

```text
identify milestones
  → list FlowSteps (small goals) inside each
  → list tools (existing / promote from a script / generate new)
  → write one table + one flowchart
  → scaffold flow YAML and tool stubs
```

Three words:

```text
Milestone  = checkpoint you would inspect. previous.out is this.in.
FlowStep   = small goal inside that checkpoint.
Tool       = Python that does the goal, at <repo>/flowsteps/tools/<id>/.
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
build. `validate_harness.py` and `run_flow.py` are optional follow-ups.

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
