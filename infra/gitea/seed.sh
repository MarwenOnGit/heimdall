#!/usr/bin/env bash
# Creates the GitOps repo in Gitea, pushes gitops/ into it the first time, and
# registers the webhook that tells Argo CD about pushes. Idempotent.
set -euo pipefail
# shellcheck source=../lib.sh
. "$(dirname "$0")/../lib.sh"

ORG=heimdall
REPO=gitops

user="$(k -n gitea get secret gitea-admin -o jsonpath='{.data.username}' | base64 -d)"
pass="$(k -n gitea get secret gitea-admin -o jsonpath='{.data.password}' | base64 -d)"

# Reach Gitea through a temporary port-forward on a random local port.
pf_log="$(mktemp)"
k -n gitea port-forward --address 127.0.0.1 svc/gitea-http :3000 >"$pf_log" 2>&1 &
pf_pid=$!
work="$(mktemp -d)"
trap 'kill "$pf_pid" 2>/dev/null || true; rm -rf "$pf_log" "$work"' EXIT

port=""
for _ in $(seq 1 50); do
  port="$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9]*\).*/\1/p' "$pf_log" | head -1)"
  [ -n "$port" ] && break
  sleep 0.2
done
[ -n "$port" ] || die "could not port-forward to Gitea: $(cat "$pf_log")"
base="http://127.0.0.1:${port}"

api() { # api <method> <path> [json body]
  local method="$1" path="$2" body="${3:-}"
  curl -sS -o "$work/resp" -w '%{http_code}' -u "${user}:${pass}" -X "$method" \
    -H 'Content-Type: application/json' ${body:+-d "$body"} "${base}/api/v1${path}"
}

for _ in $(seq 1 30); do
  [ "$(api GET /version)" = 200 ] && break
  sleep 1
done

if [ "$(api GET "/orgs/${ORG}")" != 200 ]; then
  log "gitea: creating org ${ORG}"
  [ "$(api POST /orgs "{\"username\":\"${ORG}\"}")" = 201 ] || die "org create failed: $(cat "$work/resp")"
fi

if [ "$(api GET "/repos/${ORG}/${REPO}")" != 200 ]; then
  log "gitea: creating repo ${ORG}/${REPO}"
  [ "$(api POST "/orgs/${ORG}/repos" "{\"name\":\"${REPO}\",\"default_branch\":\"main\",\"private\":false}")" = 201 ] ||
    die "repo create failed: $(cat "$work/resp")"
  api GET "/repos/${ORG}/${REPO}" >/dev/null
fi

if [ "$(jq -r .empty "$work/resp")" = true ]; then
  log "gitea: pushing initial gitops content"
  cp -r "$ROOT/gitops/." "$work/repo"
  git -C "$work/repo" init -q -b main
  git -C "$work/repo" add -A
  git -C "$work/repo" -c user.name=bootstrap -c user.email=bootstrap@heimdall.local \
    commit -q -m "Initial GitOps content"
  git -C "$work/repo" push -q "http://${user}:${pass}@127.0.0.1:${port}/${ORG}/${REPO}.git" main
fi

hook_url="http://argocd-server.argocd.svc/api/webhook"
api GET "/repos/${ORG}/${REPO}/hooks" >/dev/null
if ! jq -e --arg u "$hook_url" 'any(.[]; .config.url == $u)' "$work/resp" >/dev/null; then
  log "gitea: registering Argo CD webhook"
  [ "$(api POST "/repos/${ORG}/${REPO}/hooks" \
    "{\"type\":\"gitea\",\"active\":true,\"events\":[\"push\"],\"config\":{\"url\":\"${hook_url}\",\"content_type\":\"json\"}}")" = 201 ] ||
    die "webhook create failed: $(cat "$work/resp")"
fi
