import json
from pathlib import Path
import yaml
import pytest

import support
from run_flow import advance
from test_run_isolation import _scaffold, _request, _write_json


def _two_phase_scaffold(tmp_path, limit=3):
    _, harness = _scaffold(tmp_path, model=True)
    flow_path = harness / 'flow.yaml'
    flow = yaml.safe_load(flow_path.read_text(encoding='utf-8'))
    if limit is not None:
        flow['milestones'][0]['max_model_attempts'] = limit
    flow_path.write_text(yaml.safe_dump(flow), encoding='utf-8')
    for name in ('a', 'b'):
        (harness / f'{name}.md').write_text(name, encoding='utf-8')
    (harness / 'source.py').write_text(
        'from pathlib import Path\n'
        'def run(input_data, draft=None, run_dir=None, **_):\n'
        "    phase = Path(run_dir) / 'business-phase.txt'\n"
        "    if draft and draft.get('value') == 'first': phase.write_text('second')\n"
        "    if draft and draft.get('value') == 'invalid': raise ValueError('invalid phase evidence')\n"
        "    if draft and draft.get('value') == 'second': return {'outputs': {'result': draft}}\n"
        "    name = 'b' if phase.exists() else 'a'\n"
        "    return {'_flowstep': 'NEED_MODEL', 'model': 'completion', 'model_request': "
        "{'instruction': 'phase ' + name, 'references': [str(Path(__file__).with_name(name + '.md'))]}}\n",
        encoding='utf-8',
    )
    return harness


@pytest.mark.parametrize('limit', [3, None])
def test_successive_model_phases_freeze_distinct_capsules(tmp_path, limit):
    harness = _two_phase_scaffold(tmp_path, limit=limit)
    run = tmp_path / 'run'
    first = advance(harness, run, request_path=_request(tmp_path / 'request.json'))
    paths = [run / first[k] for k in ('context_capsule_path', 'model_request_path')]
    frozen = [p.read_bytes() for p in paths]
    draft = run / first['draft_path']
    _write_json(draft, {'value': 'first'})
    second = advance(harness, run, draft_path=draft)
    assert second['state'] == 'ACTION_REQUIRED', second
    assert second['attempt'] == 2
    assert second['context_capsule_path'] != first['context_capsule_path']
    task = json.loads((run / second['task_path']).read_text())
    assert task['max_attempts'] == (limit or 8)
    assert [p.read_bytes() for p in paths] == frozen
    request = json.loads((run / second['model_request_path']).read_text())
    assert request['references'] == [str(harness / 'b.md')]
    second_paths = [run / second[k] for k in ('context_capsule_path', 'model_request_path')]
    second_frozen = [p.read_bytes() for p in second_paths]
    resumed = advance(harness, run)
    assert resumed['attempt'] == 2
    assert resumed['context_capsule_path'] == second['context_capsule_path']
    _write_json(draft, {'value': 'invalid'})
    rejected = advance(harness, run, draft_path=draft)
    assert rejected['state'] == 'ACTION_REQUIRED'
    assert rejected['attempt'] == 2
    assert [p.read_bytes() for p in second_paths] == second_frozen
    _write_json(draft, {'value': 'second'})
    assert advance(harness, run, draft_path=draft)['state'] == 'COMPLETE'


def test_model_phase_budget_preserves_previous_capsule(tmp_path):
    harness = _two_phase_scaffold(tmp_path, limit=1)
    run = tmp_path / 'run'
    first = advance(harness, run, request_path=_request(tmp_path / 'request.json'))
    paths = [run / first[k] for k in ('context_capsule_path', 'model_request_path')]
    frozen = [p.read_bytes() for p in paths]
    draft = run / first['draft_path']
    _write_json(draft, {'value': 'first'})
    result = advance(harness, run, draft_path=draft)
    assert result['state'] == 'BLOCKED', result
    assert 'continuation budget 1 exhausted' in str(result)
    assert [p.read_bytes() for p in paths] == frozen


def test_product_validation_failure_preserves_model_request_and_capsule(tmp_path):
    _, harness = _scaffold(tmp_path, model=True)
    evidence = harness / 'evidence.md'
    evidence.write_text('bounded evidence', encoding='utf-8')
    (harness / 'source.py').write_text(
        'def run(input_data, draft=None, **_):\n'
        '    if not draft:\n'
        "        return {'_flowstep': 'NEED_MODEL', 'model': 'completion', "
        "'model_request': {'instruction': 'use evidence', 'references': ["
        + repr(str(evidence)) + "]}}\n"
        "    if draft.get('value') != 'valid':\n"
        "        raise ValueError('product evidence reference is invalid')\n"
        "    return {'outputs': {'result': draft}}\n",
        encoding='utf-8',
    )
    run = tmp_path / 'run'
    first = advance(harness, run, request_path=_request(tmp_path / 'request.json'))
    capsule = run / first['context_capsule_path']
    model_request = run / first['model_request_path']
    frozen = (capsule.read_bytes(), model_request.read_bytes())
    draft = run / first['draft_path']
    _write_json(draft, {'value': 'invalid'})
    second = advance(harness, run, draft_path=draft)
    assert second['state'] == 'ACTION_REQUIRED'
    assert 'product evidence reference is invalid' in ' '.join(second['blockers'])
    assert (capsule.read_bytes(), model_request.read_bytes()) == frozen
    _write_json(draft, {'value': 'valid'})
    assert advance(harness, run, draft_path=draft)['state'] == 'COMPLETE'
