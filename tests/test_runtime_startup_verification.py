"""Full verification is reused only once in the same isolated invocation."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import runtime_release as release


class StartupVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / 'runtime-manifest.json'
        self.manifest = {'fixture': 'verified'}
        self.path.write_text(json.dumps(self.manifest), encoding='utf-8')
        release._startup_verification = None

    def tearDown(self):
        release._startup_verification = None
        self.tmp.cleanup()

    def bootstrap(self):
        with patch.object(release.sys, 'flags', SimpleNamespace(isolated=True, no_site=True)), patch.object(release.sys, 'dont_write_bytecode', True):
            return release.verify_runtime_for_startup(self.path)

    def test_full_verification_once_then_no_second_scan_at_first_bind(self):
        with patch.object(release, 'verify_runtime_release', return_value=self.manifest) as verify:
            result = self.bootstrap()
            result['fixture'] = 'caller-mutated-return'
            self.assertEqual(release._runtime_for_binding(self.path), {'fixture': 'verified'})
            self.assertEqual(verify.call_count, 1)
            release._runtime_for_binding(self.path)
            self.assertEqual(verify.call_count, 2, 'the result must not survive a second invocation')

    def test_manifest_change_after_bootstrap_rejects(self):
        with patch.object(release, 'verify_runtime_release', return_value=self.manifest):
            self.bootstrap()
            self.path.write_text('{"fixture":"changed"}', encoding='utf-8')
            with self.assertRaisesRegex(release.RuntimeReleaseError, 'changed after bootstrap'):
                release._runtime_for_binding(self.path)

    def test_another_process_or_path_does_not_reuse_verification(self):
        with patch.object(release, 'verify_runtime_release', return_value=self.manifest) as verify:
            self.bootstrap()
            with patch.object(release.os, 'getpid', return_value=-1):
                release._runtime_for_binding(self.path)
            self.assertEqual(verify.call_count, 2)
            self.bootstrap()
            release._runtime_for_binding(self.root / 'other.json')
            self.assertEqual(verify.call_count, 4)

    def test_environment_flag_cannot_skip_verification(self):
        with patch.dict(release.os.environ, {'M8M_RUNTIME_VERIFIED': 'true'}), patch.object(release, 'verify_runtime_release', side_effect=release.RuntimeReleaseError('tampered')):
            with self.assertRaisesRegex(release.RuntimeReleaseError, 'tampered'):
                release._runtime_for_binding(self.path)

    def test_dependency_is_hashed_once_when_building_manifest(self):
        member = self.root / 'package.py'
        member.write_bytes(b'fixture payload')
        distribution = SimpleNamespace(files=['package.py'], version='1.0', locate_file=lambda _: member)
        with patch.object(release, '_source_members', return_value=[]), patch.object(release, '_resolved_runtime_distributions', return_value=[('fixture', distribution)]), patch.object(release, '_python_identity', return_value=('fixture', 'sha256:'+'0'*64)), patch.object(release, '_sha256_file', wraps=release._sha256_file) as hashing:
            manifest, sources = release._manifest_from_sources(self.root)
        self.assertEqual(hashing.call_count, 1)
        self.assertEqual(len(sources), 1)
        self.assertEqual(manifest['dependencies'][0]['files_digest'], release._digest_bytes(release._canonical_bytes(manifest['members'])))


if __name__ == '__main__':
    unittest.main()
