"""Offline build contract tests: Git and Podman are mocked, no downloads/builds."""
import copy
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / 'build-lock.json').read_text())
IMAGE_ID = 'sha256:' + 'a' * 64
EPOCH = 1793375168
BUILD_DATE = datetime.datetime.fromtimestamp(EPOCH, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
MOCK = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
tool = Path(sys.argv[0]).name
a = sys.argv[1:]
d = json.loads(Path(os.environ['MOCK_DATA']).read_text())
with open(os.environ['MOCK_CALLS'], 'a') as f:
    f.write(json.dumps([tool] + a) + '\\n')
def fail(name):
    if d.get('fail') == name:
        print('Synthetic failure: ' + name, file=sys.stderr)
        sys.exit(1)
if tool == 'git':
    if a[:2] == ['init', '--bare']:
        Path(a[2]).mkdir()
    else:
        assert a[0] == '--git-dir'
        command = a[2:]
        if command[0] == 'fetch':
            fail('git-fetch')
            assert command == ['fetch', '--no-tags', '--depth=1', d['lock']['source']['repository'], d['lock']['source']['revision']]
        elif command == ['rev-parse', '--verify', 'FETCH_HEAD^{commit}']:
            print(d.get('resolvedRevision', d['lock']['source']['revision']))
        elif command[:3] == ['show', '-s', '--format=%ct']:
            print(d['epoch'])
        elif command[:2] == ['archive', '--format=tar']:
            sys.stdout.buffer.write(Path(os.environ['MOCK_ARCHIVE']).read_bytes())
        else:
            raise AssertionError(repr(a))
elif a[0] == 'build':
    fail('image-build')
    assert '--push' not in a
    assert a[a.index('--platform')+1] == 'linux/amd64'
    Path(a[a.index('--iidfile')+1]).write_text(d['imageID'] + '\\n')
elif a[:2] == ['image', 'inspect']:
    fail('image-inspect')
    assert a[2] == d['imageID']
    print(json.dumps(d['inspect']))
else:
    base = ['run', '--rm', '--pull=never', '--userns=keep-id', '--network=none',
            '--platform', 'linux/amd64', '--workdir', '/src', '--env', 'HOME=/tmp',
            '--entrypoint', '/src/l8k', d['imageID']]
    assert a[:len(base)] == base, repr(a)
    name = {('version', '--output', 'json'): 'version', ('schema',): 'schema',
            ('preset', 'list'): 'presets'}[tuple(a[len(base):])]
    fail(name)
    if name == 'presets':
        print(d['presets'])
        if d.get('presetError'):
            print('Error: synthetic preset failure', file=sys.stderr)
    else:
        print(json.dumps(d[name]))
'''


def write_archive(path, unsafe=False, links=None, special=None):
    content = {'go.mod': 'module example.invalid/test\ngo 1.26.2\n', 'go.sum': 'SYNTHETIC\n',
               'Makefile': 'build:\n\tfalse\n', 'LICENSE': 'SYNTHETIC-LICENSE\n',
               'profiles/synthetic.yaml': 'synthetic: true\n'}
    if unsafe:
        content['../escape'] = 'DO-NOT-EXTRACT'
    if links:
        content.update({'pkg/presets/data/topology.yaml': 'synthetic: true\n',
                        'pkg/networkoperatorplugin/releases/releases.yaml': 'releases: []\n',
                        'docs/readme.txt': 'SYNTHETIC\n', 'dir/content': 'SYNTHETIC\n'})
    with tarfile.open(path, 'w') as tar:
        # Put links before targets to cover forward references in archives.
        for name, target in (links or {}).items():
            member = tarfile.TarInfo(name)
            member.type = tarfile.SYMTYPE
            member.linkname = target
            tar.addfile(member)
        if special:
            member = tarfile.TarInfo('special')
            member.type = tarfile.LNKTYPE if special == 'hardlink' else tarfile.FIFOTYPE
            member.linkname = 'go.mod' if special == 'hardlink' else ''
            tar.addfile(member)
        for name, value in content.items():
            raw = value.encode()
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            member.mode = 0o644
            tar.addfile(member, io.BytesIO(raw))


class BuildContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parent = Path(os.environ.get('L8K_TEST_ROOT', ROOT.parents[1] / 'tmp')).resolve(strict=True)
        cls.work = Path(tempfile.mkdtemp(prefix='l8k-build-tests-', dir=parent))
        cls.bin = cls.work / 'bin'
        cls.bin.mkdir()
        for tool in ('git', 'podman'):
            (cls.bin / tool).write_text(MOCK)
            (cls.bin / tool).chmod(0o700)
        print(f'\nRetained offline evidence: {cls.work}', flush=True)

    def fixture(self):
        source = LOCK['source']
        return {'lock': copy.deepcopy(LOCK), 'imageID': IMAGE_ID, 'epoch': EPOCH,
                'inspect': [{'Id': IMAGE_ID.removeprefix('sha256:'), 'Os': 'linux', 'Architecture': 'amd64',
                             'Labels': {'org.opencontainers.image.version': source['version'],
                                        'org.opencontainers.image.revision': source['revision'],
                                        'org.opencontainers.image.created': BUILD_DATE},
                             'Config': {'Entrypoint': ['/src/l8k'], 'WorkingDir': '/src'}}],
                'version': {'version': source['version'], 'gitCommit': source['revision'], 'buildDate': BUILD_DATE},
                'schema': {'version': source['version'], 'supportedNetworkOperatorReleases': ['26.7']},
                'presets': 'Available presets (embedded):\n  NAME MACHINETYPE GPUTYPE\n  synthetic server gpu'}

    def run_case(self, name, data=None, build=False, unsafe=False, definition=None, extra=None,
                 verify=False, links=None, special=None):
        case = self.work / name
        case.mkdir()
        parent = case / 'results with spaces'
        parent.mkdir()
        fixture = case / 'fixture.json'
        fixture.write_text(json.dumps(data or self.fixture()))
        archive = case / 'fixture.tar'
        write_archive(archive, unsafe, links, special)
        calls_path = case / 'calls.jsonl'
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                   MOCK_DATA=str(fixture), MOCK_ARCHIVE=str(archive), MOCK_CALLS=str(calls_path),
                   KUBECONFIG='SYNTHETIC-SECRET-DO-NOT-MOUNT')
        definition = definition or ROOT
        if verify:
            cmd = [os.environ.get('PYTHON', 'python3'), str(definition / 'verify.py'), 'verify',
                   '--output-parent', str(parent), '--lock', str(definition / 'build-lock.json'),
                   '--image', IMAGE_ID, '--expected-build-date', BUILD_DATE]
        else:
            cmd = ['bash', str(definition / 'build.sh'), '--output-parent', str(parent)]
            if build:
                cmd.append('--build')
        proc = subprocess.run(cmd + (extra or []), env=env, capture_output=True, text=True)
        (case / 'command.stdout').write_text(proc.stdout)
        (case / 'command.stderr').write_text(proc.stderr)
        calls = [json.loads(x) for x in calls_path.read_text().splitlines()] if calls_path.exists() else []
        dirs = list(parent.iterdir())
        receipt = None
        if dirs:
            self.assertEqual(len(dirs), 1)
            dest = dirs[0]
            receipt = json.loads((dest / 'receipt.json').read_text())
            self.assertFalse(receipt['deploymentQualified'])
            self.assertFalse(receipt['published'])
            manifest = {}
            for line in (dest / 'SHA256SUMS').read_text().splitlines():
                digest, relative = line.split('  ', 1)
                manifest[relative] = digest
                self.assertEqual(hashlib.sha256((dest / relative).read_bytes()).hexdigest(), digest)
            self.assertEqual(set(manifest), {p.relative_to(dest).as_posix() for p in dest.rglob('*')
                                            if p.is_file() and p != dest / 'SHA256SUMS'})
            for p in [dest, *dest.rglob('*')]:
                if p.is_symlink():
                    self.assertTrue(p.resolve(strict=True).is_relative_to(dest / 'context/source'))
                    continue  # Linux symlink modes do not describe target access.
                self.assertEqual(stat.S_IMODE(p.stat().st_mode) & 0o077, 0, p)
                if p.is_file():
                    self.assertNotIn(b'SYNTHETIC-SECRET-DO-NOT-MOUNT', p.read_bytes())
        return proc, receipt, calls, dirs

    def test_prepare_only_verifies_source_and_assets_without_podman(self):
        proc, receipt, calls, dirs = self.run_case('prepare')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(receipt['status'], 'SOURCE_PREPARED')
        self.assertTrue(all(c[0] == 'git' for c in calls))
        self.assertEqual(receipt['sourceRevision'], LOCK['source']['revision'])
        self.assertEqual(receipt['profileFiles'], 1)
        self.assertEqual(receipt['buildDate'], BUILD_DATE)
        self.assertTrue((dirs[0] / 'context/source/LICENSE').is_file())

    def test_build_passes_explicit_metadata_and_pins_then_verifies_image_id(self):
        proc, receipt, calls, _ = self.run_case('build', build=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(receipt['status'], 'LOCAL_BUILD_VERIFIED')
        argv = next(c for c in calls if c[:2] == ['podman', 'build'])
        args = {argv[i+1] for i, x in enumerate(argv) if x == '--build-arg'}
        self.assertEqual(args, {'BUILDER_IMAGE=' + LOCK['builderImage'], 'RUNTIME_IMAGE=' + LOCK['runtimeImage'],
                                'L8K_VERSION=' + LOCK['source']['version'],
                                'L8K_SOURCE_REVISION=' + LOCK['source']['revision'], 'L8K_BUILD_DATE=' + BUILD_DATE})
        self.assertTrue(receipt['localTag'].startswith('localhost/k8s-launch-kit:v26.10.0-rc.5-85cda5098e21-'))
        self.assertNotIn('latest', receipt['localTag'])
        runs = [c for c in calls if c[:2] == ['podman', 'run']]
        self.assertEqual(len(runs), 3)
        for c in runs:
            self.assertIn('--network=none', c)
            self.assertIn(IMAGE_ID, c)
            self.assertFalse(any(x in c for x in ('--volume', '-v', '--user', '--privileged')))

    def test_default_binary_metadata_is_retained_and_rejected(self):
        data = self.fixture()
        data['version'].update(version='v0.1.0', gitCommit='dev')
        proc, receipt, calls, _ = self.run_case('defaults', data, build=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(receipt['status'], 'FAIL')
        self.assertEqual(receipt['observedVersion']['gitCommit'], 'dev')
        self.assertEqual(receipt['reason'], 'Binary version mismatch.')
        self.assertFalse(any(c[-1] == 'schema' for c in calls))

    def test_wrong_source_and_archive_traversal_stop_before_build(self):
        data = self.fixture()
        data['resolvedRevision'] = '0' * 40
        for name, fixture, unsafe in [('wrong-source', data, False), ('unsafe-archive', None, True)]:
            proc, receipt, calls, dirs = self.run_case(name, fixture, build=True, unsafe=unsafe)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(receipt['status'], 'FAIL')
            self.assertFalse(any(c[0] == 'podman' for c in calls))
            self.assertFalse((dirs[0] / 'context/escape').exists())

    def test_internal_relative_symlinks_are_preserved_and_recorded(self):
        links = {'presets': 'pkg/presets/data',
                 'releases.yaml': 'pkg/networkoperatorplugin/releases/releases.yaml',
                 'docs/release-alias': '../releases.yaml'}
        proc, receipt, calls, dirs = self.run_case('internal-links', links=links, build=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(receipt['status'], 'LOCAL_BUILD_VERIFIED')
        self.assertEqual(receipt['sourceLinks'], links)
        source = dirs[0] / 'context/source'
        self.assertTrue((source / 'presets').is_dir())
        self.assertTrue((source / 'presets').is_symlink())
        self.assertEqual((source / 'releases.yaml').readlink().as_posix(), links['releases.yaml'])
        self.assertEqual((source / 'docs/release-alias').read_bytes(), (source / 'releases.yaml').read_bytes())
        self.assertTrue(any(c[:2] == ['podman', 'build'] for c in calls))

    def test_escaping_cyclic_dangling_and_special_links_are_rejected(self):
        cases = [('escaping-link', {'alias': '../outside'}, None),
                 ('absolute-link', {'alias': '/etc/passwd'}, None),
                 ('cycle', {'a': 'b', 'b': 'a'}, None),
                 ('dangling', {'alias': 'missing'}, None),
                 ('link-parent', {'profiles': 'pkg/presets/data'}, None),
                 ('normalized-link-parent', {'./profiles': 'pkg/presets/data'}, None),
                 ('indirect-escape', {'dir/up': '..', 'alias': 'dir/up/../outside'}, None),
                 ('hardlink', None, 'hardlink'), ('fifo', None, 'fifo')]
        for name, links, special in cases:
            with self.subTest(name=name):
                proc, receipt, calls, dirs = self.run_case(name, links=links, special=special, build=True)
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(receipt['status'], 'FAIL')
                self.assertFalse(any(c[0] == 'podman' for c in calls))
                self.assertFalse(any(p.is_symlink() for p in (dirs[0] / 'context/source').rglob('*')))

    def test_symlink_targets_are_part_of_source_tree_identity(self):
        a = self.run_case('link-identity-a', links={'alias': 'LICENSE'})[1]
        b = self.run_case('link-identity-b', links={'alias': 'go.sum'})[1]
        self.assertEqual(a['status'], 'SOURCE_PREPARED')
        self.assertEqual(b['status'], 'SOURCE_PREPARED')
        self.assertNotEqual(a['sourceTreeDigest'], b['sourceTreeDigest'])

    def test_unpinned_base_is_rejected_before_any_commands(self):
        definition = self.work / 'unpinned-definition'
        shutil.copytree(ROOT, definition)
        lock = copy.deepcopy(LOCK)
        lock['builderImage'] = 'golang:latest'
        (definition / 'build-lock.json').write_text(json.dumps(lock))
        proc, receipt, calls, _ = self.run_case('unpinned', definition=definition, build=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIsNone(receipt)
        self.assertEqual(calls, [])

    def test_source_and_runtime_failures_preserve_partial_evidence(self):
        for command in ('git-fetch', 'image-build', 'image-inspect', 'version', 'schema', 'presets'):
            with self.subTest(command=command):
                data = self.fixture()
                data['fail'] = command
                proc, receipt, _, _ = self.run_case('failed-' + command, data, build=True)
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(receipt['status'], 'FAIL')
                self.assertEqual(receipt['commands'][-1]['exitCode'], 1)

    def test_image_identity_and_full_binary_commit_are_enforced(self):
        for key in ('image', 'commit', 'date'):
            data = self.fixture()
            if key == 'image':
                data['inspect'][0]['Id'] = 'b' * 64
            elif key == 'commit':
                data['version']['gitCommit'] = LOCK['source']['revision'][:7]
            else:
                data['version']['buildDate'] = '1970-01-01T00:00:00Z'
            proc, receipt, _, _ = self.run_case('identity-' + key, data, build=True)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(receipt['status'], 'FAIL')

    def test_empty_schema_and_silent_preset_failure_cannot_pass(self):
        for key in ('schema', 'presets'):
            data = self.fixture()
            if key == 'schema':
                data['schema']['supportedNetworkOperatorReleases'] = []
            else:
                data.update(presets='No presets found', presetError=True)
            proc, receipt, _, _ = self.run_case('bad-' + key, data, build=True)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(receipt['status'], 'FAIL')

    def test_source_content_and_metadata_repeat_across_preparations(self):
        a = self.run_case('repeat-a')[1]
        b = self.run_case('repeat-b')[1]
        for key in ('sourceTreeDigest', 'sourceArchiveSHA256', 'sourceRevision', 'version', 'buildDate'):
            self.assertEqual(a[key], b[key])

    def test_existing_image_can_be_verified_without_git_or_build(self):
        proc, receipt, calls, _ = self.run_case('verify', verify=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(receipt['status'], 'IMAGE_VERIFICATION_PASS')
        self.assertTrue(all(c[0] == 'podman' and c[1] != 'build' for c in calls))

    def test_no_publication_or_moving_ref_override_is_exposed(self):
        for key, extra in [('push', ['--push']), ('revision', ['--revision', 'main'])]:
            proc, receipt, calls, _ = self.run_case('rejected-' + key, extra=extra)
            self.assertEqual(proc.returncode, 2)
            self.assertIsNone(receipt)
            self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
