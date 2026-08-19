---
name: m8m-harness-builder
description: >
  M8M — milestone to milestone. Semantic n8n. A milestone is the canvas
  node; its input schema is the previous milestone output. FlowSteps are
  tool-heavy units inside the milestone. Tools are premade Python in the
  project toolbox. Use when a Codex/Claude skill overuses intelligence,
  generates session code for tiny tasks, or keeps scripts in
  ~/.codex/skills instead of the repo. Invoke as $m8m-harness-builder.
license: MIT
metadata:
  author: dse120071750
  version: "1.1"
---

# M8M

Milestone to milestone. Three words:

```text
Milestone  = canvas node. previous.out is this.in. Human inspects this.out.
FlowStep   = tool-heavy unit *inside* a milestone. Never a canvas node.
Tool       = premade Python at <repo>/flowsteps/tools/<id>/. A FlowStep runs it.
```

This is the skill that makes skills which produce an asset or a
standardized workflow — not session-generated glue.

Read `references/milestone.md` and `references/tool-vs-intelligence.md`.

## Ownership

| Need | Owner |
| --- | --- |
| Doctrine, generate, validate, run | This skill (`$m8m-harness-builder`) |
| Tools (premade Python) | `<repo>/flowsteps/tools/<tool_id>/` |
| Milestone chain + which FlowSteps each runs | `<repo>/flowsteps/flows/<flow_id>/` |

## Invariant

```text
Milestone named crop_* / fetch_* / hash_*  = invalid (that is a FlowStep / tool)
FlowStep without a tool                    = invalid
Tool living under ~/.codex/skills          = invalid
This milestone.in != previous milestone.out = invalid
if_* / loop_* milestone names               = invalid (schema gates, not checkpoints)
foreach without maxItems                    = invalid
next without else                           = invalid
gate field not on this output schema        = invalid
foreach owned by intelligence               = invalid
```

Intelligence may exist *on* a milestone (`NEED_MODEL`) and may only call
the FlowSteps listed on that milestone.

## Working method

The factory is five milestones (`flows/m8m_build_v1.yaml`). Run them
with one premade driver. Do not generate crop/fetch/hash in the session.

```powershell
python scripts/run_m8m.py --target <skill-or-flow-dir> --codebase <repo>
```

That:

1. Writes `planning/flowstep-audit.json` (and `.md`).
2. Installs premade **tools** from `seeds/` into `<repo>/flowsteps/tools/`.
3. Generates the milestone chain (next input schema = previous output
   schema; FlowSteps listed inside each milestone; last milestone
   `asset` path+sha256).
4. Validates the harness.
5. Ships `<repo>/.agents/skills/<name>/SKILL.md`.

Unknown tools without a seed stay FINDINGS until a real fixture exists.
Do not use `--step` (v2 action nodes). Use `--from-audit` or `--milestone`.

## Audit a current harness

Identity: `agents/audit_worker.yaml`. Writes `planning/flowstep-audit.md`.
It does **not** rewrite the target.

The markdown always includes:

- audited skill
- goal (functionality separation)
- current tools
- proposed **milestones**
- FlowSteps / tools to standardize to Python (`flowsteps/tools/<id>/`)
- input and output schema of each **milestone** (next.in = previous.out)
- tool vs intelligence table (`tool_vs_intelligence_table_v1`)
- schema control (`next.when` / `foreach`) inferred from output JSON Schema, never from a model

```powershell
python scripts/audit_harness.py --target <skill-or-flow-dir>
python scripts/audit_harness.py --codebase <repo> --flow-id <flow_id>
```

## Stop conditions

- a milestone is a single crop/fetch/hash (that is a FlowStep)
- a product **tool** is written under `.codex/skills`
- the worker writes SQL/crop/Playwright in the session
- intelligence with file/hash outputs and no FlowSteps
- stub tools or `{ok: boolean}` schemas
- a milestone input schema that is not the previous output schema
- if/else or loop decided by intelligence instead of JSON Schema

## Response

```text
Outcome:
Audit: planning/flowstep-audit.md
Tools: <repo>/flowsteps/tools/
Flow: <repo>/flowsteps/flows/<flow_id>
Instruction: planning/flowstep-instruction.md
Milestones:
  - <id>: intelligence|none | FlowSteps (tools) | PENDING|DONE
```

After an audit, return the audit markdown. After generate, return the
instruction markdown.
