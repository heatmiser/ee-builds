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
matrix from enabling an example. Manual development qualification selects exactly
`dcm-base-ee`; its catalog eligibility is checked before registry access. Other
development EE selection continues through the same-repository PR route.

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

Supported EE workflows use the protected driver instead of credential-bearing
build arguments or shared-config token substitution. Some retained definitions
still describe legacy transports; the driver removes those from generated
Containerfiles and rejects unrecognized credential use. The five
plain-container projects below are enabled in their own build workflows and use
the shared inspection process. EE example designations do not disable these
projects.

Local policy tests use synthetic fixtures and mocked build/mirror operations.
They establish eligibility enforcement and routing. Real Builder rendering,
authenticated image builds, inspection of every published layer, and verification
of resulting public logs/artifacts and registry digests require separate evidence.

## Temporary EE publication holds

All eight EE image push/publication steps are explicitly disabled across
`pr-ee-build.yml`, `push-ee-build.yml`, `pr-ocp-ee-build.yml` and
`build-versioned-ocp-ee.yml`. The two legacy `build-ocp-ees` jobs are also held:
they require a base image published by the same workflow, which is unavailable
while publication is held. They do not substitute an older base image.

General EE builds and independent PR/exact-release OpenShift build routes remain
defined. Independent OpenShift preparation plans still upload and download under
their existing bounded paths and retention; the build workers consume the
prepared published-base digest. Existing narrow reports and receipt uploads remain
in place. These holds do not qualify those artifacts as credential-safe.

EE checkout steps disable persisted Git credentials. Build jobs have explicit
read permissions, with package read access where needed; the PR comment job
retains only its repository-read and pull-request-write permissions. Legacy PR
preparation accepts a same-repository pull request or explicit manual qualification.
Manual qualification uses the selected branch's exact event commit, a unique
`manual-<run>-<attempt>-<short-commit>` tag and no dependent OpenShift matrix.
The raw debug job and
runner-global credential URL rewrites are removed. Push-success flags and usage
summaries require a successful push and are skipped while publication is held.

The protected scripting requires synthetic offline checks followed by real Podman
and hosted qualification. Keep credential-bearing hosted execution separately
authorized until those checks establish working authentication, builds and output
protection. The build workers remain defined; this execution restriction is
procedural.

The normal gate checks workflow credential values. Broad upstream-content pattern
scanning is outside this gate and is not reported as successful inspection.
Re-enabling publication requires
credential transport/output qualification and separate review of trigger paths,
permissions, public artifacts and registry digest fidelity. The five plain-image
build selections and their existing publication holds remain unchanged.

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

Known step-provided credentials and raw/Base64/hex/URL/JSON/UTF-16 forms are checked
privately. The normal build does not download or invoke TruffleHog. The receipt
reports `credential_check: PASS` and `upstream_scan: NOT_RUN`; this asserts only
inspection for supplied workflow values and the documented encodings.
A known-value match, incomplete image inspection, or runtime failure prevents
creation of the public artifact directory.

Only these files can be uploaded, with 7-day development or 30-day production
retention:

- `receipt.json`: source, immutable local image identity, runtime/credential-check status,
  upstream-scan status, inspected layer count and optional bounded L8K provenance.
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

## Workflow credential protection

`ci_secrets.py` contains the explicit twelve-source inventory and a streaming
matcher. Consuming steps map their job's referenced secrets into `CI_SECRET_*`
environment entries; values are never written into workflow command text. Ordinary
subprocesses do not inherit those entries or the original credential variables.
The matcher covers raw UTF-8, standard Base64, lower/upper hex, percent-encoded URL
forms, JSON string escaping, UTF-16LE/BE, and registry Basic-auth pairs. Missing
files, unsafe links, failed reads and image inspection failures deny exposure.

`QUAY_USERNAME` is explicitly classified as a public namespace identifier so
legitimate Quay image references remain usable. Passwords/tokens and the other
credential identities remain protected. A value also supplied by a confidential
source remains forbidden even when it equals that public namespace. There is no
length-based exemption for short credentials and no detector/path suppression.

Every artifact upload depends on a preceding successful check of its exact file
list, with the consuming job's supplied values. A failed build or failed check
cannot upload a leftover receipt through an `always()` upload. Raw tool output
stays private; public failure messages do not include exception text or matches.
The OCP helper checks staged inputs, final image layers and receipt bytes before
writing its public receipt, and rechecks the image before publication.

`ee_build.py` generates a temporary Builder context, removes recognized legacy
secret ARG/ENV/Git-rewrite instructions, and adds secret mounts only to actual
Galaxy role/collection install RUN instructions. Automation Hub and per-repository
Git credentials live in sibling files outside the context, mounted at
`/run/secrets` for those RUNs. Shared tracked configuration is never populated with
a real token. Git clone URLs contain no credentials. A read-only credential helper
supplies `x-access-token` as the username and the repository's token as the password
from a separate temporary secret mount. HTTPS host and repository path matching
is required; inherited helpers are cleared, interactive prompts are disabled, and
Git's credential store/erase requests are ignored. Both `.git` and suffix-free
repository URLs are supported. The credential gate also checks the resulting HTTP
Basic encoding. Signature options and the remaining build instructions are retained.
Unrecognized generated credential instructions reject the build. The resulting
image is checked before any report/upload. Directly running a retained legacy
definition outside the protected driver does not inherit this protection.

Registry credentials are stored in a 0600 job-owned authfile under runner temporary
storage. Passwords/usernames are not passed in login argv; an always-run cleanup
step removes that authfile. Hard runner termination cannot be handled by Python
cleanup and requires runner teardown. The refresh job submits its token without
shell argv or response output and preserves its existing discard-response behavior.

Known-value inspection requires the value to be available to the supervising
step. It is not proof about arbitrary encodings, unknown upstream credentials,
unobserved derived tokens, third-party action internals or previously published
artifacts. Real synthetic Podman builds and downloaded Actions log/artifact checks
are still required before publication is enabled.

## Hosted qualification and private download audit

Qualify one `dcm-base-ee` development EE build and one `codex-ai-helpers`
development image build before reviewing publication holds. These exercise the
two build implementations; they do not qualify every EE definition, production
event handling, OpenShift release selection or registry push behavior.

The development EE manual input currently accepts only `dcm-base-ee`. Both the
selection job and worker check active catalog status. Missing required Red Hat,
Automation Hub or landing-zone credential values stop that build before registry
authentication. PR builds retain changed-project selection and fork exclusion.
The dependent OpenShift jobs and every registry publisher remain explicitly held.

Review and commit the exact qualified source, perform the required precommit
secret scan, then push the topic branch with upstream tracking when authorized.
Pushing this branch does not match these workflows' main-only push triggers;
an existing PR can still trigger its configured PR workflows on synchronization.
Check for an existing PR again immediately before pushing. Opening a PR is a
separate operation and is unnecessary for these manual development dispatches.

After hosted execution approval, use the reviewed topic branch, not main. Set
EE_QUALIFICATION_BRANCH to that branch and dispatch only one target per command:

gh workflow run pr-ee-build.yml --repo heatmiser/ee-builds --ref "$EE_QUALIFICATION_BRANCH" -f ee_name=dcm-base-ee

After inspecting that run's result, dispatch the plain-image target:

gh workflow run pr-image-build.yml --repo heatmiser/ee-builds --ref "$EE_QUALIFICATION_BRANCH" -f image_name=codex-ai-helpers

Record each run ID and its exact head commit. A run must complete successfully;
neither a skipped worker nor an empty matrix qualifies. Start a new dispatch for
a retry. The initial download auditor deliberately rejects rerun attempts to avoid
mixing logs and artifacts from different attempts.

The workspace helper `tmp/ee-hosted-qualification/audit.py` is private local
qualification tooling, not a CI uploader or a tracked repository dependency. Its
SOURCE_MANIFEST.json binds the source bytes and its two Python files. A subsequent
commit of those exact bytes is allowed; the helper also requires local HEAD to
equal the explicitly supplied run commit. Changed source requires reviewed
rebinding rather than editing hashes to bypass a mismatch. Older workstation
bundles remain historical evidence bound to their previous source snapshots.

The helper needs Python 3.12+, Git and an authenticated GitHub CLI with Actions
read access. It installs nothing and uses GET requests only. From the parent
workspace, first check bindings without credentials or network access:

python3 -B tmp/ee-hosted-qualification/audit.py check --commit "$EE_QUALIFICATION_COMMIT"

For the separately approved download, supply the same private credential input
directory used for workstation authentication. It must be outside the repository
and the auditor bundle, owner-owned mode 0700, with owner-owned single-link regular
0400/0600 files named AH_TOKEN, REDHAT_SA_USERNAME, REDHAT_SA_PASSWORD and
RHVP_OCP_LANDING_ZONE_TOKEN. Files contain the matching credential values, never
names or references. UTF-8 values may have one trailing LF/CRLF and are limited
to 65,536 bytes; empty values, embedded CR/LF/NULs and symlinks are rejected.
Values must match those used by the selected hosted run; the auditor cannot
establish that equivalence or recover older values after credential rotation.

Other persistent names from the shared twelve-source inventory may be supplied
to extend coverage, including OCP_SATELLITE_TOKEN and RHVP_BAREMETAL_OCP_TOKEN,
which the EE workflow references. The auditor rejects a GITHUB_TOKEN input file:
the completed job's ephemeral token is unavailable for independent offline
comparison and must not be retained to enable this audit. A caller's current
GitHub API token is a separate credential. The helper uses existing gh
authentication and protects any API token supplied through the caller's
GH_TOKEN/GITHUB_TOKEN environment; it never retrieves a token from gh's store.

python3 -B tmp/ee-hosted-qualification/audit.py download --project dcm-base-ee --run-id "$EE_QUALIFICATION_RUN_ID" --branch "$EE_QUALIFICATION_BRANCH" --commit "$EE_QUALIFICATION_COMMIT" --credentials-dir /absolute/private/input-directory

Use `--project codex-ai-helpers` with that target's own run ID and commit for the
plain-image download. The helper does not dispatch, rerun or cancel workflows,
modify secrets, delete remote artifacts, push images or upload audit results.

The download audit checks:

1. Completed successful first-attempt manual run identity: repository, source
   repository, branch, commit and expected workflow file.
2. Successful selected build job, successful upload and authfile-cleanup steps,
   and explicitly skipped publication. Incomplete job inventories are rejected.
3. Exactly one expected, unexpired artifact belonging to that run and commit.
   Unexpected artifacts and truncated/paginated inventories fail qualification.
4. The complete run-log ZIP and artifact ZIP downloaded through GitHub's API.
   All raw ZIP bytes, member names, metadata and decompressed regular-file bytes
   are inspected against supplied values and the guard's supported encodings.
   Paths are never extracted. Unsafe paths, duplicate members, links, encryption,
   corrupt ZIPs, unsupported log entries and size-limit overruns deny the audit.
5. Artifact ZIP SHA-256 against GitHub's recorded digest, then the exact expected
   file set. Plain receipts/reports must bind the project, commit and manual tag,
   with matching SHA256SUMS. The EE's single bounded report must match the expected
   reference/commit/check outcomes exactly. That EE upload has no separate
   pre-upload checksum; the audit records its downloaded hash without claiming
   an independent comparison with runner bytes.
6. Unchanged run/artifact metadata after download and unchanged local source
   binding before recording a bounded result.

Raw API metadata, log ZIPs and artifact ZIPs remain owner-only under the helper's
results/<UTC>-<suffix>/private/. A blocked audit can retain credentials found in
those downloads; keep the entire directory private. Share only the gated
SUMMARY.json. Nothing is copied to a public output directory. Each GET has a
300-second deadline. Downloads are bounded to 4 MiB per metadata response,
256 MiB for logs and 16 MiB for the single artifact. Log expansion is limited to
10,000 entries, 128 MiB per file and 1 GiB total; artifact expansion is limited
to 10 entries, 8 MiB per file and 32 MiB total. Limit failures are incomplete
qualification, never successful inspection.

A PASS means the selected downloads and bindings passed inspection for the
supplied values. SUMMARY.json lists both supplied sources and unavailable
workflow sources. Missing persistent values and the job's unavailable ephemeral
token prevent a claim of complete workflow credential coverage. In particular,
the plain development job consumes only its ephemeral GitHub token: comparison
against the four persistent workstation values does not independently qualify
that token's absence. Build-time protection and GitHub masking remain separate
evidence. Image-layer inspection is established by the hosted build, not repeated
by downloading metadata artifacts.

Downloaded-byte inspection measures exposure after upload; pre-upload gates
provide prevention. Any detected leak blocks qualification and requires incident
handling before another run. Hosted canary/failure behavior and registry
publication/digest qualification remain separate approval gates. A successful
download audit does not enable publication automatically.
