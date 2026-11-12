## How to read this page today

**There are no Heimdall numbers here yet.** The benchmark tooling exists
(`bench/run_bench.py`, `bench/metrics.py`, `bench/report.py`), but running it
takes the cluster and a model for 8 to 15 minutes per run, and that has not
been done. Until it has, this page shows only the rules baseline, scored
offline.

### `baseline_rules-dev-offline`

A fixed table from alert name to cause and action (`bench/baseline_rules.py`),
written from the alert rules without looking at the scenarios. It blames the
service that alerted and acts at once, with no policy gate.

"Offline" means no cluster was involved: each scenario's *expected* alerts
were fed to the table. Three consequences:

- It is the baseline's best case. In a real run more alerts fire than the
  expected ones, and each extra alert is another action.
- The runs are not independent. The table is deterministic, so five seeds of
  a scenario give the same answer unless the seed changes the alert. The
  intervals are computed as if runs were independent and are narrower than
  they should be; read the per-scenario table as "right" or "wrong".
- s13 is counted wrong because the expected alert carries no node name. With
  the real alert's `node` label the table would name the node. s15 is left
  out: it tests what Heimdall does after a forced plan.

What it shows is where reacting to the alert goes wrong, which is what the
hard scenarios were written to expose:

- **s01, s07, s14**: the frontend alerts, the cause is elsewhere (a config
  change in cartservice, Redis scaled to zero). The table rolls back a
  healthy frontend.
- **s09, s16**: latency caused by the network. The table scales the service
  that is slow.
- **s06**: the cause is right by luck of the mapping, but the action is a
  restart, which cannot fix a probe defined in git.
- **s02, s12**: it cannot know a ConfigMap is missing, or that an alert is
  noise.

### What a full results page still needs

1. `heimdall` on the dev scenarios, 5 to 10 seeds each, in dry-run mode, then
   live. Run in small batches: `uv run python -m bench.run_bench --agent
   heimdall --runs 5 --max-runs 3` continues where it stopped.
2. The rules baseline on the cluster, with the alerts that really fire.
3. The single-shot baseline (same model, no tool loop) and the gate-off
   counterfactual. Neither is built.
4. Hold-out scenarios, written by someone else, run once.
5. The third unsafe-action test (a write that lowers some service's success
   rate) needs metrics from the run and is not graded yet.
