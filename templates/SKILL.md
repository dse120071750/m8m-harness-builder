---
name: __SKILL_NAME__
description: M8M product skill. Milestones consume the previous output schema. FlowSteps inside each milestone run project tools. Use the $m8m-harness-builder driver.
---

# __SKILL_NAME__

Write this file only after `validate_harness.py` returns PASS.

Do not execute steps from markdown. Run the driver:

```powershell
python __BUILDER_ROOT__\scripts\run_flow.py --skill-dir <this-skill> --run-dir <run-dir> --request <request.json>
```

Or `python scripts/run.py --run-dir <run-dir> --request <request.json>`.

If the driver returns `ACTION_REQUIRED`, perform only the frozen `model_request`,
write that JSON to `draft_path`, then advance again with `--draft`.

The skill instruction is
`<codebase>/flowsteps/__FLOW_ID__/planning/flowstep-instruction.md`.
Tools live in that codebase tree, not in this C: skill.

Every step is `class: tool` or `class: intelligence`. Fetch, crop, hash,
render, and package are tools. Do not write those on the fly.

for is a ledger milestone (walk items until remaining=0). judge (if)
retries until the worker receipt is ok. The worker is a repo tool.

Generated files go in `address.write_to` inside the run session folder.
Do not invent a folder structure.
