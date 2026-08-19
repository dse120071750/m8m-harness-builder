# Milestone nodes (M8M — milestone to milestone)

A FlowStep is a **milestone**, not a mechanical action.

n8n’s canvas is too stiff: every HTTP call and crop is its own node, so
any change means rewiring the graph. Keep n8n’s good parts (typed units,
reusable pieces, AI does not invent IO) and invert the grain:

```text
n8n:     node = one action
M8M:     node = one milestone a human would check
         next.in = previous.out
         FlowSteps = premade tools used *inside* that milestone
```

The driver advances milestone → milestone. It does not micro-orchestrate
crop then hash then IF.

## Units

| Unit | Meaning | Path |
| --- | --- | --- |
| Milestone | Named outcome + output schema | `flowsteps/flows/<flow_id>/` |
| Tool | Deterministic function, reused | `flowsteps/tools/<tool_id>/` |
| Intelligence | Optional draft/judge/image *inside* a milestone | `NEED_MODEL` |
| Driver | Order of milestones | this skill’s `run_flow.py` |

## Milestone rules

A milestone is something you would stop and inspect: `source_ready`,
`plan_frozen`, `assets_bound`, `cards_rendered`, `release_decided`.

It is **not** `crop_4x5` or `fetch_record`. Those are tools.

Each milestone lists the only tools it may run. How many times, in what
order, on which pages — that stays inside the milestone. The next
milestone starts only when this one’s output schema PASSes.

`intelligence: none` — a short assemble script that only calls listed
tools. `intelligence: completion|image|judge` — a draft is allowed, then
the toolbox must still produce a schema-valid payload.

## Toolbox rules

Tools follow `references/tool-vs-intelligence.md`. They are never
FlowSteps. Adding a missing capability means adding
`flowsteps/tools/<id>/`, not drawing another milestone.
