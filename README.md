# FlowStep Harness Builder

A Codex / Agent Skill that treats a **FlowStep as a milestone**, not an n8n action.

Stock a **toolbox** of typed Python functions. Intelligence may exist *inside* a milestone and may only call those tools.

```text
FlowStep = milestone (human checkpoint + output schema)
Tool     = pre-made function used *inside* a milestone
```

## Install

**Codex (user skill):**

```powershell
git clone https://github.com/GaryLamindex/flowstep-harness-builder.git $env:USERPROFILE\.codex\skills\flowstep-harness-builder
pip install -r $env:USERPROFILE\.codex\skills\flowstep-harness-builder\requirements.txt
```

**GitHub CLI skill install** (after `gh` is installed):

```powershell
gh skill install GaryLamindex/flowstep-harness-builder --agent codex
```

**Repo-local skill** (share with a product repo):

```text
<repo>/.agents/skills/flowstep-harness-builder/
```

Then invoke `$flowstep-harness-builder`.

## Audit a skill

Writes `planning/flowstep-audit.md`: goal, current tools, proposed milestones, Python toolbox, each FlowStep I/O schema. Does not rewrite the target.

```powershell
python scripts/audit_harness.py --target <skill-or-flow-dir>
```

## Generate a milestone flow

Product tools live in the **codebase**, not in this skill:

```text
<repo>/flowsteps/tools/<tool_id>/
<repo>/flowsteps/flows/<flow_id>/
```

```powershell
python scripts/generate_harness.py --codebase <repo> --tool crop_4x5
python scripts/generate_harness.py --codebase <repo> --flow-id case_detail_v1 --milestone source_ready --milestone assets_bound --tools hash_bind,crop_4x5 --intelligence assets_bound
python scripts/validate_harness.py --codebase <repo> --flow-id case_detail_v1
python scripts/run_flow.py --codebase <repo> --flow-id case_detail_v1 --run-dir <run> --request <request.json>
```

## Tests

```powershell
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Live product-flow tests skip when those repos are not present.

## Layout

```text
SKILL.md                 Codex / Agent Skill entry
agents/audit_worker.yaml audit identity
scripts/                 audit, generate, validate, run
contracts/               shared JSON schemas
references/              doctrine
examples/text_pipeline   v2 fixture
templates/               generated tool / milestone stubs
```

Read `references/milestone.md` and `references/tool-vs-intelligence.md`.
