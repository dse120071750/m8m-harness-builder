# FlowStep Harness

Codex and Claude usually **overuse intelligence**. For every small task they generate fresh code in the session: a crop, a hash, a fetch, a one-off Playwright script. A skill is developed as markdown and a worker. It does **not** come with a toolbox. The scripts that do exist land in `~/.codex/skills` or `~/.claude/skills`, not in the project `src`. Different products share one skill folder, so tools mix, drift, and cannot be reused as project code.

That is the problem. The flow is not controllable.

**An AI-native n8n** is the fix. Same controllability. Different grain.

n8n works because every node has a contract: known inputs, known outputs, reusable tools, no improvisation. It fails AI work because every crop, fetch, and hash becomes its own node. The canvas gets stiff. One product change means rewiring the graph.

This skill keeps n8n’s control and inverts the node:

```text
n8n:     node = one action          (HTTP, crop, IF, hash)
here:    node = one semantic milestone a human would inspect
         tools = pre-made Python functions in the *project* toolbox
```

The model may invent *inside* a milestone. It may not invent the path, the I/O, or the toolbox. Product tools live in `<repo>/flowsteps/tools/`, never in the skill folder. The skill is doctrine and a driver. The project owns the code.

```text
request
  → source_ready      schema in / schema out
  → plan_frozen       schema in / schema out
  → assets_bound      schema in / schema out
  → cards_rendered    schema in / schema out
  → release_packaged  schema in / schema out
```

The driver advances **milestone → milestone**. It does not micro-orchestrate crop then hash then IF. The next milestone starts only when this one’s output schema PASSes.

## Why n8n, but semantic

| n8n keeps | n8n drops | FlowStep does instead |
| --- | --- | --- |
| Typed units | Action-sized nodes | A node is a **checkpoint**: `source_ready`, `plan_frozen`, `release_packaged` |
| Reusable integrations | Graph rewires for every crop | Reuse lives in `flowsteps/tools/` and is called *inside* the milestone |
| AI does not invent I/O | Open agent loops | **Schema in, schema out.** Intelligence may only fill a draft the tool admits |
| Fail-closed execution | Prompt-shaped business logic | Crop, hash, fetch, render stay Python. Judgment stays intelligence |

A milestone named `crop_4x5` or `fetch_record` is invalid. Those are tools.

## Three rules

### 1. A FlowStep is a semantic milestone

A milestone is something you would stop and inspect, not a mechanical action.

- Valid: `source_ready`, `plan_frozen`, `assets_bound`, `cards_rendered`, `release_decided`
- Invalid: `crop_4x5`, `fetch_record`, `hash_bind`

How many times a crop runs, on which pages, in what order — that stays **inside** the milestone. The canvas only sees the outcome.

### 2. Separate tool from intelligence

| | Tool | Intelligence |
| --- | --- | --- |
| What it is | Pre-made Python function | Judgment or invention the schema cannot compute |
| Where it lives | `<repo>/flowsteps/tools/<id>/` | Optional `NEED_MODEL` *on* a milestone |
| Contract | Same input → same action. Fixture-testable | Draft only. The tool still validates and emits the payload |
| Examples | fetch, crop, hash, render, package, schema-validate | plan, caption, choose, release-judge |

The agent **calls** tools. The agent does **not** write SQL, crop math, or Playwright in the session. Intelligence must not pick the next FlowStep. That is an open agent. This is a workflow.

### 3. Fix schema in and schema out

Every milestone and every tool has an input schema and an output schema. That is the control plane.

```text
validate input.schema.json
  → run tool / assemble (optional draft)
  → validate output.schema.json
  → next milestone
```

- The next step reads a typed receipt, not a chat transcript.
- `{ok: boolean}` stubs are invalid.
- A file is a `file_ref_v2` (`path` + `sha256`), not a bare string.
- A BLOCKED run stays BLOCKED. No silent repair loop.

Controllability is the schema. If the schema is loose, the flow is not a flow.

## Units

| Unit | Meaning |
| --- | --- |
| **Milestone** | Named outcome + output schema. The only canvas node. |
| **Tool** | Deterministic Python function. Reused across flows. Never a node. |
| **Intelligence** | Optional draft / judge / image *inside* a milestone. Still schema-bound. |
| **Driver** | Order of milestones. `scripts/run_flow.py`. |

Product tools belong in the **project**, not in this skill. That is how skills stay unmixed:

```text
~/.codex/skills/flowstep-harness-builder/   # doctrine + driver only
<repo>/flowsteps/tools/<tool_id>/           # reusable Python toolbox
<repo>/flowsteps/flows/<flow_id>/           # milestone flow + instruction
```

## Audit, then generate

The audit worker writes `planning/flowstep-audit.md` and does **not** rewrite the target:

- what the skill is
- the separation goal
- current tools
- proposed milestones
- which units must become standardized Python
- input and output schema of each FlowStep

```powershell
python scripts/audit_harness.py --target <skill-or-flow-dir>
```

Then stock the toolbox and generate the flow:

```powershell
python scripts/generate_harness.py --codebase <repo> --tool crop_4x5
python scripts/generate_harness.py --codebase <repo> --flow-id case_detail_v1 --milestone source_ready --milestone assets_bound --tools hash_bind,crop_4x5 --intelligence assets_bound
python scripts/validate_harness.py --codebase <repo> --flow-id case_detail_v1
python scripts/run_flow.py --codebase <repo> --flow-id case_detail_v1 --run-dir <run> --request <request.json>
```

Read [references/milestone.md](references/milestone.md) and [references/tool-vs-intelligence.md](references/tool-vs-intelligence.md).

## Install

**Codex (user skill):**

```powershell
git clone https://github.com/dse120071750/flowstep-harness-builder.git $env:USERPROFILE\.codex\skills\flowstep-harness-builder
pip install -r $env:USERPROFILE\.codex\skills\flowstep-harness-builder\requirements.txt
```

**Repo-local skill** (share with a product repo): copy this folder to `<repo>/.agents/skills/flowstep-harness-builder/`.

Then invoke `$flowstep-harness-builder`.

```powershell
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Live product-flow tests skip when those repos are not present.

## Layout

```text
SKILL.md                 agent entry (the working method)
scripts/                 audit, generate, validate, run
contracts/               shared JSON schemas (the control plane)
references/              milestone + tool-vs-intelligence doctrine
examples/text_pipeline   fixture
templates/               generated tool / milestone stubs
```
