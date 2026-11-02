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

Changes anywhere under `k8s-launch-kit/` trigger the image workflows. The
`l8k-build-evidence` artifact contains a checksummed archive of test/build logs,
source inputs and receipts, including partial evidence after failure. Its build
receipt describes verification before publication; it is not a registry digest or
a publication receipt.

Use `build.sh` rather than direct `podman build`, which lacks the prepared context
and required arguments. CI integration does not change the source or base-image
locks.

## Offline tests

python3 -m unittest discover -s tests -p test_build.py -v

Tests mock Git and Podman; they do not download source or build images. Set
L8K_TEST_ROOT to an existing artifact directory to override the default output
location. Check build.sh with bash -n and ShellCheck.
