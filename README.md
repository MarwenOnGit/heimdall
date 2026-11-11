# Heimdall

An AI SRE agent for Kubernetes. When an alert fires, Heimdall investigates the
cluster, identifies the root cause with verifiable evidence, and either applies
a safe mitigation or proposes a fix as a pull request. Safety does not depend
on the agent behaving: every action passes a policy gate and is enforced again
by the Kubernetes API server.

The full design is in [heimdall-spec-v2.md](heimdall-spec-v2.md).

## How it works

An alert becomes an action only after passing several independent checks. The
model proposes; it never acts directly. (This is the target design. See
[Status](#status) for what is built.)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/pipeline-dark.png">
  <img alt="From alert to verified action" src="docs/img/pipeline.png">
</picture>

## Lab environment

Everything runs in one kind cluster on a laptop, built by `make up`.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/environment-dark.png">
  <img alt="Heimdall lab environment" src="docs/img/environment.png">
</picture>

## Status

Built milestone by milestone, following the [spec](heimdall-spec-v2.md). The
choices that differ from it are recorded in [docs/decisions.md](docs/decisions.md).

| Milestone | State |
|---|---|
| M1 Environment | done |
| M2 Chaos suite | 15 of 16 scenarios written; see the table below for which are verified |
| M3 Ingest, incidents, audit | done, running in the cluster |
| M4 Investigator | works end to end with a local model; accuracy not yet tuned or measured at scale |
| M5 Planner, policy gate, dry run | built and unit-tested end to end against the real policy engine and a fake Kubernetes API; **not yet run on the cluster** |
| M6 – M11 | not started |

### What works today

**Environment.** `make up` builds everything from nothing: a three-node kind
cluster, Prometheus, Alertmanager, Grafana, Loki, Alloy, Gitea, Argo CD,
Linkerd, Chaos Mesh and Online Boutique deployed from the in-cluster GitOps
repo. It takes about 25 minutes the first time and about 5 afterwards, because
every image is served from local pull-through caches. `make reset` returns a
damaged cluster to the baseline in seconds.

**Safety that does not depend on the agent.** Two Kubernetes identities: one
can only read, the other can only patch Deployments in one namespace. A
ValidatingAdmissionPolicy then limits what such a patch may contain: restart,
scale between 1 and 5, adjust resources within bounds, or switch to an image
that was already deployed from git. `make test-integration` proves it with 38
server-side dry-run cases (env, command, probes, volumes, privileges, labels
and unknown images are all rejected by the API server itself).

**Alert ingest.** Alertmanager posts to Heimdall with a bearer token; a
NetworkPolicy lets only Alertmanager and Prometheus connect. Every alert is
then confirmed against Prometheus, so a forged payload cannot invent an
incident even with a valid token. Related alerts are grouped into one incident
using the service dependency graph (a Redis outage is one incident, not three).

**Audit log.** Every alert, state change, tool call, model reply and diagnosis
is a row in a hash-chained log. Signed checkpoints are committed to a separate
git repository, so a log that was edited and re-hashed, or truncated, no
longer matches what was published. `heimdall audit verify` checks both.

**Investigator.** A hand-written loop gives the model read-only tools
(pods, events, logs, deployments, rollout diffs, nodes, PromQL, LogQL, git
history, service dependencies). Tool output is redacted for secrets, cleaned,
truncated and wrapped as untrusted data. Free-form PromQL and LogQL are parsed
and rejected unless confined to the allowed namespace. A diagnosis is accepted
only if every piece of evidence quotes text that really appears in the cited
tool call's output; otherwise it is rejected and, if the model cannot repair
it, the incident is escalated as unknown.

The model runs locally through Ollama (`qwen3:8b` on a laptop GPU), with no
cloud dependency. An API provider is not implemented yet.

**Planning, policy and dry run.** After a diagnosis, the planner lets the
model choose actions, but only action types that can fix the diagnosed kind
of cause, only on a resource named in the diagnosis, and only above the
confidence threshold; anything else is dropped and the incident is escalated.

For each proposal Heimdall builds the policy input itself, from the live
cluster and its own action record, never from model text. The OPA policy then
decides allow, needs approval or deny (33 rule tests, plus a property test
that throws random proposals at the real engine and checks that nothing
unsafe is let through). The gate denies when OPA cannot be reached.

An allowed action goes to the API server as a dry run, with the actor
identity and a precondition on the object's version, so the server runs the
whole request, admission policy included, and stores nothing. A change
between the policy check and the write is refused by the server. The same
action is never applied twice, and `heimdall freeze` stops all actions at
once, with no restart.

This path is tested with the real policy engine and a fake Kubernetes API. It
has not been run on the cluster yet, and there is no live mode: Heimdall
still changes nothing.

**Chaos suite.** Each scenario is a YAML file: how to inject the fault, which
alerts must fire, the true root cause and the acceptable fixes. Parameters
(target service, magnitude, timing) are sampled from a seed. The runner
injects, waits for the alerts, optionally waits for Heimdall and grades its
diagnosis without any model involved, then resets.

| ID | Fault | Expected alert | Verified |
|---|---|---|---|
| s01 | Commit points cartservice at a Redis address that does not exist | `HighErrorRate` frontend | alert seen firing; not re-run since the expectation was corrected |
| s02 | ConfigMap that checkoutservice mounts is deleted | `PodPendingConfig` checkoutservice | yes, 147 s |
| s03 | Commit lowers a memory limit below what the service needs | `ContainerOOMKilled` | yes, 30 s |
| s04 | Commit releases a frontend build that fails 20 to 40% of requests | `HighErrorRate` frontend | yes, 182 s |
| s05 | Commit sets an image tag that does not exist | `ImagePullFailing` | yes, 91 s (two runs) |
| s06 | Commit points a readiness probe at the wrong port | `PodNotReady` currencyservice | yes, 147 s |
| s07 | Redis scaled to zero | `HighErrorRate` frontend | alert seen firing; not re-run since the expectation was corrected |
| s08 | CPU stress inside adservice | `CPUThrottlingHigh` adservice | yes, 384 s |
| s09 | Network delay between frontend and productcatalogservice | `HighLatencyP95` frontend | yes, 228 s |
| s10 | Redis data volume fills up | `VolumeAlmostFull` redis-cart | not run yet |
| s11 | Two faults at once: low memory limit and bad image tag | both of the above | image half fired (86 s); memory half fixed, not re-run |
| s12 | Nothing wrong; a badly written rule flaps | `LatencyBudgetBurn` | rule fixed after a syntax error, not re-run |
| s13 | A worker node stops responding | `NodeNotReady` | not run yet |
| s14 | Harmless commit two minutes before Redis is scaled to zero | `HighErrorRate` frontend | not run yet |
| s16 | Redis is slow, not down | `HighLatencyP95` cartservice | not run yet |

"Verified" means one run in which the expected alert fired and the cluster
returned to baseline afterwards; the time is from injection to the alert. The
spec asks for ten clean runs per scenario, which has not been done.

One scenario has been run with Heimdall investigating: s05, where it named
the right cause (`bad_image_reference` on the right Deployment) in 96 seconds
with 7 tool calls. That is one run, not an accuracy figure.

### Known gaps

- The diagnosis accuracy target for M4 (8 of 10 on four scenarios) has not
  been measured. Running every scenario with a local model on the GPU is
  heavy for a laptop; it needs to be done in small batches.
- Dry-run mode has only been exercised in unit tests, not on the cluster.
- Pull requests, human approval, verification and rollback (M6) are not
  built. An action that needs approval is recorded and the incident escalated.
- s15 (a forced bad fix that must be rolled back) needs M6.
- The hold-out scenarios are deliberately not written by the author
  ([why](docs/decisions.md)).
- `HighLatencyP95` uses one threshold for every service.

## Quickstart

Requirements: Linux, Docker, [kind](https://kind.sigs.k8s.io/), kubectl,
[uv](https://docs.astral.sh/uv/), 16 GB RAM (32 GB recommended), 4 cores,
40 GB free disk. Helm and OPA are downloaded into `.bin/` at pinned versions.
For the investigator, [Ollama](https://ollama.com) with `qwen3:8b` pulled;
without it Heimdall still groups alerts into incidents but does not
investigate.

```sh
make up                # create the cluster and install everything (about 25 min the first time)
make test              # unit tests and policy tests
make test-integration  # RBAC and admission policy checks against the cluster
make reset             # return the cluster to the baseline state
make down              # delete the cluster

uv run python -m chaos.run s05 --seed 1 --agent   # inject one fault and grade the diagnosis
uv run python -m chaos.suite --runs 3 --agent     # every scenario, three seeds each
```

A warning about load: the cluster, the workload's load generator and a local
model on the GPU together push a laptop hard, and the suite runs for hours.
Run single scenarios, or small batches, and watch the temperature.

Every tool, image and chart version is pinned in
[infra/versions.env](infra/versions.env).
