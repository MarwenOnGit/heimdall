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
| M1 Environment | in progress: cluster, local registry and namespaces come up with `make up` |
| M2 – M11 | not started |

## Quickstart

Requirements: Linux, Docker, [kind](https://kind.sigs.k8s.io/), kubectl,
[uv](https://docs.astral.sh/uv/), 16 GB RAM (32 GB recommended), 4 cores,
40 GB free disk. Helm and OPA are downloaded into `.bin/` at pinned versions.

```sh
make up      # create the cluster and install everything
make test    # unit tests
make down    # delete the cluster
```

Every tool, image and chart version is pinned in
[infra/versions.env](infra/versions.env).
