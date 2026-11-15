# OpenShift 4.22 execution environment builds

One definition builds exact OpenShift 4.22 patch releases. Client and installer
archives come from the same version-specific OpenShift mirror directory. The
shared helper verifies both SHA256 checksums before staging them, and the image
checks them again before extraction. The checksum manifest is fetched over HTTPS
from the same mirror; it is not an independent signature verification.

## Build selected patches

Run **Versioned OpenShift 4.22 EE Build** in GitHub Actions, selecting the reviewed
source ref. Set `versions` to a comma-separated list such as `4.22.14,4.22.22`.
Supply a published `dcm-base-ee` reference. Preparation resolves that base once;
every matrix entry uses the same immutable digest. No base rebuild is required.

| Selection | Release alias | Unique build tag | Changes latest |
|---|---|---|---|
| `4.22.14` | `:14` | `:14-<commit12>-<run-id>-<attempt>` | No |
| `4.22.22` | `:22` | `:22-<commit12>-<run-id>-<attempt>` | No |
| Existing stable production build | Resolved patch, e.g. `:22` | Same patch-prefixed format | Yes |
| Existing stable PR build | None | `:22-pr-<pr>-<commit12>-<run-id>-<attempt>` | No |

All tags are in `quay.io/<namespace>/dcm-ocp-ee-4.22`, except PR tags, which go
to GHCR. The patch alias can move when the same patch is rebuilt with an updated
base. Unique build tags distinguish those builds, but registry tags themselves
are not immutable. Deployments should use the published `@sha256:...` reference.
The helper removes the inherited `quay.expires-after` label and verifies its
absence. Registry administrators' other retention/deletion policies still apply.

Explicit version builds never promote `latest`. Existing dependent production
builds resolve `stable-4.22/release.txt` once, then fetch both archives and checksums
from the resulting exact release directory. Only that stable production path
updates `latest`. Other minor-version definitions retain their existing behavior.
Production 4.22 jobs share a concurrency group; submit one manual release batch at
a time because GitHub concurrency permits only one pending job per group.

## Local preparation and build

From the repository root, with CI-pinned Ansible Builder installed, Podman
available, and registry authentication already established:

python3 .github/scripts/ocp_ee.py build --release 4.22.22 --base quay.io/s4v0/dcm-base-ee:latest --commit "$(git rev-parse HEAD)" --run-id 12345-1 --output /tmp/ocp-ee-22

Use a fresh output directory and a distinct numeric run/attempt identity for each
build. The helper stages a private copy of the definition; source files are not
edited. It passes `OCP_VERSION` and digest-pinned `EE_BASE_IMAGE` build arguments.
Direct `ansible-builder build` in the source directory is no longer the supported
entry point: its required verified release files are staged by the helper.

Build and publication are separate commands. Only invoke publication after review:

python3 .github/scripts/ocp_ee.py publish --receipt /tmp/ocp-ee-22/receipt.json --registry quay.io/s4v0

Before a receipt is marked verified, the final image must report the requested
installer and client versions, an immutable amd64 release payload, the exact
`ocp.version` label, and no expiry label. Publication rechecks tool versions and
pushes the verified image ID, then records each successful tag and digest. A
partial publication preserves completed pushes; do not blindly retry a receipt
with existing push evidence. The receipt records archive checksums, source SHA,
base digest, release payload, and run identity. Failed build output remains in
`build.log`; unverified builds cannot publish.

## Verification and automation

python3 -m unittest discover -s tests -p test_ocp_ee.py -v

The dedicated test workflow requires the CI-pinned Builder and renders a real
Containerfile with fixture archives, without building an image or using a
registry. Offline tests exercise checksums, version mismatches, matrix inputs,
tag isolation, publication failures, and workflow routing. An actual image build
and runtime qualification remain required before accepting the release image.

The existing general matrix excludes OCP-dependent directories. Consequently,
changing only this definition does not automatically publish a new image. Use the
dedicated versioned workflow for explicit releases; the existing base-dependent
workflow continues to drive stable builds. Do not dispatch this image through the
generic EE build job, which does not stage release inputs.
