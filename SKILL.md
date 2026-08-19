---
name: flowstep-harness-builder
description: F8F — a semantic n8n. Skill for making skills that ship assets or standardized workflows by milestone, with premade Python tools in the project toolbox. Use when a Codex/Claude skill overuses intelligence, generates session code for tiny tasks, or keeps scripts in ~/.codex/skills instead of the repo.
license: MIT
metadata:
  author: dse120071750
  version: "1.0"
---

# F8F

Semantic n8n. Heavy tools. Premade scripts. Ship the skill by milestone.
This is the skill that makes skills which produce an asset or a
standardized workflow — not session-generated glue.

Read `references/milestone.md` and `references/tool-vs-intelligence.md`.

## Ownership

| Need | Owner |
| --- | --- |
| Doctrine, generate, validate, run | This skill |
| Reusable tools | `<repo>/flowsteps/tools/<tool_id>/` |
| Milestone flow + instruction | `<repo>/flowsteps/flows/<flow_id>/` |

## Invariant

```text
FlowStep = milestone (human checkpoint + output schema)
Tool     = pre-made function used *inside* a milestone
```

A milestone named `crop_*` / `fetch_*` / `hash_*` is invalid. Those are
tools. Intelligence may exist *on* a milestone (`NEED_MODEL`) and may
only call tools listed on that milestone.

## Working method

1. Audit the target skill. The audit worker writes
   `planning/flowstep-audit.md` (goal, current tools, proposed
   milestones, Python toolbox, each FlowStep I/O schema). It does not
   rewrite the target:

```powershell
python scripts/audit_harness.py --target <skill-or-flow-dir>
```

2. Name the final payload (last milestone schema in that audit).
3. Split into the fewest milestones a human would inspect.
4. For each milestone, list toolbox functions. If a tool is missing,
   generate it first:

```powershell
python scripts/generate_harness.py --codebase <repo> --tool crop_4x5
```

5. Generate the flow and instruction MD:

```powershell
python scripts/generate_harness.py --codebase <repo> --flow-id case_detail_v1 --milestone source_ready --milestone assets_bound --tools hash_bind,crop_4x5 --intelligence assets_bound
```

6. Implement tools, then each milestone assemble. Mark the instruction
   after each milestone is DONE. Return
   `flowsteps/flows/<flow_id>/planning/flowstep-instruction.md`.

## Audit a current harness

Identity: `agents/audit_worker.yaml`. The Python tool inventories the
skill and writes `planning/flowstep-audit.md`. It does **not** rewrite
the target.

The markdown always includes:

- audited skill
- goal (functionality separation)
- current tools (scripts, agents, declared toolbox, handlers)
- proposed milestone split
- tools to standardize to Python (`flowsteps/tools/<id>/`)
- input and output schema of each FlowStep

```powershell
python scripts/audit_harness.py --target <skill-or-flow-dir>
python scripts/audit_harness.py --codebase <repo> --flow-id <flow_id>
```

A current v1 worker harness will come back `NEEDS_UPGRADE` with P0s
(`in_process`, persistent worker, action-named steps). That is the audit,
not a migrate command.

## Stop conditions

- a milestone is a single crop/fetch/hash
- a product tool is written under `.codex/skills`
- the worker writes SQL/crop/Playwright in the session
- intelligence with file/hash outputs and an empty toolbox
- stub tools or `{ok: boolean}` schemas

## Response

```text
Outcome:
Audit: planning/flowstep-audit.md
Tools: <repo>/flowsteps/tools/
Flow: <repo>/flowsteps/flows/<flow_id>
Instruction: planning/flowstep-instruction.md
Milestones:
  - <id>: intelligence|none | tools | PENDING|DONE
```

After an audit, return the audit markdown. After generate, return the
instruction markdown.
