# antigravity-ai-helpers

Build directory for the `antigravity-ai-helpers` container image.

## Files

| File | Purpose |
|------|---------|
| `Containerfile` | Image definition — UBI 10 base, tooling layer, Antigravity CLI install |
| `antigravity-entrypoint.sh` | Container entrypoint — passthrough to `agy` |
| `image.yaml` | CLI binary configuration for CI matrix tests |

## Building locally

```bash
podman build \
  --tag antigravity-ai-helpers:dev \
  ee-builds/antigravity-ai-helpers/
```

Override the ai-helpers branch:

```bash
podman build \
  --build-arg AI_HELPERS_REF=main \
  --tag antigravity-ai-helpers:dev \
  ee-builds/antigravity-ai-helpers/
```

## CI/CD

Changes to this directory trigger the image build workflows automatically:

- **PR** → `pr-image-build.yml` builds and pushes to GHCR
- **Merge to main** → `push-image-build.yml` builds and pushes to quay.io

See `.github/workflows/` for details.
