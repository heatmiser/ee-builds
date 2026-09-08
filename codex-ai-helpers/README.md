# codex-ai-helpers

Build directory for the `codex-ai-helpers` container image.

**User-facing documentation** — usage, tool inventory, and authentication options —
lives in the
[ai-helpers repository](https://github.com/opendatahub-io/ai-helpers/tree/main/images/codex/).

## Files

| File | Purpose |
|------|---------|
| `Containerfile` | Image definition — UBI 10 base, tooling layer, codex-cli install |
| `codex-entrypoint.sh` | Container entrypoint — passthrough to `codex` |
| `image.yaml` | Per-image metadata used by CI workflows |

## Building locally

```bash
podman build \
  --tag codex-ai-helpers:dev \
  ee-builds/codex-ai-helpers/
```

Override the codex version or ai-helpers branch:

```bash
podman build \
  --build-arg CODEX_VERSION=0.153.4 \
  --build-arg AI_HELPERS_REF=main \
  --tag codex-ai-helpers:dev \
  ee-builds/codex-ai-helpers/
```

## CI/CD

Changes to this directory trigger the image build workflows automatically:

- **PR** → `pr-image-build.yml` builds and pushes to GHCR
- **Merge to main** → `push-image-build.yml` builds and pushes to quay.io

See `.github/workflows/` for details.
