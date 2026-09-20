---
name: __SKILL_NAME__
description: M8M 3.1 product skill whose closed native source compiles into a displayable, executable milestone workflow.
---

# __SKILL_NAME__

Invoke `$__SKILL_NAME__` through `agents/openai.yaml`. The canvas owns the
closed public workflow contracts and each node owns one exact
`agents/<milestone>.yaml` plus `references/<milestone>.md` Gem. That Gem is the
milestone's complete master prompt. Author it first: role, goal, inputs and
reference roles, instructions, constraints, and exact output. Start every
milestone from the whole prompt; optional FlowStep notes support it.

Name new milestones `milestone01`, `milestone02`, etc. Keep IDs stable on edits.
Bind later inputs explicitly to earlier named outputs and describe those same
references in each master prompt. Graph order controls execution.

Canonical `flow.yaml` is generated review output, never a second authoring
surface. A generated tool remains `BUILD_REQUIRED` and `non_runnable` until
implemented and validated.
Use generated `planning/m8m-flowchart.md` only as the human-readable canvas
projection; it never replaces `agents/openai.yaml` as graph authority.

Each active milestone defaults to `loop: none` and completes from its actual
named outputs and schema checks. Do not add hashes, revisions, proof graphs,
or automatic image reviews. A checksum or separate review belongs only to an
explicit user request or an existing external API contract. Keep one current
chosen output per milestone.

Local execution coordinates existing tools through the installed M8M runner;
ordinary workflow edits do not require packaging. For an explicitly packaged
workflow, `scripts/m8m_run.py` points only to the
codebase launcher at `flowsteps/flows/<flow_id>/launch.py`; that launcher
selects a digest-addressed runtime release installed beside the harness. It
must never import or invoke `m8m-harness-builder`. This skill never pushes,
publishes, deploys, or activates anything. Builder 2.x run folders are
untrusted import evidence.
