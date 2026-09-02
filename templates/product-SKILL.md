---
name: __SKILL_NAME__
description: M8M 3.1 product skill whose closed native source compiles into a displayable, executable milestone workflow.
---

# __SKILL_NAME__

Invoke `$__SKILL_NAME__` through `agents/openai.yaml`. The canvas owns the
closed public workflow contracts and each node owns one exact
`agents/<milestone>.yaml` plus `references/<milestone>.md` Gem.

Canonical generated `flow.yaml` is review-only. A generated tool remains
`BUILD_REQUIRED` and `non_runnable` until implemented and validated; edit the
native source instead of the generated representation.
Use generated `planning/m8m-flowchart.md` only as the human-readable canvas
projection; it never replaces `agents/openai.yaml` as graph authority.

Execution is owned by this codebase. `scripts/m8m_run.py` points only to the
codebase launcher at `flowsteps/flows/<flow_id>/launch.py`; that launcher
selects a digest-addressed runtime release installed beside the harness. It
must never import or invoke `m8m-harness-builder`. This skill never pushes,
publishes, deploys, or activates anything. Builder 2.x run folders are
untrusted import evidence.
