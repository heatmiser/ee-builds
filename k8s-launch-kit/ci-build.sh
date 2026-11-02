#!/usr/bin/env bash
# Build and verify before handing local tags to the workflow's publisher.
set -euo pipefail
umask 077

if [[ $# -ne 4 ]]; then
  echo "Usage: $0 ABSOLUTE_RESULTS_DIRECTORY IMAGE TAG WORKFLOW_COMMIT" >&2
  exit 2
fi
artifact_root=$1
image=$2
image_tag=$3
workflow_commit=$4
definition_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
[[ $artifact_root == /* && $image == k8s-launch-kit &&
   $image_tag =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ &&
   $workflow_commit =~ ^[0-9a-f]{40}$ ]] || {
  echo 'Require an absolute results path, k8s-launch-kit, a valid tag and full workflow commit.' >&2
  exit 2
}
python3 -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ is required"'
mkdir -p -- "$(dirname -- "$artifact_root")"
artifact_parent=$(cd -- "$(dirname -- "$artifact_root")" && pwd -P)
artifact_root="$artifact_parent/$(basename -- "$artifact_root")"
[[ $artifact_root != "$definition_root" && $artifact_root != "$definition_root/"* ]] || {
  echo 'Results must be outside the build-definition directory.' >&2
  exit 2
}
# A fresh directory prevents stale successful receipts from authorizing tags.
mkdir -m 0700 -- "$artifact_root"

finish() {
  local result=$?
  trap - EXIT
  set +e
  printf '%s\n' "$result" > "$artifact_root/ci-exit-code.txt"
  # Preserve source symlinks, private modes and hidden files in one artifact.
  if tar -czf "$artifact_root.tar.gz" -C "$artifact_root" .; then
    (cd -- "$artifact_parent" && sha256sum "$(basename -- "$artifact_root").tar.gz") \
      > "$artifact_root.tar.gz.sha256" || result=1
  else
    result=1
  fi
  exit "$result"
}
trap finish EXIT
mkdir -m 0700 -- "$artifact_root/tests" "$artifact_root/builds"
cd -- "$definition_root"
L8K_TEST_ROOT="$artifact_root/tests" \
  python3 -m unittest discover -s tests -p test_build.py -v 2>&1 | tee "$artifact_root/tests.log"
./build.sh --build --output-parent "$artifact_root/builds" 2>&1 | tee "$artifact_root/build.log"

# The build script verifies the immutable ID; never select an image by tag here.
image_id=$(python3 - "$artifact_root/builds" <<'PY'
import json
from pathlib import Path
import re
import sys

receipts = list(Path(sys.argv[1]).glob('l8k-build-*/receipt.json'))
if len(receipts) != 1:
    raise SystemExit('Expected exactly one fresh build receipt.')
receipt = json.loads(receipts[0].read_text())
if receipt.get('status') != 'LOCAL_BUILD_VERIFIED':
    raise SystemExit('A verified build is required before tagging.')
image = receipt.get('imageID', '')
if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
    raise SystemExit('Missing immutable image ID.')
print(image)
PY
)
podman tag "$image_id" "$image:$image_tag"
podman tag "$image_id" "$image:$workflow_commit"

python3 - "$artifact_root/builds" "$workflow_commit" > "commands-$image.txt" <<'PY'
import json
from pathlib import Path
import sys

receipt = json.loads(next(Path(sys.argv[1]).glob('l8k-build-*/receipt.json')).read_text())
print('### k8s-launch-kit\n')
print(f'- Source version: {receipt["version"]}')
print(f'- Source commit: {receipt["sourceRevision"]}')
print(f'- Embedded build date: {receipt["buildDate"]}')
print(f'- Verified local image ID: {receipt["imageID"]}')
print(f'- Workflow commit: {sys.argv[2]}')
print('- Checksummed build evidence: l8k-build-evidence artifact')
print('\nRuntime qualification does not establish cluster deployment compatibility.')
PY
cp -- "commands-$image.txt" "$artifact_root/"
