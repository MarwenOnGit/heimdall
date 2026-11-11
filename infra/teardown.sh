#!/usr/bin/env bash
# Deletes the heimdall cluster and its registry containers. The registry and
# image cache volumes are kept so the next `make up` does not download every
# image again; pass --purge to remove them too.
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  log "deleting kind cluster ${CLUSTER_NAME}"
  kind delete cluster --name "$CLUSTER_NAME"
fi

docker rm -f heimdall-ollama-bridge >/dev/null 2>&1 || true

if docker inspect "$REGISTRY_NAME" >/dev/null 2>&1; then
  log "removing registry container"
  docker rm -f "$REGISTRY_NAME" >/dev/null
fi

if [ "${1:-}" = "--purge" ]; then
  log "removing registry and image cache volumes"
  docker volume rm -f "${REGISTRY_NAME}-data" >/dev/null
  "$ROOT/infra/registry/caches.sh" purge
else
  "$ROOT/infra/registry/caches.sh" down
fi
