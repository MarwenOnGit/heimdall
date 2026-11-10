#!/usr/bin/env bash
# Returns the cluster to the baseline state without rebuilding it. Used
# between chaos runs. Undoes: chaos experiments, paused nodes, commits and
# pull requests in the GitOps repo, and any change made directly to the
# workload (images, resources, replicas, deleted ConfigMap, paused sync).
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

start=$(date +%s)

# Chaos Mesh experiments of every kind, in every namespace.
kinds="$(k api-resources --api-group=chaos-mesh.org -o name 2>/dev/null | paste -sd, -)"
if [ -n "$kinds" ]; then
  k delete "$kinds" --all -A --ignore-not-found --wait=false >/dev/null
fi

# Nodes paused by a node-failure scenario.
for node in $(kind get nodes --name "$CLUSTER_NAME"); do
  if [ "$(docker inspect -f '{{.State.Paused}}' "$node")" = "true" ]; then
    log "unpausing node ${node}"
    docker unpause "$node" >/dev/null
  fi
done

"$ROOT/infra/gitea/seed.sh" --reset
[ -n "${RESET_DEBUG:-}" ] && log "git restored at $(($(date +%s) - start))s"
ensure_checkout_settings

# Restore the Application (automated sync may have been paused), then run one
# explicit sync: with self-heal off, Argo CD does not undo live changes on its
# own.
k apply -f "$ROOT/infra/argocd/application-boutique.yaml" >/dev/null
k -n argocd patch application boutique --type merge \
  -p '{"operation":{"sync":{"revision":"main","prune":true}}}' >/dev/null
for _ in $(seq 1 60); do
  [ -z "$(k -n argocd get application boutique -o jsonpath='{.operation}')" ] && break
  sleep 1
done
phase="$(k -n argocd get application boutique -o jsonpath='{.status.operationState.phase}')"
[ "$phase" = "Succeeded" ] || die "Argo CD sync ended as ${phase:-unknown}"
[ -n "${RESET_DEBUG:-}" ] && log "synced at $(($(date +%s) - start))s"

# Replicas are ignored by Argo CD on purpose, so they are restored here.
k -n boutique scale deploy --all --replicas=1 >/dev/null

# Pods that are not ready now are either from a broken revision or stuck in a
# back-off that would take minutes to retry. Deleting them lets the restored
# Deployments start clean pods immediately.
stuck="$(k -n boutique get pods -o json |
  jq -r '.items[] | select(.metadata.deletionTimestamp == null) |
    select((.status.containerStatuses // []) | length == 0 or any(.[]; .ready | not)) | .metadata.name')"
if [ -n "$stuck" ]; then
  # shellcheck disable=SC2086  # one argument per pod name
  k -n boutique delete pod $stuck --wait=false >/dev/null
fi
[ -n "${RESET_DEBUG:-}" ] && log "pods kicked at $(($(date +%s) - start))s"

wait_boutique_healthy 300 || die "cluster did not return to baseline within 5 minutes"
log "baseline restored in $(($(date +%s) - start))s"
