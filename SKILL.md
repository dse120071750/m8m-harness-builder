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

Invoke `$m8m-harness-builder` using `references/builder-authoring.md` to coordinate
existing tools and FlowSteps.
Inventory the workflow first; preserve working implementations and make only
needed changes. Read `references/flowstep-development.md` for tool reuse.

Name new milestones `milestone01`, `milestone02`, etc.; keep IDs stable on edits.
Bind later inputs to earlier named outputs. See `references/master-prompts.md`.
Every milestone starts with its own complete master prompt at
`references/<milestone>.md` (the Gem). Author it first: role, goal, bound inputs
and reference roles, domain instructions, constraints, and exact deliverable.
Read the entire prompt before execution or recovery. FlowSteps support it;
a short success sentence or isolated section cannot replace it. Preserve supplied
prompts in full, adapted to explicit user requirements.

Native `agents/openai.yaml` owns the graph; `flow.yaml` is generated. Existing
v4 flows may keep their format. Keep named outputs, structural validation,
runtime-owned progress, bounded retries, explicit resume, and one current chosen
output per milestone. Default to `loop: none`. Do not add hashes, revisions,
proof graphs, or automatic image reviews as milestone work or completion gates.
A checksum or review needs an explicit request or an existing external API
requirement. Internal runtime/package integrity stays internal.

Use `scripts/run_m8m.py --mode coordinate` by default. It validates and refreshes
the workflow without archives or runtime releases. Existing packaged flows keep
their launcher and pins; local flows use the returned coordination runner.

Only explicit packaging or isolated distribution uses `--mode package` and
`references/runtime-packaging.md`. The five-stage Builder canvas belongs to
that path; its closure, vendoring, installation, and platform-parity rules do
not apply to ordinary workflow edits. This skill never pushes or deploys.
