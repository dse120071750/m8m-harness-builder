<!-- flowstep_instruction_v1 -->
# FlowStep instruction: text_pipeline_v1

This file is the skill instruction. Each section is a milestone.
A milestone input schema is the previous milestone output schema.
Each milestone declares named output ports: FlowSteps refine candidates, structural admission always runs, and an authored semantic judge is optional.
Mark DONE only after admission (and judge PASS when declared) materializes the chosen output bundle in chosen-output.json.
FlowSteps inside a milestone are a guide: prefer one tool each, in table order.
The tool is optional. If it fails, recover like a normal agent. Do not skip required named outputs.

- harness: `C:\Users\gasil\.codex\skills\m8m-harness-builder\examples\text_pipeline`
- flow_id: `text_pipeline_v1`
- final_payload: `label_v1` from `label`
- updated_at: 2026-09-20T14:04:28Z

## Run

```powershell
python <installed-skill>/scripts/m8m_run.py --harness-root C:\NisanRuntime --request <request.json>
```

The source repo is code/specification authority only. New mutable run state, goals, retries, checkpoints, milestone media, and candidate cache live under the host-local harness root.
Request file_ref_v2 assets are hash-verified and copied once into inputs/source-assets; milestones receive only the rewritten run-local paths.
File and media candidate paths must already resolve inside the active run; chosen-output commit copies them directly into the chosen bundle.
Resume an exact initialized legacy run only by its exact --run-dir; never discover or migrate it from the source repo.
Generic M8M does not export final packages. Any final archive copy remains the product runtime's authority.

If a milestone returns ACTION_REQUIRED, write only the frozen draft and advance.

## Tool vs intelligence

Schema: `tool_vs_intelligence_table_v1`.

| id | class | test | why |
| --- | --- | --- | --- |
| `ingest` | `tool` | same input → same action; fixture-testable; receipt not opinion; junior can implement from schema | steps/ingest/tool.py |
| `segment` | `tool` | same input → same action; fixture-testable; receipt not opinion; junior can implement from schema | steps/segment/tool.py |
| `label` | `intelligence` | fails at least one of the four tests; no fixture without a model | semantic class is not derivable from punctuation alone |

## Milestones

The M8M flowchart is `planning/m8m-flowchart.md` plus `planning/m8m-flowchart.jpg`.
The JPEG is rewritten on generate and on every step edit. It is the portable audit copy.

## Toolbox


## Teaching contracts

Same rule as tools. These live on the flow, not in `~/.codex/skills` or `~/.claude/skills`.

- `references/ingest.md`
- `references/label.md`
- `references/segment.md`

## Milestone index


| # | Step | Class | Handler | Model | Why model | Inputs | Output contract | Output schema |
| ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `ingest` | `tool` | `steps/ingest/tool.py` | `none` | none | request=user.request | `ingest_v1` | `steps/ingest/output.schema.json` |
| 2 | `segment` | `tool` | `steps/segment/tool.py` | `none` | none | ingest={'from': 'ingest.ingest_v1', 'output': 'result'} | `segment_v1` | `steps/segment/output.schema.json` |
| 3 | `label` | `intelligence` | `steps/label/tool.py` | `completion` | semantic class is not derivable from punctuation alone | segment={'from': 'segment.segment_v1', 'output': 'result'} | `label_v1` | `steps/label/output.schema.json` |

This table is generated from the flow YAML. The Python tool and schemas are the runtime.

## Steps

### `ingest`
- status: DONE
- order: 1
- class: `tool`
- intelligence: `none`
- assemble: `steps/ingest/tool.py`
- toolbox: none
- flowsteps (guide): none
- test: `steps/ingest/tests/test_tool.py`
- model: `none`
- model_justification: none
- inputs: request=user.request
- input_schema: `steps/ingest/input.schema.json`
- output_schema: `steps/ingest/output.schema.json`
- output_contract: `ingest_v1`
- expected_return: `{"outputs": "object"}`

### `segment`
- status: DONE
- order: 2
- class: `tool`
- intelligence: `none`
- assemble: `steps/segment/tool.py`
- toolbox: none
- flowsteps (guide): none
- test: `steps/segment/tests/test_tool.py`
- model: `none`
- model_justification: none
- inputs: ingest={'from': 'ingest.ingest_v1', 'output': 'result'}
- input_schema: `steps/segment/input.schema.json`
- output_schema: `steps/segment/output.schema.json`
- output_contract: `segment_v1`
- expected_return: `{"outputs": "object"}`

### `label`
- status: DONE
- order: 3
- class: `intelligence`
- intelligence: `completion`
- assemble: `steps/label/tool.py`
- toolbox: none
- flowsteps (guide): none
- test: `steps/label/tests/test_tool.py`
- model: `completion`
- model_justification: semantic class is not derivable from punctuation alone
- inputs: segment={'from': 'segment.segment_v1', 'output': 'result'}
- input_schema: `steps/label/input.schema.json`
- output_schema: `steps/label/output.schema.json`
- output_contract: `label_v1`
- expected_return: `{"outputs": "object"}`
- draft_schema: `steps/label/draft.schema.json`

After a step's tool, schemas, and test are real, mark it DONE.
Do not start the next step while the current step is PENDING.
