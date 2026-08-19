<!-- flowstep_instruction_v1 -->
# FlowStep instruction: text_pipeline_v1

This file is the skill instruction. Each section is a milestone.
Use only the listed toolbox functions inside a milestone.
Mark a milestone DONE when its output schema PASSes.

- harness: `C:\Users\gasil\.codex\skills\flowstep-harness-builder\examples\text_pipeline`
- flow_id: `text_pipeline_v1`
- final_payload: `label_v1` from `label`
- updated_at: 2026-08-19T14:39:12Z

## Run

```powershell
python <builder>/scripts/run_flow.py --codebase <repo> --flow-id text_pipeline_v1 --run-dir <run-dir> --request <request.json>
```

If a milestone returns ACTION_REQUIRED, write only the frozen draft and advance.

## Tool vs intelligence

Schema: `tool_vs_intelligence_table_v1`.

| id | class | test | why |
| --- | --- | --- | --- |
| `ingest` | `tool` | same input → same action; fixture-testable; receipt not opinion; junior can implement from schema | steps/ingest/tool.py |
| `segment` | `tool` | same input → same action; fixture-testable; receipt not opinion; junior can implement from schema | steps/segment/tool.py |
| `label` | `intelligence` | fails at least one of the four tests; no fixture without a model | semantic class is not derivable from punctuation alone |

## Step index


| # | Step | Class | Handler | Model | Why model | Inputs | Output contract | Output schema |
| ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `ingest` | `tool` | `steps/ingest/tool.py` | `none` | none | request=user.request | `ingest_v1` | `steps/ingest/output.schema.json` |
| 2 | `segment` | `tool` | `steps/segment/tool.py` | `none` | none | ingest=ingest.ingest_v1 | `segment_v1` | `steps/segment/output.schema.json` |
| 3 | `label` | `intelligence` | `steps/label/tool.py` | `completion` | semantic class is not derivable from punctuation alone | segment=segment.segment_v1 | `label_v1` | `steps/label/output.schema.json` |

This table is generated from the flow YAML. The Python tool and schemas are the runtime.

## Steps

### `ingest`
- status: DONE
- order: 1
- class: `tool`
- intelligence: `none`
- assemble: `steps/ingest/tool.py`
- toolbox: none
- test: `steps/ingest/tests/test_tool.py`
- model: `none`
- model_justification: none
- inputs: request=user.request
- input_schema: `steps/ingest/input.schema.json`
- output_schema: `steps/ingest/output.schema.json`
- output_contract: `ingest_v1`
- expected_return: `{"text": "string", "char_count": "integer"}`

### `segment`
- status: DONE
- order: 2
- class: `tool`
- intelligence: `none`
- assemble: `steps/segment/tool.py`
- toolbox: none
- test: `steps/segment/tests/test_tool.py`
- model: `none`
- model_justification: none
- inputs: ingest=ingest.ingest_v1
- input_schema: `steps/segment/input.schema.json`
- output_schema: `steps/segment/output.schema.json`
- output_contract: `segment_v1`
- expected_return: `{"sentences": "array", "sentence_count": "integer"}`

### `label`
- status: DONE
- order: 3
- class: `intelligence`
- intelligence: `completion`
- assemble: `steps/label/tool.py`
- toolbox: none
- test: `steps/label/tests/test_tool.py`
- model: `completion`
- model_justification: semantic class is not derivable from punctuation alone
- inputs: segment=segment.segment_v1
- input_schema: `steps/label/input.schema.json`
- output_schema: `steps/label/output.schema.json`
- output_contract: `label_v1`
- expected_return: `{"label": "question | statement | other", "sentence": "string"}`
- draft_schema: `steps/label/draft.schema.json`

After a step's tool, schemas, and test are real, mark it DONE.
Do not start the next step while the current step is PENDING.
