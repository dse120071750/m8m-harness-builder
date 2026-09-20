"""Declared project imports under the same isolated loader used by products."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest
import yaml

from support import SCRIPTS, SKILL_ROOT
from project_imports import ProjectImportError, import_context, module_map, validate_roots
from flowstep_runtime import FlowError, implementation_lock, load_flow, load_tool
import test_closed_implementation_imports as closed_fixtures
from test_skill_source import SkillFixture
from skill_source import compile_skill_source


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return path


def package(root, value='captured'):
    return [write(root / 'closed_demo/__init__.py', 'from .eager import Marker\n'),
        write(root / 'closed_demo/eager.py', 'class Marker: pass\n'),
        write(root / 'closed_demo/lazy.py', 'from .eager import Marker\nVALUE = ' + repr(value) + '\n')]


def test_relative_lazy_imports_reentrant_cache_and_cleanup(tmp_path):
    paths = package(tmp_path)
    write(tmp_path / 'closed_demo/uncaptured.py', "raise AssertionError('must never execute')\n")
    context = import_context(tmp_path, ['.'], paths)
    original = list(sys.path)
    with context.activate():
        parent = importlib.import_module('closed_demo')
        with import_context(tmp_path, ['.'], paths).activate():
            lazy = importlib.import_module('closed_demo.lazy')
            assert lazy.Marker is parent.Marker and lazy.VALUE == 'captured'
        assert sys.modules['closed_demo'] is parent
        with pytest.raises(ModuleNotFoundError, match='not captured'):
            importlib.import_module('closed_demo.uncaptured')
    assert not any(name.startswith('closed_demo') for name in sys.modules)
    assert sys.path == original
    with pytest.raises(RuntimeError, match='test failure'):
        with context.activate():
            assert importlib.import_module('closed_demo') is parent
            raise RuntimeError('test failure')
    assert 'closed_demo' not in sys.modules


def test_namespace_sibling_and_foreign_cache_reject(tmp_path, monkeypatch):
    paths = [write(tmp_path / 'closed_namespace/member.py', 'VALUE = 1\n')]
    write(tmp_path / 'closed_namespace/uncaptured.py', 'VALUE = 2\n')
    context = import_context(tmp_path, ['.'], paths)
    with context.activate():
        assert importlib.import_module('closed_namespace.member').VALUE == 1
        with pytest.raises(ModuleNotFoundError, match='not captured'):
            importlib.import_module('closed_namespace.uncaptured')
    foreign = ModuleType('closed_namespace')
    foreign.__path__ = [str(tmp_path / 'closed_namespace')]
    monkeypatch.setitem(sys.modules, 'closed_namespace', foreign)
    with pytest.raises(ProjectImportError, match='foreign cached'):
        with context.activate():
            pytest.fail('foreign namespace was reused')


@pytest.mark.parametrize('name', ['json', 'jsonschema'])
def test_stdlib_and_installed_vendor_shadowing_reject(tmp_path, name):
    path = write(tmp_path / (name + '.py'), "raise AssertionError('project shadow executed')\n")
    with pytest.raises(ProjectImportError, match='shadow'):
        with import_context(tmp_path, ['.'], [path]).activate():
            pytest.fail('shadowing was admitted')


def test_isolated_vendor_namespace_cannot_be_shadowed(tmp_path):
    vendor = tmp_path / 'vendor/distribution'
    write(vendor / 'vendor_namespace/member.py', 'VALUE = "vendor"\n')
    candidate = tmp_path / 'project'
    path = write(candidate / 'vendor_namespace/member.py', 'VALUE = "project"\n')
    code = '''import sys
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from project_imports import import_context,ProjectImportError
try:
    with import_context(Path(sys.argv[3]),['.'],[Path(sys.argv[4])]).activate():
        raise AssertionError('namespace collision admitted')
except ProjectImportError as error:
    assert 'shadow a trusted dependency' in str(error)
    print('NAMESPACE_PRIORITY_PASS')
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(SCRIPTS), str(vendor), str(candidate), str(path)],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'NAMESPACE_PRIORITY_PASS' in result.stdout


def test_vendor_beneath_project_still_has_priority(tmp_path, monkeypatch):
    vendor = tmp_path / 'flowsteps/flows/demo/runtime/releases/release/vendor/distribution'
    write(vendor / 'captured_vendor/__init__.py', 'VALUE = "vendor"\n')
    path = write(tmp_path / 'captured_vendor.py', 'VALUE = "project"\n')
    monkeypatch.syspath_prepend(str(vendor))
    assert 'captured_vendor' not in sys.modules
    with pytest.raises(ProjectImportError, match='trusted dependency'):
        with import_context(tmp_path, ['.'], [path]).activate():
            pytest.fail('in-project vendor collision admitted')


@pytest.mark.parametrize('roots', [[], ['..'], ['a/../b'], ['/tmp'], ['C:/data'], ['a\\b'], ['.','.'], ['a/'], [True]])
def test_invalid_ordered_roots_reject(roots):
    with pytest.raises(ProjectImportError):
        validate_roots(roots)


def test_ambiguity_uncovered_and_changed_codebase_context(tmp_path):
    a = write(tmp_path / 'one/duplicate.py', 'VALUE = 1\n')
    b = write(tmp_path / 'two/duplicate.py', 'VALUE = 2\n')
    with pytest.raises(ProjectImportError, match='ambiguous'):
        module_map(tmp_path, ['one', 'two'], [a, b])
    (tmp_path / 'empty').mkdir()
    with pytest.raises(ProjectImportError, match='no captured'):
        module_map(tmp_path, ['empty'], [a])
    paths = package(tmp_path / 'first', 'first')
    first = import_context(tmp_path / 'first', ['.'], paths)
    with first.activate():
        old = importlib.import_module('closed_demo.lazy')
    other_paths = package(tmp_path / 'second', 'second')
    second = import_context(tmp_path / 'second', ['.'], other_paths)
    with second.activate():
        assert importlib.import_module('closed_demo.lazy').VALUE == 'second'
        with pytest.raises(ProjectImportError, match='cannot nest different'):
            with first.activate():
                pytest.fail('different context nested')
    paths[-1].write_text("VALUE = 'adopted'\n", encoding='utf-8')
    with pytest.raises(ProjectImportError, match='bytes changed'):
        with first.activate():
            pytest.fail('old context reused changed bytes')
    adopted = import_context(tmp_path / 'first', ['.'], paths)
    assert adopted.key != first.key
    with adopted.activate():
        current = importlib.import_module('closed_demo.lazy')
        assert current is not old and current.VALUE == 'adopted'


def test_compiler_preserves_root_order_and_fingerprint_changes(tmp_path):
    fixture = SkillFixture(tmp_path / 'source')
    path = fixture.root / 'agents/openai.yaml'
    canvas = yaml.safe_load(path.read_text())
    canvas['canvas']['codebase_import_roots'] = ['.', 'domain']
    path.write_text(yaml.safe_dump(canvas), encoding='utf-8')
    assert compile_skill_source(fixture.root)['codebase_import_roots'] == ['.', 'domain']
    harness, project = closed_fixtures.ClosedImplementationImportTests()._harness(tmp_path / 'flow',
        'def run(input_data, **_):\n    from shared import ready\n    return {"outputs":{"result":{"value":ready.VALUE}}}\n',
        dependencies=['shared/ready.py', 'extra/another.py'])
    write(project / 'extra/another.py', 'VALUE = 2\n')
    path = harness / 'flow.yaml'
    raw = yaml.safe_load(path.read_text())
    raw['codebase_import_roots'] = ['.', 'extra']
    path.write_text(yaml.safe_dump(raw), encoding='utf-8')
    first = implementation_lock(harness, load_flow(harness))
    raw['codebase_import_roots'].reverse()
    path.write_text(yaml.safe_dump(raw), encoding='utf-8')
    second = implementation_lock(harness, load_flow(harness))
    assert first != second


def test_actual_isolated_load_tool_keeps_scope_for_lazy_execution(tmp_path):
    harness, project = closed_fixtures.ClosedImplementationImportTests()._harness(tmp_path,
        'def run(input_data, **_):\n    from shared import ready\n    return {"outputs":{"result":{"value":ready.VALUE}}}\n',
        dependencies=['shared/ready.py'])
    path = harness / 'flow.yaml'
    raw = yaml.safe_load(path.read_text())
    raw['codebase_import_roots'] = ['.']
    path.write_text(yaml.safe_dump(raw), encoding='utf-8')
    code = '''import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from flowstep_runtime import load_flow,load_tool
harness=Path(sys.argv[2]); flow=load_flow(harness)
tool=load_tool(harness,flow['steps'][0],flow=flow)
assert 'shared' not in sys.modules
assert tool.run({})['outputs']['result']['value']=='closed'
assert 'shared' not in sys.modules
print('ISOLATED_LAZY_PASS')
'''
    completed = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(SCRIPTS), str(harness)],
        cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert 'ISOLATED_LAZY_PASS' in completed.stdout


def test_product_cannot_impersonate_runtime_loader_by_filename(tmp_path):
    harness, project = closed_fixtures.ClosedImplementationImportTests()._harness(tmp_path,
        'def run(input_data, **_):\n    return {"outputs":{"result":{"value":"ready"}}}\n',
        dependencies=['shared/project_imports.py'])
    write(project / 'shared/project_imports.py',
        'import importlib.util\ndef load(path):\n    return importlib.util.spec_from_file_location("untrusted", path)\n')
    with pytest.raises(FlowError, match='dynamic execution path.*not statically provable'):
        implementation_lock(harness, load_flow(harness))


def test_generated_isolated_launcher_executes_captured_lazy_import(tmp_path):
    from runtime_release import stage_runtime_release
    harness, project = closed_fixtures.ClosedImplementationImportTests()._harness(tmp_path / 'codebase',
        'def run(input_data, **_):\n    from shared import ready\n    return {"outputs":{"result":{"value":ready.VALUE}}}\n',
        dependencies=['shared/ready.py'])
    path = harness / 'flow.yaml'
    raw = yaml.safe_load(path.read_text())
    raw['codebase_import_roots'] = ['.']
    path.write_text(yaml.safe_dump(raw), encoding='utf-8')
    release = stage_runtime_release(builder_root=SKILL_ROOT, harness=harness,
        product_roots=[project / '.agents/skills/example'], flow_id='import_demo_v1', codebase=project)
    execution = tmp_path / 'execution'
    execution.mkdir()
    request = write(execution / 'input.json', '{}\n')
    run = execution / 'runs/import_probe'
    completed = subprocess.run([sys.executable, '-B', str(harness / 'launch.py'), '--request', str(request),
        '--harness-root', str(execution), '--run-dir', str(run)], cwd=execution,
        capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)['state'] == 'COMPLETE'
    lock = json.loads((run / 'm8m-runtime-lock.json').read_text())
    assert lock['runtime_id'] == release['runtime_id']
    chosen = json.loads((run / 'milestones/ready/out/members/result/asset.json').read_text())
    assert chosen == 'closed'
