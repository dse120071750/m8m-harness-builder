"""A tool-side model continuation must not collide with the next cycle retry."""
import json

import yaml

import support  # noqa: F401
import run_flow
from test_cycle import _scaffold, _request, _write


def test_model_continuation_then_cycle_retry_uses_unused_attempt(tmp_path):
    _, harness = _scaffold(str(tmp_path), fail_first=True)
    path = harness / 'flow.yaml'
    flow = yaml.safe_load(path.read_text(encoding='utf-8'))
    step = next(s for s in flow['milestones'] if s['id'] == 'page_bound')
    step.update(intelligence='completion', on_tool_fail='need_model', max_model_attempts=4,
                draft_schema='milestones/page_bound/draft.schema.json')
    path.write_text(yaml.safe_dump(flow, sort_keys=False), encoding='utf-8')
    _write(harness / step['draft_schema'], json.dumps({
        'type': 'object', 'additionalProperties': False,
        'required': ['phase'], 'properties': {'phase': {'type': 'string'}}}))
    _write(harness / 'milestones/page_bound/assemble.py',
           "from pathlib import Path\n"
           "def run(input_data, draft=None, run_dir=None, **_):\n"
           "    marker = Path(run_dir) / 'business-phase.txt'\n"
           "    if draft and draft['phase'] == 'bad-raster': marker.write_text('replacement')\n"
           "    elif draft: return {'outputs': {'result': {'page': 'p-' + input_data['row']}}}\n"
           "    phase = marker.read_text() if marker.exists() else 'initial'\n"
           "    return {'_flowstep': 'NEED_MODEL', 'model': 'completion',\n"
           "            'model_request': {'instruction': 'render ' + phase}}\n")
    run = tmp_path / 'run'
    first = run_flow.advance(harness, run, request_path=_request(run, [{'id': '001'}]))
    assert first['state'] == 'ACTION_REQUIRED', first
    draft = run / first['draft_path']
    _write(draft, json.dumps({'phase': 'bad-raster'}))
    second = run_flow.advance(harness, run, draft_path=draft)
    assert second['attempt'] == 2, second
    frozen_paths = [run / second[k] for k in ('task_path', 'context_capsule_path')]
    frozen = [p.read_bytes() for p in frozen_paths]
    _write(draft, json.dumps({'phase': 'valid-raster'}))
    third = run_flow.advance(harness, run, draft_path=draft)
    assert third['state'] == 'ACTION_REQUIRED', third
    assert third['attempt'] == 3, third
    assert third['step_id'] == 'page_bound'
    assert [p.read_bytes() for p in frozen_paths] == frozen
    resumed = run_flow.advance(harness, run)
    assert resumed['attempt'] == 3
    assert resumed['context_capsule_path'] == third['context_capsule_path']
    _write(run / third['draft_path'], json.dumps({'phase': 'valid-raster'}))
    final = run_flow.advance(harness, run, draft_path=run / third['draft_path'])
    assert final['state'] == 'COMPLETE', final
