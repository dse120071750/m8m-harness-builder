# Tool vs intelligence

A **tool** is a pre-made function in `flowsteps/tools/`. It is not a
FlowStep. A **FlowStep is a milestone** (see `milestone.md`). Intelligence
is how a milestone may be reached, not a third kind of canvas node.

Classify every **tool** and every **milestone’s intelligence** before
generate. The class is not a label for the YAML. It decides whether the
work is a **repo function** or a **model call that must still use those
functions**.

This split is the one used by the highest-star agent/workflow systems:

| Source | Stars (order) | Their split |
| --- | --- | --- |
| [n8n](https://github.com/n8n-io/n8n) | ~201k | Workflow **nodes** (HTTP, DB, transform) vs an **AI Agent node** that *calls* those nodes |
| [LangChain / LangGraph](https://github.com/langchain-ai/langchain) | ~120k+ | Typed `@tool` functions / deterministic **graph nodes** vs an **agent node** that chooses tools |
| [CrewAI](https://github.com/crewAIInc/crewAI) | ~57k | “Keep application logic in regular Python”; wrap agents in deterministic workflows |
| [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) | ~19–27k | **Function tools** (Pydantic-validated Python) vs **agents-as-tools** (intelligence callable without handing off the flow) |
| [Anthropic, Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) | — | **Workflow** = predefined code path; **agent** = model directs the path. Tools are an agent-computer interface (ACI), designed like HCI |

They agree on three rules we adopt:

1. Prefer a **workflow** (our driver) over an open agent loop.
2. A **tool** is a prewritten, schema-bound function. The model does not
   invent it at runtime.
3. **Intelligence** is still wrapped as a tool so I/O stays typed
   (OpenAI “agents as tools”; Anthropic evaluator-optimizer).

Read this file once per new flow. Do not restate the catalog in `SKILL.md`.

## The test (use this, not vibes)

A step is a **tool** if **all** of these are true:

1. **Same input → same action.** Given the same typed payload, it performs
   the same query, crop, hash, or write (allowing clock/IDs in receipts).
2. **Unit-testable with a fixture.** You can test it with a file, a mock
   row, or a PNG. No model key is required.
3. **The next step needs a receipt, not an opinion.** IDs, hashes, pixel
   size, row counts, file refs.
4. **A junior engineer could implement it from the schema alone.**

If any of 1–4 fail, it is **intelligence**. It still gets
`steps/<id>/tool.py`. That tool validates I/O and may return `NEED_MODEL`.
It must not `return draft`.

A third thing is **not a step class**: **orchestration**. The driver already
owns order. Intelligence must not pick the next FlowStep.

## What a tool is

A tool is a **function with a contract** (LangChain `@tool`, OpenAI
`FunctionTool`, n8n node, MCP tool):

- lives in `<codebase>/flowsteps/<flow_id>/steps/<id>/tool.py`
- `class: tool`, `model: none`
- input schema in, output schema out
- side effects allowed (DB, HTTP, disk) but **named and fail-closed**
- documented like a junior-dev API (Anthropic ACI / poka-yoke: obvious
  parameters, absolute paths, hard-to-misuse enums)

The agent **calls** the tool. The agent does **not** write SQL, crop math,
or a Playwright script in the session.

### Tool catalog (expand here, not in chat)

| Family | Examples | Why it is a tool |
| --- | --- | --- |
| Fetch / IO | MCP `get_case`, SQL by primary key, HTTP GET of a known URL, read a UTF-8 file | Structured read; same id → same record |
| Transform | crop 4:3→4:5, letterbox, resize to 1080×1350, EXIF strip | Pixel math; fixture PNG in, PNG+hash out |
| Bind | sha256, `file_ref_v2`, locale normalize, whitespace collapse | Pure function |
| Render | HTML shell → Chromium screenshot, DXF emit, contact sheet | Generator + fixed viewport |
| Validate | JSON Schema, language/mojibake gate, dimension check | Pass/fail from rules |
| Package | zip pages, write manifest, copy captions from an approved plan | Assembly of already-typed inputs |
| Write | exact-id patch, upload with a frozen path, Firestore write behind an IO skill | Side effect with a receipt |
| Extract (library) | regex, date parse, Playwright a11y dump, OCR **if** it is a pinned engine | Deterministic enough to fixture |

Studio-shaped examples that **must** be tools:

- fetch CaseRecordV2 / style bundle by key
- crop a 4:3 source to native 4:5
- hash-bind a generated PNG
- render seven 1080×1350 cards from plan + images
- materialize Instagram/Threads copy that already exists on the plan
- quote line-item math from a ratebook

## What intelligence is

Intelligence is **judgment or invention** the schema cannot compute:

- choose which lesson / page / crop *meaning* to keep
- draft wording, prompts, or a plan
- judge PASS/FAIL from evidence when the rule is not a pixel measurement
- decide among plausible alternatives

`class: intelligence` plus `model: completion|image|judge` and a real
`model_justification`. The repo tool still owns the contract. The model
only fills a **draft** that the tool admits.

OpenAI’s name for this wrap is **agents as tools**: intelligence is
callable, but it does not take over the workflow. Anthropic’s
evaluator-optimizer is the same idea: generate, then a separate typed
gate.

Studio-shaped examples that **are** intelligence (still wrapped):

- pick three educational highlights from a candidate pool
- write zh-Hant-HK caption wording
- compile a scene prompt that invents camera/wardrobe
- release-judge whether a card *teaches* (not whether footer pixels exist)

Footer geometry, token presence, and 920×978 size are **tools**, even
when a judge later reads them.

## Borderline cases

| Work | Class | Why |
| --- | --- | --- |
| OCR / speech-to-text via a pinned engine | tool | Engine is the implementation; fixture the transcript |
| “Is this Traditional Chinese?” via character set + denylist | tool | Rule |
| “Is this good Hong Kong copy?” | intelligence | Taste |
| Prompt = fill a frozen template with plan fields | tool | Substitution |
| Prompt = invent composition and lighting | intelligence | Invention |
| Route “url vs file vs text” by request keys | tool | Branch on data |
| Route “which worker should handle this customer?” | intelligence if the rule is fuzzy; tool if it is a table |
| Image **generate** | intelligence (`model: image`) | Model produces bytes; the tool must still hash and size-check |
| Image **crop / composite** | tool | No model |

When unsure, ship a **tool** first. Anthropic: start with the simplest
path; add a model only when a fixture test cannot express the work.

## Forbidden (all of the above systems reject this)

- Generating product tools under `.codex/skills`
- Marking fetch, crop, hash, render, package, or schema-validate as
  `intelligence`
- The agent writing a one-off SQL/crop/Playwright script “just for this
  run” (that is inventing a tool at runtime)
- `class: tool` with `model != none`
- Intelligence that `return draft`s without validation
- Letting intelligence choose the next FlowStep (that is an open agent;
  we run a workflow)

## How the highest-star systems strengthen us

Apply these as engineering gates, not extra classes:

- **n8n:** integrations are nodes. The AI node only *selects* nodes. Our
  `class: tool` is a node. Our driver is the canvas.
- **LangGraph:** deterministic nodes and LLM nodes share one state
  schema. Our output schema is that state.
- **OpenAI Agents SDK:** every tool has a Pydantic schema; wrap
  intelligence as a tool, do not hand off the whole flow.
- **CrewAI:** business logic stays in Python. Do not put ratebook math
  or crop math in a prompt.
- **Anthropic ACI:** spend time on tool signatures (absolute paths,
  enums, examples). A bad tool format is why models “write code on the
  fly.”

## Classification table (required before generate)

Fixed schema: `contracts/tool_vs_intelligence_table_v1.schema.json`.
Columns are `id`, `class`, `test`, `why`. Audit and generate must emit
this object. See the doctrine rows in the GitHub README.

If a step name contains `fetch`, `crop`, `hash`, `render`, `package`,
`resize`, `normalize`, `query`, `upload`, or `parse`, it is a tool unless
the engineer writes a justification that beats the four tests. The
harness rejects the obvious mismatches.
