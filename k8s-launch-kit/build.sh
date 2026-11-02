#!/usr/bin/env bash
# Prepare the pinned source; --build additionally builds and verifies locally.
set -euo pipefail
umask 077
definition_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec python3 "$definition_root/verify.py" build "$@"
