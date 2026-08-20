---
name: __SKILL_NAME__
description: Product skill scaffolded by M8M. Milestones, FlowSteps, and tools live in the repo.
---

# __SKILL_NAME__

Written by `$m8m-harness-builder`. Each milestone is a harness checkpoint
with a required asset (file, image, json proof, or data). Missing it is
BLOCKED. FlowSteps inside are a guide: prefer one tool, recover like a
normal agent if it fails. for is a ledger milestone; judge (if) retries
until a repo worker receipt is `ok: true`. Branch is after a
milestone: AI drafts the path, `branch_receipt` writes `{ok, branch}`,
the other path is skipped. Cycle wraps milestones over a frozen
ledger: `cycle_receipt` writes pass|fail and updates the ledger.
Finished rounds are preserved; unfinished residue is purged.

Put generated images in `address.write_to` on the milestone input.
Do not invent a folder. Do not leave files in Downloads or `/tmp`.
The driver creates `<repo>/flowsteps/runs/<flow_id>/<run_id>/`.

- flow: `<repo>/flowsteps/flows/__FLOW_ID__/`
- chart: `<repo>/flowsteps/flows/__FLOW_ID__/planning/m8m-flowchart.md`
- chart jpeg: `<repo>/flowsteps/flows/__FLOW_ID__/planning/m8m-flowchart.jpg`
- tools: `<repo>/flowsteps/tools/`

__CLASSIFICATION_TABLE__
