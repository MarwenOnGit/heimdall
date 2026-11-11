# Decisions

Short records of choices that differ from, or add detail to, the
[spec](../heimdall-spec-v2.md). Newest last.

## 1. Linkerd stays as the mesh

The spec left the mesh open pending a memory measurement. Linkerd's control
plane uses about 95 MiB and the per-pod proxies are small
([measurements](architecture.md)). It is used only for request metrics.

## 2. Pull-through caches instead of pre-pulling images

The spec asks for all upstream images to be pre-pulled into the local
registry. Instead, one pull-through cache runs per upstream registry and
containerd on each node is pointed at them. The first `make up` fills the
caches; later ones are served from them. This needs no list of images to
maintain, and a rebuild drops from about 25 minutes to about 5.

`cr.l5d.io` only redirects to `ghcr.io/linkerd`, which a pull-through cache
cannot follow, so it is served by the ghcr.io cache.

## 3. A dedicated workload node

One worker is tainted `heimdall.io/workload` and only Online Boutique
tolerates it. The node-failure scenario pauses that node; without the split it
could take Prometheus or Gitea down with it and no alert would ever arrive.

## 4. Bad env vars and bad probes can only be fixed through git

The admission policy (spec 10.2) forbids the actor identity from changing env
vars or probes, because either is a way to run arbitrary code or hide a broken
pod. The spec's scenario table also lists `rollback_deployment` as an
acceptable fix for a bad env var (s01) and a bad probe (s06). Those two cannot
both hold. The admission rule wins: for s01 and s06 the only acceptable fix is
a pull request. `rollback_deployment` is in practice an image rollback.

## 5. Scenarios are cleaned up by a global reset

The spec gives each scenario its own `cleanup`. Instead every run ends with
`make reset`, which restores the whole baseline (git repo, live workload,
chaos experiments, paused nodes) in seconds. One well-tested path is more
reliable than sixteen hand-written ones, and it also cleans up after whatever
the agent did.

## 6. Scenario artifacts must not give the answer away

The broken frontend build is tagged like a normal release
(`frontend:v0.11.x`), not `frontend-faulty` as in the spec. Scenario commits
are authored by a developer identity with ordinary commit messages, and patch
files have neutral names. Otherwise the benchmark would measure reading
comprehension, not diagnosis.

## 7. Alerts carry no Prometheus external labels

Heimdall confirms each alert by looking up its exact label set in the `ALERTS`
series. Alertmanager payloads normally include Prometheus external labels
(`prometheus`, `prometheus_replica`) that `ALERTS` does not have, so they are
cleared in the Prometheus configuration.

## 8. NodeNotReady has no namespace or service label

Nodes are not namespaced. The incident manager treats an alert without a
service as related to any open incident, since a failing node can affect any
service.

## 9. The hold-out scenarios are not written by the project author

The hold-out set only measures generalization if whoever tunes the prompts has
never seen it. `chaos/scenarios/holdout/` is left for a third party to fill.

## 10. Redis symptoms surface at the frontend

Scaling Redis to zero (s07) does not raise the cartservice error rate as the
spec assumed: cartservice calls hang rather than fail, so the alerts that fire
are frontend errors and cartservice latency. The expected alerts follow what
the system actually does. This makes the scenario harder, which is the point:
the cause is two hops from the symptom.

## 11. A plain HTTPS client for Kubernetes

The spec names the official `kubernetes` Python client. Heimdall uses a small
client built on `httpx` instead. The two identities are just two tokens, the
requests are easy to fake in tests, and writes will need explicit control of
`resourceVersion` preconditions and dry run, which is simpler at this level.

## 12. The model replies with one JSON object per step

Instead of provider-specific tool calling, every model turn is a JSON object
(hypotheses, then either one tool request or a diagnosis) constrained by a
schema. It works the same with any provider, small local models follow it
more reliably, and each turn is trivially recordable for replay.

## 13. Limits above the admission cap are denied, not sent for approval

The spec sends any resource limit above the bounds to human approval. The
admission policy rejects anything above 1Gi or 1 CPU regardless, so asking a
human to approve it would be pointless. The OPA policy denies it. Approval is
asked only for increases that more than double the current limit but stay
under the cap. Lowering a limit is denied.

## 14. A plan that breaks a limit is escalated whole

If the model proposes one acceptable action and one that targets a symptom or
is not allowed for the cause, the planner does not run the acceptable half.
The plan was produced by reasoning that went wrong somewhere, so a human gets
all of it.

## 15. The local model runs on the host, reached through a relay

Ollama listens on localhost only. A small relay container bound to the kind
bridge address forwards to it, and Heimdall sends `Host: localhost` because
Ollama refuses other host names. Nothing leaves the machine.
