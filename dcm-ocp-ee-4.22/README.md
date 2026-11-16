# Versioned OpenShift execution environment builds

One parameterized definition builds exact releases from configured streams. Client and installer
archives come from the same version-specific OpenShift mirror directory. The
shared helper verifies both SHA256 checksums before staging them, and the image
checks them again before extraction. The checksum manifest is fetched over HTTPS
from the same mirror; it is not an independent signature verification.

## Build selected patches

Run **Versioned OpenShift EE Build** in GitHub Actions, selecting the reviewed
source ref. Set `versions` to a comma-separated list such as `4.22.14,4.22.22`.
The list may span configured streams; each requested exact release must exist on
the configured mirror and must pass the final tool checks.
Supply a published `dcm-base-ee` reference. Preparation resolves that base once;
every matrix entry uses the same immutable digest. No base rebuild is required.

| Selection | Repository | Release alias | Changes latest |
|---|---|---|---|
| `4.22.14` | `dcm-ocp-ee-4.22` | `:14` | No |
| `4.22.22` | `dcm-ocp-ee-4.22` | `:22` | No |
| `5.0.1` (naming example) | `dcm-ocp-ee-5.0` | `:1` | No |
| Stable production build | Matching stream | Resolved patch | Yes |
| Stable PR build | Matching stream, GHCR | None | No |

Unique build tags use `:<patch>-<commit12>-<run-id>-<attempt>`; PR tags use
`:<patch>-pr-<pr>-<commit12>-<run-id>-<attempt>`.
Production tags are in `quay.io/<namespace>/dcm-ocp-ee-<major>.<minor>`;
PR tags go to GHCR. The patch alias can move when the same patch is rebuilt with an updated
base. Unique build tags distinguish those builds, but registry tags themselves
are not immutable. Deployments should use the published `@sha256:...` reference.
The helper removes the inherited `quay.expires-after` label and verifies its
absence. Registry administrators' other retention/deletion policies still apply.

Explicit version builds never promote `latest`. Existing dependent production
builds resolve `stable-<major>.<minor>/release.txt` once, reject metadata from a
different stream, then fetch both archives and checksums
from the resulting exact release directory. Only that stable production path
updates `latest`. Legacy minor-version definitions retain their existing behavior.
Production jobs share a concurrency group per stream; submit one manual release batch at
a time because GitHub concurrency permits only one pending job per group.

## Stream configuration and qualification

`.github/ocp-ee-streams.json` declares the mirror root, versioned stream mappings,
automatic stable-build flags, and the legacy streams that retain the older build
path. Output repository names are derived from the release, independently of the
definition directory. The historical `dcm-ocp-ee-4.22` directory currently supplies
the shared definition for 4.22 and 5.0. There is no Python image-name pin.

The mirror root remains `https://mirror.openshift.com/pub/openshift-v4/clients/ocp`
for these configured streams, including 5.0. Product major version does not select
the mirror directory prefix. Change the configured HTTPS root only when the
upstream layout requires it.

Only 4.22 has automatic stable builds enabled. 5.0 is an explicit build
target for qualification, not a qualified release image. Adding another stream
requires a reviewed configuration entry selecting an existing EE definition;
no helper edit is needed. If that stream needs different OS dependencies, add a
compatible definition and select it in the configuration. Verify the actual
base/client/installer combination before enabling automatic builds. A single
manual matrix shares one base digest; use separate dispatches when streams need
different bases. The inherited dcm-base-ee architecture remains amd64.

## Independent PR qualification

**PR OpenShift EE Qualification** runs for same-repository PRs changing the helper,
stream configuration, qualification workflow, or OpenShift definition files. It
does not depend on `build-ee` and does not rebuild `dcm-base-ee`. The existing
base-rebuild PR workflow remains available for changes to that base.

`pr_qualification` in `.github/ocp-ee-streams.json` selects exact releases and the
published base reference. Its initial target is 4.22.22, using
`quay.io/s4v0/dcm-base-ee:latest`. The prepare job pulls that existing image and
resolves the tag to one repository digest. Every qualification matrix entry then
builds against that digest, even if the base's tag changes during the run. The
configuration also accepts an already pinned base reference. Adding a 5.0 release
to this PR matrix is a separate reviewed configuration choice.

Both preparation and build jobs use GitHub's `test` environment. Its
`QUAY_USERNAME` and `QUAY_PASSWORD` provide pull access to the existing Quay base;
pull-only robot credentials are sufficient. The build job uses the existing
`REDHAT_SA_USERNAME` and `REDHAT_SA_PASSWORD` for registry.redhat.io, and the GitHub
token with `packages: write` for GHCR publication.

After offline checks, the build job verifies the final tool versions, release
payload and labels, then publishes only the unique PR tag to
`ghcr.io/<owner>/dcm-ocp-ee-<stream>:<patch>-pr-<pr>-<commit12>-<run-id>-<attempt>`.
It does not publish Quay images, patch aliases, or `latest`. The workflow preserves
the resolved plan, build log and receipt; the receipt and Actions summary contain
the base digest and the published image digest. Green offline checks and rendered
Containerfiles alone do not establish that this qualification build succeeded;
review its actual build and publication jobs.

## Local preparation and build

From the repository root, with CI-pinned Ansible Builder installed, Podman
available, and registry authentication already established:

python3 .github/scripts/ocp_ee.py build --release 4.22.22 --base quay.io/s4v0/dcm-base-ee:latest --commit "$(git rev-parse HEAD)" --run-id 12345-1 --output /tmp/ocp-ee-22

Use a fresh output directory and a distinct numeric run/attempt identity for each
build. The helper stages a private copy of the definition; source files are not
edited. It passes `OCP_VERSION` and digest-pinned `EE_BASE_IMAGE` build arguments.
Verified archives are mounted read-only during extraction and are not copied into
image layers.
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
base digest, release payload, stream, selected definition, mirror root, and run
identity. Stream-aware receipts use schema version 2; older receipts must be
rebuilt and verified with this helper before publication. Failed build output remains in
`build.log`; unverified builds cannot publish.

## Verification and automation

python3 -m unittest discover -s tests -p 'test_*.py' -v

The dedicated test workflow requires the CI-pinned Builder and renders a real
Containerfile with fixture archives, without building an image or using a
registry. Offline tests exercise checksums, version mismatches, matrix inputs,
tag isolation, publication failures, and workflow routing. An actual image build
and runtime qualification remain required before accepting the release image.

The existing general matrix excludes OCP-dependent directories. Consequently,
changing only this definition does not automatically publish a new image. Use the
dedicated versioned workflow for explicit production releases; independent PR
qualification builds the configured exact PR targets, and the existing
base-dependent workflow continues to drive stable builds. Do not dispatch this image through the
generic EE build job, which does not stage release inputs.
