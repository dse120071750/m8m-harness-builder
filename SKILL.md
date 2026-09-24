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
Start each milestone with its complete master prompt at
`references/<milestone>.md`: role, goal, context and reference roles,
instructions, constraints, and deliverable. Preserve supplied prompts in full,
adapted to explicit requirements. Read the whole prompt at execution and recovery.

Every milestone Markdown then lists numbered FlowSteps with descriptive actions
and actual tools, followed by named outputs. Match executable bindings; leave no
`<>` placeholders. See `references/master-prompts.md` for layout and output guidance.

Milestone inputs are semantic context: notes, text, files, references, and earlier
results. Do not author, require, or validate a milestone input schema. Interpret
context through the master prompt; retain useful bindings. Every milestone must
return actual structured named outputs validated against its output schema.

Native `agents/openai.yaml` owns the graph; `flow.yaml` is generated. Existing
v4 flows may keep their format. Keep runtime-owned progress, bounded retries,
explicit resume, and one chosen output per milestone. Default to `loop: none`.
Do not add hashes, revisions, proof graphs, or automatic image reviews as
milestone gates. A checksum or review needs an explicit request or external API
requirement. Internal runtime/package integrity stays internal.

Use `scripts/run_m8m.py --mode coordinate` by default and its returned runner.
Only explicit packaging uses `--mode package` and `references/runtime-packaging.md`;
its five-stage canvas does not apply to workflow edits. This skill never pushes or deploys.
