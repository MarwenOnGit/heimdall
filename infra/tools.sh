#!/usr/bin/env bash
# Downloads the pinned CLI tools into .bin/. Idempotent.
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

mkdir -p "$ROOT/.bin"

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$(uname -m)" in
  x86_64) arch=amd64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *) die "unsupported architecture $(uname -m)" ;;
esac

# $2 is a list of arguments and must be split.
# shellcheck disable=SC2086
have() { [ -x "$ROOT/.bin/$1" ] && "$ROOT/.bin/$1" $2 2>/dev/null | grep -q "$3"; }

if ! have helm "version --short" "${HELM_VERSION}"; then
  log "installing helm ${HELM_VERSION}"
  tmp="$(mktemp -d)"
  curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-${os}-${arch}.tar.gz" | tar -xz -C "$tmp"
  install -m 0755 "$tmp/${os}-${arch}/helm" "$ROOT/.bin/helm"
  rm -rf "$tmp"
fi

if ! have opa "version" "${OPA_VERSION#v}"; then
  log "installing opa ${OPA_VERSION}"
  curl -fsSL -o "$ROOT/.bin/opa" \
    "https://github.com/open-policy-agent/opa/releases/download/${OPA_VERSION}/opa_${os}_${arch}_static"
  chmod 0755 "$ROOT/.bin/opa"
fi

log "tools ready: $(helm version --short), opa $(opa version | awk '/^Version/ {print $2}')"
