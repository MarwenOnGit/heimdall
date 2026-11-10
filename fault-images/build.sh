#!/usr/bin/env bash
# Builds the bad-release images and pushes them to the local registry.
#
# They are tagged like ordinary releases (frontend:v0.11.x) on purpose: an
# image called "faulty" would give the diagnosis away.
#
#   v0.11.0  20% of requests fail
#   v0.11.1  30%
#   v0.11.2  40%
#
# Existing tags are left alone; set FORCE=1 to rebuild them.
set -euo pipefail
# shellcheck source=../infra/lib.sh
. "$(dirname "$0")/../infra/lib.sh"

repo="localhost:${REGISTRY_PORT}/boutique/frontend"

build() { # build <tag> <fail percent>
  if [ -z "${FORCE:-}" ] && curl -fsS -o /dev/null "http://localhost:${REGISTRY_PORT}/v2/boutique/frontend/manifests/$1" \
    -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' 2>/dev/null; then
    return
  fi
  log "building ${repo}:$1 (${2}% of requests fail)"
  docker build -q -t "${repo}:$1" \
    --build-arg GO_IMAGE="$GO_IMAGE" \
    --build-arg FRONTEND_IMAGE="$BOUTIQUE_FRONTEND_IMAGE" \
    --build-arg FAIL_PERCENT="$2" \
    "$ROOT/fault-images/frontend" >/dev/null
  docker push -q "${repo}:$1" >/dev/null
}

build v0.11.0 20
build v0.11.1 30
build v0.11.2 40
