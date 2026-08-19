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

Every audit and every generated skill must emit this table. The contract is
[`contracts/tool_vs_intelligence_table_v1.schema.json`](contracts/tool_vs_intelligence_table_v1.schema.json).

| id | class | test | why |
| --- | --- | --- | --- |
| `fetch_record` | `tool` | same id → same record | structured read; MCP/DB/HTTP GET |
| `crop_4x5` | `tool` | fixture PNG in, PNG+hash out | pixel math; not a milestone |
| `hash_bind` | `tool` | same bytes → same sha256 | pure bind; `file_ref_v2` receipt |
| `schema_validate` | `tool` | pass/fail from rules | JSON Schema gate |
| `render_html_shell` | `tool` | fixture HTML → screenshot hash | fixed viewport generator |
| `materialize_package` | `tool` | typed inputs → zip/manifest | assembly of already-valid receipts |
| `plan_frozen` | `intelligence` | no fixture without a model | editorial plan is not a typed transform |
| `choose_lesson` | `intelligence` | no fixture without a model | judgment among plausible alternatives |
| `image_generate` | `intelligence` | model produces bytes | invention; `hash_bind` still sizes and binds |
| `release_judge` | `intelligence` | not a pixel measurement | taste / teaching quality; footer geometry stays a tool |

A row is one toolbox function or one milestone intelligence. Columns are
fixed: **id**, **class**, **test**, **why**. Audit writes the instance table
into `planning/flowstep-audit.json`. Generate writes
`planning/tool-vs-intelligence.json` and copies the same table into the
product skill and the flow instruction.

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

## Factory (audit → toolbox → flow → validate → ship)

Five premade milestones in `flows/f8f_build_v1.yaml`. One driver:

```powershell
python scripts/run_f8f.py --target <skill-or-flow-dir> --codebase <repo>
```

That writes `planning/flowstep-audit.json`, copies seed tools into
`<repo>/flowsteps/tools/`, generates the milestone flow from the audit
(last step is an `asset` receipt), validates, and ships
`<repo>/.agents/skills/<name>/SKILL.md`.

Or step by step:

```powershell
python scripts/audit_harness.py --target <skill-or-flow-dir>
python scripts/generate_harness.py --codebase <repo> --from-audit <skill>/planning/flowstep-audit.json
python scripts/run_flow.py --codebase <repo> --flow-id <flow_id> --run-dir <run> --request <request.json>
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
