# NVIDIA Kubernetes Launch Kit image

Build a source-pinned l8k container with matching executable metadata, OCI labels
and profile assets. [build-lock.json](build-lock.json) selects the source commit,
version, Linux amd64 platform and digest-pinned builder/runtime images.

This image embeds the selected l8k version, full source commit and build date in
both the executable and OCI labels. These identifiers allow automation to verify
that the packaged binary matches the intended source revision, detect inconsistent
release metadata, and trace deployment artifacts to a specific build.

## Requirements

Bash, Python 3.12+ and Git. Building and image verification also require rootless
Podman. Source retrieval, dependency downloads and image pulls require network
access. Compilation runs inside the builder image; no host Go installation or
cluster credentials are required.

## Usage

Run from this directory. Replace OUTPUT_PARENT with an existing absolute artifact
directory outside this build-definition directory.

Prepare the locked source and record its hashes:

./build.sh --output-parent OUTPUT_PARENT

Build and verify the image in a new run directory:

./build.sh --build --output-parent OUTPUT_PARENT

The build explicitly embeds the version, full source commit and commit timestamp
in the executable and OCI labels. Automatic Go toolchain downloads are disabled;
the build checks that go.mod/go.sum remain unchanged. The final image contains
`/src/l8k`, `/src/profiles` and `/src/LICENSE`.

Verification uses the immutable local image ID. It checks image identity/layout,
executable metadata, supported-release schema and preset inventory. Verification
containers use `--userns=keep-id`, disabled networking and no host mounts.

To verify an existing image, supply its sha256 image ID and the UTC build date
from its original receipt:

python3 verify.py verify --lock build-lock.json --image IMAGE_ID --expected-build-date BUILD_DATE --output-parent OUTPUT_PARENT

## Artifacts and results

Each invocation creates a private directory containing the locked inputs, source
archive/context, source/profile hashes, command logs, receipt and SHA256SUMS.
Failures return nonzero and preserve partial evidence. Existing runs are retained.

| Status | Meaning |
| --- | --- |
| SOURCE_PREPARED | Locked source retrieved, verified and prepared |
| LOCAL_BUILD_VERIFIED | Local image built and runtime checks passed |
| IMAGE_VERIFICATION_PASS | Existing local image passed runtime checks |
| FAIL | Preparation, build or verification failed; inspect retained logs |

The embedded build date is the source commit timestamp; actual execution times
are recorded separately. Bit-for-bit reproducibility and cluster deployment
compatibility are not established by these checks.

## Publication

`build.sh` builds locally and performs no push or pattern-lock update. Obtain the
published repository/platform digests before consuming an image through a pattern
lock; a local image ID is a different identifier.

The ee-builds PR and production workflows invoke `ci-build.sh` for this image.
It runs the offline tests, then calls `build.sh --build` and tags the verified image
ID for the existing publisher. PR builds publish to GHCR; production builds publish
to Quay. Publication requires successful tests, compilation and runtime checks.
The generic shell/toolbox checks are skipped for this distroless image.

Changes anywhere under `k8s-launch-kit/` trigger the image workflows. The PR image
build job is temporarily disabled while public output and comment publication are
hardened; re-enabling it requires explicit review.

The `l8k-build-evidence` artifact contains only `receipt.json`, `summary.txt`,
`commands-k8s-launch-kit.txt` and their `SHA256SUMS`, generated in
`tmp/l8k-ci/public/`. `public-evidence.py` checks successful provenance against the
source lock, validates the image ID/date/digests and selects explicit fields.
Original receipts, command arguments, observed tool output and exception messages
are excluded. Failures export only fixed status/stage/exit-code fields and the
workflow commit. Invalid success metadata fails CI and exports a failure summary.
The receipt precedes registry publication; it establishes no registry digest or
cluster deployment compatibility.

CI captures raw tests/build/tag output in the runner's local results directory;
it does not stream that output or create a whole-directory archive. Raw logs,
source trees, Git metadata, image inspection and test scratch are not uploaded by
these L8K evidence steps. Local operator runs retain their results until explicitly
removed; the scripts perform no automatic cleanup. Artifact retention is 7 days
for PRs and 30 days for production, including the separate command-report upload.
Retention does not govern PR comments or copies already downloaded. Other image
workflows' login/push output and generic command reports require separate review.

Use `build.sh` rather than direct `podman build`, which lacks the prepared context
and required arguments. CI integration does not change the source or base-image
locks.

## Offline tests

python3 -m unittest discover -s tests -p 'test_*.py' -v

Tests mock Git and Podman; they do not download source or build images. Set
L8K_TEST_ROOT to an existing artifact directory to override the default output
location. Publication tests run the shell wrapper/exporter with synthetic secrets
and stand-ins for tests/build/Podman, covering success and failed stages without
building containers. Check build.sh and ci-build.sh with bash -n and ShellCheck.
