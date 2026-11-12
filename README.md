# Heimdall

An AI SRE agent for Kubernetes. When an alert fires, Heimdall investigates the
cluster, identifies the root cause with verifiable evidence, and either applies
a safe mitigation or proposes a fix as a pull request. Safety does not depend
on the agent behaving: every action passes a policy gate and is enforced again
by the Kubernetes API server.

The full design is in [heimdall-spec-v2.md](heimdall-spec-v2.md).

## How it works

An alert becomes an action only after passing several independent checks. The
model proposes; it never acts directly. Every stage in the picture is built
and unit-tested; see [Status](#status) for what has also run on the cluster.

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
| M2 Chaos suite | all 16 scenarios written; see the table below for which are verified |
| M3 Ingest, incidents, audit | done, running in the cluster |
| M4 Investigator | works end to end with a local model; accuracy not yet tuned or measured at scale |
| M5 Planner, policy gate, dry run | built and tested against the real policy engine and a fake Kubernetes API; **not yet run on the cluster** |
| M6 Live execution, GitOps, verification, undo, approvals | built and tested the same way; **not yet run on the cluster** |
| M7 Postmortems and past-incident search | built; reports come from the audit log only |
| M8 Benchmark | runner, grader, metrics and results page built; only the rules baseline has been scored, offline ([results](docs/results.md)). No Heimdall numbers yet: those need the cluster and many model runs |
| M9 Injection suite and threat model | i01 to i10 written and passing without a cluster; the two cluster-only checks are part of `make test-integration` |
| M10 – M11 | not started |

This is a first prototype: the whole path from alert to postmortem exists in
code and is covered by 417 unit, property and injection tests plus 36 policy
rule tests. What is missing is time on the cluster. Nothing after M4 has run
there yet, so expect bugs at the seams when it does.

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
that was already deployed from git. `make test-integration` proves it with
server-side dry-run cases (env, command, probes, volumes, privileges, labels
and unknown images are all rejected by the API server itself). 38 cases have
passed on the cluster; 7 newer ones, for the Argo CD Application, have been
written but not run.

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
cloud dependency. Claude through the Anthropic API is the second provider
(`HEIMDALL_LLM_PROVIDER=anthropic`); it is tested against a fake client only,
because no API key has been used with it yet.

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

**Live execution, verification and undo.** In `live` mode an allowed action
is applied for real. Heimdall then checks recovery itself, with PromQL and
pod readiness rather than alert state, for up to five minutes. A fix that
does not help is undone through the same restricted identity (previous
image, replicas or limits, saved before the write), and the incident goes to
a human. At most three writes per incident, never the same one twice.

**GitOps.** A rollback or a resource change makes the cluster differ from
git on purpose. Before such a write Heimdall pauses Argo CD's automatic
sync, so an unrelated commit cannot put the broken state back; without the
pause there is no write. It then opens a pull request that reverts the
offending commit. The incident is `mitigated` until a human merges it;
Heimdall notices the merge, resumes sync and marks it `resolved`. Heimdall
never merges.

**Human approval.** Actions the policy will not allow on its own (scaling
Redis, for example) are sent to a chat webhook with a link. The token in the
link is an HMAC over the exact action and the version of the object it was
computed for, works once, and expires after 15 minutes. Opening the link
only shows the request; deciding takes a POST. After approval the policy
runs again on fresh state, and if the target has changed the approval is
void.

**Postmortems.** When an incident closes, a report is built from the audit
log and nothing else: timeline with the audit sequence number of every
line, root causes, quoted evidence, ruled-out hypotheses, each action with
its policy decision, the API server's answer and the verification result,
drift and pull request, and what the agent cost. No model writes any of it.
`heimdall incident postmortem <id>` prints Markdown or HTML.

**Past incidents.** The investigator can search earlier reports (BM25). A
past incident is a hint, never proof: evidence that cites the search is
rejected. For the benchmark, a run never sees reports about its own
scenario, and hold-out reports are never searchable.

**Revision tracker.** Heimdall measures how long each revision of a
Deployment was actually healthy, so the policy can refuse a rollback to a
revision that never worked.

**Prompt injection.** `tests/injection/` plants hostile content (a log line
ordering everything scaled to zero, an event asking to delete the namespace,
a commit message approving itself, a forged alert, secrets printed to
stdout, and more) and then assumes the model obeys it. Each test passes only
if a layer below the model stops the harm. Results are in
[docs/threat-model.md](docs/threat-model.md).

**Surviving a restart.** Before a live write is sent, Heimdall stores what
it is about to do and what the target looks like now. If it is killed at any
point, the next start never applies anything again: an unverified write is
verified, undone if the service has not recovered, and the incident goes to
a human. Tested by killing it before the write, right after it, during
verification, and between pausing sync and writing.

**Benchmark tooling.** `bench/run_bench.py` runs scenarios by seed and can be
stopped and resumed; the grader judges diagnoses and actions by rules fixed
in advance; `bench/report.py` writes [docs/results.md](docs/results.md) with
Wilson intervals, pass^k and calibration. A rules-only baseline (alert name
to action, no investigation) is included: scored offline it is right on
about half the scenarios and applies an unsafe action on 40% of them, which
is what the harder scenarios were written to expose.

All of this is tested with the real policy engine and fakes for Kubernetes
and Gitea. None of it has run on the cluster yet.

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
| s15 | The bad release of s04, plus a wrong fix forced by the harness | `HighErrorRate` frontend | replayed without a cluster (the restart is undone and escalated); not run yet |
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
- Dry-run and live mode have only been exercised in unit tests, not on the
  cluster. [NEXT_STEPS.md](NEXT_STEPS.md) lists what to check there, lightest
  first.
- The benchmark has not been run with Heimdall: no accuracy, safety or cost
  numbers for the agent. The single-shot baseline and the gate-off
  counterfactual are not built.
- Postmortems contain no model-written summary; follow-ups are derived from
  the record ([why](docs/decisions.md)).
- The Anthropic provider has never talked to the real API.
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

uv run heimdall incident list                     # what Heimdall has seen
uv run heimdall incident postmortem <id> --html --out report.html
uv run heimdall audit verify                      # check the audit chain
uv run heimdall freeze                            # stop all actions, now

uv run python -m chaos.run s05 --seed 1 --agent   # inject one fault and grade the diagnosis
uv run python -m chaos.suite --runs 3 --agent     # every scenario, three seeds each
```

A warning about load: the cluster, the workload's load generator and a local
model on the GPU together push a laptop hard, and the suite runs for hours.
Run single scenarios, or small batches, and watch the temperature.

Every tool, image and chart version is pinned in
[infra/versions.env](infra/versions.env).
