# Upgrade: M8M as a Codex skill-creator dialect

Status: accepted and absorbed into the Builder 3.1 hard cutover.
Target: m8m-harness-builder 3.1.
Does not change the three words (Milestone, FlowStep, Tool) or the harness
(required asset or BLOCK, isolated context, chosen output).

Builder 3.1 adds the explicit local execution boundary omitted by the original
2.1 proposal. The dialect compiles a deterministic, base-independent
`m8m.workflow_source_bundle.v1`, packages an immutable runtime release into the
owning codebase harness, and installs a product-skill pointer to that codebase
launcher. It does not create remote bindings, deployment, or provider side
effects.

P0 invariant: Builder is a compiler/packager, never the live runtime of a
built product. Existing runs pin the exact codebase-owned runtime release and
resume with it after Builder upgrades. A Builder upgrade cannot require a
product workflow edit; incompatibility is handled by building a new side-by-side
runtime release for future runs.

## Why this upgrade

Codex already knows how to make a skill:

```text
SKILL.md
agents/*.yaml
references/*.md
```

That is not a bug. It is the platform. Builder 2.0 previously asked the same
model to also author a parallel canvas at `flowsteps/flows/<id>/flow.yaml`
plus gems in a different `references/` folder. The model follows Codex.
`flow.yaml` is left thin or stale. Product teaching lands in
`agents/*.yaml` `rules:` and `skill/references/**`.

Fighting that tendency is a losing product. The upgrade is the opposite:
**keep Codex’s files, make them mean M8M.**

`flow.yaml` stays useful as a **compiled runtime snapshot**. It is not an
authoring surface. The skill creator never writes it.

## Philosophy

M8M is a dialect of a Codex skill, not a second product beside it.

| Codex already has | M8M meaning |
| --- | --- |
| `agents/openai.yaml` | Skill entry + **canvas roster** (milestone order) |
| `agents/<id>.yaml` | **One milestone.** `this.in` is `previous.out`. |
| `references/<id>.md` | **That milestone’s gem.** FlowStep prompts only; YAML `success` is authority. |
| `SKILL.md` | How to invoke. Pointer, not the recipe. |
| Python tools | Still `<repo>/flowsteps/tools/<id>/`. Unchanged. |

Enhancement, not replacement:

- Codex already splits work across several agent YAML files (worker,
  judge, compiler). Today those files are **personalities** that span
  many product steps and dump the recipe into `rules:`.
- M8M says an extra agent file is a **checkpoint**. It consumes the
  previous chosen output, prefers listed tools, and is BLOCKED without
  its declared asset.
- Codex already puts teaching in `references/`. M8M says that folder is
  not a junk drawer: **one file per milestone, same id, no glob.**

The builder’s job is to teach and validate this dialect so a normal
Codex skill creator produces a harness.

## Authoring layout (what the model writes)

```text
<skill>/
  SKILL.md                          # invoke + canvas pointer
  agents/
    openai.yaml                     # interface + milestone order
    <milestone>.yaml                # one file per canvas node
  references/
    <milestone>.md                  # gem (required, same id)
  schemas/
    workflow-request.schema.json   # authored closed public request contract
    workflow-configuration.schema.json
    workflow-result.schema.json
  milestones/<milestone>/
    input.schema.json              # authored milestone contracts
    output.schema.json
```

Schema paths are authored resources, not compiler inventions. They may use
other safe relative locations when declared explicitly, but every referenced
schema must be present in the skill source and becomes a digest-bound source
bundle resource.

Example for caption:

```text
agents/openai.yaml
agents/source_ready.yaml
agents/instagram_frozen.yaml
agents/threads_frozen.yaml
agents/alt_frozen.yaml
agents/details_frozen.yaml
agents/copy_checked.yaml
references/source_ready.md
references/instagram_frozen.md
...
```

A Codex skill creator already wants to add YAML and markdown next to
the skill. This layout uses that instinct. It forbids the failure mode
we keep seeing: one fat worker plus `read_paths: [references/**]`.

## `agents/openai.yaml` — the canvas

Every Codex skill already has this file. Grow it. Do not add a sibling
`flow.yaml` for humans to edit.

```yaml
interface:
  display_name: Nisan Interior Caption
  short_description: Language-only rephrasing of approved interior highlights
  default_prompt: >
    Use $nisan-interior-caption to rephrase this frozen handoff for the
    requested platform. Do not change design meaning.

canvas:
  schema: m8m_skill_canvas_v1
  flow_id: interior_caption_zh_hant_v1
  context_policy: isolated
  workflow_contracts:
    request_schema: schemas/workflow-request.schema.json
    configuration_schema: schemas/workflow-configuration.schema.json
    result_schema: schemas/workflow-result.schema.json
    terminal_bindings:
      - name: caption_result
        from: copy_checked.caption_result_v1
        output: caption_result
  milestones:
    - source_ready
    - instagram_frozen
    - threads_frozen
    - alt_frozen
    - details_frozen
    - copy_checked
```

Rules for this file:

- `default_prompt` is the invoke line. It is not the writing gem.
- `canvas.milestones` is the only authored order. Missing an
  `agents/<id>.yaml` for a listed id is P0.
- Extra YAML files that are not in this list and are not a declared
  `<id>_judge` are P0 (personalities that are not checkpoints).
- New public workflows declare closed Draft 2020-12 request, configuration,
  and result schemas plus exact named success-terminal output bindings.
  These are authoring-only source-bundle contracts, not runtime graph fields.
- Host paths, run IDs, chat IDs, credentials, deployment IDs, and mutable
  cloud identities are never authored into the canvas or source bundle.

## `agents/<milestone>.yaml` — the node

Replace `flowstep_agent_environment_v1` fat workers with one milestone
agent schema. Keep it looking like Codex agent YAML (id, read_paths,
write_paths, intelligence). Add the harness fields the builder already
requires on a v4 milestone.

```yaml
schema: m8m_milestone_agent_v1
agent_id: instagram_frozen
role: milestone
success: >
  Instagram copy is a Traditional-Chinese language-only realization of
  the frozen handoff and passes preflight.

inputs:
  source: source_ready.caption_source_v1

outputs:
  - id: result
    name: Instagram Caption Frozen
    kind: json
    cardinality: one
    required: true

output_contract: caption_instagram_v1
gem: references/instagram_frozen.md

flowsteps:
  - id: instagram_caption_preflight
    tool: instagram_caption_preflight
  - id: hash_bind
    tool: hash_bind
tools: [instagram_caption_preflight, hash_bind]
execution:
  candidate_executor:
    ref: handler.caption_flow.instagram_frozen@3.1.0
  tool_bindings:
    - { tool: instagram_caption_preflight, ref: instagram_caption_preflight@3.1.0 }
    - { tool: hash_bind, ref: hash_bind@3.1.0 }

intelligence: completion
on_tool_fail: need_model
loop: none

read_paths:
  - references/instagram_frozen.md
  - milestones/instagram_frozen/draft.schema.json
write_paths:
  - milestones/instagram_frozen/work/draft.json

rules:
  - Read only the declared read_paths. Do not set ok, branch, or cycle.
  - Write only the declared write_paths.
```

The block above is authored-agent syntax. FlowStep `tool` values are local
packages, while `tools` and binding keys are milestone-local FlowStep IDs.
The compiler emits the exact versioned binding refs as executable FlowStep
tools.

That is the enhancement of Codex `rules:` / `read_paths:`:

| Old Codex habit | M8M dialect |
| --- | --- |
| `rules:` holds the product recipe | `rules:` is harness-only (≤ 5 lines). Recipe is the gem. |
| `read_paths: [references/**]` | Exact paths. Must include this milestone’s gem. Glob of `references/**` is P0. |
| `allowed_steps: [determine_facts, build_infographics, …]` | `flowsteps:` inside **this** milestone. One preferred tool each. |
| One worker YAML for the whole skill | One YAML per milestone. Previous output is the input. |
| Judge rules inlined in the worker | Closed `execution.judge` ref/profile on the milestone plus `m8m_milestone_judge_v1`; standalone judge YAML is rejected until it has a typed contract. |

`this.in = previous.out` is expressed with `inputs:`, which Codex-style
contracts already approximate via `input_contracts`. We keep the field,
but it must name a **chosen** upstream bundle, not a bag of skill-wide
schemas.

## `references/<milestone>.md` — the gem Codex already wants to write

Do not invent a second references folder for authoring. Constrain the
one Codex uses.

Required shape (already in `templates/milestone/gem.md`):

```markdown
# Instagram Frozen

## `<flowstep_id>`

<prompt for that FlowStep>
```

Do not add a new `## Rule of success`. A legacy section is migration-only and
must normalize to the exact authored YAML `success` value.

Builder rules:

- Filename **is** `agent_id`. `references/contracts.md` as a living
  recipe is P0 unless a milestone gem links it as a named extra.
- Every `flowsteps[].id` has a `##` section. Missing section is P0.
- A giant `planning/*-generation.md` is an index at most, never the
  live prompt.
- `read_paths` may add extra files the gem names. It may not replace
  the gem.

This is how we stop fighting “put the reference inside the skill.”
The skill `references/` **is** the gem folder. It is just not a dump.

## What happens to `flow.yaml`

It becomes compile output, like a lockfile.

```text
agents/*.yaml + references/*.md
        │
        ▼
  m8m-harness-builder compile
        │
        ▼
<repo>/flowsteps/flows/<flow_id>/
  flow.yaml                 # generated flowstep_flow_v4
  references/<id>.md        # installed copy of the gems (hash-bound)
  milestones/<id>/...       # handlers, schemas
```

- Skill creator **does not edit** `flow.yaml`.
- Runtime may keep reading `flow.yaml` until `run_flow.py` can load
  `m8m_skill_canvas_v1` directly. That is an internal step, not a
  second philosophy.
- Editing generated `flow.yaml` by hand is P0 on the next audit
  (“authoring drifted; regenerate from agents/”).
- Tools remain authored in `<repo>/flowsteps/tools/<id>/`. They were
  never a Codex-skill natural file, so they stay in the repo.

Install copies gems into the flow folder so a run is hash-bound and
does not depend on a particular `~/.codex/skills` checkout. Authoring
still happens in the skill, where Codex writes files.

## What the builder becomes

Before (Builder 2): “split this skill into a flow.yaml we invented.”
Now (Builder 3): “make this Codex skill a valid M8M dialect, then compile.”

When `$m8m-harness-builder` is invoked on a skill, its own canonical five-node
workflow performs:

```text
1. Source audit freezes SKILL.md + declared agents/references/schemas/code
2. Toolbox construction proves every tool package built or BUILD_REQUIRED
3. Source generation emits two chosen ports:
     workflow_source_bundle (portable)
     staged_harness (run-local manifest/path report)
4. Harness validation runs the current fail-closed validator
5. Local installation copies only the validated staged-member allowlist
```

The portable source bundle contains canonical `flowstep_flow_v4`, root
workflow contracts, canonical resource descriptors, implementation/profile/
capability requirements, observer data, the pinned contract bundle, and a
closed advisory validation summary. It never embeds the run-local staged
report, chosen outputs, judge receipts, host paths, or cloud identities.

Audit P0 (the dialect gates):

1. `openai.yaml` missing `canvas.milestones`.
2. Listed milestone with no `agents/<id>.yaml` or no `references/<id>.md`.
3. Agent YAML whose `agent_id` is not a canvas milestone (except `openai`).
4. `read_paths` contains `references/**` or `references/*`.
5. Product recipe in `rules:` or in `SKILL.md` beyond invoke.
6. `allowed_steps` spanning more than this milestone’s FlowSteps.
7. Gem missing a listed FlowStep heading, or a legacy optional
   `## Rule of success` that differs from the milestone `success` authority.
8. Hand-edited `flow.yaml` that does not match compile.
9. Missing or open workflow-level schemas, invalid terminal binding, missing
   referenced resource, duplicate logical requirement, or unsafe path.
10. Generated stub, missing handler/tool/judge, secret-shaped content, or a
    preflight validator failure presented as `SOURCE_VALID`.
11. Every business output is optional, or a handler is a direct/aliased input
    passthrough.
12. An AI milestone omits its exact executor/profile, Gem/schema/tool/
    capability binding, token budget, or timeout; a judge loop omits its
    distinct judge requirement.
13. A strict judge omits `m8m_milestone_judge_v1`, a named worker, or a closed
    receipt schema; a persisted receipt is not
    `m8m.milestone_judge_receipt.v1` with the actual attempt.

Generate writes **agent YAML and gems first**, compile second.
`--write-skill-md` still writes a pointer SKILL, plus the canvas
`openai.yaml`. It does not emit `flowstep_agent_environment_v1`
personalities.

The builder dogfoods this dialect with `agents/openai.yaml` plus exactly five
deterministic `loop: none` milestone agents and five Gems. The old
`agents/audit_worker.yaml`, redundant Builder judge workers, and manual
pseudo-milestone launcher are retired. Every Builder milestone must produce a
normal structurally admitted chosen manifest through the canonical runtime.

## Isolated intelligence (unchanged harness, Codex-shaped)

`ACTION_REQUIRED` still launches a fresh no-history worker from
`m8m_context_capsule_v1`.

The capsule’s allowlist is taken from **that milestone agent’s**
`read_paths` plus the current run input, not from parent chat and not
from `references/**`.

This is the Codex-native reading of isolated context: each milestone
agent is already a worker definition. We stop using one persistent
worker for the whole skill.

## What this does not change

- Milestone = compulsory checkpoint. No chosen output → BLOCKED.
- FlowStep = guide inside the checkpoint. Prefer one tool.
- Tool = Python at `<repo>/flowsteps/tools/<id>/`.
- Intelligence may draft. It may not set `ok`, `branch`, or `cycle`.
- Cycle / branch / wait / judge semantics from 2.0.
- Session folder and `address.write_to`.
- Nisan ownership: `$nisan-case-io` remains the only platform writer.
  Instagram and Threads stay separate milestones.

## Migration

Builder first, then the smallest product skill as proof.

1. **Builder 3.1** — schema `m8m_milestone_agent_v1` +
   `m8m_skill_canvas_v1`; audit P0 above; compile to existing
   `flowstep_flow_v4` plus an exact contract-bundle digest and source-bundle
   proof; closed root workflow contracts; frozen source snapshot; five real
   FlowStep tool packages; two clean self-compiles with byte-identical source
   bundle and all five chosen outputs. Builder-2 runs are untrusted import
   inputs only. Existing fully specified v4 products use the explicit
   `import_flow_v4.py` inspect → digest-bound accept → stage → verify path;
   heuristic import is disabled and semantic differences remain blocked.
2. **Caption** — six small milestone agents, six gems, thin
   `openai.yaml`. Proof that invoke YAML ≠ writing gem.
3. **Case infographic** — split
   `agents/case_infographic_worker.yaml` into the canvas list already
   in that SKILL (`request_ready` … `published_live`). Delete fat
   `rules:` and `allowed_steps`.
4. **Article, highlight, restyle** — same split. Restyle SKILL.md
   recipe moves into `references/<milestone>.md`, not into agent
   `rules:`.

Do not format-lift `flow.yaml` in place as the authoring fix. That
is the 2.0 habit this upgrade retires.

## Success

A Codex skill creator, following `$m8m-harness-builder`, produces:

```text
agents/openai.yaml          canvas order
agents/<milestone>.yaml     checkpoint
references/<milestone>.md   gem
schemas/*.json              closed workflow and milestone contracts
```

and a deterministic source bundle whose logical bytes are independent of the
checkout root. The Builder itself reaches five PASS chosen outputs and installs
only after fail-closed validation. It does **not** produce a fat worker YAML,
claim a generated stub is runnable, contact authoring IO, or deploy. `flow.yaml`
exists only because the runtime compiled it.

That is M8M enhancing Codex, not conquering it.
