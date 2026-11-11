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

## 16. Dry runs count toward the limits

The policy limits how many writes an incident may have and forbids repeating
an action within ten minutes. In dry-run mode those counters include dry
runs. Otherwise a dry-run report would show actions that live mode would
have refused.

## 17. A plan stops at its first failure

Proposals run in order. If one is denied, rejected by the API server, needs
approval or fails, the rest are not attempted: later steps were chosen on
the assumption that earlier ones worked.

## 18. Rollback targets: measured where possible, estimated otherwise

The policy only allows a rollback to a revision that was healthy for ten
minutes. Kubernetes does not record that, so Heimdall measures it: every 30
seconds a Deployment that is fully rolled out and Available has the elapsed
time credited to its current revision (`heimdall/core/revisions.py`). The
spec describes a watch; a poll is simpler and precise enough for a
ten-minute threshold. Gaps longer than 90 seconds credit nothing, so time
when Heimdall was down does not count.

Revisions older than anything the tracker has seen are still estimated from
ReplicaSet creation times, which over-credits a revision that was broken
while it was current. That only affects revisions deployed before Heimdall
was installed. The admission policy limits a rollback to images that were
deployed from git either way.

## 19. Live mode needs a verifier

`HEIMDALL_MODE` accepts `observe`, `dry_run` and `live`. The pipeline refuses
to be built in live mode without a verifier: a write that is not checked
afterwards is not allowed to exist. The default in the cluster manifest
stays `dry_run`.

## 20. Sync is paused by patching the Application, not through the Argo CD API

The spec suggests a project-scoped Argo CD role. Instead the actor identity
may patch exactly one `Application` object, and a second admission policy
limits that patch to switching automatic sync off and on. This keeps one
identity and one enforcement point (the API server) and needs no Argo CD
token. If the pause fails, the drift-creating write is not attempted.

## 21. The reconciling pull request is a revert

Heimdall does not write manifests. The pull request it opens reverts the most
recent commit that touched the files of the affected resource, and the policy
checks the files and size of that real diff. A fix that cannot be expressed
as a revert is left to a human.

## 22. Opening an approval link changes nothing

Chat clients fetch links to build previews. So `GET /approvals/<id>` only
shows what is being asked; deciding takes a `POST` with the token in the
body. Tokens are an HMAC over the approval id, the hash of the exact action,
the object version and the expiry. Without `HEIMDALL_APPROVAL_SECRET` there
is no approval flow and such actions are escalated.

## 23. Postmortems are rendered in Python, with no model-written text

The spec asks for a Jinja template and allows a model to write the summary
and follow-ups, with a citation check. The report is instead built and
rendered by plain Python (`heimdall/report/postmortem.py`): one dependency
fewer, and escaping is in one place. No model writes any part of it.
Follow-ups are derived from the record (an undo that failed, a pull request
waiting, an escalation). The impact section lists services, alerts and
durations but not the success-rate trough, which is not in the audit log.

Reports are written when an incident reaches `mitigated` or a terminal
state, by a listener on the audit log, so no code path can forget to.

## 24. Past incidents are hints, not evidence

`search_past_incidents` returns text about other incidents, part of it
written by a model. A diagnosis that cites that tool as evidence is
rejected. Otherwise one wrong postmortem could reproduce itself.

Leakage rules (spec 13.3) are enforced where reports are stored: the
benchmark harness labels each run (`heimdall label --scenario s04`), a
labelled run only searches reports from dev runs of other scenarios, and
hold-out reports are never searchable. Unlabelled reports are left out
during a labelled run, because nothing says which scenario they are about.

## 25. The API provider gets its schema as an instruction

Heimdall's reply schemas use number ranges and free-form argument objects,
which strict structured output does not accept. The Anthropic provider
therefore puts the JSON Schema in a cached system block and relies on the
validation and retry every provider already goes through. It uses the
official SDK, sends no sampling parameters (current models reject them),
and enables the API's refusal fallback so a declined request is retried on
another model; the model that answered is recorded with every call. A
refusal that survives is an `LLMError`, which ends in escalation.

## 26. s15 forces a plan through a benchmark-only switch

s15 needs a wrong fix to be attempted. The real planner would drop it, so
`HEIMDALL_FORCED_PLAN` replaces the planner with a fixed plan. It skips the
planner's limits and nothing else: policy, admission, verification and undo
still apply, the audit log marks the plan as forced, and startup logs a
warning. It must not be set outside the benchmark.

## 27. Injection tests assume the model obeys

A test that shows a model ignoring an injection proves little: the next
model, or the next phrasing, may not. Each test in `tests/injection/`
scripts the model to do what the hostile text asks and passes only if a
lower layer stops it. Whether a real model is fooled is a benchmark
question. i10 and the admission half of i01 need an API server and live in
`make test-integration`.
