#!/usr/bin/env python3
"""Run local image checks and export metadata-only reports for public CI artifacts.

Raw command output stays in private local files. Reports never include versions,
collection listings, environment dumps, exception strings or command output.
"""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
TOOL_CHECK = ('jq --version && yq --version && rg --version && '
              """python3 -c 'import jc; print("jc:", jc.__version__)'""")


def require(condition):
    if not condition:
        raise ValueError('Invalid CI report input')


def generate(kind, local_image, reference, commit, output, run=None, *, network=None, container_env=None):
    run = run or subprocess.run
    require(kind in ('ee', 'image'))
    match = re.fullmatch(r'([a-z0-9][a-z0-9_-]{0,127}):([A-Za-z0-9_][A-Za-z0-9_.-]{0,127})', local_image)
    require(match is not None and match[1] != 'k8s-launch-kit')
    image = match[1]
    require(re.fullmatch(r'(?:quay\.io|ghcr\.io)/[a-z0-9_-]+/' + re.escape(local_image), reference))
    require(re.fullmatch(r'[a-f0-9]{40}', commit))
    base = ['podman', 'run', '--rm', '--pull=never']
    require(network in (None, 'none'))
    require(container_env is None or isinstance(container_env, dict))
    if network is not None:
        base += ['--network=' + network]
    for name, value in (container_env or {}).items():
        require(isinstance(name, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name))
        require(isinstance(value, str) and '\x00' not in value and '\n' not in value and '\r' not in value)
    for name, value in sorted((container_env or {}).items()):
        base += ['--env', name + '=' + value]
    if kind == 'ee':
        checks = [('ansible-collections', base + [local_image, 'ansible-galaxy', 'collection', 'list']),
                  ('ansible-version', base + [local_image, 'ansible', '--version'])]
    else:
        config = ROOT / image / 'image.yaml'
        require(not config.is_symlink() and config.is_file())
        cli = re.findall(r'(?m)^cli_binary:[ \t]*([A-Za-z][A-Za-z0-9_-]{0,63})[ \t]*$', config.read_text())
        require(len(cli) == 1)
        checks = [('cli-version', base + ['--entrypoint', cli[0], local_image, '--version']),
                  ('tool-spot-check', base + ['--entrypoint', 'sh', local_image, '-c', TOOL_CHECK])]
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    logs = output / 'private-logs'
    logs.mkdir(mode=0o700)
    results = []
    for name, argv in checks:
        with (logs / f'{name}.stdout').open('xb') as stdout, (logs / f'{name}.stderr').open('xb') as stderr:
            try:
                result = run(argv, stdout=stdout, stderr=stderr, check=False, timeout=120)
                status = 'PASS' if result.returncode == 0 else 'FAIL'
            except (OSError, subprocess.SubprocessError, ValueError):
                status = 'ERROR'
        results.append((name, status))
        if status != 'PASS':
            break
    report = (f'### Local image checks\n\nImage reference: `{reference}`\nSource commit: `{commit}`\n\n'
              + ''.join(f'- {name}: {status}\n' for name, status in results)
              + '\nThese checks precede publication and establish no registry digest or deployment qualification.\n')
    (output / f'commands-{image}.txt').write_text(report)
    return 0 if all(status == 'PASS' for _, status in results) else 1


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('kind', 'local-image', 'reference', 'commit'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = generate(args.kind, args.local_image, args.reference, args.commit, args.output)
        if result:
            print('CI checks failed; raw output suppressed.', file=sys.stderr)
        else:
            print('CI report written with bounded metadata only.')
        return result
    except (ValueError, TypeError, OSError, subprocess.SubprocessError):
        print('CI report failed; command output suppressed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
