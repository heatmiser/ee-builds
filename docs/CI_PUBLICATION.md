# EE lifecycle policy and publication status

Build usable execution-environment (EE) and plain-container images while keeping
required credential values out of public logs, downloadable files and image layers.
This document describes eligibility, build routes, credential handling and operator
qualification procedures.

## Contents

- [Active EE definitions](#active-ee-definitions)
- [Retained examples](#retained-examples)
- [Hardening and qualification boundaries](#hardening-and-qualification-boundaries)
- [Temporary EE publication holds](#temporary-ee-publication-holds)
- [Plain-container builds](#plain-container-builds)
- [Workflow credential protection](#workflow-credential-protection)
- [Hosted qualification and private download audit](#hosted-qualification-and-private-download-audit)

## At a glance

| Area | Build behavior | Publication behavior |
|---|---|---|
| Active EE definitions | Eligible for supported CI selection; actual jobs depend on event and matrix | EE publication steps remain disabled |
| Retained EE examples | Preserved as source; excluded from supported CI builds | Rejected until reviewed re-enablement |
| Five plain-container projects | Selectable in their own development/production build workflows | Registry publication remains disabled |
| Independent OpenShift routes | PR/exact-release workers consume a prepared published-base digest | Registry publication remains disabled |
| Two legacy dependent OpenShift jobs | Held because they require a base published by the same workflow | No older-base substitution |

**Eligibility is not qualification.** A catalog entry or successful preparation job
does not establish image build success, runtime behavior or credential safety.

## Active EE definitions

The reviewed catalog, `.github/ee-projects.json`, determines which EE definitions
are eligible for CI builds and publication. Every definition needs an explicit
`active` or `example` entry. Unknown projects and incomplete or invalid catalogs
are rejected.

- `auto-satellite-ee`
- `centosstream10-ansible`
- `dcm-base-ee`
- `dcm-bootstrap-ee`
- `dcm-ocp-ee-4.16` through `dcm-ocp-ee-4.22`
- `ocp-satellite-ee-4.22`
- `rh-isam-ee`

These projects still require credential and publication qualification for the
paths that will be enabled.

## Retained examples

These directories remain at their existing paths as source examples:

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

### Eligibility enforcement

| Entry point | Enforcement |
|---|---|
| Automatic EE matrix | Omits examples and explains the skip |
| Supported manual production EE dispatch | Rejects examples before registry authentication |
| Build worker and publication step | Rechecks eligibility; an externally assembled matrix cannot enable an example |
| Manual development qualification | Selects exactly `dcm-base-ee`; checks its eligibility before registry access |
| Other development EE selection | Uses the same-repository PR route |
| OpenShift helper | Checks the target before mirror reads, staging, container commands and publication |
| Dedicated PR/exact-release workflows | Check eligibility before registry access |
| Existing receipt | Cannot publish after its project changes to `example` |
| Empty eligible dependent matrix | Skips the worker jobs |

Release stream configuration is separate from catalog eligibility. Sharing an
active source definition does not enable automatic builds for stream 5.0 or
qualify its runtime.

### Re-enable an example

1. Submit a topic-branch PR changing its catalog status to `active`.
2. Review its credentials and dependencies.
3. Complete required build/publication qualification before enabling its
   production path.

Source edits or manual matrix construction alone do not re-enable an example.
New EE definitions also require an explicit reviewed catalog entry. Repository
workflows and supported helpers enforce this policy; users copying examples can
adapt the source in their own projects.

## Hardening and qualification boundaries

| Evidence or scope | What it establishes or requires |
|---|---|
| Local policy tests with synthetic fixtures and mocked build/mirror operations | Eligibility enforcement and routing |
| Real Builder rendering | Separate evidence that the actual generator produces the intended instructions |
| Authenticated image builds | Separate evidence of working credential access and successful builds |
| Public logs, artifacts, every published layer and registry digests | Separate inspection/verification evidence |
| Retained examples | Source credential hygiene remains in scope so examples do not teach unsafe patterns |
| Historical artifacts and published images | Remain in audit scope regardless of the source directory's current designation |

Supported EE workflows use the protected driver instead of credential-bearing
build arguments or shared-config token substitution. Some retained definitions
still describe legacy transports; the driver removes recognized transports from
generated Containerfiles and rejects unrecognized credential use.

The five [plain-container projects](#plain-container-builds) have their own build
workflows and use the shared inspection process. EE example designations do not
disable them.

## Temporary EE publication holds

### Build routes and credential environments

The environment below is the GitHub Actions environment attached to the
credential-bearing jobs. Environment secrets are available alongside applicable
repository secrets and the job's ephemeral GitHub token.

| Workflow | Trigger/selection | Credential environment | Publication |
|---|---|---|---|
| `pr-ee-build.yml` | Same-repository PR; manual qualification selects `dcm-base-ee` | `test` | Held |
| `push-ee-build.yml` | Matching main-branch push or manual EE selection | `deploy` | Held |
| `pr-ocp-ee-build.yml` | Matching same-repository PR; independent published-base qualification | `test` | Held |
| `build-versioned-ocp-ee.yml` | Manual exact-release selection | `deploy` | Held |
| `pr-image-build.yml` | Matching same-repository PR or manual plain-image selection | `test` | Held |
| `push-image-build.yml` | Matching main-branch push or manual dispatch from main | `deploy` | Held |

A topic-branch push with an open PR can trigger PR workflows on synchronization.
Review their execution scope before pushing; draft status does not suppress the
configured PR handlers. Test qualification does not enable deploy publication.

### Held jobs and preserved outputs

- All eight EE image push/publication steps are explicitly disabled across
  `pr-ee-build.yml`, `push-ee-build.yml`, `pr-ocp-ee-build.yml` and
  `build-versioned-ocp-ee.yml`.
- Both legacy `build-ocp-ees` jobs are held. They require a base image published by
  the same workflow; they do not substitute an older base image.
- General EE builds and independent PR/exact-release OpenShift build routes remain
  defined. Independent workers use the prepared published-base digest.
- Independent OpenShift preparation plans retain their bounded upload/download
  paths and retention. Existing narrow reports and receipt uploads remain in place.
  Publication holds do not qualify those artifacts as credential-safe.
- Push-success flags and usage summaries require a successful push and skip while
  publication is held. Plain-image publication holds also remain in place.

### Permissions and execution controls

- EE checkout steps disable persisted Git credentials.
- Build jobs have explicit read permissions, with package read access where needed.
  The PR comment job retains only repository-read and pull-request-write permissions.
- Legacy PR preparation accepts a same-repository pull request or explicit manual
  qualification. Manual qualification uses the selected branch's exact event
  commit, a unique `manual-<run>-<attempt>-<short-commit>` tag and no dependent
  OpenShift matrix.
- The raw debug job and runner-global credential URL rewrites are removed.
- Synthetic offline checks precede real Podman and hosted qualification. Keep
  credential-bearing hosted execution separately authorized until those checks
  establish working authentication, builds and output protection. Build workers
  remain defined; this execution restriction is procedural.

Before re-enabling publication, qualify credential transport/output protection
and review trigger paths, permissions, public artifacts and registry digest
fidelity. The normal gate checks workflow credential values; broad upstream-content
pattern scanning is outside that gate and is not reported as successful inspection.

### Publication readiness and digest evidence

Publication remains disabled. The production EE workflow accepts main-branch push
and manual events, checks out the exact event commit, and verifies that identity
before registry access in the primary build worker. Push selection compares the
event's `before` commit with its `after` commit, including every commit in a
multi-commit push. Missing commits, zero commit IDs and inconsistent event/checkout
identities stop selection; the workflow does not guess a range from `HEAD^1`.

The general EE `ci_secrets.py push` helper re-inspects the local image and pushes
that immutable image ID using the job-owned authfile. A successful invocation
retains this private runner evidence:

| Path under `$RUNNER_TEMP/ee-publication-<unique>/` | Contents |
|---|---|
| `manifest.digest` | Bounded push-reported manifest digest; validated and checked for supplied credential values |
| `receipt.json` | Schema version, inspected image ID, destination and manifest digest only; checked before writing |

The directory is mode `0700`; successful digest evidence and receipts are mode
`0600`. These files are outside every artifact upload path, and the helper prints
neither command output nor receipt contents. Failed pushes, missing/invalid digest
evidence or credential matches produce no success receipt for that invocation.
Private evidence may remain after failure; earlier successful tag receipts remain.
A push may already have reached the registry before a later check fails.

A push-reported digest does **not** independently verify registry contents or
establish that multiple tags share a manifest. Those checks remain qualification
steps: resolve each published tag, compare its manifest digest, then pull by digest
and verify image identity, runtime behavior and credential protection.

| Next phase | Required evidence before proceeding |
|---|---|
| Explicit GHCR publication in `test` | Narrow package-write permission; selected canary publish, digest pull and image checks |
| Production builds in `deploy` with publication held | Actual deploy credential transport, builds, runtime checks and output audit |
| Explicit Quay publication | Immutable-tag checks before shared tags; registry digest and pulled-image verification |
| Normal automatic publication | Qualified route, event selection, active/example enforcement and dependent-base handling |

Deploy credentials and registry write access are separate from the successful
test-environment builds and download audit. No publication, permission expansion,
dependent-job enablement or registry operation follows from this readiness change.

## Plain-container builds

### Select a project

All five projects are selectable in **Devel Image Build** and **Prod Image Build**:

- `antigravity-ai-helpers`
- `codex-ai-helpers`
- `gemini-ai-helpers`
- `opencode-ai-helpers`
- `k8s-launch-kit`

1. Select the appropriate development or production workflow.
2. Select **Run workflow**.
3. Choose an `image_name` or `all`.

| Selection rule | Behavior |
|---|---|
| Development event | Same-repository PR or manual dispatch |
| Production event | Main-branch push or manual dispatch from main |
| Checkout | Must match the event's exact commit |
| Normal changes | Select affected images |
| Shared build/report helper or either image workflow changes | Select all five images |
| Push comparison | Uses the event's before/after commits, including pushes containing multiple commits |
| Empty matrix | Build jobs skip under their nonempty-matrix conditions |

Builds and artifact inspection can run while registry publication remains disabled.
Enabling publication requires hosted qualification and reviewed job/token permissions
appropriate to the registry; current build jobs need only repository read access.

### Build and inspect before preparing public files

1. **Capture output privately.** The shared helper retains build/runtime output in
   private runner storage. It does not forward workflow credentials to build or
   runtime-check subprocesses.
2. **Run runtime checks.** Ordinary images retain CLI-version/tool checks. L8K uses
   its source-pinned build/runtime verifier and includes bounded provenance in the
   common receipt.
3. **Export the immutable image.** Save it as an uncompressed OCI directory before
   preparing an upload.
4. **Verify integrity.** Check blob digests, configuration identity and layer diff IDs.
5. **Inspect every layer.** Inspect configuration/history and regular-file content
   from every layer separately. Deletion or replacement in a later layer does not
   conceal older contents. Container symlinks are recorded as metadata and never
   followed on the runner.
6. **Inspect nested content.** Expand common nested archives without trusting member
   paths. Unsupported archive types, integrity errors and inspection limits deny
   the operation.
7. **Prepare bounded public files.** A known-value match, incomplete image inspection
   or runtime failure prevents creation of the public artifact directory.

Known step-provided values and their raw/Base64/hex/URL/JSON/UTF-16 forms are checked
privately. The normal build does not download or invoke TruffleHog. Receipts report
`credential_check: PASS` and `upstream_scan: NOT_RUN`: only supplied workflow values
and documented encodings are covered.

### Image inspection limits

| Resource | Bound |
|---|---|
| Single file | 2 GiB |
| Total inspected payloads | 16 GiB |
| Payload entries | 500,000 |
| Nested expansion levels | Eight |

Hosted qualification must establish that all five real images fit these bounds
and the available disk.

### Public files and retention

Only these files can be uploaded:

| File | Contents |
|---|---|
| `receipt.json` | Source, immutable local image identity, runtime/credential-check status, upstream-scan status, inspected layer count and optional bounded L8K provenance |
| `report.txt` | Fixed metadata and check outcomes; explicitly states that the build step did not perform publication |
| `SHA256SUMS` | Hashes of the receipt and report |

| Channel | Artifact retention |
|---|---|
| Development | 7 days |
| Production | 30 days |

Raw logs, OCI blobs, expanded payloads, scanner findings and registry authentication
files stay outside upload paths. Image inspection cannot prove absence of every
conceivable secret. Successful hosted builds, failure/canary cases, downloaded
artifacts and registry digests still require independent qualification.

### Revalidate before publishing

The publication helper performs a separate check before login or push:

1. Revalidate the inspection binding, source and immutable local image identity.
2. Check the exact public file set and inspect against newly supplied credentials.
3. Authenticate using password stdin and a temporary authfile outside source.
4. Push the inspected image ID. Retain per-tag digest evidence privately, including
   evidence from earlier tags if a later push fails.
5. Remove the authfile after success or failure.

## Workflow credential protection

### Credential purpose and transport

Workflow steps map their referenced secrets into `CI_SECRET_*` environment entries.
Values are never written into workflow command text. `ci_secrets.py` contains the
explicit twelve-source inventory and streaming matcher; each supervising step
can protect only values supplied to it.

| Credential/input | Access or purpose | Transport |
|---|---|---|
| `AH_TOKEN` | Automation Hub collections, including `redhat.satellite` | Temporary Ansible configuration mounted during Galaxy installation |
| `RHVP_OCP_LANDING_ZONE_TOKEN` | `heatmiser/rhvp.ocp_landing_zone` | Repository-scoped Git credential helper and temporary secret mount |
| `RHVP_BAREMETAL_OCP_TOKEN` | `heatmiser/rhvp.baremetal_ocp` | Repository-scoped Git credential helper and temporary secret mount |
| `OCP_SATELLITE_TOKEN` | `heatmiser/ocp_satellite` | Repository-scoped Git credential helper and temporary secret mount |
| `REDHAT_SA_USERNAME`, `REDHAT_SA_PASSWORD` | Red Hat registry access in the EE workflows | Private runner registry authfile |
| `GITHUB_TOKEN` | Workflow GitHub/GHCR access where referenced | Supplied to guarded steps; GHCR credentials use the private authfile |
| `QUAY_USERNAME`, `QUAY_PASSWORD` | Quay namespace and registry authentication | Public namespace identifier plus private registry authentication |
| Public `elise.quadlet` source | Public GitHub collection access | Normal HTTPS clone; no token needed |

This table describes access routes, not a claim that every credential is supplied
to every job. The complete matching inventory remains in `ci_secrets.py`.
The old dual-purpose `COLLECTION_TOKEN` is not needed by the protected driver.

### EE credential handling step by step

1. **Receive inputs.** The GitHub workflow supplies the relevant credential values
   to the protected driver, `ee_build.py`. Ordinary subprocesses do not inherit
   `CI_SECRET_*` entries or the original credential variables.
2. **Generate the context.** Run Builder to create a temporary context. Remove
   recognized legacy secret ARG/ENV/Git-rewrite instructions from its Containerfile;
   reject unrecognized generated credential instructions.
3. **Create private credential files.** Write temporary Ansible configuration,
   Git configuration and Git credentials as sibling files outside the build context.
   Shared tracked configuration is never populated with a real token.
4. **Mount only during installation.** Add secret mounts under `/run/secrets` only
   to actual Galaxy role/collection install RUN instructions. Retain signature
   options and remaining build instructions.
5. **Authenticate without rewriting clone URLs.** Use clean Git URLs and the
   repository-scoped helper described below. Authenticated builds require these
   driver-provided mounts; running Builder directly does not supply them.
6. **Inspect before exposure.** Check the generated context before building and the
   resulting image before preparing a report/upload. Secret mounts are not intended
   to become image layers; image inspection also checks for copied credential values.
7. **Clean up.** Remove the temporary build workspace and credential files when the
   driver's workspace scope exits, including build failures.

The `dcm-base-ee`, `dcm-bootstrap-ee` and `ocp-satellite-ee-4.22` definitions contain
no legacy credential ARG/ENV or Git rewrite instructions. The driver still rejects
unrecognized credential use and removes recognized legacy transports when generated.

### Git helper restrictions

- Supply `x-access-token` as the username and the repository's token as the password
  from a separate temporary secret mount.
- Require HTTPS host and repository path matching; support both `.git` and
  suffix-free repository URLs.
- Clear inherited helpers and disable interactive prompts.
- Ignore Git credential store/erase requests; the helper is read-only.
- Inspect the resulting HTTP Basic encoding as well as individual supplied values.

### Credential matching and exposure gates

| Check | Behavior |
|---|---|
| Supported representations | Raw UTF-8, standard Base64, lower/upper hex, percent-encoded URL forms, JSON string escaping, UTF-16LE/BE and registry Basic-auth pairs |
| Missing files, unsafe links, failed reads or image inspection failures | Deny exposure |
| Short credentials | No length-based exemption |
| Detector/path suppression | None in the known-value gate |
| Artifact upload | Requires a preceding successful check of its exact file list using the consuming job's supplied values |
| Failed build/check | Cannot upload a leftover receipt through an `always()` upload |
| Tool output and public failures | Raw output stays private; public diagnostics omit exception text and matches |
| OCP helper | Checks staged inputs, final image layers and receipt bytes before writing its public receipt; rechecks the image before publication |

`QUAY_USERNAME` is a public namespace identifier so legitimate Quay image references
remain usable. Passwords/tokens and other credential identities remain protected.
A value also supplied by a confidential source is forbidden even when it equals
that public namespace.

### Private registry authfiles and token refresh

| Property | Requirement or behavior |
|---|---|
| Storage | 0600 job-owned authfile under runner temporary storage |
| OCP `registry-login` inputs | Require `REGISTRY_AUTH_FILE` and `RUNNER_TEMP`; validate the `ee-auth-*` path |
| Default storage/subprocess fallback | No fallback to default authentication storage or a `podman login` subprocess in the OCP helper |
| Credential arguments | Passwords/usernames are not passed in login argv on this authfile-writing route |
| Authentication result | Writing the authfile does not authenticate against the registry; subsequent registry operations exercise the credentials |
| Cleanup | An always-run step removes the job authfile |
| Hard runner termination | Python cleanup cannot handle it; runner teardown is required |
| Refresh job | Submits its token without shell argv or response output; preserves discard-response behavior |

The plain-image publication helper has its own temporary-authfile/password-stdin
route, described under [Revalidate before publishing](#revalidate-before-publishing).

### Protection limits

Known-value inspection requires the value to be available to the supervising step.
It does not prove absence of:

- Arbitrary encodings or unknown upstream credentials.
- Unobserved derived tokens or exposure inside third-party action internals.
- Credential content in previously published artifacts.

Real synthetic Podman builds and downloaded Actions log/artifact checks remain
required before publication enablement. Broad upstream-content scanning is outside
the normal known-value gate and is reported as `NOT_RUN`.

## Hosted qualification and private download audit

### Qualification scope and prerequisites

Qualify one `dcm-base-ee` development EE build and one `codex-ai-helpers`
development image build before reviewing publication holds. These exercise the
two build implementations; they do not qualify every EE definition, production
event handling, OpenShift release selection or registry push behavior.

| Prerequisite | Requirement |
|---|---|
| Development EE manual selection | Only `dcm-base-ee`; both selection job and worker check active catalog status |
| Required base EE inputs | Missing Red Hat, Automation Hub or landing-zone values stop the build before registry authentication |
| PR selection | Retains changed-project selection and fork exclusion |
| Publication/dependent jobs | Every registry publisher and the dependent OpenShift jobs remain held |
| Source | Review the exact qualified source, perform the required precommit secret scan, then commit and push the topic branch with upstream tracking when authorized |
| Push scope | Topic push does not match main-only push triggers; an existing PR can still trigger workflows on synchronization |

Check for an existing PR immediately before pushing. Opening a PR is a separate
operation and is unnecessary for these manual development dispatches.

### Dispatch one target at a time

After hosted execution approval:

1. Use the reviewed topic branch, not main. Set `EE_QUALIFICATION_BRANCH` to it.
2. Dispatch the development EE target:

gh workflow run pr-ee-build.yml --repo heatmiser/ee-builds --ref "$EE_QUALIFICATION_BRANCH" -f ee_name=dcm-base-ee

3. Inspect that run's result before dispatching the plain-image target:

gh workflow run pr-image-build.yml --repo heatmiser/ee-builds --ref "$EE_QUALIFICATION_BRANCH" -f image_name=codex-ai-helpers

4. Record each run ID and its exact head commit. A skipped worker or empty matrix
   does not qualify; the selected run must complete successfully.
5. Start a new dispatch for a retry. The initial download auditor rejects rerun
   attempts to avoid mixing logs and artifacts from different attempts.

### Private auditor and source binding

The workspace helper `tmp/ee-hosted-qualification/audit.py` is private local
qualification tooling. It is not a CI uploader or tracked repository dependency.

| Property | Contract |
|---|---|
| Source manifest | `SOURCE_MANIFEST.json` binds source bytes and the auditor's two Python files |
| Commit binding | A subsequent commit of those exact bytes is allowed; local HEAD must equal the explicitly supplied run commit |
| Changed source | Requires reviewed rebinding, not hash edits to bypass a mismatch |
| Older workstation bundles | Remain historical evidence bound to their previous source snapshots |
| Prerequisites | Python 3.12+, Git and an authenticated GitHub CLI with Actions read access |
| Network/install behavior | Installs nothing; uses GET requests only |

From the parent workspace, check bindings first without credentials or network
access:

python3 -B tmp/ee-hosted-qualification/audit.py check --commit "$EE_QUALIFICATION_COMMIT"

### Credential input directory

For the separately approved download, supply the same private credential input
directory used for workstation authentication.

| Input | Requirement |
|---|---|
| Directory location | Outside the repository and auditor bundle |
| Directory ownership/mode | Owner-owned, mode 0700 |
| Files | Owner-owned single-link regular files, mode 0400/0600; no symlinks |
| Required filenames | `AH_TOKEN`, `REDHAT_SA_USERNAME`, `REDHAT_SA_PASSWORD`, `RHVP_OCP_LANDING_ZONE_TOKEN` |
| File contents | Matching credential values, never names or references |
| Encoding/size | UTF-8; one trailing LF/CRLF allowed; at most 65,536 bytes |
| Rejected contents | Empty values or embedded CR/LF/NULs |
| Run equivalence | Values must match the selected run; the auditor cannot establish equivalence or recover older values after rotation |
| Additional persistent inputs | Other names in the shared twelve-source inventory may extend coverage, including `OCP_SATELLITE_TOKEN` and `RHVP_BAREMETAL_OCP_TOKEN`, which the EE workflow references |
| Completed job's `GITHUB_TOKEN` | Input file rejected; the ephemeral token is unavailable for independent offline comparison and must not be retained to enable this audit |
| Caller API authentication | Current `GH_TOKEN`/`GITHUB_TOKEN` is a separate credential; supplied environment values are protected, and the helper never retrieves a token from gh's store |

### Download and inspect

Run the separately approved download with that run's identifiers and private input
directory:

python3 -B tmp/ee-hosted-qualification/audit.py download --project dcm-base-ee --run-id "$EE_QUALIFICATION_RUN_ID" --branch "$EE_QUALIFICATION_BRANCH" --commit "$EE_QUALIFICATION_COMMIT" --credentials-dir /absolute/private/input-directory

For the plain-image download, use `--project codex-ai-helpers` with that target's
own run ID and commit.

The helper does **not** dispatch, rerun or cancel workflows; modify secrets; delete
remote artifacts; push images; or upload audit results.

| Stage | Verification |
|---|---|
| 1. Run identity | Completed, successful, first-attempt manual run; expected repository, source repository, branch, commit and workflow file |
| 2. Job outcomes | Selected build, upload and authfile cleanup succeed; publication explicitly skipped; incomplete job inventories rejected |
| 3. Artifact inventory | Exactly one expected, unexpired artifact belonging to the run and commit; unexpected artifacts or truncated/paginated inventories fail |
| 4. Downloaded bytes | Inspect complete run-log ZIP and artifact ZIP: raw bytes, member names, metadata and decompressed regular-file bytes against supplied values and supported encodings |
| 5. ZIP safety | No extraction; reject unsafe paths, duplicate members, links, encryption, corrupt ZIPs, unsupported log entries and size-limit overruns |
| 6. Integrity/content | Match artifact ZIP SHA-256 to GitHub's digest and require the exact expected file set |
| 7. Final binding | Recheck unchanged run/artifact metadata and local source binding before recording a bounded result |

Content verification differs by target:

| Target | Content contract |
|---|---|
| Plain image | Receipt/report bind project, commit and manual tag; `SHA256SUMS` matches |
| EE | Single bounded report exactly matches expected reference/commit/check outcomes; no separate pre-upload checksum exists, so the downloaded hash does not establish an independent comparison with runner bytes |

### Private evidence and audit limits

Raw API metadata, log ZIPs and artifact ZIPs remain owner-only under the helper's
results/<UTC>-<suffix>/private/. A blocked audit can retain credentials found in
those downloads: keep the entire directory private. **Share only the gated
`SUMMARY.json`.** Nothing is copied to a public output directory.

| Resource | Bound |
|---|---|
| Each GET | 300 seconds |
| Metadata response | 4 MiB |
| Logs ZIP download | 256 MiB |
| Single artifact download | 16 MiB |
| Log expansion entries | 10,000 |
| Single expanded log file | 128 MiB |
| Total log expansion | 1 GiB |
| Artifact expansion entries | 10 |
| Single expanded artifact file | 8 MiB |
| Total artifact expansion | 32 MiB |

Limit failures mean incomplete qualification, never successful inspection.

### Interpret PASS and stop conditions

| Result or evidence | Meaning |
|---|---|
| Audit PASS | Selected downloads and bindings passed for supplied values and supported encodings |
| Source inventory | `SUMMARY.json` lists supplied sources and unavailable workflow sources |
| Missing persistent values or unavailable ephemeral job token | Prevent a claim of complete workflow credential coverage |
| Plain development job | Consumes only its ephemeral GitHub token; comparing four persistent workstation values does not independently qualify that token's absence |
| Hosted gate and GitHub masking | Separate evidence from the offline audit |
| Image-layer inspection | Established by the hosted build, not repeated by downloading metadata artifacts |
| Pre-upload gates | Provide prevention before publication of files |
| Downloaded-byte inspection | Measures exposure after upload |
| Detected leak | Blocks qualification; requires incident handling before another run |
| Publication enablement | Never follows automatically from a successful download audit |

Hosted canary/failure behavior and registry publication/digest qualification remain
separate approval gates.
