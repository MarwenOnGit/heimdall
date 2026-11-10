# Architecture notes

The design is in [the spec](../heimdall-spec-v2.md). This file records facts
measured on the running system.

## Memory footprint

Steady state after `make up`, load generator running, measured on
2026-11-10 (working set of all containers, from cAdvisor):

| Namespace | MiB |
|---|---|
| kube-system | 1796 |
| monitoring (Prometheus, Alertmanager, Grafana, Loki, Alloy) | 1291 |
| argocd | 431 |
| boutique (12 services, each with a Linkerd proxy) | 424 |
| linkerd | 95 |
| gitea | 93 |
| chaos-mesh | 67 |
| heimdall (alert echo endpoint only, for now) | 13 |
| local-path-storage | 12 |
| **Total** | **4222** |

The three kind node containers together use about 5.3 GiB as reported by
`docker stats`, which also counts the kubelet and containerd.

Linkerd adds little: 95 MiB for the control plane, and the proxies are
included in the `boutique` figure. It stays as the mesh.

## Timings

| Operation | Time |
|---|---|
| `make up` from scratch, images not cached | about 25 min |
| `make up` with nothing to change | about 3 min |
| `make reset` after a bad image, Redis scaled to 0, a deleted ConfigMap, a chaos experiment and a rogue git commit | 8 s |
| Broken Deployment to alert delivered at the ingest endpoint | 98 s |
