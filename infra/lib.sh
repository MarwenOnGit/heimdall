#!/usr/bin/env bash
# Shared helpers for the scripts in infra/. Source this, do not run it.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROOT
export PATH="$ROOT/.bin:$PATH"

set -a
# shellcheck source=versions.env
. "$ROOT/infra/versions.env"
set +a

export KUBE_CONTEXT="kind-${CLUSTER_NAME}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Always address the heimdall cluster explicitly, so a different current
# context on this machine is never touched.
k() { kubectl --context "$KUBE_CONTEXT" "$@"; }
h() { helm --kube-context "$KUBE_CONTEXT" "$@"; }

# helm_install <release> <namespace> <chart> <repo-url> <version> [extra helm args]
# Installs or upgrades a pinned chart straight from its repo URL, so nothing
# depends on the user's local `helm repo` configuration.
helm_install() {
  local release="$1" ns="$2" chart="$3" repo="$4" version="$5"
  shift 5
  log "helm: ${release} (${chart} ${version}) -> ${ns}"
  h upgrade --install "$release" "$chart" --repo "$repo" --version "$version" \
    --namespace "$ns" --wait --timeout 10m "$@" >/dev/null
}

# ensure_secret <namespace> <name> <key> <value>: creates the Secret only if it
# does not exist, so generated credentials stay stable across runs.
ensure_secret() {
  k -n "$1" get secret "$2" >/dev/null 2>&1 ||
    k -n "$1" create secret generic "$2" --from-literal="$3=$4" >/dev/null
}
