# opencode-ai-helpers

Build directory for the `opencode-ai-helpers` container image.

Installs OpenCode v2 from the pinned npm package `@opencode/cli@2.0.28`.
The build checks that the installed `opencode` executable reports its version.

**User-facing documentation** — usage, tool inventory, and authentication options —
lives in the
[ai-helpers repository](https://github.com/opendatahub-io/ai-helpers/tree/main/images/opencode/).

## Files

| File | Purpose |
|------|---------|
| `Containerfile` | Image definition — UBI 10 base, tooling layer, pinned OpenCode v2 install |
| `opencode-entrypoint.sh` | Container entrypoint — passthrough to `opencode` |
| `image.yaml` | Per-image metadata used by CI workflows |

## Building locally

podman build --tag opencode-ai-helpers:dev ee-builds/opencode-ai-helpers/

Override the pinned OpenCode v2 version or ai-helpers branch:

podman build --build-arg OPENCODE_VERSION=2.0.28 --build-arg AI_HELPERS_REF=main --tag opencode-ai-helpers:dev ee-builds/opencode-ai-helpers/

## Upgrading from OpenCode v1

Existing mounted v1 provider configurations require migration to the v2
`providers` shape with `package` and `settings` fields. The corresponding
v1 fields are `provider`, `npm`, and `options`. OpenAI-compatible providers
use the bundled `@opencode/ai/providers/openai-compatible` package in v2.
Review mounted configuration and plugins for v2 compatibility before use;
rebuilding this image does not rewrite host configuration.

## CI/CD

Changes to this directory trigger the image build workflows automatically:

- **PR** → `pr-image-build.yml` builds and pushes to GHCR
- **Merge to main** → `push-image-build.yml` builds and pushes to quay.io

See `.github/workflows/` for details.
