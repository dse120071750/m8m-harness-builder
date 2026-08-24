---
name: __SKILL_NAME__
description: Product skill scaffolded by M8M. Milestones, FlowSteps, and tools live in the repo.
---

# __SKILL_NAME__

Written by `$m8m-harness-builder` 2.0. Each milestone is a canvas checkpoint
with named output ports. FlowSteps refine the current candidate; the judge
accepts it as the milestone's one chosen output bundle. Without
`out/chosen-output.json` the milestone is BLOCKED. Each milestone has a gem
(`flowsteps/flows/__FLOW_ID__/references/<id>.md`): **Rule of success**
for the judge, plus one prompt section per FlowStep. Quality boxes use
`loop: judge` plus a named `<id>_judge`. The model may draft; it may not set `ok`.
A FlowStep section is not a canvas node. FlowSteps inside are a guide: prefer
one tool, recover like a normal agent if it fails. Branch is after a
milestone: AI drafts the path, `branch_receipt` writes `{ok, branch}`,
the other path is skipped. Cycle wraps milestones over a frozen
ledger: `cycle_receipt` writes pass|fail and updates the ledger.
Finished rounds are preserved; unfinished residue is purged.

Put generated media in `address.write_to` on the milestone input.
Do not invent a folder. Do not leave files in Downloads or `/tmp`.
The driver creates `<repo>/flowsteps/runs/<flow_id>/<run_id>/`.
A roster (`roster.json`) is frozen at the start with one row per
milestone. Wait pauses that roster and exits; resume finds it and
re-judges the wait gem. Cycle keeps its own ledger.

The flow context is isolated. A normal invocation or a user request to
**rerun** must create a new run folder with cache mode `off`. Do not use the
current chat, a prior task summary, another run folder, or remembered assets
as milestone input. When the runtime returns `ACTION_REQUIRED`, launch a
fresh no-history worker from its `context_capsule_path`. That worker reads
only `allowed_files` and writes only `write_file`; the orchestration chat
must not author the draft itself. If the fresh request omits a required
input, ask for it instead of recovering it from earlier chat.

Downstream milestones bind only chosen outputs. Use a whole binding such
as `render.render_v1`, or `{from, output, member}` for an exact named member.
Plain resume skips valid chosen manifests. Intentional replacement uses
`--replace-milestone <id>` and invalidates all transitively downstream
session output; it does not create revisions.

Resume must be explicit (`--run-mode resume --run-dir <exact-run>`). If the
workflow was repaired, use `--continue-after-edit <id>` on that same run:
preserve compatible upstream chosen outputs, adopt the new implementation,
and invalidate that milestone plus its downstream graph. This is run-state
continuation, not cache. Start fresh if milestone order, flow-level policy,
or a preserved output contract changed.

For repeated-case goals, use `run_goal.py`. It owns an outer
`goal-ledger.json` and launches one fresh, cache-off, context-isolated child
run per row. Continue a repaired current row with `--continue-after-edit`;
use `--abandon-row` only to preserve the old attempt and start a new one.

Workflow state and cross-run cache are separate. Cache is off unless both
the milestone declares `{reuse: candidate, ttl_seconds, side_effects: none}`
and the run enables `--cache-mode`. A hit is only a candidate: copy it into
this run, validate it, and let the current judge decide. It never supplies
chosen state or a judge receipt. Resume does not read cache for completed
milestones, and replacement bypasses cache reads for its invalidated
subgraph. Use the tenant ID as `--cache-namespace` in platform runs.

- flow: `<repo>/flowsteps/flows/__FLOW_ID__/`
- chart: `<repo>/flowsteps/flows/__FLOW_ID__/planning/m8m-flowchart.md`
- chart jpeg: `<repo>/flowsteps/flows/__FLOW_ID__/planning/m8m-flowchart.jpg`
- tools: `<repo>/flowsteps/tools/`

__CLASSIFICATION_TABLE__
