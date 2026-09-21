---
name: m8m-harness-builder
description: >
  Build and edit M8M harnesses that coordinate existing tools and FlowSteps,
  validate milestone outputs, and track progress and resume. Reuse working
  implementations in place. Compile workflow changes without runtime packaging;
  build portable packages and immutable releases only when explicitly requested.
license: MIT
metadata:
  author: dse120071750
  version: "3.2"
---

# M8M harness builder 3.2

Invoke `$m8m-harness-builder` using `references/builder-authoring.md`.
Inventory existing tools and FlowSteps; preserve working implementations.
Read `references/flowstep-development.md` before adding adapters.

Name new milestones `milestone01`, `milestone02`, etc.; keep IDs stable on edits.
Bind later inputs to earlier named outputs. See `references/master-prompts.md`.
Start every milestone with its complete master prompt at
`references/<milestone>.md` (the Gem): role, goal, inputs and reference roles,
instructions, constraints, and deliverable. Preserve supplied prompts in full,
adapted to explicit user requirements. Read the whole prompt at execution and
recovery; a success sentence or isolated FlowStep cannot replace it.

Every milestone Markdown must then list numbered FlowSteps with descriptive
actions and actual tools, followed by named outputs. Match executable bindings;
never leave `<>` tool placeholders. See `references/master-prompts.md` for layout.

Native `agents/openai.yaml` owns the graph; `flow.yaml` is generated. Existing
v4 flows may keep their format. Keep named outputs, structural validation,
runtime-owned progress, bounded retries, explicit resume, and one current chosen
output per milestone. Default to `loop: none`. Do not add hashes, revisions,
proof graphs, or automatic image reviews as milestone work or completion gates.
A checksum or review needs an explicit request or an external API requirement.
Internal runtime/package integrity stays internal.

Use `scripts/run_m8m.py --mode coordinate` by default. Validate and refresh the
workflow and milestone outlines without packaging. Use the returned runner;
existing packaged workflows retain their launcher and pins.

Only explicit packaging uses `--mode package` and
`references/runtime-packaging.md`. Its five-stage canvas and isolation rules do
not apply to ordinary workflow edits. This skill never pushes or deploys.
