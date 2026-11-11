#!/usr/bin/env bash
# Builds the Heimdall image, pushes it to the local registry, and prints the
# image reference. The tag is a hash of the sources, so the image (and the
# Deployment that uses it) only changes when the code does.
set -euo pipefail
# shellcheck source=../infra/lib.sh
. "$(dirname "$0")/../infra/lib.sh"

cd "$ROOT"
tag="$({
  cat Dockerfile pyproject.toml uv.lock docs/dependencies.yaml
  find heimdall \( -name '*.py' -o -name '*.md' \) -print0 | sort -z | xargs -0 cat
} | sha256sum | cut -c1-12)"
image="localhost:${REGISTRY_PORT}/heimdall/heimdall:${tag}"

if ! curl -fsS -o /dev/null "http://localhost:${REGISTRY_PORT}/v2/heimdall/heimdall/manifests/${tag}" \
  -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' 2>/dev/null; then
  log "building ${image}" >&2
  docker build -q -t "$image" --build-arg PYTHON_IMAGE="$PYTHON_IMAGE" \
    --build-arg UV_IMAGE="$UV_IMAGE" . >/dev/null
  docker push -q "$image" >/dev/null
fi
echo "$image"
