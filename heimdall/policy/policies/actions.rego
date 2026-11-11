# Decides what happens to an action the planner proposes.
#
#   allow             run it
#   require_approval  wait for a human
#   deny              never run it
#
# The input is built by Heimdall from the live cluster and its own store,
# never from model text. This policy is the first line of defence; the
# Kubernetes admission policy enforces the hard limits again, independently.
package heimdall.actions

import rego.v1

allowed_namespaces := {"boutique"}

# Actions that change the cluster directly.
cluster_writes := {"restart_deployment", "rollback_deployment", "scale_deployment", "patch_resources"}

known_actions := cluster_writes | {"open_pr", "escalate", "none"}

# Never blocked: saying "a human should look" or "nothing to do" is always safe.
passive := {"escalate", "none"}

min_confidence := 0.8

max_writes_per_incident := 3

repeat_window_minutes := 10

min_replicas := 1

max_replicas := 5

max_memory_bytes := 1073741824 # 1Gi, the same cap the admission policy enforces

max_cpu_cores := 1

max_pr_diff_lines := 50

action := input.action

is_cluster_write if action.type in cluster_writes

target := {"kind": "Deployment", "namespace": action.namespace, "name": action.name}

# ---------------------------------------------------------------- deny

deny contains "Heimdall is frozen" if input.frozen

deny contains sprintf("unknown action type %q", [action.type]) if not action.type in known_actions

deny contains "observe mode: no actions are taken" if {
	input.mode == "observe"
	not action.type in passive
}

deny contains sprintf("namespace %q is not allowed", [action.namespace]) if {
	is_cluster_write
	not action.namespace in allowed_namespaces
}

deny contains "target is outside the incident's affected resources" if {
	is_cluster_write
	not target in {r | some r in input.incident.affected_resources}
}

deny contains sprintf("confidence %v is below %v", [input.incident.confidence, min_confidence]) if {
	not action.type in passive
	input.incident.confidence < min_confidence
}

deny contains sprintf("already %d write actions in this incident", [input.incident.write_actions_so_far]) if {
	is_cluster_write
	input.incident.write_actions_so_far >= max_writes_per_incident
}

deny contains sprintf("%s was already run on %s %d minutes ago", [action.type, action.name, recent.minutes_ago]) if {
	is_cluster_write
	some recent in input.recent_actions
	recent.type == action.type
	recent.name == action.name
	object.get(recent, "namespace", action.namespace) == action.namespace
	recent.minutes_ago < repeat_window_minutes
}

# --- scale_deployment

deny contains sprintf("replicas must be between %d and %d", [min_replicas, max_replicas]) if {
	action.type == "scale_deployment"
	not replicas_in_range
}

replicas_in_range if {
	action.replicas >= min_replicas
	action.replicas <= max_replicas
}

deny contains "replicas may change by at most +2 or -1 per action" if {
	action.type == "scale_deployment"
	delta := action.replicas - input.current.replicas
	not delta_ok(delta)
}

delta_ok(delta) if {
	delta <= 2
	delta >= -1
}

# --- patch_resources

deny contains "patch_resources must set a memory or CPU limit" if {
	action.type == "patch_resources"
	not action.limits.memory
	not action.limits.cpu
}

deny contains "memory limit above 1Gi" if {
	action.type == "patch_resources"
	units.parse_bytes(action.limits.memory) > max_memory_bytes
}

deny contains "CPU limit above 1" if {
	action.type == "patch_resources"
	units.parse(action.limits.cpu) > max_cpu_cores
}

deny contains "resource limits may only be raised, not lowered" if {
	action.type == "patch_resources"
	lowers_a_limit
}

lowers_a_limit if units.parse_bytes(action.limits.memory) < units.parse_bytes(input.current.limits.memory)

lowers_a_limit if units.parse(action.limits.cpu) < units.parse(input.current.limits.cpu)

# --- rollback_deployment

deny contains sprintf("revision %v was never healthy for 10 minutes", [action.to_revision]) if {
	action.type == "rollback_deployment"
	not proven_revision
}

proven_revision if {
	some revision in input.revision_history
	revision.revision == action.to_revision
	revision.available_minutes >= 10
}

# --- open_pr

deny contains sprintf("pull request touches %q, which does not belong to an affected resource", [file]) if {
	action.type == "open_pr"
	some file in action.files
	not file in {f | some f in input.incident.affected_files}
}

deny contains sprintf("pull request diff is %d lines, more than %d", [action.diff_lines, max_pr_diff_lines]) if {
	action.type == "open_pr"
	action.diff_lines > max_pr_diff_lines
}

# ---------------------------------------------------- require_approval

require_approval contains "target is stateful" if {
	is_cluster_write
	input.current.stateful
}

require_approval contains "memory limit more than doubles" if {
	action.type == "patch_resources"
	units.parse_bytes(action.limits.memory) > 2 * units.parse_bytes(input.current.limits.memory)
}

require_approval contains "CPU limit more than doubles" if {
	action.type == "patch_resources"
	units.parse(action.limits.cpu) > 2 * units.parse(input.current.limits.cpu)
}

# ------------------------------------------------------------ decision

default decision := "allow"

decision := "deny" if count(deny) > 0

decision := "require_approval" if {
	count(deny) == 0
	count(require_approval) > 0
}

reasons := sort(deny) if decision == "deny"

reasons := sort(require_approval) if decision == "require_approval"

reasons := [] if decision == "allow"

result := {"decision": decision, "reasons": reasons}
