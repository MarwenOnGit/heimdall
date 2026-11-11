#!/usr/bin/env bash
# Proves what the two Heimdall identities can and cannot do, using server-side
# dry run so nothing in the cluster changes. Each case states the expected
# outcome and which layer must produce it:
#   allow      the request is accepted
#   rbac       rejected by RBAC (403 Forbidden)
#   admission  rejected by the ValidatingAdmissionPolicy
set -uo pipefail
# shellcheck source=../../lib.sh
. "$(dirname "$0")/../../lib.sh"

ACTOR=system:serviceaccount:heimdall:heimdall-actor
READER=system:serviceaccount:heimdall:heimdall-readonly
D=frontend
OTHER_IMAGE="$(k -n boutique get deploy cartservice -o jsonpath='{.spec.template.spec.containers[0].image}')"

pass=0
fail=0

check() { # check <expected> <description> <command...>
  local expected="$1" desc="$2" out actual
  shift 2
  if out="$("$@" 2>&1)"; then
    actual=allow
  elif grep -q 'ValidatingAdmissionPolicy' <<<"$out"; then
    actual=admission
  elif grep -qi 'forbidden' <<<"$out"; then
    actual=rbac
  else
    actual="error"
  fi
  if [ "$actual" = "$expected" ]; then
    pass=$((pass + 1))
    printf '  \033[1;32mpass\033[0m %-9s %s\n' "$expected" "$desc"
  else
    fail=$((fail + 1))
    printf '  \033[1;31mFAIL\033[0m %-9s %s (got %s)\n      %s\n' "$expected" "$desc" "$actual" "$(tail -1 <<<"$out")"
  fi
}

actor()  { k --as "$ACTOR" "$@"; }
reader() { k --as "$READER" "$@"; }

# The actor may patch but not read, and `kubectl patch` reads first. So actor
# patches go straight to the API with the ServiceAccount's own token, which
# also tests the real identity instead of impersonation.
server="$(kubectl config view -o jsonpath="{.clusters[?(@.name==\"$KUBE_CONTEXT\")].cluster.server}")"
ca="$(mktemp)"
trap 'rm -f "$ca"' EXIT
kubectl config view --raw -o jsonpath="{.clusters[?(@.name==\"$KUBE_CONTEXT\")].cluster.certificate-authority-data}" |
  base64 -d >"$ca"
token="$(k -n heimdall create token heimdall-actor --duration 10m)"

# api_patch <path> <merge|json> <body>: dry-run PATCH as the actor. Succeeds on
# HTTP 200, otherwise prints the API server's message and fails.
api_patch() {
  local resp code
  resp="$(curl -sS --cacert "$ca" -X PATCH -H "Authorization: Bearer $token" \
    -H "Content-Type: application/$2-patch+json" -w '\n%{http_code}' \
    -d "$3" "${server}$1?dryRun=All")"
  code="$(tail -1 <<<"$resp")"
  [ "$code" = 200 ] && return 0
  sed '$d' <<<"$resp" | jq -r '.message // .' >&2
  return 1
}
patch() { api_patch "/apis/apps/v1/namespaces/boutique/deployments/$D" "$1" "$2"; }
scale() { api_patch "/apis/apps/v1/namespaces/boutique/deployments/$D/scale" merge "{\"spec\":{\"replicas\":$1}}"; }
c0=/spec/template/spec/containers/0

log "actor: allowed changes"
check allow "restart (restartedAt annotation)" \
  patch merge '{"spec":{"template":{"metadata":{"annotations":{"kubectl.kubernetes.io/restartedAt":"2026-01-01T00:00:00Z"}}}}}'
check allow "scale to 3 through the scale subresource" \
  scale 3
check allow "replicas 5 on the Deployment" patch merge '{"spec":{"replicas":5}}'
check allow "memory limit 256Mi" \
  patch json "[{\"op\":\"replace\",\"path\":\"$c0/resources/limits/memory\",\"value\":\"256Mi\"}]"
check allow "image that is in the allowed list" \
  patch json "[{\"op\":\"replace\",\"path\":\"$c0/image\",\"value\":\"$OTHER_IMAGE\"}]"

log "actor: changes the admission policy must reject"
check admission "image not deployed from git" \
  patch json "[{\"op\":\"replace\",\"path\":\"$c0/image\",\"value\":\"docker.io/attacker/miner:latest\"}]"
check admission "change an env var" \
  patch json "[{\"op\":\"add\",\"path\":\"$c0/env/-\",\"value\":{\"name\":\"X\",\"value\":\"y\"}}]"
check admission "set a command" \
  patch json "[{\"op\":\"add\",\"path\":\"$c0/command\",\"value\":[\"sh\",\"-c\",\"id\"]}]"
check admission "change the readiness probe" \
  patch json "[{\"op\":\"replace\",\"path\":\"$c0/readinessProbe/httpGet/path\",\"value\":\"/nope\"}]"
check admission "add a container" \
  patch json "[{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/-\",\"value\":{\"name\":\"x\",\"image\":\"$OTHER_IMAGE\",\"resources\":{\"limits\":{\"cpu\":\"100m\",\"memory\":\"64Mi\"}}}}]"
check admission "add a hostPath volume" \
  patch json '[{"op":"add","path":"/spec/template/spec/volumes","value":[{"name":"host","hostPath":{"path":"/"}}]}]'
check admission "change the service account" \
  patch merge '{"spec":{"template":{"spec":{"serviceAccountName":"default"}}}}'
check admission "make the container privileged" \
  patch json "[{\"op\":\"replace\",\"path\":\"$c0/securityContext\",\"value\":{\"privileged\":true}}]"
check admission "enable hostNetwork" patch merge '{"spec":{"template":{"spec":{"hostNetwork":true}}}}'
check admission "add another pod template annotation" \
  patch merge '{"spec":{"template":{"metadata":{"annotations":{"linkerd.io/inject":"disabled"}}}}}'
check admission "change a Deployment label" patch merge '{"metadata":{"labels":{"heimdall.io/stateful":"false"}}}'
check admission "replicas 0 on the Deployment" patch merge '{"spec":{"replicas":0}}'
check admission "replicas 6 on the Deployment" patch merge '{"spec":{"replicas":6}}'
check admission "scale to 0 through the scale subresource" \
  scale 0
check admission "scale to 6 through the scale subresource" \
  scale 6
check admission "memory limit 2Gi" \
  patch json "[{\"op\":\"replace\",\"path\":\"$c0/resources/limits/memory\",\"value\":\"2Gi\"}]"
check admission "CPU limit 2" \
  patch json "[{\"op\":\"replace\",\"path\":\"$c0/resources/limits/cpu\",\"value\":\"2\"}]"
check admission "remove the resource limits" \
  patch json "[{\"op\":\"remove\",\"path\":\"$c0/resources/limits\"}]"

log "actor: the Argo CD Application"
app=/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/boutique
check allow "pause automatic sync" api_patch "$app" merge '{"spec":{"syncPolicy":{"automated":null}}}'
check allow "resume automatic sync" \
  api_patch "$app" merge '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":false}}}}'
check admission "switch self-heal on" \
  api_patch "$app" merge '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
check admission "point the Application at another path" \
  api_patch "$app" merge '{"spec":{"source":{"path":"somewhere-else"}}}'
check admission "deploy into another namespace" \
  api_patch "$app" merge '{"spec":{"destination":{"namespace":"kube-system"}}}'
check admission "trigger a sync operation" \
  api_patch "$app" merge '{"operation":{"sync":{"revision":"main"}}}'
check rbac "patch another Application" \
  api_patch /apis/argoproj.io/v1alpha1/namespaces/argocd/applications/other merge '{"spec":{}}'

log "actor: operations RBAC must reject"
check rbac "delete a Deployment" actor -n boutique delete deploy "$D" --dry-run=server
check rbac "read a Deployment" actor -n boutique get deploy "$D"
check rbac "create a pod" actor -n boutique run x --image=busybox --dry-run=server
check rbac "read Secrets" actor -n boutique get secrets
check rbac "patch a Deployment outside boutique" \
  api_patch /apis/apps/v1/namespaces/monitoring/deployments/kps-operator merge '{"spec":{"replicas":2}}'
check rbac "patch the allowed-images list" \
  api_patch /api/v1/namespaces/heimdall/configmaps/heimdall-allowed-images merge '{"data":{"images":"x"}}'
check rbac "patch the admission policy itself" \
  api_patch /apis/admissionregistration.k8s.io/v1/validatingadmissionpolicies/heimdall-actor-deployments merge '{"spec":{"failurePolicy":"Ignore"}}'

log "readonly: what it can and cannot do"
check allow "list pods in boutique" reader -n boutique get pods
check allow "read pod logs in boutique" reader -n boutique logs "deploy/$D" -c server --tail=1
check allow "list nodes" reader get nodes
check rbac "read Secrets in boutique" reader -n boutique get secrets
check rbac "read Secrets in heimdall" reader -n heimdall get secrets
check rbac "patch a Deployment" \
  reader -n boutique patch deploy "$D" --dry-run=server --type merge -p '{"spec":{"replicas":2}}'
check rbac "delete a pod" reader -n boutique delete pod --all --dry-run=server
check rbac "list pods outside boutique" reader -n monitoring get pods

echo
if [ "$fail" -eq 0 ]; then
  log "all ${pass} cases passed"
else
  die "${fail} of $((pass + fail)) cases failed"
fi
