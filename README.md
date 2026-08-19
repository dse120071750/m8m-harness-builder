# M8M

**M8M is milestone-to-milestone.** A semantic n8n. Each milestone consumes the previous milestone’s output schema as its input schema. FlowSteps live *inside* the milestone and are tool-heavy.

This is not a chat skill. It is a **skill for making skills** that produce assets or a standardized workflow — cards, packages, IO receipts — not another session transcript.

## The problem

Codex and Claude usually **overuse intelligence**. For every small task they generate fresh code in the session: a crop, a hash, a fetch, a one-off Playwright script. A skill is developed as markdown and a worker. It does **not** come with a toolbox. The scripts that do exist land in `~/.codex/skills` or `~/.claude/skills`, not in the project `src`. Different products share one skill folder, so tools mix, drift, and cannot be reused as project code.

That is the problem. The flow is not controllable. The skill does not ship an asset.

## What M8M is

n8n is controllable because every node has a contract. It is the wrong grain for AI: every crop and hash becomes its own canvas node.

M8M keeps n8n’s control and changes the grain to **milestone → milestone**:

```text
n8n:     node = one action          (HTTP, crop, IF, hash)
M8M:     node = one milestone a human would inspect
         FlowSteps = premade tools that run *inside* that milestone
         next.in   = previous.out   (fixed schema)
```

```text
request
  → source_ready.in  = request
  → source_ready.out
  → plan_frozen.in   = source_ready.out
  → plan_frozen.out
  → assets_bound.in  = plan_frozen.out
  → assets_bound.out
  → cards_rendered.in = assets_bound.out
  → cards_rendered.out
  → release_packaged.in = cards_rendered.out
  → release_packaged.out = asset {path, sha256}
```

The next milestone cannot start until the previous output schema PASSes. Crop, hash, fetch, render stay **inside** the milestone as tools. The canvas never draws them.

The M8M skill is doctrine and a driver. Product tools live in `<repo>/flowsteps/tools/`.

## Why M8M, not another agent loop

| n8n | Usual Codex / Claude skill | M8M |
| --- | --- | --- |
| Action node | Generate code for the tiny task | Premade tool *inside* a milestone |
| Integrations on the canvas | Scripts dumped in `~/.codex/skills` | `<repo>/flowsteps/tools/<id>/` |
| Graph of HTTP/IF/crop | Markdown + worker, no toolbox | **Milestone → milestone** |
| Typed I/O | Chat in, chat out | **Next input schema = previous output schema** |
| Runs to a side effect | Stops at a draft | Stops at an **asset** or a verified receipt |

A milestone named `crop_4x5` or `fetch_record` is invalid. Those are FlowSteps inside a milestone. Use them heavily. Do not regenerate them.

## Three rules

### 1. Milestone to milestone

A milestone is something you would stop and inspect, not a mechanical action.

- Valid: `source_ready`, `plan_frozen`, `assets_bound`, `cards_rendered`, `release_decided`
- Invalid: `crop_4x5`, `fetch_record`, `hash_bind`

How many times a crop runs stays **inside** the milestone. The next milestone starts only when this one’s output schema PASSes. That output **is** the next input schema.

### 2. FlowSteps live inside the milestone (tool-heavy)

| | Tool (default, inside the milestone) | Intelligence (exception, still inside) |
| --- | --- | --- |
| What | Premade Python script | Judgment the schema cannot compute |
| Where | `<repo>/flowsteps/tools/<id>/` | Optional `NEED_MODEL` *on* that milestone |
| Contract | Same input → same action. Fixture-testable | Draft only. The tools still emit the payload |
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

### 3. Previous schema in. This schema out.

```text
validate this milestone's input.schema.json
  (= previous milestone's output.schema.json)
  → run the FlowSteps listed on this milestone (tools)
  → validate this milestone's output.schema.json
  → that object is the next milestone's input
```

- The next milestone reads a typed receipt, not a transcript.
- `{ok: boolean}` stubs are invalid.
- A file is a `file_ref_v2` (`path` + `sha256`).
- A BLOCKED run stays BLOCKED.

If the schema is loose, you do not have a workflow. You have a chat.

## Ownership

```text
this skill (M8M)                         doctrine + audit + driver
<repo>/flowsteps/tools/<tool_id>/        premade FlowSteps (the real product)
<repo>/flowsteps/flows/<flow_id>/        milestone → milestone + instruction
```

Do not put product tools in `~/.codex/skills` or `~/.claude/skills`. That is how skills mix.

## Factory (audit → toolbox → flow → validate → ship)

Five premade milestones in `flows/m8m_build_v1.yaml`. One driver:

```powershell
python scripts/run_m8m.py --target <skill-or-flow-dir> --codebase <repo>
```

That writes `planning/flowstep-audit.json`, copies seed tools into
`<repo>/flowsteps/tools/`, generates the milestone chain from the audit
(each input schema is the previous output; last step is an `asset`
receipt), validates, and ships `<repo>/.agents/skills/<name>/SKILL.md`.

Or step by step:

```powershell
python scripts/audit_harness.py --target <skill-or-flow-dir>
python scripts/generate_harness.py --codebase <repo> --from-audit <skill>/planning/flowstep-audit.json
python scripts/run_flow.py --codebase <repo> --flow-id <flow_id> --run-dir <run> --request <request.json>
```

Read [references/milestone.md](references/milestone.md) and [references/tool-vs-intelligence.md](references/tool-vs-intelligence.md).

## Install

```powershell
git clone https://github.com/dse120071750/m8m-harness-builder.git $env:USERPROFILE\.codex\skills\flowstep-harness-builder
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
SKILL.md                 M8M working method (the skill that makes skills)
scripts/                 audit, generate, validate, run
contracts/               shared JSON schemas
references/              milestone + tool-vs-intelligence
examples/text_pipeline   fixture
templates/               generated tool / milestone stubs
```
