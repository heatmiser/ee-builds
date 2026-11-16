"""Public report/comment contracts with synthetic secrets; no containers or network."""

import ast
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('ci_report', ROOT / '.github/scripts/ci_report.py')
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)
COMMIT = 'a' * 40
SECRET = 'synthetic-report-secret:/+$'
ENCODED = base64.b64encode(SECRET.encode()).decode()
PAYLOADS = (SECRET, f'Authorization: Basic {ENCODED}', f'Authorization: Bearer {SECRET}',
            json.dumps({'auth': ENCODED, 'identitytoken': SECRET}),
            f'::error::{SECRET}\r\n\x1b[31m{ENCODED}\x1b[0m')


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        definition = self.work / 'example-image'
        definition.mkdir()
        (definition / 'image.yaml').write_text('cli_binary: example\n')
        self.output = self.work / 'results'

    def generate(self, kind='image', run=None, output=None):
        with patch.object(REPORT, 'ROOT', self.work):
            return REPORT.generate(kind, 'example-image:pr-64-test', 'ghcr.io/heatmiser/example-image:pr-64-test',
                                   COMMIT, output or self.output, run=run)

    def test_synthetic_stdout_and_stderr_never_reach_success_or_failure_reports(self):
        for kind in ('ee', 'image'):
            for failed_check in (None, 0, 1):
                for index, payload in enumerate(PAYLOADS):
                    with self.subTest(kind=kind, failed_check=failed_check, payload=index):
                        output = self.work / f'{kind}-{failed_check}-{index}'
                        calls = []

                        def run(argv, **kwargs):
                            self.assertTrue(argv[:4] == ['podman', 'run', '--rm', '--pull=never'])
                            self.assertNotIn('-it', argv)
                            self.assertEqual(kwargs['timeout'], 120)
                            self.assertFalse(kwargs['check'])
                            kwargs['stdout'].write(payload.encode())
                            kwargs['stderr'].write(payload.encode())
                            calls.append(argv)
                            return subprocess.CompletedProcess(argv, 125 if len(calls) - 1 == failed_check else 0)

                        stdout, stderr = io.StringIO(), io.StringIO()
                        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                            result = self.generate(kind, run, output)
                        self.assertEqual(result, 0 if failed_check is None else 1)
                        self.assertEqual(len(calls), 1 if failed_check == 0 else 2)
                        report = (output / 'commands-example-image.txt').read_text()
                        self.assertIn('ghcr.io/heatmiser/example-image:pr-64-test', report)
                        self.assertIn(COMMIT, report)
                        expected_names = ('ansible-collections', 'ansible-version') if kind == 'ee' else ('cli-version', 'tool-spot-check')
                        self.assertIn(f'- {expected_names[0]}: ' + ('FAIL' if failed_check == 0 else 'PASS'), report)
                        if failed_check != 0:
                            self.assertIn(f'- {expected_names[1]}: ' + ('FAIL' if failed_check == 1 else 'PASS'), report)
                        self.assertNotIn(SECRET, report + stdout.getvalue() + stderr.getvalue())
                        self.assertNotIn(ENCODED, report + stdout.getvalue() + stderr.getvalue())
                        self.assertEqual(stdout.getvalue() + stderr.getvalue(), '')
                        logs = list((output / 'private-logs').iterdir())
                        self.assertEqual(len(logs), len(calls) * 2)
                        self.assertTrue(all(p.read_bytes() == payload.encode() for p in logs))

    def test_runtime_exception_messages_are_excluded_from_reports(self):
        errors = (OSError(SECRET), ValueError(SECRET),
                  subprocess.TimeoutExpired(['podman', SECRET], 120, output=SECRET, stderr=SECRET))
        for index, error in enumerate(errors):
            with self.subTest(error=type(error).__name__):
                output = self.work / f'error-{index}'
                self.assertEqual(self.generate(run=unittest.mock.Mock(side_effect=error), output=output), 1)
                report = (output / 'commands-example-image.txt').read_text()
                self.assertIn('- cli-version: ERROR', report)
                self.assertNotIn(SECRET, report)

    def test_invalid_metadata_stops_before_commands_or_report_creation(self):
        valid = ['image', 'example-image:tag', 'quay.io/s4v0/example-image:tag', COMMIT]
        invalid = ((0, SECRET), (1, '../example-image:tag'), (1, 'k8s-launch-kit:tag'),
                   (1, 'example-image:tag\n' + SECRET), (2, 'https://registry.invalid/?auth=' + SECRET),
                   (2, 'quay.io/s4v0/other-image:tag'), (2, 'quay.io/s4v0/example-image:other-tag'),
                   (2, valid[2] + '\n::error::' + SECRET), (3, SECRET))
        for index, value in invalid:
            arguments = valid.copy()
            arguments[index] = value
            with self.subTest(field=index), patch.object(REPORT, 'subprocess') as process, \
                    self.assertRaises(ValueError):
                REPORT.generate(*arguments, self.output)
            process.run.assert_not_called()
            self.assertFalse(self.output.exists())

    def test_invalid_or_symlink_cli_metadata_cannot_run_or_export(self):
        config = self.work / 'example-image/image.yaml'
        for content in ('cli_binary: ' + SECRET, 'cli_binary: first\ncli_binary: second\n',
                        'cli_binary: ../example\n', 'cli_binary: example --token\n'):
            config.write_text(content)
            with patch.object(REPORT.subprocess, 'run') as run, self.assertRaises(ValueError):
                self.generate()
            run.assert_not_called()
            self.assertFalse(self.output.exists())
        config.unlink()
        target = self.work / 'elsewhere.yaml'
        target.write_text('cli_binary: example\n')
        config.symlink_to(target)
        with self.assertRaises(ValueError):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_existing_directory_is_not_reused(self):
        self.output.mkdir()
        original = self.output / 'previous.txt'
        original.write_text('previous evidence')
        run = unittest.mock.Mock()
        with self.assertRaises(FileExistsError):
            self.generate(run=run)
        run.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [original])
        self.assertEqual(original.read_text(), 'previous evidence')

    def test_cli_failure_messages_suppress_arbitrary_exceptions(self):
        arguments = ['ci_report.py', '--kind', 'ee', '--local-image', 'example-image:tag',
                     '--reference', 'ghcr.io/heatmiser/example-image:tag', '--commit', COMMIT,
                     '--output', str(self.output)]
        for error in (OSError(SECRET), ValueError(SECRET),
                      UnicodeDecodeError('utf-8', SECRET.encode(), 0, 1, SECRET)):
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(error=type(error).__name__), patch('sys.argv', arguments), \
                    patch.object(REPORT, 'generate', side_effect=error), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(REPORT.main(), 1)
            self.assertEqual(stdout.getvalue(), '')
            self.assertEqual(stderr.getvalue(), 'CI report failed; command output suppressed.\n')

    def test_tool_spot_check_preserves_python_code_as_one_shell_argument(self):
        argv = shlex.split(REPORT.TOOL_CHECK)
        self.assertEqual(argv[-3:-1], ['python3', '-c'])
        ast.parse(argv[-1])
        self.assertIn('jc.__version__', argv[-1])


class WorkflowReportTests(unittest.TestCase):
    def workflow(self, name):
        return yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)

    def test_all_four_workflows_use_bounded_reports_and_explicit_uploads(self):
        for name in ('pr-ee-build.yml', 'push-ee-build.yml', 'pr-image-build.yml', 'push-image-build.yml'):
            with self.subTest(workflow=name):
                workflow = self.workflow(name)
                kind = 'ee' if '-ee-' in name else 'image'
                steps = workflow['jobs']['build-ee' if kind == 'ee' else 'build-image']['steps']
                build = next(s for s in steps if s['name'] == 'Build image and create artifact')
                self.assertNotIn('COMMANDS_FILE', build['run'])
                self.assertNotIn('podman run', build['run'])
                report = next(s for s in steps if s['name'] == 'Generate bounded build report')
                self.assertIn(f'ci_report.py --kind {kind}', report['run'])
                self.assertNotIn('${{', report['run'])
                self.assertIn('--reference "$REPORT_REFERENCE"', report['run'])
                if kind == 'image':
                    self.assertEqual(report['if'], "matrix.image != 'k8s-launch-kit'")
                upload = next(s for s in steps if s['name'] == 'Upload build artifact')
                self.assertNotIn('if', upload)  # failed checks cannot publish a report as successful evidence
                self.assertEqual(upload['with']['retention-days'], '7' if name.startswith('pr-') else '30')
                path = upload['with']['path']
                self.assertNotIn('*', path)
                self.assertNotIn('private-logs', path)
                if kind == 'ee':
                    self.assertEqual(path, '${{ runner.temp }}/ci-report-${{ matrix.ee }}/commands-${{ matrix.ee }}.txt')
                else:
                    self.assertIn("'tmp/l8k-ci/public/commands-k8s-launch-kit.txt'", path)
                    self.assertIn("format('{0}/ci-report-{1}/commands-{1}.txt', runner.temp, matrix.image)", path)

    def test_pr_comments_contain_only_fixed_workflow_link_even_with_secret_artifacts_present(self):
        self.assertIsNotNone(shutil.which('node'), 'Node is required to execute the comment-script contract')
        for name in ('pr-ee-build.yml', 'pr-image-build.yml'):
            with self.subTest(workflow=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'commands-malicious'
                path.mkdir()
                (path / 'commands-malicious.txt').write_text('\n'.join(PAYLOADS))
                (path / 'extra.json').write_text(json.dumps({'auth': SECRET}))
                comment = self.workflow(name)['jobs']['post-comment']
                self.assertEqual(comment['permissions'], {'contents': 'read', 'pull-requests': 'write'})
                self.assertIn("github.event_name == 'pull_request'", comment['if'])
                self.assertIn('head.repo.full_name == github.repository', comment['if'])
                self.assertEqual(len(comment['steps']), 1)
                step = comment['steps'][0]
                self.assertTrue(step['uses'].startswith('actions/github-script@'))
                script = step['with']['script']
                self.assertNotIn('readFile', script)
                harness = '''const context = {repo: {owner: 'heatmiser', repo: 'ee-builds'}, runId: 12345, issue: {number: 64}};
let posted;
const github = {rest: {issues: {createComment: async request => {posted = request;}}}};
(async () => {
''' + script + '''
process.stdout.write(JSON.stringify(posted));
})().catch(() => {process.stderr.write('Comment contract failed'); process.exit(1);});
'''
                proc = subprocess.run(['node', '-e', harness], cwd=directory, capture_output=True,
                                      text=True, timeout=10, env=dict(os.environ, SYNTHETIC_SECRET=SECRET))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, '')
                request = json.loads(proc.stdout)
                self.assertEqual(request, {'owner': 'heatmiser', 'repo': 'ee-builds', 'issue_number': 64,
                                          'body': 'Build job completed. Bounded check reports are available in the '
                                          '[workflow run](https://github.com/heatmiser/ee-builds/actions/runs/12345).'})
                self.assertNotIn(SECRET, proc.stdout)
                self.assertNotIn(ENCODED, proc.stdout)

    def test_new_sources_and_image_workflows_trigger_offline_validation(self):
        paths = self.workflow('test-ocp-ee.yml')['on']['pull_request']['paths']
        for name in ('.github/scripts/ci_report.py', 'tests/test_ci_report.py',
                     '.github/workflows/pr-image-build.yml', '.github/workflows/push-image-build.yml'):
            self.assertIn(name, paths)


if __name__ == '__main__':
    unittest.main()
