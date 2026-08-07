#!/usr/bin/env bash
# Real run_as isolation — the boundary that the org-level refactor wraps every
# member turn in (the 2026-07-16 rig -> org ruling).
#
# Builds an ubuntu:24.04 container and, as root INSIDE it, provisions the exact
# operator setup the docs prescribe — a sudo-LESS agent user, a shared work
# group, a scoped `visudo`-validated sudoers drop-in, setgid 2770 staging +
# workplace dirs — then runs a REAL org-level `run_as` dispatch turn and asserts
# the boundary holds from inside: the turn's effective user is the agent, env
# survived sudoers env_reset, staging writes land group-owned, the agent cannot
# sudo, it cannot read the router's home, and it can neither enumerate R4T_HOME
# nor read the member's k7e store under it. Functional checks throughout — a
# real user doing real writes, not trusting mode bits.
#
# The container is the only entry point; the same script runs locally (Docker
# Desktop) and in CI. Slow (~1-2 min: it apt-installs python3+sudo) by design.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
R4T_DIR="$(cd "$HERE/../.." && pwd)"   # apps/r4t
# ubuntu:24.04 is the CI default; override to reuse an already-pulled base
# locally (e.g. R4T_TEST_IMAGE=ubuntu:latest) when a registry pull is awkward.
IMAGE="${R4T_TEST_IMAGE:-ubuntu:24.04}"

if ! command -v docker >/dev/null 2>&1; then
  echo "run-as.sh: docker not found — this test needs Docker (Desktop locally, or CI)." >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "run-as.sh: docker daemon not reachable — start Docker and retry." >&2
  exit 2
fi

echo "run-as.sh: launching ${IMAGE} (installs python3+sudo; slow by design)..."
exec docker run --rm \
  -v "$R4T_DIR":/src-r4t:ro \
  -e DEBIAN_FRONTEND=noninteractive \
  "$IMAGE" \
  bash /src-r4t/tests/docker/inside.sh
