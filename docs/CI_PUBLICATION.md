# EE lifecycle policy and publication status

The reviewed catalog, `.github/ee-projects.json`, determines which execution
environment definitions are eligible for this repository's CI builds and
publication. Every EE definition must have an explicit `active` or `example`
entry. Unknown projects and incomplete or invalid catalogs are rejected.

## Active EE definitions

- `auto-satellite-ee`
- `centosstream10-ansible`
- `dcm-base-ee`
- `dcm-bootstrap-ee`
- `dcm-ocp-ee-4.16` through `dcm-ocp-ee-4.22`
- `ocp-satellite-ee-4.22`
- `rh-isam-ee`

Eligibility does not establish build success, supported runtime behavior, or
credential safety. These projects remain in scope for the broader credential
and publication qualification work.

## Retained examples

- `aws-ee`
- `base-de`
- `base-de-25`
- `controller-101-aap25-rhel8`
- `controller-101-ee`
- `first-playbook-ee`
- `lightspeed-101-ee`
- `product-demos-ee`
- `rhel-90-ee`
- `servicenow-ee`
- `telemetry-ee`
- `terraform-ee`
- `vmware-ee`
- `windows-ee`
- `zta-ee`

These directories remain at their existing paths as source examples. Automatic
EE matrices omit them and explain the skip. Supported manual production EE
dispatch rejects them before registry authentication. Build workers and
publication steps check eligibility again, preventing an externally assembled
matrix from enabling an example. The development EE workflow continues to
require pull-request context; this policy does not add manual development builds.

The OpenShift helper checks the target definition before mirror reads, staging,
container commands and publication. A previously verified receipt cannot publish
after its project is changed to `example`. Dedicated PR/exact-release workflows
also check eligibility before registry access. Dependent matrices with no eligible
targets skip their worker jobs. Release stream configuration remains separate:
sharing an active source definition does not enable automatic builds for stream
5.0 or qualify its runtime.

To re-enable an example, submit a topic-branch PR changing its catalog status to
`active`, review its credentials and dependencies, and complete the required
build/publication qualification before enabling its production path. Source edits
or manual matrix construction alone do not re-enable it. New EE definitions also
require an explicit reviewed catalog entry. Repository workflows and supported
helpers enforce the catalog; users copying examples can adapt the source in
their own projects.

## Hardening and qualification boundaries

Source credential hygiene remains in scope for examples so they do not teach
unsafe patterns. Historical artifacts and published images remain in audit
scope regardless of the source directory's current designation.

EE credential-bearing build arguments and legacy environment dumps require the
subsequent EE hardening phase. The five plain-container projects below are enabled
in their own build workflows and use the shared inspection process. EE example
designations do not disable these projects.

Local policy tests use synthetic fixtures and mocked build/mirror operations.
They establish eligibility enforcement and routing. Real Builder rendering,
authenticated image builds, inspection of every published layer, and verification
of resulting public logs/artifacts and registry digests require separate evidence.

## Plain-container builds

All five projects are selectable in **Devel Image Build** and **Prod Image Build**:

- `antigravity-ai-helpers`
- `codex-ai-helpers`
- `gemini-ai-helpers`
- `opencode-ai-helpers`
- `k8s-launch-kit`

Select **Run workflow**, then choose an `image_name` or `all`. Development builds
accept a same-repository PR or manual dispatch. Production builds accept main-branch
pushes or manual dispatch from main. Source checkout must match the event's exact
commit. Normal changes select affected images; changes to shared build/report
helpers or either image workflow select all five. Push selection compares the
event's before/after commits, covering pushes containing multiple commits.

The image build jobs use their nonempty-matrix conditions. Registry publication
steps remain explicitly disabled pending hosted qualification. This permits builds
and artifact inspection without automatically publishing unqualified images.
Enabling publication also requires the reviewed job/token permissions appropriate
to its registry; current build jobs need only repository read access.

The shared helper captures build/runtime output in private runner storage and
does not forward workflow credentials to build or runtime-check subprocesses.
Ordinary images retain CLI-version/tool checks. L8K retains its source-pinned
build/runtime verifier and includes its bounded provenance in the common receipt.

Before preparing an upload, the helper saves the immutable image as an uncompressed
OCI directory. It verifies blob digests, configuration identity and layer diff IDs,
then inspects configuration/history and regular-file content from every layer.
Layers are examined separately, so deletion or replacement in a later layer does
not conceal older contents. Container symlinks are recorded as metadata and never
followed on the runner. Common nested archives are expanded without trusting their
member paths; unsupported archive types, integrity errors and inspection limits
deny the operation. Limits are 2 GiB per file, 16 GiB of inspected payloads,
500,000 payload entries and eight nested expansion levels. Hosted qualification
must establish that all five real images fit those bounds and available disk.

Known step-provided credentials and raw/base64/hex/URL/JSON/UTF-16 forms are checked
privately. TruffleHog is downloaded from its versioned upstream release and checked
against the SHA256 in `.github/ci-tools.json` before execution. Scanning disables
live credential verification; findings, scanner errors, skipped inspection, and
runtime failures prevent creation of the public artifact directory. A checksum
from the same release is a provenance pin, not an independent signature.

Only these files can be uploaded, with 7-day development or 30-day production
retention:

- `receipt.json`: source, immutable local image identity, runtime/scan status,
  scanner version, inspected layer count and optional bounded L8K provenance.
- `report.txt`: fixed metadata and check outcomes; explicitly describes publication
  as not performed by the build step.
- `SHA256SUMS`: hashes of those two files.

Raw logs, OCI blobs, expanded payloads, scanner findings and registry authentication
files remain outside the upload paths. Image inspection cannot prove absence of
every conceivable secret; successful hosted builds, failure/canary cases, downloaded
artifacts and registry digests still require independent qualification.

The publication helper separately revalidates the inspection binding, source,
immutable image identity, exact public file set, and newly supplied credentials
before login or push. Passwords use stdin and a temporary authfile outside source;
authfiles are removed after success or failure. Pushes use the inspected image ID,
and per-tag digest evidence is retained privately even if a later push fails.
