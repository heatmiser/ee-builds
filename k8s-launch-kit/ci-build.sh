#!/usr/bin/env bash
# Keep raw evidence local; export only validated provenance for publication.
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

stage=setup

finish() {
  local result=$?
  trap - EXIT
  set +e
  printf '%s\n' "$result" > "$artifact_root/ci-exit-code.txt"
  if ! python3 "$definition_root/public-evidence.py" export \
    --results "$artifact_root" --workflow-commit "$workflow_commit" \
    --stage "$stage" --exit-code "$result" > "$artifact_root/export.log" 2>&1; then
    if [[ $result == 0 ]]; then result=1; fi
  fi
  if [[ $result == 0 ]]; then
    echo 'L8K CI completed; validated public evidence prepared.'
  else
    echo 'L8K CI failed; raw output suppressed. See bounded public evidence if available.' >&2
  fi
  exit "$result"
}
trap finish EXIT
mkdir -m 0700 -- "$artifact_root/tests" "$artifact_root/builds"
cd -- "$definition_root"
stage=tests
L8K_TEST_ROOT="$artifact_root/tests" \
  python3 -m unittest discover -s tests -p 'test_*.py' -v > "$artifact_root/tests.log" 2>&1
stage=build
./build.sh --build --output-parent "$artifact_root/builds" > "$artifact_root/build.log" 2>&1

# Validate the original receipt against the source lock before selecting an ID.
stage=receipt
python3 "$definition_root/public-evidence.py" image-id --results "$artifact_root" \
  > "$artifact_root/image-id.txt" 2> "$artifact_root/receipt-check.log"
image_id=$(< "$artifact_root/image-id.txt")
stage=tag
{
  podman tag "$image_id" "$image:$image_tag"
  podman tag "$image_id" "$image:$workflow_commit"
} > "$artifact_root/tag.log" 2>&1
stage=complete
