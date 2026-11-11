#!/usr/bin/env bash
# Runs one pull-through cache per upstream registry and points every node at
# them. The first `make up` fills the caches; later ones pull from them, so a
# rebuild is fast and works without internet access.
#
#   caches.sh up     start the caches and configure the nodes (idempotent)
#   caches.sh down   remove the cache containers (volumes are kept)
#   caches.sh purge  remove the cache containers and their volumes
set -euo pipefail
# shellcheck source=../lib.sh
. "$(dirname "$0")/../lib.sh"

# registry host as written in image names -> URL the cache proxies.
# A third field names another host whose cache serves this one: cr.l5d.io only
# redirects to ghcr.io/linkerd, which a pull-through cache cannot follow.
UPSTREAMS="
docker.io=https://registry-1.docker.io
quay.io=https://quay.io
ghcr.io=https://ghcr.io
registry.k8s.io=https://registry.k8s.io
cr.l5d.io=https://cr.l5d.io=ghcr.io
docker.gitea.com=https://docker.gitea.com
ecr-public.aws.com=https://ecr-public.aws.com
us-central1-docker.pkg.dev=https://us-central1-docker.pkg.dev
"

cmd="${1:-up}"

for entry in $UPSTREAMS; do
  IFS='=' read -r host url served_by <<<"$entry"
  name="heimdall-cache-${served_by:-$host}"

  case "$cmd" in
    up)
      if [ -z "$served_by" ] && [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || true)" != "true" ]; then
        log "starting image cache for ${host}"
        docker rm -f "$name" >/dev/null 2>&1 || true
        # TTL 0 keeps cached images forever instead of expiring them.
        docker run -d --restart=always --name "$name" --network kind \
          -e REGISTRY_PROXY_REMOTEURL="$url" -e REGISTRY_PROXY_TTL=0 \
          -e OTEL_TRACES_EXPORTER=none -e REGISTRY_LOG_LEVEL=warn \
          -v "${name}:/var/lib/registry" "$REGISTRY_CACHE_IMAGE" >/dev/null
      fi
      for node in $(kind get nodes --name "$CLUSTER_NAME"); do
        dir="/etc/containerd/certs.d/${host}"
        docker exec "$node" mkdir -p "$dir"
        printf 'server = "%s"\n\n[host."http://%s:5000"]\n  capabilities = ["pull", "resolve"]\n' \
          "$url" "$name" | docker exec -i "$node" cp /dev/stdin "$dir/hosts.toml"
      done
      ;;
    down | purge)
      [ -z "$served_by" ] || continue
      docker rm -f "$name" >/dev/null 2>&1 || true
      if [ "$cmd" = purge ]; then docker volume rm -f "$name" >/dev/null; fi
      ;;
    *) die "usage: caches.sh up|down|purge" ;;
  esac
done
