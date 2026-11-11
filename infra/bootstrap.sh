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

# Pull-through caches for every upstream registry.
"$ROOT/infra/registry/caches.sh" up

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

# --- bad-release images used by the chaos scenarios ------------------------
"$ROOT/fault-images/build.sh"

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

# --- logs -----------------------------------------------------------------
helm_install loki monitoring loki "$LOKI_REPO" "$LOKI_VERSION" \
  -f "$ROOT/infra/helm-values/loki.yaml"
helm_install alloy monitoring alloy "$ALLOY_REPO" "$ALLOY_VERSION" \
  -f "$ROOT/infra/helm-values/alloy.yaml"

# --- git server -----------------------------------------------------------
if ! k -n gitea get secret gitea-admin >/dev/null 2>&1; then
  k -n gitea create secret generic gitea-admin --from-literal=username=heimdall-admin \
    --from-literal=password="$(openssl rand -hex 16)" >/dev/null
fi
helm_install gitea gitea gitea "$GITEA_REPO" "$GITEA_VERSION" \
  -f "$ROOT/infra/helm-values/gitea.yaml"

# --- service mesh (request metrics only) ----------------------------------
certs="$(mktemp -d)"
trap 'rm -rf "$certs"' EXIT
"$ROOT/infra/linkerd/certs.sh" "$certs"
helm_install linkerd-crds linkerd linkerd-crds "$LINKERD_REPO" "$LINKERD_VERSION" \
  --set installGatewayAPI=true
helm_install linkerd-control-plane linkerd linkerd-control-plane "$LINKERD_REPO" "$LINKERD_VERSION" \
  -f "$ROOT/infra/helm-values/linkerd.yaml" \
  --set-file identityTrustAnchorsPEM="$certs/ca.crt" \
  --set-file identity.issuer.tls.crtPEM="$certs/issuer.crt" \
  --set-file identity.issuer.tls.keyPEM="$certs/issuer.key"
k apply -f "$ROOT/infra/linkerd/podmonitor.yaml" >/dev/null

# --- chaos ----------------------------------------------------------------
helm_install chaos-mesh chaos-mesh chaos-mesh "$CHAOS_MESH_REPO" "$CHAOS_MESH_VERSION" \
  -f "$ROOT/infra/helm-values/chaos-mesh.yaml"

# --- GitOps ---------------------------------------------------------------
helm_install argocd argocd argo-cd "$ARGOCD_REPO" "$ARGOCD_VERSION" \
  -f "$ROOT/infra/helm-values/argocd.yaml"
"$ROOT/infra/gitea/seed.sh"

# --- workload -------------------------------------------------------------
ensure_checkout_settings
k apply -f "$ROOT/infra/argocd/appproject-boutique.yaml" \
  -f "$ROOT/infra/argocd/application-boutique.yaml" >/dev/null
log "waiting for Online Boutique to become healthy"
wait_boutique_healthy 600 || die "Online Boutique did not become healthy"

# --- alert rules ----------------------------------------------------------
k apply -f "$ROOT/infra/alert-rules/" >/dev/null

# --- identities and API-server limits -------------------------------------
# The list of images the actor may switch a Deployment to: everything the
# GitOps repo deploys. Heimdall has no permission to change this list.
images="$(k kustomize "$ROOT/gitops/online-boutique" | sed -n 's/^ *image: *//p' | sort -u)"
k -n heimdall create configmap heimdall-allowed-images --from-literal=images="$images" \
  --dry-run=client -o yaml | k apply -f - >/dev/null
k apply -f "$ROOT/infra/rbac/" -f "$ROOT/infra/admission/actor-policy.yaml" >/dev/null

# --- Heimdall -------------------------------------------------------------
# Key that signs audit log checkpoints. Generated once.
if ! k -n heimdall get secret heimdall-audit-key >/dev/null 2>&1; then
  keyfile="$(mktemp)"
  openssl genpkey -algorithm ed25519 -out "$keyfile"
  k -n heimdall create secret generic heimdall-audit-key --from-file=audit.key="$keyfile" >/dev/null
  rm -f "$keyfile"
fi
"$ROOT/infra/gitea/bot.sh"
# The action policy, served by the OPA sidecar. Tests are not shipped.
policy="$ROOT/heimdall/policy/policies/actions.rego"
k -n heimdall create configmap heimdall-policy --from-file="$policy" \
  --dry-run=client -o yaml | k apply -f - >/dev/null
HEIMDALL_POLICY_HASH="$(sha256sum "$policy" | cut -c1-16)"
HEIMDALL_IMAGE="$("$ROOT/deploy/build.sh")"
HEIMDALL_LLM_PROVIDER="$("$ROOT/infra/ollama-bridge.sh")"
export HEIMDALL_IMAGE HEIMDALL_LLM_PROVIDER HEIMDALL_POLICY_HASH OPA_IMAGE
# shellcheck disable=SC2016  # envsubst wants the literal variable names
envsubst '${HEIMDALL_IMAGE} ${HEIMDALL_LLM_PROVIDER} ${HEIMDALL_POLICY_HASH} ${OPA_IMAGE}' \
  <"$ROOT/deploy/heimdall.yaml" |
  k apply -f - >/dev/null
k -n heimdall rollout status deploy/heimdall --timeout=180s >/dev/null

log "environment ready"
k get nodes
