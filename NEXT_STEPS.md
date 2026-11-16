# Next steps

Handover note, last updated on 2026-11-16. Read this first when picking the
project up again.

## Where things stand

- `main` has M1 to M7, M9, restart recovery and the benchmark tooling.
  Branch **`m8-api-provider-baselines`** adds everything in "What was added
  last" below; it is committed locally and not pushed.
- The whole path exists in code: alert → confirmation → incident →
  investigation → plan → policy → approval → execution → verification → undo
  or pull request → postmortem.
- **Every item that can be done without the cluster is done.** What is left
  is running it (section 1) and measuring it (section 2).
- 446 unit, property and injection tests and 36 OPA rule tests pass. `ruff`
  and `mypy` are clean.
- **Nothing after M4 has run on the cluster.** That is the next job, and it
  will find bugs.
- The kind cluster is **stopped** (`docker stop`), not deleted. The local
  model is unloaded. Neither was started while finishing the prototype.

## Rules agreed with the owner

- **No heavy workloads on the laptop.** No scenario batches, no local model on
  the GPU for long, no CPU-stress scenarios left running. Ask before starting
  the cluster or the model, and say how long it will take.
- Commits: short messages, **no `Co-Authored-By` line**.
- README diagrams are made with the `stack-diagram` skill. The source of the
  pipeline diagram is `docs/img/pipeline-src.html` (icons and fonts are
  inlined by the skill's `build.py`, then `render_png.py` writes both PNGs).

## What was added last (2026-11-16)

| Item | Where | Tested how |
|---|---|---|
| OpenAI-compatible provider (Gemini, OpenRouter, Groq, ...) that waits on rate limits | `heimdall/llm/openai_compat.py`, `HEIMDALL_LLM_PROVIDER=openai` | `test_openai_llm.py`, fake API only |
| Hosted model on the cluster: key in a Secret, `make up` switches over | `infra/llm-api.sh`, `infra/bootstrap.sh`, `deploy/heimdall.yaml` | **not run**; `shellcheck` runs in CI only |
| Single-shot baseline | `HEIMDALL_SINGLE_SHOT`, `Investigator(single_shot=True)` | `test_investigator.py` |
| Comparison runs: `--variant gate-off`, `single-shot`, `no-search` | `bench/run_bench.py` | `test_bench.py`, fake kubectl |
| Third unsafe-action test (a write lowers a success rate by more than 2 points) | `chaos/grader.py`, `chaos/run.py::write_drops`, `executed_at` in `incident list --json` | `test_bench.py`, `test_incidents.py` |
| `get_rollout_history` shows measured healthy minutes | `heimdall/tools/k8s.py` | `test_k8s_tools.py` |
| Decisions 30 to 32, threat model, README | `docs/` | |

## Added before (2026-11-12)

| Item | Where | Tested how |
|---|---|---|
| Postmortems from the audit log, Markdown and HTML | `heimdall/report/postmortem.py`, `heimdall incident postmortem <id>` | `test_postmortem.py` |
| Stored reports, written when an incident closes | `heimdall/report/store.py` | same |
| `search_past_incidents` (BM25) with leakage rules | `heimdall/report/store.py`, `heimdall/tools/history.py`, `heimdall label` | `test_past_incidents.py` |
| Injection suite i01 to i10 | `tests/injection/` | runs with `make test` |
| Anthropic API provider | `heimdall/llm/anthropic.py` | fake client only |
| Revision tracker | `heimdall/core/revisions.py` | `test_revisions.py` |
| Scenario s15 and the forced-plan switch | `chaos/scenarios/dev/s15.yaml`, `ForcedPlanner` | `test_s15.py` |
| Docs: README, decisions 18 to 27, threat model, pipeline diagram | `docs/` | |

Two small hardening changes came out of the injection suite: notification
text is redacted before it goes to the webhook, and service names shown to
the model outside a data block are reduced to name characters.

## 1. Run and debug (needs the cluster; ask first)

Bring it back:

```sh
docker start heimdall-registry heimdall-control-plane heimdall-worker heimdall-worker2
make up && make reset     # a fault may still be injected from a killed batch
deploy/build.sh           # the image must be rebuilt: new code and a new dependency
```

To keep the GPU out of it, configure a hosted model **before** `make up`
(the namespace must exist, so on a fresh cluster run `make up` once first):

```sh
HEIMDALL_LLM_API_KEY=... infra/llm-api.sh <base-url> <model> [seconds-between-requests]
make up
kubectl -n heimdall exec deploy/heimdall -- env | grep HEIMDALL_LLM   # provider, model, URL
```

With the Secret present `make up` sets the provider to `openai` and removes
the Ollama relay. The first investigation is the first real call: read the
`llm_call` events of the incident (`heimdall audit` or the postmortem). If
the API rejects `response_format`, set `HEIMDALL_LLM_JSON_MODE=false` on the
Deployment. Free tiers train on what they are sent; that is fine for this lab
workload only.

If the nodes do not come back healthy, `make down && make up` rebuilds in
about 5 minutes from the image caches.

Then check, lightest first:

1. `make test-integration`. 45 cases, including 7 for the Argo CD
   Application (pause/resume allowed; self-heal, source, destination and sync
   operation rejected). **The admission policy `heimdall-actor-applications`
   has never been applied to a cluster**; its CEL may need fixing. This run is
   also what proves i10 and the admission half of i01.
2. Heimdall pod starts with the **OPA sidecar** and the **actor token** mount
   (`kubectl -n heimdall get pods`, both containers ready). Check the log for
   the revision tracker: it lists Deployments every 30 seconds with the
   read-only identity.
3. Assumptions to confirm on the real API server:
   - `metadata.resourceVersion` works as a precondition on the **scale
     subresource** patch.
   - The actor's merge patch that sets `spec.syncPolicy.automated` to `null`
     is accepted for the Application.
   - Gitea accepts the multi-file `POST /contents` with `delete` operations
     and returns 409 for a duplicate pull request, as the fake does.
   - `status.observedGeneration`, `updatedReplicas` and `availableReplicas`
     look the way `is_available` in the revision tracker expects.
4. One scenario without the model, to see a report:
   `uv run python -m chaos.run s05 --seed 1`, then
   `kubectl -n heimdall exec deploy/heimdall -- heimdall incident list` and
   `... heimdall incident postmortem <id>`.
5. One scenario in **dry-run** mode with the model:
   `uv run python -m chaos.run s04 --seed 1 --agent` (about 10 minutes).
   Expect a dry-run rollback, a described and unopened pull request, and a
   stored postmortem. The runner now calls `heimdall label` in the pod before
   and after; that path is untested.
6. Scenarios never verified: **s10, s13, s14, s15, s16**. Changed and not
   re-run: **s01, s07, s11, s12**. All but s15 can run without the model.
7. Only after dry run looks right: `HEIMDALL_MODE=live` on s04, watching for
   pause sync → rollback → verify → PR → merge → resolved. Then s15, which
   switches Heimdall to live mode with a forced plan by itself
   (`kubectl set env`, which restarts the pod) and switches it back.
8. Accuracy for M4 (8 of 10 on s01, s04, s05, s07) is still unmeasured. Do it
   one run at a time.
9. Latency baselines for per-service thresholds, with the load generator
   running and no fault injected for an hour:
   `max_over_time(heimdall:service_latency_ms:p95_rate5m{namespace="boutique"}[1h])`.
   Then give `HighLatencyP95` in `infra/alert-rules/heimdall-rules.yaml` a
   threshold per service (about three times its baseline).

## 2. What is still missing

- **M8, the benchmark runs.** All the tooling is built; only the runs are
  missing. One run is 8 to 15 minutes of cluster time and about 20 model
  requests (up to 200,000 tokens). In small batches:

  ```sh
  uv run python -m bench.run_bench --runs 5 --max-runs 3           # heimdall, dry run
  uv run python -m bench.run_bench --variant single-shot --runs 5 --max-runs 3
  uv run python -m bench.run_bench --variant gate-off --runs 5 --max-runs 3
  uv run python -m bench.run_bench --agent baseline_rules --runs 5 --max-runs 3
  uv run python -m bench.report
  ```

  A batch that is stopped continues where it was. A free tier's daily quota
  decides how many runs fit in a day: when it runs out, investigations end
  as `unknown` with "LLM failure" as the reason, and those runs must be
  deleted from `bench/results/<config>/` and redone, not counted. The
  runner's cluster path (`run_one`, `set_variant`) has never been executed.
- **No hosted provider has called a real API.** No key is configured here.
  For Anthropic, check that the request is accepted as sent (the
  refusal-fallback beta and the cache markers in particular), and set
  `HEIMDALL_LLM_FALLBACKS=false` if the fallback is not wanted.
- **Crash recovery is built and unit-tested** (`Pipeline.recover`,
  `test_crash_recovery.py`), but only against fakes. On the cluster: kill the
  pod during a live action and check the incident ends `escalated` with the
  write verified or undone. Recovery runs in the worker thread at startup
  and can take as long as one verification (5 minutes).
- **Per-service latency thresholds.** `HighLatencyP95` uses 1000 ms for every
  service. Needs baselines measured on the cluster (section 1, step 9).
- Hold-out scenarios are deliberately left for someone else to write.
- The demo video and the blog post (M11). The README now has the detection,
  remediation, lifecycle and safety-layer sections with diagrams.

## Where to look

| Topic | File |
|---|---|
| Spec | `heimdall-spec-v2.md` |
| Deviations from the spec | `docs/decisions.md` |
| Threats, and what is tested by what | `docs/threat-model.md` |
| Pipeline (plan → policy → approve → execute → verify → undo) | `heimdall/agent/pipeline.py` |
| Executor and undo | `heimdall/executor/executor.py` |
| GitOps (revert PRs, sync pause) | `heimdall/executor/gitops.py` |
| Approvals | `heimdall/core/approvals.py`, endpoints in `heimdall/api.py` |
| Policy | `heimdall/policy/policies/actions.rego` |
| Reports and past-incident search | `heimdall/report/` |
| Revision tracker | `heimdall/core/revisions.py` |
| LLM providers | `heimdall/llm/` |
| Admission and RBAC | `infra/admission/`, `infra/rbac/` |
| Scenarios and runner | `chaos/` |
