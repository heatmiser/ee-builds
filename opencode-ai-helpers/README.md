# opencode-ai-helpers

Build directory for the `opencode-ai-helpers` container image.

**User-facing documentation** — usage, tool inventory, and authentication options —
lives in the
[ai-helpers repository](https://github.com/opendatahub-io/ai-helpers/tree/main/images/opencode/).

## Files

| File | Purpose |
|------|---------|
| `Containerfile` | Image definition — UBI 10 base, tooling layer, opencode-ai install |
| `opencode-entrypoint.sh` | Container entrypoint — passthrough to `opencode` |
| `image.yaml` | Per-image metadata used by CI workflows |

## Building locally

```bash
podman build \
  --tag opencode-ai-helpers:dev \
  ee-builds/opencode-ai-helpers/
```

Override the opencode version or ai-helpers branch:

```bash
podman build \
  --build-arg OPENCODE_VERSION=1.19.0 \
  --build-arg AI_HELPERS_REF=main \
  --tag opencode-ai-helpers:dev \
  ee-builds/opencode-ai-helpers/
```

## CI/CD

Changes to this directory trigger the image build workflows automatically:

- **PR** → `pr-image-build.yml` builds and pushes to GHCR
- **Merge to main** → `push-image-build.yml` builds and pushes to quay.io

See `.github/workflows/` for details.
