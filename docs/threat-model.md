# Threat model

Status: a working draft that covers what is built so far (ingest, incidents,
audit log, investigator, planner, policy gate, dry-run executor, admission).
Approvals, pull requests, verification and live execution are not built yet;
their rows are marked *planned*. The planner, gate and executor are
unit-tested but have not run on the cluster. The
injection test suite (i01 to i10 in the spec) is not written yet, so the
"tested by" column lists only tests that exist.

## What we are protecting

1. **The workload.** Heimdall must never make the cluster worse: no data loss,
   no new code running in it, no outage caused by the agent.
2. **Secrets.** Nothing secret may reach the model provider, a webhook, a
   report or the audit log.
3. **The record.** What Heimdall saw and did must be reconstructable and
   tamper-evident.

## Trust boundaries

| Source | Trusted? | Why |
|---|---|---|
| Workload output: logs, events, annotations, label values | No | Written by the workloads Heimdall watches; a compromised or buggy pod controls it |
| Git content: commit messages, diffs | No | Anyone who can push writes it |
| Alert payloads | No, until confirmed | The webhook could be called by anything that has the token |
| Model output | No | It is produced from untrusted input and can be steered by it |
| Prometheus, the Kubernetes API | Yes | They are the ground truth Heimdall checks everything else against |
| The human approver | Yes | Out of scope: a compromised approver can approve anything |
| The Heimdall process itself | **No** | See below |

The last row is the design's main point. A model that reads hostile text can
be talked into anything, and code has bugs. So the limits that matter are
enforced outside the Heimdall process, by the Kubernetes API server.

## Layers

| Layer | Enforced by | Holds if Heimdall is fully compromised? | State |
|---|---|---|---|
| Model can only read, through fixed tools | Heimdall | no | built |
| Queries confined to one namespace (parsed PromQL and LogQL) | Heimdall | no | built |
| Evidence must quote real tool output | Heimdall | no | built |
| Actions only from a fixed catalog, per kind of cause, on diagnosed resources only | Heimdall | no | built |
| Policy gate (OPA), input built from cluster state | Heimdall and OPA | no | built |
| Kill switch, checked before every action | Heimdall | no | built |
| RBAC: read-only identity cannot write or read Secrets; actor can only patch Deployments in one namespace | API server | **yes** | built |
| Admission policy: what an actor patch may contain | API server | **yes** | built |
| Argo CD project: one source repo, one destination namespace | Argo CD | **yes** | built |
| Signed audit anchors in a separate git repo | Signing key, git history | **yes** (tampering is detectable) | built |

Worst case with a fully hijacked Heimdall holding the actor token: restart a
Deployment, scale it between 1 and 5, change its resource limits within
bounds, or switch it to an image that was already deployed from git. It cannot
run new code, change env vars, commands, probes, volumes or privileges, read
Secrets, delete anything, or act outside the workload namespace.

## Threats by component (STRIDE)

### Alert ingest

| Threat | Mitigation | Tested by |
|---|---|---|
| Spoofing: forged alert opens an incident and steers actions | Bearer token; NetworkPolicy allows only Alertmanager; every alert is re-confirmed against Prometheus, so a valid token alone is not enough | `test_api.py` (401 cases, `test_forged_alert_with_valid_token_opens_nothing`); NetworkPolicy checked by hand in M1 |
| Tampering: label values crafted to break out of the confirmation query | Label values are escaped into the PromQL selector | `test_label_values_cannot_break_out_of_the_selector` |
| Denial of service: alert flood | Alerts for one cause are grouped into one incident; flapping alerts are marked and stay in one incident | `test_incidents.py` |
| Elevation: starting without a token | The service refuses to start with an empty token | `test_refuses_to_start_without_a_token` |

Self-monitoring alerts (`HeimdallDown`, rejected requests, unconfirmed alerts)
go to a human, not to Heimdall.

### Investigator and tools

| Threat | Mitigation | Tested by |
|---|---|---|
| Prompt injection through logs, events, commit messages | Tool output is wrapped in a block with a random boundary the content cannot close; instruction-like lines are flagged; the model has no write tools at all | `test_redact_sanitize.py`, `test_injected_instruction_reaches_the_model_only_as_flagged_data` |
| Information disclosure: secrets in logs reach the model or the audit log | Redaction of known formats and high-entropy tokens before storage and before the model; env var values are never returned; the read-only identity has no access to Secrets | `test_redact_sanitize.py`, property test `test_a_planted_secret_never_survives`, `make test-integration` |
| Information disclosure: reading other namespaces | Kubernetes tools refuse other namespaces; PromQL and LogQL are parsed and every selector must pin the namespace; RBAC backs this up for the Kubernetes API | `test_metrics_logs_tools.py`, `test_k8s_tools.py` |
| Fabricated diagnosis | Every evidence item must cite an existing tool call and quote text found in its stored output, from at least two different calls; otherwise rejected, then escalated as unknown | `test_validator.py`, `test_investigator.py` |
| Denial of service: log flood exhausts the context or budget | Repeated lines collapsed, output truncated, hard limits on tool calls, time and tokens | `test_log_flood_stays_within_budget`, budget tests |
| Model or provider outage | The investigation ends as unknown and the incident is escalated | `test_llm_outage_ends_as_unknown_not_a_crash`; observed live |

Known weakness: redaction is pattern-based. A secret in an unusual format, in
lowercase hex, or split across lines can get through. Redaction reduces
exposure; it is the RBAC rule on Secrets and the masking of env values that
keep the important ones out in the first place.

### Planner, policy gate and executor

| Threat | Mitigation | Tested by |
|---|---|---|
| Model proposes a harmful action | Catalog only; the planner drops anything not allowed for the diagnosed cause or aimed at another resource; policy checks blast radius, bounds, rate and confidence on fields built from cluster state, not model text | `test_planner.py`, `actions_test.rego` (33 cases), property test over random proposals, `test_pipeline.py` |
| Policy engine unreachable or erroring | The gate fails closed: any error is a deny | `test_policy_gate.py` |
| Change between policy check and execution | Writes carry the `resourceVersion` the policy saw; the API server refuses if it moved | `test_change_since_the_policy_check_is_a_conflict` |
| An action applied twice after a restart | Idempotency key per incident and action | `test_the_same_action_is_never_applied_twice` |
| Runaway agent | Kill switch read before every action; at most three writes per incident; no repeat within ten minutes | `test_frozen_heimdall_touches_nothing`, `test_fourth_write_in_an_incident_is_denied` |
| Approval reused for a different action | Approval tokens bound to the hash of the exact action | planned |
| Heimdall bypasses its own policy (bug or hijack) | The admission policy rejects the request at the API server | `make test-integration` (38 cases) |

Known weakness: the admission policy lists the pod fields that must not
change. A field added by a future Kubernetes version would not be covered
until the list is updated. The tests would not catch that on their own.

### Audit log

| Threat | Mitigation | Tested by |
|---|---|---|
| Tampering: a row is edited | Hash chain breaks at that row | `test_tampering_is_detected_at_the_right_row` |
| Tampering: a row is edited and the whole chain recomputed | Published signed anchors no longer match | `test_rewritten_chain_passes_chain_check_but_fails_against_anchors` |
| Tampering: the log is truncated | The anchored row is missing | `test_truncated_log_fails_against_anchors` |
| Spoofing: forged anchor | Ed25519 signature check | `test_anchor_signed_by_another_key_is_rejected` |
| Repudiation | The postmortem will be generated from this log only | planned |

Accepted risk: the signing key lives in a Kubernetes Secret that the Heimdall
pod mounts. Someone who compromises the pod can sign new anchors, but cannot
rewrite the ones already committed to the anchor repository without changing
its git history. Events after the last anchor are protected only by the hash
chain until the next anchor is published.

## Accepted risks

- The model provider sees redacted cluster data. With the local Ollama
  provider nothing leaves the machine.
- A compromised human approver, cluster administrator or Prometheus is out of
  scope.
- Gitea, Argo CD and the registry are lab components with lab credentials.
- Online Boutique pods can reach each other freely; there is no network policy
  inside the workload namespace.
