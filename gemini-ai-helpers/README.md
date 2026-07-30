# gemini-ai-helpers

Build directory for the `gemini-ai-helpers` container image.

**User-facing documentation** — usage, tool inventory, and authentication options —
lives in the
[ai-helpers repository](https://github.com/opendatahub-io/ai-helpers/tree/main/images/gemini/).

## Files

| File | Purpose |
|------|---------|
| `Containerfile` | Image definition — UBI 10 base, tooling layer, gemini-cli install |
| `gemini-entrypoint.sh` | Container entrypoint — passthrough to `gemini` |

## Building locally

```bash
podman build \
  --tag gemini-ai-helpers:dev \
  ee-builds/gemini-ai-helpers/
```

Override the gemini-cli version or ai-helpers branch:

```bash
podman build \
  --build-arg GEMINI_CLI_VERSION=0.53.0 \
  --build-arg AI_HELPERS_REF=main \
  --tag gemini-ai-helpers:dev \
  ee-builds/gemini-ai-helpers/
```

## CI/CD

Changes to this directory trigger the image build workflows automatically:

- **PR** → `pr-image-build.yml` builds and pushes to GHCR
- **Merge to main** → `push-image-build.yml` builds and pushes to quay.io

See `.github/workflows/` for details.
