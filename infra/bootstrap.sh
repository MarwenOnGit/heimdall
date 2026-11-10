#!/usr/bin/env bash
# Creates the heimdall cluster and installs everything on it. Idempotent:
# a second run changes nothing.
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

"$ROOT/infra/preflight.sh"
"$ROOT/infra/tools.sh"

# --- local registry -------------------------------------------------------
if [ "$(docker inspect -f '{{.State.Running}}' "$REGISTRY_NAME" 2>/dev/null || true)" != "true" ]; then
  log "starting local registry on 127.0.0.1:${REGISTRY_PORT}"
  docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
  docker run -d --restart=always --name "$REGISTRY_NAME" \
    -p "127.0.0.1:${REGISTRY_PORT}:5000" \
    -v "${REGISTRY_NAME}-data:/var/lib/registry" \
    "$REGISTRY_IMAGE" >/dev/null
fi

# --- cluster --------------------------------------------------------------
if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  log "creating kind cluster ${CLUSTER_NAME}"
  kind create cluster --name "$CLUSTER_NAME" --image "$KIND_NODE_IMAGE" \
    --config "$ROOT/infra/kind-config.yaml" --wait 120s
fi

# Point every node at the registry under the name localhost:<port>, and attach
# the registry to the kind network so nodes can reach it by container name.
for node in $(kind get nodes --name "$CLUSTER_NAME"); do
  dir="/etc/containerd/certs.d/localhost:${REGISTRY_PORT}"
  docker exec "$node" mkdir -p "$dir"
  printf '[host."http://%s:5000"]\n' "$REGISTRY_NAME" |
    docker exec -i "$node" cp /dev/stdin "$dir/hosts.toml"
done
if [ "$(docker inspect -f '{{json .NetworkSettings.Networks.kind}}' "$REGISTRY_NAME")" = "null" ]; then
  docker network connect kind "$REGISTRY_NAME"
fi

# Standard discovery ConfigMap for local registries (KEP-1755).
k apply -f - <<EOF >/dev/null
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-registry-hosting
  namespace: kube-public
data:
  localRegistryHosting.v1: |
    host: "localhost:${REGISTRY_PORT}"
    help: "https://kind.sigs.k8s.io/docs/user/local-registry/"
EOF

# --- namespaces -----------------------------------------------------------
k apply -f "$ROOT/infra/namespaces.yaml" >/dev/null

log "cluster ready"
k get nodes
