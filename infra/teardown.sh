#!/usr/bin/env bash
# Deletes the heimdall cluster and its registry container. The registry volume
# is kept so the next `make up` does not pull every image again; pass --purge
# to remove it too.
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  log "deleting kind cluster ${CLUSTER_NAME}"
  kind delete cluster --name "$CLUSTER_NAME"
fi

if docker inspect "$REGISTRY_NAME" >/dev/null 2>&1; then
  log "removing registry container"
  docker rm -f "$REGISTRY_NAME" >/dev/null
fi

if [ "${1:-}" = "--purge" ]; then
  log "removing registry volume"
  docker volume rm -f "${REGISTRY_NAME}-data" >/dev/null
fi
