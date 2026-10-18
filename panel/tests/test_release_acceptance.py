"""仅验证门禁规则；合成回执不能用于产品验收或标准发行。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
import shutil
import uuid
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/public'))
import release_acceptance as gate
import build_panel_release as builder
REAL_SOURCE_IDENTITY = gate.source_identity


class ReleaseAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.root = gate.ROOT / 'staging' / ('release-gate-test-' + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.now = datetime.now(timezone.utc)
        self.identity = {'git_commit': 'a' * 40, 'source_tree_sha256': 'b' * 64, 'contract_sha256': 'c' * 64,
                         'baseline_manifest_sha256': 'd' * 64}
        self.contract = gate.read_json(gate.ROOT / gate.CONTRACT)
        self.write(gate.CONTRACT, self.contract)
        self.write('panel/frontend/package.json', {'devDependencies': {'@playwright/test': '1.61.1'},
                   'scripts': {'build': 'npm run typecheck && vite build', 'typecheck': 'vue-tsc --noEmit'}})
        self.fixture = self.root / 'panel/frontend/e2e/fixture_runtime.py'
        self.fixture.parent.mkdir(parents=True)
        self.fixture.write_text('# 测试夹具', encoding='utf-8')
        self.candidate = self.root / 'candidate.tar.gz'
        self.candidate_meta = {**self.identity, 'schema_version': 1, 'state': 'UNVERIFIED_CANDIDATE',
                               'deployable': False, 'created_at': (self.now - timedelta(minutes=10)).isoformat()}
        self.candidate_meta['build'] = {'kind': 'fixed-npm-build', 'command': gate.BUILD_COMMAND,
            'dist': 'panel/frontend/dist', 'source_tree_sha256': self.identity['source_tree_sha256'],
            'started_at': (self.now - timedelta(minutes=12)).isoformat(),
            'finished_at': (self.now - timedelta(minutes=11)).isoformat()}
        self.make_archive()
        self.report = {**self.identity, 'schema_version': 1, 'producer': 'shenzhou-browser-regression',
                       'run_id': 'unit-test-only', 'status': 'PASS',
                       'started_at': (self.now - timedelta(minutes=9)).isoformat(),
                       'finished_at': (self.now - timedelta(minutes=1)).isoformat(),
                       'candidate_archive_sha256': gate.sha256(self.candidate.read_bytes()),
                       'fixture': {'path': 'panel/frontend/e2e/fixture_runtime.py', 'sha256': gate.sha256(self.fixture.read_bytes())},
                       'environment': {'application': 'django-compiled-candidate', 'timezone': 'Asia/Shanghai',
                                       'locale': 'zh-CN', 'browser': 'chromium', 'browser_version': '144.0.0.0',
                                       'playwright_version': '1.61.1', 'base_url': 'http://127.0.0.1:18770'},
                       'journey': {'status': 'PASS', 'session_id': 'unit-test-only',
                                   'scenario_ids': [s['id'] for s in self.contract['required_scenarios'][:7]]}, 'results': []}
        self.screenshots = set()
        for scenario in self.contract['required_scenarios']:
            for viewport in scenario['viewports']:
                image = self.png(self.contract['viewports'][viewport]['width'])
                name = f'shots/{scenario["id"]}-{viewport}.png'
                self.write(name, image)
                digest = gate.sha256(image)
                self.screenshots.add(digest)
                self.report['results'].append({'scenario_id': scenario['id'], 'viewport': viewport, 'status': 'PASS',
                    'assertions': [{'id': a['id'], 'status': 'PASS'} for a in scenario['assertions']],
                    'screenshots': [{'checkpoint': checkpoint, 'path': name, 'sha256': digest} for checkpoint in scenario['screenshot_checkpoints']],
                    'network_log': {'path': 'network.json', 'sha256': gate.sha256(b'[]')}})
        self.write('network.json', b'[]')
        self.native = {'config': {'version': '1.61.1'}, 'errors': [],
                       'stats': {'expected': 1, 'unexpected': 0, 'skipped': 0, 'flaky': 0},
                       'suites': [{'specs': [{'ok': True, 'tests': [{'status': 'expected', 'expectedStatus': 'passed',
                                           'results': [{'status': 'passed', 'errors': []}]}]}]}]}
        self.save_native()
        self.review = {'schema_version': 1, 'status': 'PASS', 'candidate_archive_sha256': self.report['candidate_archive_sha256'],
                       'reviewer': 'unit-test-only', 'reviewed_at': self.now.isoformat(),
                       'screenshot_sha256': sorted(self.screenshots), 'baseline_change': 'none',
                       'baseline_before_sha256': 'd' * 64, 'baseline_after_sha256': 'd' * 64}
        self.save()
        mocked = patch.object(gate, 'source_identity', return_value=self.identity)
        mocked.start()
        self.addCleanup(mocked.stop)

    def cleanup(self):
        assert self.root.resolve().is_relative_to((gate.ROOT / 'staging').resolve())
        shutil.rmtree(self.root)

    def write(self, name, value):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())

    def png(self, width):
        def chunk(kind, payload):
            return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))
        return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, 100, 8, 2, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress((b'\0' + b'\0' * width * 3) * 100)) + chunk(b'IEND', b''))

    def make_archive(self, *, extra=None, break_legacy=False):
        front = {'index.html': b'<html>test</html>'}
        files = {'frontend-release/' + key: value for key, value in front.items()}
        files['frontend-release/frontend-manifest.json'] = json.dumps({'schema_version': 1, 'files': {
            key: {'bytes': len(value), 'sha256': gate.sha256(value)} for key, value in front.items()}}).encode()
        for name in ('portal.css', 'v2.css', 'portal.js', 'v2.js'):
            files['portal/static/portal/' + name] = name.encode()
            files['collected-static/portal/' + name] = b'changed' if break_legacy else name.encode()
        files['release-candidate.json'] = json.dumps(self.candidate_meta).encode()
        if extra:
            files.update(extra)
        files['release-manifest.json'] = json.dumps({'schema_version': 1, 'files': {
            key: {'bytes': len(value), 'sha256': gate.sha256(value)} for key, value in files.items()},
            'frontend_manifest_sha256': gate.sha256(files['frontend-release/frontend-manifest.json'])}).encode()
        with tarfile.open(self.candidate, 'w:gz') as archive:
            for name, value in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))

    def save(self):
        self.write('acceptance.json', self.report)
        self.review['acceptance_sha256'] = gate.sha256((self.root / 'acceptance.json').read_bytes())
        self.write('review.json', self.review)

    def save_native(self):
        self.write('playwright.json', self.native)
        self.report['playwright_report'] = {'path': 'playwright.json',
                                            'sha256': gate.sha256((self.root / 'playwright.json').read_bytes())}

    def verify(self):
        return gate.verify_acceptance(self.candidate, self.root / 'acceptance.json', self.root / 'review.json', root=self.root, now=self.now)

    def test_complete_synthetic_receipt_exercises_gate_only(self):
        result = self.verify()
        self.assertEqual(result['state'], 'ACCEPTED_LOCAL')
        self.assertFalse(result['deployment_authorized'])
        self.assertEqual(result['production_acceptance'], 'NOT TESTED')

    def test_binding_rejects_every_wrong_digest(self):
        for key in (*self.identity, 'candidate_archive_sha256'):
            with self.subTest(key=key):
                original = self.report[key]
                self.report[key] = '0' * len(original)
                self.save()
                with self.assertRaises(ValueError):
                    self.verify()
                self.report[key] = original

    def test_candidate_source_git_or_contract_changed(self):
        for key in self.identity:
            with self.subTest(key=key), patch.object(gate, 'source_identity', return_value={**self.identity, key: '0' * len(self.identity[key])}):
                with self.assertRaises(ValueError):
                    self.verify()

    def test_missing_failed_skipped_assertions_and_screenshots(self):
        original = deepcopy(self.report)
        mutations = [lambda r: r['results'].pop(), lambda r: r['results'].append(r['results'][0]),
                     lambda r: r['results'][0].update(status='SKIP'), lambda r: r['results'][0].update(status='FAIL'),
                     lambda r: r['results'][0]['assertions'].pop(),
                     lambda r: r['results'][0]['assertions'][0].update(status='NOT TESTED'),
                     lambda r: r['results'][0]['screenshots'].pop(),
                     lambda r: r['results'][0].update(viewport='tablet')]
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                self.report = deepcopy(original)
                mutate(self.report)
                self.save()
                with self.assertRaises(ValueError):
                    self.verify()

    def test_expired_or_future_or_reversed_times(self):
        for start, end in ((-1500, -1490), (10, 11), (-1, -2)):
            with self.subTest(start=start):
                self.report['started_at'] = (self.now + timedelta(minutes=start)).isoformat()
                self.report['finished_at'] = (self.now + timedelta(minutes=end)).isoformat()
                self.save()
                with self.assertRaises(ValueError):
                    self.verify()

    def test_fixture_environment_journey_and_network_required(self):
        original = deepcopy(self.report)
        network_scenarios = {s['id'] for s in self.contract['required_scenarios']
                             if 'network-log' in s['evidence_types']}
        self.assertTrue(network_scenarios)
        mutations = [lambda r: r['fixture'].update(sha256='0' * 64),
                     lambda r: r['environment'].update(application='vite-dev'),
                     lambda r: r['environment'].update(base_url='https://example.com'),
                     lambda r: r['journey'].update(scenario_ids=[]),
                     lambda r: next(row for row in r['results']
                                    if row['scenario_id'] in network_scenarios).pop('network_log')]
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                self.report = deepcopy(original)
                mutate(self.report)
                self.save()
                with self.assertRaises(ValueError):
                    self.verify()

    def test_visual_review_cannot_miss_or_change_screenshots(self):
        self.review['screenshot_sha256'].pop()
        self.save()
        with self.assertRaises(ValueError):
            self.verify()

    def test_unreviewed_baseline_change_rejected(self):
        self.review['baseline_change'] = 'approved'
        self.save()
        with self.assertRaises(ValueError):
            self.verify()

    def test_artifact_traversal_tamper_and_wrong_width(self):
        shot = self.report['results'][0]['screenshots'][0]
        for name in ('../outside.png', 'C:/outside.png', '/absolute.png', 'shots\\bad.png'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                gate.safe_relative(self.root, name)
        target = self.root / shot['path']
        target.write_bytes(self.png(390))
        with self.assertRaises(ValueError):
            self.verify()
        shot['sha256'] = gate.sha256(target.read_bytes())
        self.save()
        with self.assertRaises(ValueError):
            self.verify()

    def test_duplicate_json_keys_and_size_limit(self):
        with self.assertRaises(ValueError):
            gate.json_bytes(b'{"status":"FAIL","status":"PASS"}')
        with self.assertRaises(ValueError):
            gate.json_bytes(b' ' * (gate.MAX_JSON + 1))
        with patch.object(gate, 'MAX_TOTAL', 10), self.assertRaises(ValueError):
            gate.validate_archive(self.candidate)

    def test_archive_path_and_legacy_integrity(self):
        self.make_archive(extra={'../escape.py': b'bad'})
        with self.assertRaises(ValueError):
            gate.validate_archive(self.candidate)

    def test_real_source_fingerprint_tracks_dirty_baseline_and_ci(self):
        with patch.object(gate.subprocess, 'run') as git:
            git.return_value.stdout = 'a' * 40
            initial = REAL_SOURCE_IDENTITY(self.root)
            self.write('panel/portal/empty.py', b'')
            empty = REAL_SOURCE_IDENTITY(self.root)
            self.assertNotEqual(initial['source_tree_sha256'], empty['source_tree_sha256'])
            self.write('panel/frontend/e2e/__screenshots__/baseline.png', self.png(390))
            baseline = REAL_SOURCE_IDENTITY(self.root)
            self.assertNotEqual(empty['source_tree_sha256'], baseline['source_tree_sha256'])
            self.assertNotEqual(empty['baseline_manifest_sha256'], baseline['baseline_manifest_sha256'])
            self.write('.github/workflows/check.yml', b'name: gate')
            workflow = REAL_SOURCE_IDENTITY(self.root)
            self.assertNotEqual(baseline['source_tree_sha256'], workflow['source_tree_sha256'])
            self.write('.gitignore', b'staging/')
            ignored = REAL_SOURCE_IDENTITY(self.root)
            self.assertNotEqual(workflow['source_tree_sha256'], ignored['source_tree_sha256'])

    def test_png_bad_crc_rejected_even_when_receipt_hash_updated(self):
        shot = self.report['results'][0]['screenshots'][0]
        raw = bytearray((self.root / shot['path']).read_bytes())
        raw[-1] ^= 1
        self.write(shot['path'], bytes(raw))
        shot['sha256'] = gate.sha256(raw)
        with self.assertRaises(ValueError):
            gate.artifact(self.root, shot, png=True, width=1440)

    def test_junction_and_symlink_ancestors_rejected(self):
        with patch.object(Path, 'is_junction', return_value=True), self.assertRaises(ValueError):
            gate.read_bytes(self.fixture)
        with patch.object(Path, 'is_symlink', return_value=True), self.assertRaises(ValueError):
            gate.read_bytes(self.fixture)

    def test_standard_promotion_keeps_exact_candidate_bytes(self):
        output = self.root / 'accepted.tar.gz'
        args = ['build_panel_release.py', '--candidate-archive', str(self.candidate),
                '--acceptance', str(self.root / 'acceptance.json'), '--visual-review', str(self.root / 'review.json'),
                '--output', str(output)]
        with patch.object(builder, 'ROOT', self.root), patch.object(builder, 'source_identity', return_value=self.identity), \
                patch.object(sys, 'argv', args), patch('sys.stdout', new_callable=io.StringIO):
            builder.main()
        self.assertEqual(output.read_bytes(), self.candidate.read_bytes())
        self.assertEqual(gate.read_json(output.with_suffix('.gz.manifest.json'))['state'], 'ACCEPTED_LOCAL')

    def test_changed_baseline_requires_old_manifest_and_approval(self):
        old = {'files': {}}
        self.write('baseline-before.json', old)
        digest = gate.sha256((self.root / 'baseline-before.json').read_bytes())
        self.review.update(baseline_change='approved', approval_reference='测试需求与审查记录',
                           baseline_before_sha256=digest,
                           baseline_before_manifest={'path': 'baseline-before.json', 'sha256': digest})
        self.save()
        self.assertEqual(self.verify()['state'], 'ACCEPTED_LOCAL')
        self.review['baseline_after_sha256'] = '0' * 64
        self.save()
        with self.assertRaises(ValueError):
            self.verify()
        self.make_archive(break_legacy=True)
        with self.assertRaises(ValueError):
            gate.validate_archive(self.candidate)

    def test_archive_links_and_duplicate_members_rejected(self):
        for kind in ('link', 'duplicate'):
            with self.subTest(kind=kind):
                with tarfile.open(self.candidate, 'w:gz') as archive:
                    info = tarfile.TarInfo('item')
                    if kind == 'link':
                        info.type = tarfile.SYMTYPE
                        info.linkname = 'outside'
                        archive.addfile(info)
                    else:
                        archive.addfile(info)
                        archive.addfile(info)
                with self.assertRaises(ValueError):
                    gate.validate_archive(self.candidate)

    def test_default_builder_refuses_unverified_dist(self):
        with patch.object(sys, 'argv', ['build_panel_release.py', '--dist', str(self.root), '--output', str(self.root / 'out.tar.gz')]):
            with self.assertRaises(SystemExit) as error:
                builder.main()
        self.assertEqual(error.exception.code, 2)
        self.assertFalse((self.root / 'out.tar.gz').exists())

    def test_native_playwright_report_rejects_zero_skips_flaky_and_failure(self):
        original = deepcopy(self.native)
        mutations = [lambda r: r['stats'].update(expected=0), lambda r: r['stats'].update(skipped=1),
                     lambda r: r['stats'].update(flaky=1), lambda r: r['stats'].update(unexpected=1),
                     lambda r: r.update(errors=[{'message': 'failed'}]),
                     lambda r: r['suites'][0]['specs'][0]['tests'][0]['results'][0].update(status='failed'),
                     lambda r: r['suites'][0]['specs'][0]['tests'][0].update(expectedStatus='failed'),
                     lambda r: r['config'].update(version='0.0.0')]
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                self.native = deepcopy(original)
                mutate(self.native)
                self.save_native()
                self.save()
                with self.assertRaises(ValueError):
                    self.verify()

    def test_placeholder_browser_and_unpinned_playwright_rejected(self):
        for value in ('unknown', 'pending', ''):
            with self.subTest(value=value):
                self.report['environment']['browser_version'] = value
                self.save()
                with self.assertRaises(ValueError):
                    self.verify()

    def test_candidate_requires_fresh_fixed_build_and_records_identity(self):
        with patch.object(builder, 'ROOT', self.root), patch.object(builder, 'source_identity', return_value=self.identity), \
                patch.object(builder.shutil, 'which', return_value='npm'), patch.object(builder.subprocess, 'run') as run:
            identity, build = builder.build_current_frontend(self.root / 'panel/frontend/dist')
        self.assertEqual(identity, self.identity)
        self.assertEqual(build['source_tree_sha256'], self.identity['source_tree_sha256'])
        run.assert_called_once_with(gate.BUILD_COMMAND, cwd=self.root / 'panel/frontend', check=True, timeout=300)

    def test_candidate_rejects_other_dist_without_running_build(self):
        with patch.object(builder, 'ROOT', self.root), patch.object(builder.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                builder.build_current_frontend(self.root / 'old-dist')
        run.assert_not_called()

    def test_candidate_rejects_source_change_during_build(self):
        with patch.object(builder, 'ROOT', self.root), \
                patch.object(builder, 'source_identity', side_effect=[self.identity, {**self.identity, 'source_tree_sha256': '0' * 64}]), \
                patch.object(builder.shutil, 'which', return_value='npm'), patch.object(builder.subprocess, 'run'):
            with self.assertRaises(ValueError):
                builder.build_current_frontend(self.root / 'panel/frontend/dist')

    def test_failed_build_creates_no_candidate_even_with_old_dist(self):
        self.write('panel/frontend/dist/index.html', b'<html>old bundle</html>')
        output = self.root / 'failed.tar.gz'
        with patch.object(builder, 'ROOT', self.root), patch.object(builder, 'source_identity', return_value=self.identity), \
                patch.object(builder.shutil, 'which', return_value='npm'), \
                patch.object(builder.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, gate.BUILD_COMMAND)), \
                patch.object(sys, 'argv', ['build_panel_release.py', '--candidate', '--dist', str(self.root / 'panel/frontend/dist'), '--output', str(output)]):
            with self.assertRaises(subprocess.CalledProcessError):
                builder.main()
        self.assertFalse(output.exists())

    def test_unreviewed_build_script_is_rejected(self):
        package = gate.read_json(self.root / 'panel/frontend/package.json')
        package['scripts']['build'] = 'echo skip-build'
        self.write('panel/frontend/package.json', package)
        with patch.object(builder, 'ROOT', self.root), patch.object(builder.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                builder.build_current_frontend(self.root / 'panel/frontend/dist')
        run.assert_not_called()

    def test_previous_candidate_without_build_proof_rejected(self):
        self.candidate_meta.pop('build')
        self.make_archive()
        self.report['candidate_archive_sha256'] = gate.sha256(self.candidate.read_bytes())
        self.review['candidate_archive_sha256'] = self.report['candidate_archive_sha256']
        self.save()
        with self.assertRaisesRegex(ValueError, '固定构建证明'):
            self.verify()


if __name__ == '__main__':
    unittest.main()
