#!/usr/bin/env python3
"""Export allowlisted L8K CI provenance without publishing raw build evidence."""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent
STAGES = ('setup', 'tests', 'build', 'receipt', 'tag', 'complete')
PUBLIC_FILES = ('receipt.json', 'summary.txt', 'commands-k8s-launch-kit.txt', 'SHA256SUMS')


def require(condition):
    if not condition:
        raise ValueError('Invalid public evidence input')


def matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def read_json(path):
    require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 1024 * 1024)
    data = json.loads(path.read_text())
    require(isinstance(data, dict))
    return data


def verified_build(results, lock_path):
    """Select exact metadata; original receipts also contain unpublishable data."""
    lock = read_json(lock_path)
    source = lock['source']
    require(lock['schemaVersion'] == 1 and lock['platform'] == 'linux/amd64')
    require(source['repository'] == 'https://github.com/NVIDIA/k8s-launch-kit.git')
    require(matches(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?', source['version']))
    require(matches(r'[a-f0-9]{40}', source['revision']))
    for key in ('builderImage', 'runtimeImage'):
        require(matches(r'[a-z0-9][a-zA-Z0-9./:_-]*@sha256:[a-f0-9]{64}', lock[key]))
    builds = results / 'builds'
    require(not results.is_symlink() and not builds.is_symlink() and builds.is_dir())
    candidates = list(builds.glob('l8k-build-*/receipt.json'))
    require(len(candidates) == 1 and not candidates[0].parent.is_symlink())
    raw = read_json(candidates[0])
    require(raw['schemaVersion'] == 1 and raw['status'] == 'LOCAL_BUILD_VERIFIED'
            and raw['deploymentQualified'] is False and raw['published'] is False)
    require(raw['version'] == source['version'] and raw['sourceRevision'] == source['revision'])
    lock_hash = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    require(raw['lockSHA256'] == lock_hash)
    require(raw['builderImage'] == lock['builderImage'] and raw['runtimeImage'] == lock['runtimeImage'])
    require(matches(r'[0-9]{4}(?:-[0-9]{2}){2}T[0-9]{2}(?::[0-9]{2}){2}Z', raw['buildDate']))
    datetime.datetime.strptime(raw['buildDate'], '%Y-%m-%dT%H:%M:%SZ')
    require(matches(r'sha256:[a-f0-9]{64}', raw['imageID']))
    require(matches(r'[a-f0-9]{64}', raw['sourceTreeDigest']))
    return {'version': source['version'], 'sourceRevision': source['revision'],
            'buildDate': raw['buildDate'], 'imageID': raw['imageID'], 'lockSHA256': lock_hash,
            'sourceTreeDigest': raw['sourceTreeDigest'], 'platform': 'linux/amd64',
            'builderImage': lock['builderImage'], 'runtimeImage': lock['runtimeImage']}


def export_evidence(results, workflow_commit, stage, exit_code, lock_path=ROOT / 'build-lock.json'):
    require(matches(r'[a-f0-9]{40}', workflow_commit) and stage in STAGES)
    require(type(exit_code) is int and 0 <= exit_code <= 255)
    require(results.is_dir() and not results.is_symlink())
    record = {'schemaVersion': 1, 'status': 'CI_FAIL', 'stage': stage, 'exitCode': exit_code,
              'workflowCommit': workflow_commit, 'deploymentQualified': False, 'published': False}
    if exit_code == 0:
        try:
            require(stage == 'complete')
            record['verifiedBuild'] = verified_build(results, lock_path)
            record['status'] = 'CI_PASS'
        except (ValueError, KeyError, TypeError, OSError):
            record.update(stage='receipt', exitCode=1)
    public = results / 'public'
    # Never reuse a directory that could contain stale or unreviewed files.
    public.mkdir(mode=0o700, exist_ok=False)
    (public / 'receipt.json').write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')
    summary = (f'L8K CI: {record["status"]}\nStage: {record["stage"]}\n'
               f'Exit code: {record["exitCode"]}\nWorkflow commit: {workflow_commit}\n')
    (public / 'summary.txt').write_text(summary)
    report = '### k8s-launch-kit\n\n' + summary + '\n'
    if record['status'] == 'CI_PASS':
        build = record['verifiedBuild']
        report += (f'- Source version: {build["version"]}\n'
                   f'- Source commit: {build["sourceRevision"]}\n'
                   f'- Embedded build date: {build["buildDate"]}\n'
                   f'- Verified local image ID: {build["imageID"]}\n')
    report += ('\nAllowlisted CI evidence: l8k-build-evidence artifact.\n'
               'This is pre-publication evidence; no registry digest is established.\n'
               'Runtime qualification does not establish cluster deployment compatibility.\n')
    (public / 'commands-k8s-launch-kit.txt').write_text(report)
    (public / 'SHA256SUMS').write_text(''.join(
        f'{hashlib.sha256((public / name).read_bytes()).hexdigest()}  {name}\n'
        for name in PUBLIC_FILES[:-1]))
    return record['exitCode']


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('image-id', 'verified-json', 'export'))
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--workflow-commit')
    parser.add_argument('--stage')
    parser.add_argument('--exit-code', type=int)
    args = parser.parse_args()
    try:
        if args.mode == 'image-id':
            print(verified_build(args.results, ROOT / 'build-lock.json')['imageID'])
            return 0
        if args.mode == 'verified-json':
            print(json.dumps(verified_build(args.results, ROOT / 'build-lock.json'), sort_keys=True))
            return 0
        return export_evidence(args.results, args.workflow_commit, args.stage, args.exit_code)
    except (ValueError, KeyError, TypeError, OSError):
        print('L8K public evidence rejected; details suppressed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
