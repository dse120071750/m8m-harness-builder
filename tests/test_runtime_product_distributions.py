"""Exact product distribution closure, including real isolated binary imports."""
from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from support import SKILL_ROOT
import runtime_release as release
from flowstep_runtime import implementation_lock, load_flow
from skill_source import compile_skill_source, SkillSourceError
from test_skill_source import SkillFixture
import test_closed_implementation_imports as closed_fixtures

PINS = [{"name": "Pillow", "version": "12.1.0"}, {"name": "requests", "version": "2.32.4"}]


@pytest.mark.parametrize('value', [[], [{"name": "Pillow", "version": ">=12"}],
    [{"name": "Pillow[docs]", "version": "12.1.0"}],
    [{"name": "Pillow", "version": "12.1.0", "url": "https://example.invalid"}],
    [{"name": "some_package", "version": "1"}, {"name": "Some-Package", "version": "2"}]])
def test_closed_declaration_rejects_unpinned_or_duplicate(value):
    with pytest.raises(release.RuntimeReleaseError):
        release.validate_product_distributions(value)


def test_missing_and_wrong_exact_distribution_fail():
    with pytest.raises(release.RuntimeReleaseError, match='not installed'):
        release._resolved_runtime_distributions([{"name": "m8m-deliberately-absent", "version": "1.0"}])
    with pytest.raises(release.RuntimeReleaseError, match='does not satisfy'):
        release._resolved_runtime_distributions([{"name": "requests", "version": "0.0.1"}])
    # Core is encountered before the same named authored requirement.
    with pytest.raises(release.RuntimeReleaseError, match='does not satisfy'):
        release._resolved_runtime_distributions([{"name": "attrs", "version": "0.0.1"}])


def test_lock_comparison_never_uses_host_metadata(monkeypatch):
    def forbidden(*_):
        pytest.fail('startup consulted host metadata')
    monkeypatch.setattr(importlib.metadata, 'distribution', forbidden)
    release.assert_product_distribution_lock(PINS, PINS)
    with pytest.raises(release.RuntimeReleaseError, match='verified runtime lock'):
        release.assert_product_distribution_lock(PINS, PINS[:1])
    with pytest.raises(release.RuntimeReleaseError, match='verified runtime lock'):
        release.assert_product_distribution_lock(PINS, [PINS[0], {"name": "requests", "version": "2.32.3"}])


@pytest.mark.parametrize('child', ['child[feature]>=1', 'child @ https://example.invalid/child.whl'])
def test_active_transitive_extras_or_url_reject(monkeypatch, child):
    monkeypatch.setattr(release, 'RUNTIME_DEPENDENCIES', ('root',))
    distribution = SimpleNamespace(metadata={"Name": "root"}, version='1.0', requires=[child])
    monkeypatch.setattr(importlib.metadata, 'distribution', lambda _: distribution)
    with pytest.raises(release.RuntimeReleaseError, match='extras/direct URLs'):
        release._resolved_runtime_distributions()


def test_inactive_extra_dependencies_are_excluded(monkeypatch):
    monkeypatch.setattr(release, 'RUNTIME_DEPENDENCIES', ('root',))
    distribution = SimpleNamespace(metadata={"Name": "root"}, version='1.0',
        requires=['unavailable; extra == "docs"', 'unavailable; python_version < "2"'])
    def lookup(name):
        assert name == 'root'
        return distribution
    monkeypatch.setattr(importlib.metadata, 'distribution', lookup)
    assert release._resolved_runtime_distributions() == [('root', distribution)]


def test_compiler_preserves_distribution_order_and_changes_fingerprint(tmp_path):
    fixture = SkillFixture(tmp_path / 'source')
    path = fixture.root / 'agents/openai.yaml'
    source = yaml.safe_load(path.read_text())
    source['canvas']['runtime_distributions'] = PINS
    path.write_text(yaml.safe_dump(source), encoding='utf-8')
    assert compile_skill_source(fixture.root)['runtime_distributions'] == PINS
    harness, _ = closed_fixtures.ClosedImplementationImportTests()._harness(tmp_path / 'flow',
        'def run(input_data, **_):\n    return {"outputs":{"result":{"value":"ready"}}}\n')
    path = harness / 'flow.yaml'
    raw = yaml.safe_load(path.read_text())
    raw['runtime_distributions'] = PINS
    path.write_text(yaml.safe_dump(raw), encoding='utf-8')
    before = implementation_lock(harness, load_flow(harness))
    raw['runtime_distributions'] = list(reversed(PINS))
    path.write_text(yaml.safe_dump(raw), encoding='utf-8')
    assert before != implementation_lock(harness, load_flow(harness))
    source['canvas']['runtime_distributions'] = [PINS[0], {"name":"pillow", "version":"12.0"}]
    (fixture.root / 'agents/openai.yaml').write_text(yaml.safe_dump(source), encoding='utf-8')
    with pytest.raises(SkillSourceError, match='duplicate canonical'):
        compile_skill_source(fixture.root)


@pytest.fixture(scope='module')
def product_runtime(tmp_path_factory):
    base = tmp_path_factory.mktemp('product-distributions')
    handler = '''def run(input_data, **_):
    import io, sys
    from pathlib import Path
    import PIL, requests
    from PIL import Image
    assert sys.flags.isolated and sys.flags.no_site
    assert '/vendor/pillow/' in Path(PIL.__file__).as_posix()
    assert '/vendor/requests/' in Path(requests.__file__).as_posix()
    data = io.BytesIO()
    Image.new('RGB', (3, 2), (1, 2, 3)).save(data, format='PNG')
    data.seek(0)
    with Image.open(data) as image:
        assert image.getpixel((1, 1)) == (1, 2, 3)
    assert requests.Request('GET', 'https://example.invalid').prepare().method == 'GET'
    try:
        import packaging
    except ModuleNotFoundError:
        pass
    else:
        raise AssertionError('undeclared host package leaked')
    return {'outputs': {'result': {'value': 'PIL_REQUESTS_ISOLATED_PASS'}}}
'''
    harness, project = closed_fixtures.ClosedImplementationImportTests()._harness(base / 'codebase', handler)
    path = harness / 'flow.yaml'
    raw = yaml.safe_load(path.read_text())
    raw['runtime_distributions'] = PINS
    path.write_text(yaml.safe_dump(raw), encoding='utf-8')
    frozen = release.stage_runtime_release(builder_root=SKILL_ROOT, harness=harness,
        product_roots=[project / '.agents/skills/example'], flow_id='import_demo_v1',
        codebase=project, product_distributions=PINS)
    return base, harness, frozen


def launch(base, harness, name):
    execution = base / name
    execution.mkdir()
    request = execution / 'input.json'
    request.write_text('{}\n')
    run = execution / 'runs/probe'
    completed = subprocess.run([sys.executable, '-B', str(harness / 'launch.py'),
        '--request', str(request), '--harness-root', str(execution), '--run-dir', str(run)],
        cwd=execution, capture_output=True, text=True, timeout=120)
    return completed, run


def test_actual_generated_launcher_imports_binary_pillow_and_requests(product_runtime):
    base, harness, frozen = product_runtime
    completed, run = launch(base, harness, 'execution')
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)['state'] == 'COMPLETE'
    assert json.loads((run / 'milestones/ready/out/members/result/asset.json').read_text()) == 'PIL_REQUESTS_ISOLATED_PASS'
    assert json.loads((run / 'm8m-runtime-lock.json').read_text())['runtime_id'] == frozen['runtime_id']
    manifest = json.loads(Path(frozen['manifest_path']).read_text())
    names = {row['name'].lower().replace('_', '-') for row in manifest['dependencies']}
    assert {'pillow', 'requests', 'certifi', 'charset-normalizer', 'idna', 'urllib3'} <= names
    assert names - {name.lower().replace('_', '-') for name in release.RUNTIME_DEPENDENCIES} == {
        'pillow', 'requests', 'certifi', 'charset-normalizer', 'idna', 'urllib3', 'typing-extensions'}
    assert any(row['path'].startswith('vendor/pillow/') and row['path'].endswith(('.pyd','.so')) for row in manifest['members'])
    assert any(row['path'].endswith('certifi/cacert.pem') for row in manifest['members'])
    release.verify_runtime_release(Path(frozen['manifest_path']))


def test_launcher_rejects_wrong_declaration_before_handler(product_runtime):
    base, harness, _ = product_runtime
    path = harness / 'flow.yaml'
    original = path.read_bytes()
    raw = yaml.safe_load(original)
    raw['runtime_distributions'][0]['version'] = '12.0.0'
    try:
        path.write_text(yaml.safe_dump(raw), encoding='utf-8')
        completed, run = launch(base, harness, 'wrong-pin')
        assert completed.returncode != 0
        assert 'verified runtime lock' in completed.stderr
        assert not (run / 'flow-execution-record.json').exists()
        assert not (run / 'milestones').exists()
    finally:
        path.write_bytes(original)
