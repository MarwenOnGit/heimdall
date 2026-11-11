#!/usr/bin/env bash
# Gives Heimdall its own Gitea identity: a `heimdall-bot` user with an access
# token, write access to the GitOps repo (for pull requests) and to the
# `audit-anchors` repo, where signed audit checkpoints are published.
# Idempotent: does nothing once the token Secret exists.
set -euo pipefail
# shellcheck source=../lib.sh
. "$(dirname "$0")/../lib.sh"

ORG=heimdall
BOT=heimdall-bot

if k -n heimdall get secret heimdall-gitea >/dev/null 2>&1; then
  exit 0
fi

admin="$(k -n gitea get secret gitea-admin -o jsonpath='{.data.username}' | base64 -d)"
admin_pass="$(k -n gitea get secret gitea-admin -o jsonpath='{.data.password}' | base64 -d)"

pf_log="$(mktemp)"
resp="$(mktemp)"
k -n gitea port-forward --address 127.0.0.1 svc/gitea-http :3000 >"$pf_log" 2>&1 &
pf_pid=$!
trap 'kill "$pf_pid" 2>/dev/null || true; rm -f "$pf_log" "$resp"' EXIT
port=""
for _ in $(seq 1 50); do
  port="$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9]*\).*/\1/p' "$pf_log" | head -1)"
  [ -n "$port" ] && break
  sleep 0.2
done
[ -n "$port" ] || die "could not port-forward to Gitea: $(cat "$pf_log")"

api() { # api <user:pass> <method> <path> [json body]
  local auth="$1" method="$2" path="$3" body="${4:-}"
  curl -sS -o "$resp" -w '%{http_code}' -u "$auth" -X "$method" \
    -H 'Content-Type: application/json' ${body:+-d "$body"} "http://127.0.0.1:${port}/api/v1${path}"
}
expect() { # expect <description> <acceptable codes...> ; reads code from stdin
  local what="$1" code
  shift
  read -r code || true # curl prints the code without a newline
  for ok in "$@"; do [ "$code" = "$ok" ] && return 0; done
  die "${what} failed with HTTP ${code}: $(cat "$resp")"
}

log "gitea: creating ${BOT} and the audit-anchors repo"
bot_pass="$(openssl rand -hex 16)"
api "${admin}:${admin_pass}" DELETE "/admin/users/${BOT}?purge=true" >/dev/null
api "${admin}:${admin_pass}" POST /admin/users \
  "{\"username\":\"${BOT}\",\"email\":\"${BOT}@heimdall.local\",\"password\":\"${bot_pass}\",\"must_change_password\":false}" |
  expect "creating ${BOT}" 201

api "${admin}:${admin_pass}" POST "/orgs/${ORG}/repos" \
  '{"name":"audit-anchors","default_branch":"main","private":false,"auto_init":true,"description":"Signed checkpoints of the Heimdall audit log"}' |
  expect "creating audit-anchors" 201 409

for repo in audit-anchors gitops; do
  api "${admin}:${admin_pass}" PUT "/repos/${ORG}/${repo}/collaborators/${BOT}" '{"permission":"write"}' |
    expect "granting ${BOT} access to ${repo}" 204
done

api "${BOT}:${bot_pass}" POST "/users/${BOT}/tokens" \
  '{"name":"heimdall","scopes":["write:repository"]}' | expect "creating the access token" 201
token="$(jq -r .sha1 "$resp")"

k -n heimdall create secret generic heimdall-gitea \
  --from-literal=username="$BOT" --from-literal=token="$token" >/dev/null
