"""Two independent nine-row loops must not inherit each other's cursor/done flag."""
import copy
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml
import support  # noqa: F401
import run_flow
from test_cycle import _scaffold, _request
from session_layout import load_ledger


def scaffold_two_cycles(temp):
    codebase, harness = _scaffold(temp)
    path = harness / 'flow.yaml'
    flow = yaml.safe_load(path.read_text(encoding='utf-8'))
    original = flow['milestones']
    names = {'pages_ledger_frozen': 'scenes_ledger_frozen',
             'page_bound': 'scene_bound', 'page_rendered': 'scene_rendered'}
    scenes = copy.deepcopy(original[:3])
    for node in scenes:
        old = node['id']
        new = names[old]
        encoded = json.dumps(node)
        for source, target in names.items():
            encoded = encoded.replace(source, target)
        node.clear()
        node.update(json.loads(encoded))
        if 'on_cycle' in node:
            node['on_cycle'] = 'scenes'
        if 'cycle' in node:
            node['cycle']['id'] = 'scenes'
        shutil.copytree(harness / 'milestones' / old, harness / 'milestones' / new)
        # Cloned milestones also own schemas and master prompts outside their dirs.
        for key in ('output_schema', 'gem'):
            old_ref = next(x for x in original if x['id'] == old)[key]
            new_ref = node[key]
            if old_ref != new_ref and not (harness / new_ref).exists():
                (harness / new_ref).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(harness / old_ref, harness / new_ref)
                if key == 'gem':
                    prompt = (harness / new_ref).read_text(encoding='utf-8')
                    for source, target in names.items():
                        prompt = prompt.replace(source, target)
                    (harness / new_ref).write_text(prompt, encoding='utf-8')
    original[2]['cycle']['join'] = 'scenes_ledger_frozen'
    flow['milestones'] = original[:3] + scenes + original[3:]
    for node in flow['milestones']:
        if 'cycle' in node:
            node['cycle']['max_rounds'] = 20
        if node['id'].endswith('ledger_frozen'):
            schema_path = harness / node['output_schema']
            schema = json.loads(schema_path.read_text(encoding='utf-8'))
            def expand(value):
                if isinstance(value, dict):
                    if 'maxItems' in value:
                        value['maxItems'] = 20
                    for child in value.values():
                        expand(child)
                elif isinstance(value, list):
                    for child in value:
                        expand(child)
            expand(schema)
            schema_path.write_text(json.dumps(schema), encoding='utf-8')
    path.write_text(yaml.safe_dump(flow, sort_keys=False), encoding='utf-8')
    return codebase, harness


def test_two_nine_row_cycles_complete_and_resume_without_repeating():
    with tempfile.TemporaryDirectory() as temp:
        codebase, harness = scaffold_two_cycles(temp)
        run = codebase / 'runs' / 'two-cycles'
        request = _request(run, [{'id': f'{i:03}'} for i in range(1, 10)])
        result = run_flow.advance(harness, run, request_path=request)
        assert result['state'] == 'COMPLETE', result
        assert all(row['status'] == 'done' for cid in ('pages', 'scenes')
                   for row in load_ledger(run, cid)['rows'])
        counts = [harness / 'milestones' / mid / '_handler_calls.txt'
                  for mid in ('page_bound', 'scene_bound')]
        assert [p.read_text() for p in counts] == ['9', '9']
        assert run_flow.advance(harness, run)['state'] == 'COMPLETE'
        assert [p.read_text() for p in counts] == ['9', '9']


def test_second_cycle_interruption_keeps_first_cycle_and_restarts_at_first_row():
    with tempfile.TemporaryDirectory() as temp:
        codebase, harness = scaffold_two_cycles(temp)
        run = codebase / 'runs' / 'interrupted'
        real = run_flow._execute_step
        def interrupt(skill, run_dir, flow, step, fingerprint, draft):
            if step['id'] == 'scene_bound':
                raise KeyboardInterrupt('between cycles')
            return real(skill, run_dir, flow, step, fingerprint, draft)
        with patch.object(run_flow, '_execute_step', side_effect=interrupt):
            try:
                run_flow.advance(harness, run, request_path=_request(run))
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError('second cycle was incorrectly skipped')
        previous = (run / 'cycles/pages/ledger.json').read_bytes()
        result = run_flow.advance(harness, run)
        assert result['state'] == 'COMPLETE', result
        assert (run / 'cycles/pages/ledger.json').read_bytes() == previous
        assert (harness / 'milestones/scene_bound/_handler_calls.txt').read_text() == '2'
        assert [r['id'] for r in load_ledger(run, 'scenes')['rows']] == ['001', '002']


def test_last_row_retry_does_not_spend_noncycle_join_attempt_budget():
    with tempfile.TemporaryDirectory() as temp:
        codebase, harness = _scaffold(temp, fail_first=True)
        flow_path = harness / 'flow.yaml'
        flow = yaml.safe_load(flow_path.read_text(encoding='utf-8'))
        join = next(node for node in flow['milestones'] if node['id'] == 'release_packaged')
        join['max_attempts'] = 1
        flow_path.write_text(yaml.safe_dump(flow, sort_keys=False), encoding='utf-8')
        run = codebase / 'runs' / 'retried-final-row'
        result = run_flow.advance(harness, run, request_path=_request(run, [{'id': '001'}]))
        assert result['state'] == 'COMPLETE', result
        inputs = json.loads((run / 'work/release_packaged/input.json').read_text())
        assert 'row' not in inputs and 'ledger_row' not in inputs
        receipt = json.loads((run / 'milestones/release_packaged/out/judge-receipt.json').read_text())
        assert receipt['attempt'] == 1
        frozen = (run / 'cycles/pages/ledger.json').read_bytes()
        assert run_flow.advance(harness, run)['state'] == 'COMPLETE'
        assert (run / 'cycles/pages/ledger.json').read_bytes() == frozen
