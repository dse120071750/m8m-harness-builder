---
name: __SKILL_NAME__
description: M8M product skill. Milestone to milestone. Each milestone consumes the previous output schema. FlowSteps inside the milestone run project tools. Produces a typed asset.
---

# __SKILL_NAME__

This skill is the instruction surface. M8M is milestone to milestone.
Each milestone input schema is the previous output schema. FlowSteps
are tool-heavy units inside the milestone. Tools (premade Python) live
in the **project** toolbox, not in `~/.codex/skills` or `~/.claude/skills`.
Teaching contracts live on the **flow**, not in this skill folder.

- flow: `<repo>/flowsteps/flows/__FLOW_ID__/`
- tools: `<repo>/flowsteps/tools/`
- teaching: `<repo>/flowsteps/flows/__FLOW_ID__/references/`
- last milestone emits `asset` (`path` + `sha256`)
- classification: `<repo>/flowsteps/flows/__FLOW_ID__/planning/tool-vs-intelligence.json`

## Tool vs intelligence

Schema: `tool_vs_intelligence_table_v1`.

__CLASSIFICATION_TABLE__

```powershell
python __BUILDER_ROOT__/scripts/run_flow.py --codebase <repo> --flow-id __FLOW_ID__ --run-dir <run-dir> --request <request.json>
```

If the driver returns `ACTION_REQUIRED`, write only the frozen draft, then
advance again. Do not invent tools in the session. Do not put product scripts
in this skill folder.
