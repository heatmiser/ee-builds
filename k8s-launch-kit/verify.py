"""Pinned l8k source preparation, local Podman builds and credential-free checks."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parent


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_source(archive, source):
    """Extract regular entries first, then validate internal relative symlinks."""
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        links = {}
        for member in members:
            path = Path(member.name)
            require(not path.is_absolute() and '..' not in path.parts
                    and (member.isfile() or member.isdir() or member.issym()),
                    'Unsafe source archive member.')
            if member.issym():
                target = member.linkname
                normalized = posixpath.normpath(posixpath.join(str(path.parent), target))
                require(target and not Path(target).is_absolute()
                        and normalized != '..' and not normalized.startswith('../'),
                        f'Unsafe source symlink: {member.name}')
                name = path.as_posix()
                require(name not in links, 'Duplicate source symlink.')
                links[name] = target
        # Never extract entries through an archive-provided symlink parent.
        for member in members:
            require(not any(str(parent) in links for parent in Path(member.name).parents),
                    f'Source archive member has a symlink parent: {member.name}')
        tar.extractall(source, members=[m for m in members if not m.issym()], filter='data')
    # Retained regular entries must already be private if link validation fails.
    for p in source.rglob('*'):
        p.chmod(0o700 if p.is_dir() or p.stat().st_mode & 0o111 else 0o600)
    created = []
    try:
        for name, target in links.items():
            link = source / name
            link.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            link.symlink_to(target)
            created.append(link)
        for link in created:
            # strict resolution rejects dangling targets and cycles; the final
            # containment check also catches escapes through another symlink.
            require(link.resolve(strict=True).is_relative_to(source),
                    f'Source symlink resolves outside the source tree: {link.relative_to(source)}')
    except Exception:
        # These are only links created above, with no symlink parents. Remove
        # them before failure handling hashes or inspects the partial context.
        for link in created:
            link.unlink()
        raise
    return links


def load_lock(path):
    lock = json.loads(path.read_text())
    source = lock['source']
    require(lock.get('schemaVersion') == 1, 'Unsupported lock schema.')
    require(source.get('repository') == 'https://github.com/NVIDIA/k8s-launch-kit.git',
            'Require the NVIDIA source repository.')
    require(re.fullmatch(r'[0-9a-f]{40}', source.get('revision', '')), 'Require a full source commit.')
    require(re.fullmatch(r'v[0-9][0-9A-Za-z.+-]*', source.get('version', '')), 'Invalid source version.')
    for key in ('builderImage', 'runtimeImage'):
        require(re.fullmatch(r'[a-z0-9][a-zA-Z0-9./:_-]*@sha256:[0-9a-f]{64}', lock.get(key, '')),
                f'{key} must be pinned by digest.')
    require(lock.get('platform') == 'linux/amd64', 'Only linux/amd64 is implemented.')
    require(lock.get('entrypoint') == '/src/l8k' and lock.get('workingDirectory') == '/src',
            'Unsupported image layout.')
    require(lock.get('publication') == 'local-only', 'Only local builds are implemented.')
    return lock


class Run:
    def __init__(self, parent, prefix, lock_path):
        parent = Path(parent).resolve(strict=True)
        require(parent.is_dir(), 'Output parent must be an existing directory.')
        require(parent != ROOT and ROOT not in parent.parents, 'Output must be outside the build definition.')
        self.path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
        self.commands = []
        self.record = {'schemaVersion': 1, 'status': 'FAIL', 'reason': '',
                       'startedUTC': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       'deploymentQualified': False, 'published': False}
        (self.path / 'build-lock.json').write_bytes(lock_path.read_bytes())
        self.record['lockSHA256'] = sha(self.path / 'build-lock.json')
        print(f'Results directory: {self.path}', flush=True)

    def capture(self, name, argv, stdout=None):
        out_path = self.path / (stdout or f'{name}.stdout')
        err_path = self.path / f'{name}.stderr'
        with out_path.open('wb') as out, err_path.open('wb') as err:
            proc = subprocess.run(argv, stdout=out, stderr=err, check=False,
                                  env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))
        self.commands.append({'name': name, 'argv': argv, 'exitCode': proc.returncode,
                              'stdout': out_path.relative_to(self.path).as_posix(),
                              'stderr': err_path.relative_to(self.path).as_posix()})
        require(proc.returncode == 0, f'{name} failed; inspect retained stderr.')
        return out_path

    def finish(self):
        self.record['commands'] = self.commands
        self.record['finishedUTC'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        (self.path / 'receipt.json').write_text(json.dumps(self.record, indent=2) + '\n')
        files = sorted(p for p in self.path.rglob('*') if p.is_file() and p != self.path / 'SHA256SUMS')
        (self.path / 'SHA256SUMS').write_text(''.join(
            f'{sha(p)}  {p.relative_to(self.path).as_posix()}\n' for p in files))
        print(f'Result: {self.record["status"]}\nChecksums: {self.path / "SHA256SUMS"}', flush=True)


def inspect_runtime(run, lock, image, expected_date):
    require(re.fullmatch(r'sha256:[0-9a-f]{64}', image), 'Verify by immutable local image ID.')
    require(re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', expected_date), 'Invalid expected build date.')
    source = lock['source']
    inspected = json.loads(run.capture('image-inspect', ['podman', 'image', 'inspect', image]).read_text())
    require(isinstance(inspected, list) and len(inspected) == 1, 'Expected one inspected image.')
    info = inspected[0]
    config = info.get('Config') or {}
    labels = info.get('Labels') or config.get('Labels') or {}
    require(info.get('Id', '').removeprefix('sha256:') == image.removeprefix('sha256:'), 'Image ID mismatch.')
    require(info.get('Os') == 'linux' and info.get('Architecture') == 'amd64', 'Image platform mismatch.')
    require(labels.get('org.opencontainers.image.version') == source['version'], 'Image version label mismatch.')
    require(labels.get('org.opencontainers.image.revision') == source['revision'], 'Image source label mismatch.')
    require(labels.get('org.opencontainers.image.created') == expected_date, 'Image build-date label mismatch.')
    require(config.get('Entrypoint') == [lock['entrypoint']], 'Image entrypoint mismatch.')
    require(config.get('WorkingDir') == lock['workingDirectory'], 'Image working directory mismatch.')
    base = ['podman', 'run', '--rm', '--pull=never', '--userns=keep-id', '--network=none',
            '--platform', lock['platform'], '--workdir', lock['workingDirectory'],
            '--env', 'HOME=/tmp', '--entrypoint', lock['entrypoint'], image]
    version = json.loads(run.capture('version', base + ['version', '--output', 'json']).read_text())
    # Record observed values before gating so default/mismatched metadata survives failure.
    run.record['observedVersion'] = version
    require(version.get('version') == source['version'], 'Binary version mismatch.')
    require(version.get('gitCommit') == source['revision'], 'Binary must report the full locked source commit.')
    require(version.get('buildDate') == expected_date, 'Binary build date mismatch.')
    schema = json.loads(run.capture('schema', base + ['schema']).read_text())
    releases = schema.get('supportedNetworkOperatorReleases')
    require(schema.get('version') == source['version'], 'Schema version mismatch.')
    require(isinstance(releases, list) and len(releases) > 0
            and all(isinstance(x, str) and x for x in releases), 'Missing supported-release catalog.')
    run.record['supportedNetworkOperatorReleases'] = releases
    presets = run.capture('presets', base + ['preset', 'list']).read_text()
    require('Available presets (' in presets and not (run.path / 'presets.stderr').read_text().strip(),
            'Missing or erroneous preset inventory.')
    run.record['imageID'] = image
    run.record['status'] = 'IMAGE_VERIFICATION_PASS'


def prepare_and_build(run, lock, build):
    src = lock['source']
    context = run.path / 'context'
    context.mkdir(mode=0o700)
    definitions = ['Containerfile', '.containerignore', 'build-lock.json', 'build.sh', 'verify.py']
    run.record['definitionSHA256'] = {name: sha(ROOT / name) for name in definitions}
    for name in ('Containerfile', '.containerignore'):
        shutil.copyfile(ROOT / name, context / name)
    git_dir = run.path / 'source.git'
    run.capture('git-init', ['git', 'init', '--bare', str(git_dir)])
    git = ['git', '--git-dir', str(git_dir)]
    run.capture('git-fetch', git + ['fetch', '--no-tags', '--depth=1', src['repository'], src['revision']])
    resolved = run.capture('source-revision', git + ['rev-parse', '--verify', 'FETCH_HEAD^{commit}']).read_text().strip()
    require(resolved == src['revision'], 'Fetched source does not match the locked commit.')
    epoch = int(run.capture('source-time', git + ['show', '-s', '--format=%ct', resolved]).read_text().strip())
    build_date = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    run.record.update(sourceRevision=resolved, version=src['version'], buildDate=build_date,
                      buildDatePolicy='source-commit-time', platform=lock['platform'])
    archive = run.capture('source-archive', git + ['archive', '--format=tar', resolved], stdout='source.tar')
    run.record['sourceArchiveSHA256'] = sha(archive)
    source = context / 'source'
    source.mkdir(mode=0o700)
    links = extract_source(archive, source)
    run.record['sourceLinks'] = links
    for name in ('go.mod', 'go.sum', 'Makefile', 'LICENSE', 'profiles'):
        require((source / name).exists(), f'Source missing required asset: {name}')
    files = {p.relative_to(source).as_posix(): sha(p) for p in sorted(source.rglob('*'))
             if p.is_file() and not p.is_symlink()}
    run.record['sourceTreeDigest'] = hashlib.sha256(
        json.dumps({'files': files, 'links': links}, sort_keys=True).encode()).hexdigest()
    (run.path / 'source-files.json').write_text(json.dumps(files, indent=2) + '\n')
    run.record['profileFiles'] = sum(name.startswith('profiles/') for name in files)
    require(run.record['profileFiles'] > 0, 'Source profile directory is empty.')
    run.record['status'] = 'SOURCE_PREPARED'
    if not build:
        return
    iidfile = run.path / 'image.iid'
    tag = f'localhost/k8s-launch-kit:{src["version"]}-{resolved[:12]}-{run.path.name.rsplit("-", 1)[-1]}'
    argv = ['podman', 'build', '--platform', lock['platform'], '--file', str(context / 'Containerfile'),
            '--iidfile', str(iidfile), '--tag', tag]
    for key, value in {'BUILDER_IMAGE': lock['builderImage'], 'RUNTIME_IMAGE': lock['runtimeImage'],
                       'L8K_VERSION': src['version'], 'L8K_SOURCE_REVISION': resolved,
                       'L8K_BUILD_DATE': build_date}.items():
        argv.extend(['--build-arg', key + '=' + value])
    run.record.update(localTag=tag, builderImage=lock['builderImage'], runtimeImage=lock['runtimeImage'])
    # SOURCE_PREPARED is not a successful build if Podman or later checks fail.
    run.record['status'] = 'FAIL'
    run.capture('image-build', argv + [str(context)])
    image = iidfile.read_text().strip()
    run.record['imageID'] = image
    inspect_runtime(run, lock, image, build_date)
    run.record['status'] = 'LOCAL_BUILD_VERIFIED'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='mode', required=True)
    build_parser = subs.add_parser('build', help='Prepare source; --build also builds and verifies locally')
    build_parser.add_argument('--output-parent', required=True)
    build_parser.add_argument('--build', action='store_true')
    verify_parser = subs.add_parser('verify', help='Verify a previously built local image by ID')
    verify_parser.add_argument('--output-parent', required=True)
    verify_parser.add_argument('--lock', required=True)
    verify_parser.add_argument('--image', required=True)
    verify_parser.add_argument('--expected-build-date', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    lock_path = (ROOT / 'build-lock.json') if args.mode == 'build' else Path(args.lock).resolve(strict=True)
    lock = load_lock(lock_path)
    for utility in (('git', 'podman') if args.mode == 'build' and args.build else
                    ('git',) if args.mode == 'build' else ('podman',)):
        require(shutil.which(utility), f'Missing utility: {utility}')
    run = Run(args.output_parent, 'l8k-build-' if args.mode == 'build' else 'l8k-image-verification-', lock_path)
    try:
        if args.mode == 'build':
            prepare_and_build(run, lock, args.build)
        else:
            inspect_runtime(run, lock, args.image, args.expected_build_date)
    except Exception as exc:
        run.record.update(status='FAIL', reason=str(exc))
        print(f'Reason: {exc}', file=sys.stderr)
    finally:
        run.finish()
    return 0 if run.record['status'] in ('SOURCE_PREPARED', 'LOCAL_BUILD_VERIFIED', 'IMAGE_VERIFICATION_PASS') else 1


if __name__ == '__main__':
    sys.exit(main())
