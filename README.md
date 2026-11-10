# Heimdall

An AI SRE agent for Kubernetes. When an alert fires, Heimdall investigates the
cluster, identifies the root cause with verifiable evidence, and either applies
a safe mitigation or proposes a fix as a pull request. Safety does not depend
on the agent behaving: every action passes a policy gate and is enforced again
by the Kubernetes API server.

The full design is in [heimdall-spec-v2.md](heimdall-spec-v2.md).

## Status

Work in progress, built milestone by milestone.

| Milestone | State |
|---|---|
| M1 Environment | mostly done, see below |
| M2 – M11 | not started |

M1, verified on a running cluster:

- `make up` builds everything from scratch and is safe to re-run: kind
  (1 control plane, 2 workers), local registry, Prometheus, Alertmanager,
  Grafana, Loki, Alloy, Gitea, Linkerd, Argo CD and Online Boutique deployed
  from the in-cluster GitOps repo.
- Per-service success rate and latency from the mesh, logs for every service
  in Loki, 11 alert rules.
- An alert travels from a broken Deployment to the token-protected ingest
  endpoint in about 100 seconds. Requests without the token get 401, and a
  NetworkPolicy lets only Alertmanager connect.
- `make test-integration` runs 38 server-side dry-run cases against the two
  Heimdall identities: what RBAC rejects, what the admission policy rejects,
  and what is allowed.

- `make reset` returns a damaged cluster to the baseline in seconds, including
  the GitOps repo.

M1, still to do: mirroring images into the local registry for offline use.
Measured memory use and timings are in [docs/architecture.md](docs/architecture.md).

## Quickstart

Requirements: Linux, Docker, [kind](https://kind.sigs.k8s.io/), kubectl,
[uv](https://docs.astral.sh/uv/), 16 GB RAM (32 GB recommended), 4 cores,
40 GB free disk. Helm and OPA are downloaded into `.bin/` at pinned versions.

```sh
make up                # create the cluster and install everything (about 25 min the first time)
make test              # unit tests
make test-integration  # RBAC and admission policy checks against the cluster
make reset             # return the cluster to the baseline state
make down              # delete the cluster
```

Every tool, image and chart version is pinned in
[infra/versions.env](infra/versions.env).
