#!/usr/bin/env bash
# Checks the machine before bootstrap and fails early with a clear message.
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

MIN_RAM_GB=16
MIN_AVAIL_GB=8
MIN_CORES=4
MIN_DISK_GB=40

fail=0
bad() { printf '  \033[1;31mFAIL\033[0m %s\n' "$*"; fail=1; }
ok()  { printf '  \033[1;32m ok \033[0m %s\n' "$*"; }
note(){ printf '  \033[1;33mnote\033[0m %s\n' "$*"; }

log "preflight"

for t in docker kind kubectl curl jq openssl git; do
  if command -v "$t" >/dev/null; then ok "$t found"; else bad "$t is not installed"; fi
done

if docker info >/dev/null 2>&1; then ok "docker daemon reachable"; else bad "docker daemon not reachable"; fi

ram_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024 + 0.5}' /proc/meminfo)
avail_gb=$(awk '/MemAvailable/ {printf "%d", $2/1024/1024}' /proc/meminfo)
cores=$(nproc)
disk_gb=$(df -BG --output=avail "$ROOT" | tail -1 | tr -dc '0-9')

if [ "$ram_gb" -ge "$MIN_RAM_GB" ]; then ok "RAM ${ram_gb} GB (min ${MIN_RAM_GB})"; else bad "RAM ${ram_gb} GB, need ${MIN_RAM_GB}"; fi
if [ "$cores" -ge "$MIN_CORES" ]; then ok "${cores} cores (min ${MIN_CORES})"; else bad "${cores} cores, need ${MIN_CORES}"; fi
if [ "$disk_gb" -ge "$MIN_DISK_GB" ]; then ok "${disk_gb} GB free disk (min ${MIN_DISK_GB})"; else bad "${disk_gb} GB free disk, need ${MIN_DISK_GB}"; fi

cluster_exists=0
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then cluster_exists=1; fi

# Available memory and the registry port only matter before the cluster exists;
# afterwards the cluster itself is what is using them.
if [ "$cluster_exists" -eq 0 ]; then
  if [ "$avail_gb" -ge "$MIN_AVAIL_GB" ]; then
    ok "${avail_gb} GB memory available (min ${MIN_AVAIL_GB})"
  else
    bad "only ${avail_gb} GB memory available, need ${MIN_AVAIL_GB}; close something first"
  fi
  if docker ps --format '{{.Names}}' | grep -qx "$REGISTRY_NAME"; then
    ok "registry container already running"
  elif ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":${REGISTRY_PORT}\$"; then
    bad "port ${REGISTRY_PORT} is in use (needed by the local registry)"
  else
    ok "port ${REGISTRY_PORT} free"
  fi
else
  ok "cluster ${CLUSTER_NAME} already exists"
fi

others=$(kind get clusters 2>/dev/null | grep -vx "$CLUSTER_NAME" | tr '\n' ' ' || true)
[ -n "$others" ] && note "other kind clusters are running and share this machine's memory: ${others}"

# kind with several nodes needs generous inotify limits, otherwise pods fail
# with "too many open files".
inst=$(cat /proc/sys/fs/inotify/max_user_instances)
watches=$(cat /proc/sys/fs/inotify/max_user_watches)
if [ "$inst" -ge 512 ] && [ "$watches" -ge 524288 ]; then
  ok "inotify limits (${inst} instances, ${watches} watches)"
else
  bad "inotify limits too low (${inst} instances, ${watches} watches); run:
       sudo sysctl fs.inotify.max_user_instances=512 fs.inotify.max_user_watches=524288"
fi

[ "$fail" -eq 0 ] || die "preflight failed"
log "preflight passed"
