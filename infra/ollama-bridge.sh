#!/usr/bin/env bash
# Makes the Ollama server on this machine reachable from the cluster as
# http://ollama.heimdall.svc:11434, so Heimdall can use a local model with no
# cloud dependency.
#
# Ollama listens on 127.0.0.1 only. A small relay container on the host
# network forwards to it, bound to the kind bridge address so it is reachable
# from the cluster nodes but not from the rest of the network.
#
# Prints "ollama" when the bridge is up, "off" when Ollama is not running.
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

NAME=heimdall-ollama-bridge
PORT=11435

if ! curl -fsS -o /dev/null --max-time 3 http://127.0.0.1:11434/api/version; then
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  warn "Ollama is not running on this machine; Heimdall will group alerts but not investigate"
  echo off
  exit 0
fi

gateway="$(docker network inspect kind |
  jq -r '.[0].IPAM.Config[].Gateway | select(test("^[0-9.]+$"))' | head -1)"
[ -n "$gateway" ] || die "could not find the IPv4 gateway of the kind network"

if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)" != "true" ]; then
  log "starting the Ollama bridge on ${gateway}:${PORT}" >&2
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --restart=always --name "$NAME" --network host "$SOCAT_IMAGE" \
    "TCP-LISTEN:${PORT},bind=${gateway},fork,reuseaddr" TCP:127.0.0.1:11434 >/dev/null
fi

# A Service without a selector, pointed at the bridge by hand.
k apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Service
metadata:
  name: ollama
  namespace: heimdall
spec:
  ports: [{port: 11434, targetPort: ${PORT}, name: http}]
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: ollama-host
  namespace: heimdall
  labels: {kubernetes.io/service-name: ollama}
addressType: IPv4
ports: [{port: ${PORT}, name: http}]
endpoints: [{addresses: ["${gateway}"]}]
YAML
echo ollama
