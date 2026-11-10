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
