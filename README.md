# F8F

**F8F is a semantic n8n.** Heavy tools. Premade scripts. Skills ship by milestone.

This is not a chat skill. It is a **skill for making skills** that actually produce assets or a standardized workflow — cards, packages, IO receipts, a frozen plan — not another session transcript.

## The problem

Codex and Claude usually **overuse intelligence**. For every small task they generate fresh code in the session: a crop, a hash, a fetch, a one-off Playwright script. A skill is developed as markdown and a worker. It does **not** come with a toolbox. The scripts that do exist land in `~/.codex/skills` or `~/.claude/skills`, not in the project `src`. Different products share one skill folder, so tools mix, drift, and cannot be reused as project code.

That is the problem. The flow is not controllable. The skill does not ship an asset.

## What F8F is

n8n is controllable because every node has a contract. It is the wrong grain for AI: every crop and hash becomes its own node, and the canvas gets stiff.

F8F keeps n8n’s control and inverts the node:

```text
n8n:     node = one action          (HTTP, crop, IF, hash)
F8F:     node = one semantic milestone a human would inspect
         tools = premade Python scripts in the *project* toolbox
```

You ship a skill as a **sequence of milestones**. Each milestone is schema in / schema out. Intelligence is rare. Tools are the default.

```text
request
  → source_ready      premade tools + schema out
  → plan_frozen       intelligence only if the schema cannot compute it
  → assets_bound      premade tools + schema out
  → cards_rendered    premade tools + schema out
  → release_packaged  asset + receipt
```

The finished skill produces an **asset or a standardized workflow**, not generated glue code. The F8F skill itself is only doctrine and a driver. Product tools live in `<repo>/flowsteps/tools/`.

## Why F8F, not another agent loop

| n8n | Usual Codex / Claude skill | F8F |
| --- | --- | --- |
| Action node | Generate code for the tiny task | Premade script in the project toolbox |
| Integrations on the canvas | Scripts dumped in `~/.codex/skills` | `<repo>/flowsteps/tools/<id>/` |
| Graph of HTTP/IF/crop | Markdown + worker, no toolbox | Skill ships as **milestones** |
| Typed I/O | Chat in, chat out | **Schema in / schema out** |
| Runs to a side effect | Stops at a draft | Stops at an **asset** or a verified workflow receipt |

A milestone named `crop_4x5` or `fetch_record` is invalid. Those are tools. Use them heavily. Do not regenerate them.

## Three rules

### 1. Ship the skill by milestone

A milestone is something you would stop and inspect, not a mechanical action.

- Valid: `source_ready`, `plan_frozen`, `assets_bound`, `cards_rendered`, `release_decided`
- Invalid: `crop_4x5`, `fetch_record`, `hash_bind`

How many times a crop runs stays **inside** the milestone. The canvas only sees the outcome. The next milestone starts only when this one’s output schema PASSes.

### 2. Heavy tools. Rare intelligence.

| | Tool (default) | Intelligence (exception) |
| --- | --- | --- |
| What | Premade Python script | Judgment the schema cannot compute |
| Where | `<repo>/flowsteps/tools/<id>/` | Optional `NEED_MODEL` *on* a milestone |
| Contract | Same input → same action. Fixture-testable | Draft only. The tool still emits the payload |
| Examples | fetch, crop, hash, render, package, validate | plan, caption, choose, release-judge |

The agent **calls** tools. It does not write SQL, crop math, or Playwright in the session. Intelligence must not pick the next milestone.

### 3. Schema in. Schema out.

```text
validate input.schema.json
  → run premade tools (optional draft)
  → validate output.schema.json
  → next milestone
```

- The next step reads a typed receipt, not a transcript.
- `{ok: boolean}` stubs are invalid.
- A file is a `file_ref_v2` (`path` + `sha256`).
- A BLOCKED run stays BLOCKED.

If the schema is loose, you do not have a workflow. You have a chat.

## Ownership

```text
this skill (F8F)                         doctrine + audit + driver
<repo>/flowsteps/tools/<tool_id>/        premade scripts (the real product)
<repo>/flowsteps/flows/<flow_id>/        milestone flow + instruction
```

Do not put product tools in `~/.codex/skills` or `~/.claude/skills`. That is how skills mix.

## Audit, then generate

F8F first writes `planning/flowstep-audit.md` for an existing skill. It does **not** rewrite the target. The audit is how you turn a chat-shaped skill into a milestone skill that can produce an asset:

- what the skill is
- the separation goal
- current tools
- proposed milestones
- which units must become premade Python
- input and output schema of each milestone

```powershell
python scripts/audit_harness.py --target <skill-or-flow-dir>
```

Then stock the project toolbox and generate the flow:

```powershell
python scripts/generate_harness.py --codebase <repo> --tool crop_4x5
python scripts/generate_harness.py --codebase <repo> --flow-id case_detail_v1 --milestone source_ready --milestone assets_bound --tools hash_bind,crop_4x5 --intelligence assets_bound
python scripts/validate_harness.py --codebase <repo> --flow-id case_detail_v1
python scripts/run_flow.py --codebase <repo> --flow-id case_detail_v1 --run-dir <run> --request <request.json>
```

Read [references/milestone.md](references/milestone.md) and [references/tool-vs-intelligence.md](references/tool-vs-intelligence.md).

## Install

```powershell
git clone https://github.com/dse120071750/flowstep-harness-builder.git $env:USERPROFILE\.codex\skills\flowstep-harness-builder
pip install -r $env:USERPROFILE\.codex\skills\flowstep-harness-builder\requirements.txt
```

Repo-local: copy this folder to `<repo>/.agents/skills/flowstep-harness-builder/`.

Invoke `$flowstep-harness-builder`.

```powershell
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## Layout

```text
SKILL.md                 F8F working method (the skill that makes skills)
scripts/                 audit, generate, validate, run
contracts/               shared JSON schemas
references/              milestone + tool-vs-intelligence
examples/text_pipeline   fixture
templates/               generated tool / milestone stubs
```
