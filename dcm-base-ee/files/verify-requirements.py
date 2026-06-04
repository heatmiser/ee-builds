#!/usr/bin/env python3
"""
verify-requirements.py

Validates ansible-builder's introspect output against a pre-verified hash
manifest, then REPLACES /tmp/src/requirements.txt with the complete manifest
so pip runs against a fully pinned, fully hashed lockfile (including transitive
deps that introspect does not enumerate).

Fails the build if any package discovered by introspect is absent from the
manifest — ensuring collection updates that introduce new dependencies are
caught before any unvetted code is pulled.

Usage: verify-requirements.py <introspect-path> <manifest-path>
"""
import re
import sys

INDEX_URL = 'https://pypi.org/simple/'


def normalize(name):
    """Canonical package name: lowercase, underscores to hyphens."""
    return name.lower().replace('_', '-')


def parse_requirement_name(line):
    """Extract normalized package name from a requirement line.

    Handles:
      jsonschema                         -> jsonschema
      jsonschema  # comment              -> jsonschema
      netaddr>=0.10.1  # comment         -> netaddr
      kubernetes>=24.2.0                 -> kubernetes
      requests==2.32.3                   -> requests
    """
    line = line.split('#')[0].strip()
    if not line:
        return None
    name = re.split(r'[>=<!~\[\s]', line)[0].strip()
    return normalize(name) if name else None


def load_manifest(path):
    """Parse verified-packages.txt.

    Returns: {normalized_name: (pinned_spec, ['--hash=sha256:...', ...])}
    pinned_spec is the full 'name==version' string used in output.
    """
    manifest = {}
    current_name = None
    current_spec = None
    current_hashes = []

    with open(path) as fh:
        for raw in fh:
            line = raw.rstrip()
            stripped = line.rstrip('\\').strip()

            if not stripped or stripped.startswith('#'):
                continue
            if stripped.startswith('--index-url') or stripped == '--require-hashes':
                continue
            if stripped.startswith('--hash='):
                if current_name is None:
                    sys.exit(f'FATAL: orphaned hash line in manifest: {stripped}')
                current_hashes.append(stripped)
            else:
                if current_name is not None:
                    manifest[current_name] = (current_spec, current_hashes)
                if '==' not in stripped:
                    sys.exit(f'FATAL: manifest entry must be pinned (name==version): {stripped}')
                current_spec = stripped
                current_name = normalize(stripped.split('==')[0])
                current_hashes = []

    if current_name is not None:
        manifest[current_name] = (current_spec, current_hashes)

    return manifest


def load_introspect(path):
    """Read introspect output; returns deduplicated list of normalized package names."""
    seen = set()
    packages = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('-'):
                continue
            name = parse_requirement_name(line)
            if name and name not in seen:
                seen.add(name)
                packages.append(name)
    return packages


def main():
    if len(sys.argv) != 3:
        sys.exit(f'Usage: {sys.argv[0]} <introspect-requirements.txt> <verified-packages.txt>')

    introspect_path = sys.argv[1]
    manifest_path = sys.argv[2]

    manifest = load_manifest(manifest_path)
    print(f'[verify] manifest loaded: {len(manifest)} verified packages', flush=True)

    packages = load_introspect(introspect_path)
    print(f'[verify] introspect output: {len(packages)} unique packages requested', flush=True)

    missing = [p for p in packages if p not in manifest]
    if missing:
        print('FATAL: packages not in verified manifest — vet and add them first:', file=sys.stderr)
        for p in missing:
            print(f'  {p}', file=sys.stderr)
        sys.exit(1)

    # Replace introspect output with the complete manifest so pip receives
    # all packages (direct + transitive) pinned with hashes.
    lines = [
        f'--index-url {INDEX_URL}',
        '--require-hashes',
        '',
    ]
    for name, (spec, hashes) in manifest.items():
        if not hashes:
            sys.exit(f'FATAL: "{name}" is in the manifest but has no hashes')
        lines.append(f'{spec} \\')
        for h in hashes[:-1]:
            lines.append(f'    {h} \\')
        lines.append(f'    {hashes[-1]}')
        lines.append('')

    with open(introspect_path, 'w') as fh:
        fh.write('\n'.join(lines) + '\n')

    print(
        f'[verify] {introspect_path} replaced with complete lockfile: '
        f'{len(manifest)} packages with hash enforcement',
        flush=True,
    )


if __name__ == '__main__':
    main()
