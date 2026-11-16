"""Public artifact and shell-wrapper contracts; no network or container builds."""

import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('l8k_public_evidence', ROOT / 'public-evidence.py')
PUBLIC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PUBLIC)
LOCK = json.loads((ROOT / 'build-lock.json').read_text())
COMMIT = 'a' * 40
IMAGE_ID = 'sha256:' + 'b' * 64
SECRET = 'synthetic-l8k-output-secret:/+$'
ENCODED = base64.b64encode(SECRET.encode()).decode()
PAYLOADS = (SECRET, f'Authorization: Basic {ENCODED}',
            json.dumps({'auth': ENCODED, 'identitytoken': SECRET}),
            f'::error::{SECRET}\n\x1b[31m{ENCODED}\x1b[0m')


class PublicEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('L8K_TEST_ROOT'))
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.results = self.work / 'results'
        self.raw_dir = self.results / 'builds/l8k-build-fixture'
        self.raw_dir.mkdir(parents=True)
        self.receipt = self.raw_dir / 'receipt.json'
        self.lock_path = ROOT / 'build-lock.json'
        self.raw = {'schemaVersion': 1, 'status': 'LOCAL_BUILD_VERIFIED',
                    'deploymentQualified': False, 'published': False,
                    'version': LOCK['source']['version'], 'sourceRevision': LOCK['source']['revision'],
                    'buildDate': '2026-11-15T00:00:00Z', 'imageID': IMAGE_ID,
                    'lockSHA256': hashlib.sha256(self.lock_path.read_bytes()).hexdigest(),
                    'sourceTreeDigest': 'c' * 64,
                    'builderImage': LOCK['builderImage'], 'runtimeImage': LOCK['runtimeImage']}
        self.receipt.write_text(json.dumps(self.raw))

    def public_content(self):
        public = self.results / 'public'
        self.assertEqual({p.name for p in public.iterdir()}, set(PUBLIC.PUBLIC_FILES))
        for line in (public / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split('  ', 1)
            self.assertIn(name, PUBLIC.PUBLIC_FILES[:-1])
            self.assertEqual(hashlib.sha256((public / name).read_bytes()).hexdigest(), digest)
        self.assertEqual(len((public / 'SHA256SUMS').read_text().splitlines()), 3)
        return ''.join((public / name).read_text() for name in PUBLIC.PUBLIC_FILES)

    def test_success_omits_raw_fields_logs_sources_hidden_files_and_symlinks(self):
        self.raw.update(reason='\n'.join(PAYLOADS), commands=[{'argv': [SECRET]}],
                        observedVersion={'auth': ENCODED}, supportedNetworkOperatorReleases=[SECRET])
        self.receipt.write_text(json.dumps(self.raw))
        for name in ('build.log', 'tests.log', '.auth.json', 'source.git/config',
                     'context/source/secret.txt', 'tests/scratch/result.txt', 'image-inspect.stdout'):
            path = self.results / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('\n'.join(PAYLOADS))
        (self.results / 'raw-link').symlink_to(self.results / '.auth.json')
        self.assertEqual(PUBLIC.export_evidence(self.results, COMMIT, 'complete', 0), 0)
        content = self.public_content()
        for payload in (*PAYLOADS, SECRET, ENCODED):
            self.assertNotIn(payload, content)
        record = json.loads((self.results / 'public/receipt.json').read_text())
        self.assertEqual(set(record), {'schemaVersion', 'status', 'stage', 'exitCode',
                                     'workflowCommit', 'deploymentQualified', 'published', 'verifiedBuild'})
        self.assertEqual(record['status'], 'CI_PASS')
        self.assertEqual(record['verifiedBuild']['imageID'], IMAGE_ID)
        self.assertFalse(record['published'])
        self.assertTrue((self.results / '.auth.json').exists())

    def test_failure_has_only_fixed_status_fields_and_does_not_read_original_receipt(self):
        self.receipt.write_text('\n'.join(PAYLOADS))
        for stage in PUBLIC.STAGES:
            with self.subTest(stage=stage):
                results = self.work / stage
                results.mkdir()
                self.assertEqual(PUBLIC.export_evidence(results, COMMIT, stage, 125), 125)
                record = json.loads((results / 'public/receipt.json').read_text())
                self.assertEqual(set(record), {'schemaVersion', 'status', 'stage', 'exitCode',
                                             'workflowCommit', 'deploymentQualified', 'published'})
                self.assertEqual((record['status'], record['stage'], record['exitCode']), ('CI_FAIL', stage, 125))

    def test_corrupt_or_secret_bearing_metadata_yields_only_failure_summary(self):
        changes = [(key, payload) for key in self.raw for payload in PAYLOADS]
        changes += [('buildDate', '2026-99-99T00:00:00Z')]
        for index, (key, value) in enumerate(changes):
            with self.subTest(field=key, case=index):
                results = self.work / f'malformed-{index}'
                raw = results / 'builds/l8k-build-fixture'
                raw.mkdir(parents=True)
                (raw / 'receipt.json').write_text(json.dumps({**self.raw, key: value}))
                self.assertEqual(PUBLIC.export_evidence(results, COMMIT, 'complete', 0), 1)
                record = json.loads((results / 'public/receipt.json').read_text())
                self.assertEqual(record['status'], 'CI_FAIL')
                self.assertNotIn('verifiedBuild', record)
                content = ''.join(p.read_text() for p in (results / 'public').iterdir())
                self.assertNotIn(SECRET, content)
                self.assertNotIn(ENCODED, content)

    def test_missing_duplicate_symlink_and_invalid_json_receipts_cannot_verify(self):
        cases = ('missing', 'duplicate', 'symlink-file', 'symlink-directory', 'invalid-json', 'oversized')
        for case in cases:
            with self.subTest(case=case):
                results = self.work / case
                raw = results / 'builds/l8k-build-fixture'
                raw.mkdir(parents=True)
                receipt = raw / 'receipt.json'
                receipt.write_text(json.dumps(self.raw))
                if case == 'missing':
                    receipt.unlink()
                elif case == 'duplicate':
                    other = results / 'builds/l8k-build-second'
                    other.mkdir()
                    (other / 'receipt.json').write_text(json.dumps(self.raw))
                elif case == 'symlink-file':
                    receipt.unlink()
                    receipt.symlink_to(self.receipt)
                elif case == 'symlink-directory':
                    receipt.unlink()
                    raw.rmdir()
                    raw.symlink_to(self.raw_dir, target_is_directory=True)
                elif case == 'invalid-json':
                    receipt.write_text(SECRET)
                else:
                    receipt.write_text(' ' * (1024 * 1024 + 1))
                with self.assertRaises(ValueError):
                    PUBLIC.verified_build(results, self.lock_path)

    def test_existing_public_directory_is_rejected_without_reusing_or_overwriting_files(self):
        public = self.results / 'public'
        public.mkdir()
        original = public / 'existing.txt'
        original.write_text('existing evidence')
        with self.assertRaises(FileExistsError):
            PUBLIC.export_evidence(self.results, COMMIT, 'complete', 0)
        self.assertEqual(list(public.iterdir()), [original])
        self.assertEqual(original.read_text(), 'existing evidence')

    def test_invalid_workflow_stage_and_exit_values_create_no_public_directory(self):
        for commit, stage, result in ((SECRET, 'complete', 0), (COMMIT, SECRET, 1),
                                      (COMMIT, 'build', -1), (COMMIT, 'build', 256),
                                      (COMMIT, 'build', True)):
            with self.subTest(stage_known=stage in PUBLIC.STAGES, code_type=type(result).__name__), \
                    self.assertRaises(ValueError):
                PUBLIC.export_evidence(self.results, commit, stage, result)
            self.assertFalse((self.results / 'public').exists())

    def test_cli_metadata_errors_emit_only_fixed_diagnostic(self):
        self.receipt.write_text(SECRET)
        stdout, stderr = io.StringIO(), io.StringIO()
        from unittest.mock import patch
        with patch('sys.argv', ['public-evidence.py', 'image-id', '--results', str(self.results)]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(PUBLIC.main(), 1)
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(), 'L8K public evidence rejected; details suppressed.\n')

    def test_wrapper_captures_output_and_prevents_tagging_after_failed_checks(self):
        # Run the real shell wrapper/exporter with stand-ins for tests, build and Podman.
        # The unittest stand-in prevents recursive execution of this integration test.
        for outcome in ('success', 'tests', 'build', 'receipt', 'tag'):
            with self.subTest(outcome=outcome):
                case = self.work / ('wrapper-' + outcome)
                definition, tools = case / 'definition', case / 'bin'
                definition.mkdir(parents=True)
                tools.mkdir()
                for name in ('ci-build.sh', 'public-evidence.py', 'build-lock.json'):
                    shutil.copyfile(ROOT / name, definition / name)
                fixture = case / 'fixture.json'
                raw = copy.deepcopy(self.raw)
                raw.update(reason=SECRET, commands=[{'argv': [SECRET]}], observedVersion={'auth': ENCODED})
                if outcome == 'receipt':
                    raw['imageID'] = SECRET
                fixture.write_text(json.dumps(raw))
                python_shim = tools / 'python3'
                python_shim.write_text(f'#!{sys.executable}\n' + '''import os, sys
if sys.argv[1:3] == ['-m', 'unittest']:
    print(os.environ['TEST_SECRET'])
    print(os.environ['TEST_SECRET'], file=sys.stderr)
    sys.exit(9 if os.environ['TEST_OUTCOME'] == 'tests' else 0)
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
''')
                build = definition / 'build.sh'
                build.write_text(f'#!{sys.executable}\n' + '''import os, pathlib, sys
print(os.environ['TEST_SECRET'])
print(os.environ['TEST_SECRET'], file=sys.stderr)
root = pathlib.Path(sys.argv[sys.argv.index('--output-parent') + 1]) / 'l8k-build-fixture'
root.mkdir()
(root / 'receipt.json').write_bytes(pathlib.Path(os.environ['TEST_FIXTURE']).read_bytes())
(root / '.auth.json').write_text(os.environ['TEST_SECRET'])
sys.exit(10 if os.environ['TEST_OUTCOME'] == 'build' else 0)
''')
                podman = tools / 'podman'
                podman.write_text(f'#!{sys.executable}\n' + '''import json, os, pathlib, sys
print(os.environ['TEST_SECRET'])
print(os.environ['TEST_SECRET'], file=sys.stderr)
with pathlib.Path(os.environ['TEST_CALLS']).open('a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
sys.exit(11 if os.environ['TEST_OUTCOME'] == 'tag' else 0)
''')
                for path in (python_shim, build, podman):
                    path.chmod(0o700)
                calls = case / 'calls.jsonl'
                results = case / 'results with spaces'
                environment = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ['PATH'],
                                   TEST_OUTCOME=outcome, TEST_SECRET='\n'.join(PAYLOADS),
                                   TEST_FIXTURE=str(fixture), TEST_CALLS=str(calls))
                proc = subprocess.run(['bash', str(definition / 'ci-build.sh'), str(results),
                                       'k8s-launch-kit', 'pr-64-test', COMMIT],
                                      env=environment, capture_output=True, text=True, timeout=30)
                content = proc.stdout + proc.stderr + ''.join(p.read_text() for p in (results / 'public').iterdir())
                self.assertNotIn(SECRET, content)
                self.assertNotIn(ENCODED, content)
                self.assertFalse(results.with_suffix('.tar.gz').exists())
                self.assertEqual({p.name for p in (results / 'public').iterdir()}, set(PUBLIC.PUBLIC_FILES))
                record = json.loads((results / 'public/receipt.json').read_text())
                if outcome == 'success':
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(record['status'], 'CI_PASS')
                    self.assertEqual(len(calls.read_text().splitlines()), 2)
                    for line in calls.read_text().splitlines():
                        self.assertEqual(json.loads(line)[:2], ['tag', IMAGE_ID])
                else:
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertEqual(record['status'], 'CI_FAIL')
                    self.assertEqual(record['stage'], outcome)
                    self.assertNotIn('verifiedBuild', record)
                    if outcome != 'tag':
                        self.assertFalse(calls.exists())
                self.assertIn(SECRET, (results / 'tests.log').read_text())
                if outcome != 'tests':
                    self.assertIn(SECRET, (results / 'build.log').read_text())


class WorkflowPublicationTests(unittest.TestCase):
    def test_l8k_upload_paths_and_retention_are_explicit(self):
        for name, days in (('pr-image-build.yml', '7'), ('push-image-build.yml', '30')):
            with self.subTest(workflow=name):
                text = (ROOT.parent / '.github/workflows' / name).read_text()
                evidence = re.search(r'      - name: Upload l8k build evidence\n(.*?)(?=\n      - name:)',
                                     text, re.S)[1]
                paths = re.findall(r'^            (.+)$', evidence, re.M)
                self.assertEqual(paths, ['tmp/l8k-ci/public/' + n for n in PUBLIC.PUBLIC_FILES])
                self.assertIn('          retention-days: ' + days + '\n', evidence)
                commands = re.search(r'      - name: Upload build artifact\n(.*?)(?=\n      - name:)',
                                     text, re.S)[1]
                self.assertIn("matrix.image == 'k8s-launch-kit'", commands)
                self.assertIn("'tmp/l8k-ci/public/commands-k8s-launch-kit.txt'", commands)
                self.assertIn('          retention-days: ' + days + '\n', commands)

    def test_pr_image_build_remains_disabled_during_hardening(self):
        text = (ROOT.parent / '.github/workflows/pr-image-build.yml').read_text()
        job = re.search(r'^  build-image:\n(.*?)(?=^  [a-z-]+:)', text, re.M | re.S)[1]
        self.assertIn('    if: ${{ false }}\n', job)
        self.assertNotIn("needs.prepare-matrix.outputs.length != '0'", job)
        self.assertIn('  post-comment:\n    needs: build-image\n', text)


if __name__ == '__main__':
    unittest.main()
