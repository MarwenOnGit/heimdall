# Next steps

Handover note, rewritten on 2026-11-12 after the first prototype was
finished. Read this first when picking the project up again.

## Where things stand

- `main` has M1 to M5. Branch **`m6-prototype`** adds M6, M7, M9 and the
  remaining code items, and has an open pull request.
- The whole path exists in code: alert → confirmation → incident →
  investigation → plan → policy → approval → execution → verification → undo
  or pull request → postmortem.
- A second branch, **`m8-crash-recovery-benchmark`**, adds restart recovery
  and the benchmark tooling (see below).
- 417 unit, property and injection tests and 36 OPA rule tests pass. `ruff`
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

## What was added last (2026-11-12)

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

## 2. What is still missing

- **M8, the benchmark runs.** The tooling is built: `bench/run_bench.py`
  (resumable, `--max-runs` for small batches), `chaos.grader.grade_actions`,
  `bench/metrics.py`, `bench/report.py`, and a rules baseline that is scored
  offline by `make results`. Missing: the runs themselves (many hours of
  cluster and model time, so it needs a plan that fits the laptop rule, or
  the API provider and a budget), the single-shot baseline, and the unsafe-action test that needs run metrics. The gate-off
  counterfactual is a switch (`HEIMDALL_POLICY_LOG_ONLY=true`, dry run only)
  plus `--config heimdall-dev-gate-off` on the runner. The
  runner's cluster path (`run_one`) has never been executed.
- **The Anthropic provider has never called the real API.** No key is
  configured here. First real call: check that the request is accepted as
  sent (the refusal-fallback beta and the cache markers in particular), and
  set `HEIMDALL_LLM_FALLBACKS=false` if the fallback is not wanted.
- **Crash recovery is built and unit-tested** (`Pipeline.recover`,
  `test_crash_recovery.py`), but only against fakes. On the cluster: kill the
  pod during a live action and check the incident ends `escalated` with the
  write verified or undone. Recovery runs in the worker thread at startup
  and can take as long as one verification (5 minutes).
- **Per-service latency thresholds.** `HighLatencyP95` uses 1000 ms for every
  service. Needs baselines measured on the cluster.
- `get_rollout_history` does not show the tracker's measured availability
  yet; only the policy uses it.
- Hold-out scenarios are deliberately left for someone else to write.
- More diagrams (incident state machine, safety layers), the demo video and
  the blog post (M11).

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
