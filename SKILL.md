---
name: m8m-harness-builder
description: >
  M8M 3.2 workflow compiler, validator, and codebase-runtime packager. Author a Codex-native skill
  as milestone canvas nodes, FlowSteps, reusable tools, declared output ports,
  optional semantic judges, and chosen-output contracts; compile canonical flowstep_flow_v4 JSON
  and a base-independent source bundle plus deterministic `.m8mpkg`, then install an immutable runtime
  release in the owning codebase. The product skill points only to that
  codebase launcher and never executes through this Builder installation.
  Use to design, import, scaffold, validate, or locally install an M8M workflow.
  It does not push or deploy.
license: MIT
metadata:
  author: dse120071750
  version: "3.2"
---

# M8M harness builder 3.2

Invoke `$m8m-harness-builder` through `agents/openai.yaml`. Read the declared
authoring guide at `references/builder-authoring.md`; the canvas owns the closed
workflow boundary and each node owns its exact `references/<milestone>.md` Gem.
Before designing or implementing FlowSteps, read
`references/flowstep-development.md` and classify deterministic tool work
separately from bounded intelligence. Product FlowSteps use declared in-process
codebase tools; subprocess workflow steps and hand-built runtime evidence are
build blockers.

Use the authoring guide to compile, validate, or locally install the workflow.
Making an existing skill into an M8M workflow is the same work as a new
workflow: understand the task, split milestones, build or move FlowSteps, and
place Gems, tools, and runtime in the owning codebase. Audit only supplies
that existing context. Mixed leftovers are normal. Generated `flow.yaml` is review-only. A successful local install places the
launcher and digest-addressed runtime releases—including their closed vendored
dependency files—under the target codebase's
`flowsteps/flows/<flow_id>/`; the built skill contains only
`scripts/m8m_run.py`, a pointer to that launcher. This skill never pushes or
deploys. Declare every repository-local runtime import in
`implementation_dependencies`; undeclared or dynamically unprovable local code
imports are build blockers.

Builder validation also emits advisory observer metadata:
`platform_common_profile: compatible|unsupported` and an exact sorted
`platform_unsupported_features` list. This never changes local validity and is
not platform admission. Verify cross-repository contract bytes with
`scripts/verify_platform_contract_parity.py --platform-schema-root <schemas>`.
