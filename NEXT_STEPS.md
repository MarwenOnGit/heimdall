# Next steps

Handover note written on 2026-11-11, at the end of a working session. Read this
first when picking the project up again.

## Where things stand

- `main` has M1 to M5 (PRs #1, #2, #3 merged).
- Branch **`m6-prototype`** is pushed but has **no pull request yet**. It adds
  M6: live execution, verification, undo, human approvals, notifications,
  GitOps pull requests and sync pausing.
- 341 unit and property tests and 36 OPA rule tests pass on that branch.
- **Nothing on `m6-prototype` has run on the cluster.** Nothing since PR #2
  has: PR #3 (dry-run mode, OPA sidecar, actor token) is also untested there.
- The kind cluster is **stopped** (`docker stop`), not deleted. The local
  model is unloaded.

## Rules agreed with the owner

- **No heavy workloads on the laptop.** No scenario batches, no local model on
  the GPU for long, no CPU-stress scenarios left running. Ask before starting
  the cluster or the model, and say how long it will take.
- Write code and docs first; run and debug at the end, once the prototype is
  delivered.
- Commits: short messages, **no `Co-Authored-By` line**.
- Update the README diagrams (made with the `stack-diagram` skill) as part of
  delivering the prototype.

## 1. Finish the code (no cluster needed)

In rough order of value:

1. **Open the PR for `m6-prototype`** once the items below that you want in it
   are done. Update README status, `docs/decisions.md` and
   `docs/threat-model.md` for M6 first (they still describe M5).
2. **M7 postmortems.** Not started. Plan: `heimdall/report/postmortem.py`
   builds a report from the audit log only (incident, alerts, timeline with
   `seq`, root causes, evidence with quotes, ruled-out hypotheses, actions with
   policy decision / admission result / verification, drift and PR link, agent
   stats). Render Markdown and HTML. Add `heimdall incident postmortem <id>`.
   Generate one when an incident reaches a terminal state.
3. **`search_past_incidents` tool.** BM25 over stored postmortems, registered
   in `heimdall/agent/setup.py`. Mind the leakage rules in spec 13.3.
4. **M9 injection suite** (`tests/injection/`, i01 to i10 in the spec). Most
   mechanisms exist; the tests that name them do not. Then move the threat
   model out of "draft".
5. **An API LLM provider** (`heimdall/llm/anthropic.py`). Only Ollama exists.
   Read the current API docs before writing it. No API key is configured on
   this machine.
6. **Revision tracker** (spec 8.1). Rollback targets are currently judged by
   an approximation (`docs/decisions.md`, decision 18).
7. **Scenario s15** (forced bad fix that must be undone). The undo path now
   exists; the scenario file and replay harness do not.
8. **Diagrams.** `docs/img/pipeline*.png` is labelled "target design" and
   predates the planner, approvals and GitOps path. Regenerate it, and
   consider one for the incident state machine and one for the safety layers.
   The generator script was in the session scratchpad and is gone; rebuild it
   from the skill's template.

Smaller known gaps:

- `HighLatencyP95` uses one threshold for all services.
- Hold-out scenarios are deliberately left for someone else to write.
- `README.md` still says there is no live mode.

## 2. Run and debug (needs the cluster; ask first)

Bring it back:

```sh
docker start heimdall-registry heimdall-control-plane heimdall-worker heimdall-worker2
make up && make reset     # a fault may still be injected from a killed batch
```

If the nodes do not come back healthy, `make down && make up` rebuilds in
about 5 minutes from the image caches.

Then check, lightest first:

1. `make test-integration`. It now has 45 cases, including 7 new ones for the
   Argo CD Application (pause/resume allowed; self-heal, source, destination
   and sync operation rejected). **The new admission policy
   `heimdall-actor-applications` has never been applied to a cluster**; its
   CEL may need fixing.
2. Heimdall pod starts with the **OPA sidecar** and the **actor token** mount
   (`kubectl -n heimdall get pods`, both containers ready).
3. Assumptions to confirm on the real API server:
   - `metadata.resourceVersion` works as a precondition on the **scale
     subresource** patch.
   - The actor's merge patch that sets `spec.syncPolicy.automated` to `null`
     is accepted for the Application.
   - Gitea accepts the multi-file `POST /contents` with `delete` operations
     and returns 409 for a duplicate pull request, as the fake does.
4. One scenario in **dry-run** mode: `uv run python -m chaos.run s04 --seed 1 --agent`
   (about 10 minutes, uses the model). Expect a dry-run rollback and a
   described, unopened pull request.
5. Scenarios never verified: **s10, s13, s14, s16**. Changed and not re-run:
   **s01, s07, s11, s12**. These can be run without the model (omit `--agent`).
6. Only after dry run looks right: `HEIMDALL_MODE=live` on one scenario (s04),
   watching for pause sync → rollback → verify → PR → merge → resolved.
7. Accuracy for M4 (8 of 10 on s01, s04, s05, s07) is still unmeasured. Do it
   one run at a time.

## Where to look

| Topic | File |
|---|---|
| Spec | `heimdall-spec-v2.md` |
| Deviations from the spec | `docs/decisions.md` |
| What is tested by what | `docs/threat-model.md` |
| Pipeline (plan → policy → approve → execute → verify → undo) | `heimdall/agent/pipeline.py` |
| Executor and undo | `heimdall/executor/executor.py` |
| GitOps (revert PRs, sync pause) | `heimdall/executor/gitops.py` |
| Approvals | `heimdall/core/approvals.py`, endpoints in `heimdall/api.py` |
| Policy | `heimdall/policy/policies/actions.rego` |
| Admission and RBAC | `infra/admission/`, `infra/rbac/` |
| Scenarios and runner | `chaos/` |
