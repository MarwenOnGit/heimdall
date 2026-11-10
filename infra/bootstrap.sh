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
  # kind switches the current kubectl context; put the previous one back so
  # other work on this machine is not redirected to this cluster.
  prev_ctx="$(kubectl config current-context 2>/dev/null || true)"
  kind create cluster --name "$CLUSTER_NAME" --image "$KIND_NODE_IMAGE" \
    --config "$ROOT/infra/kind-config.yaml" --wait 120s
  if [ -n "$prev_ctx" ]; then kubectl config use-context "$prev_ctx" >/dev/null; fi
fi
k wait --for=condition=Ready nodes --all --timeout=180s >/dev/null

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

# --- alert token ----------------------------------------------------------
# Shared by Alertmanager (sender) and Heimdall (receiver). Generated once.
if ! k -n heimdall get secret heimdall-alert-token >/dev/null 2>&1; then
  token="$(openssl rand -hex 32)"
  ensure_secret heimdall heimdall-alert-token token "$token"
  ensure_secret monitoring heimdall-alert-token token "$token"
fi

# --- metrics --------------------------------------------------------------
helm_install kps monitoring kube-prometheus-stack "$PROM_STACK_REPO" "$PROM_STACK_VERSION" \
  -f "$ROOT/infra/helm-values/kube-prometheus-stack.yaml"

# --- temporary alert echo endpoint (replaced by Heimdall in M3) -----------
k -n heimdall create configmap heimdall-echo --from-file="$ROOT/infra/echo/echo.py" \
  --dry-run=client -o yaml | k apply -f - >/dev/null
envsubst '${ECHO_IMAGE}' <"$ROOT/infra/echo/echo.yaml" | k apply -f - >/dev/null
k -n heimdall rollout status deploy/heimdall --timeout=180s >/dev/null

log "environment ready"
k get nodes
