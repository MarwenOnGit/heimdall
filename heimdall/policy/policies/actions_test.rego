package heimdall.actions_test

import rego.v1

import data.heimdall.actions

frontend := {"kind": "Deployment", "namespace": "boutique", "name": "frontend"}

# A request that is allowed. Each test changes one thing about it.
base := {
	"action": {"type": "restart_deployment", "namespace": "boutique", "name": "frontend"},
	"current": {
		"replicas": 2,
		"stateful": false,
		"limits": {"memory": "128Mi", "cpu": "200m"},
	},
	"incident": {
		"confidence": 0.9,
		"affected_resources": [frontend],
		"affected_files": ["online-boutique/overrides/frontend-image.yaml"],
		"write_actions_so_far": 0,
	},
	"recent_actions": [],
	"revision_history": [{"revision": 6, "available_minutes": 1440}, {"revision": 7, "available_minutes": 3}],
	"mode": "live",
	"frozen": false,
}

with_action(a) := object.union(base, {"action": object.union({"namespace": "boutique", "name": "frontend"}, a)})

decide(req) := r if {
	r := actions.result with input as req
}

denied(req, text) if {
	r := decide(req)
	r.decision == "deny"
	some reason in r.reasons
	contains(reason, text)
}

# ----------------------------------------------------------- general

test_restart_is_allowed if decide(base) == {"decision": "allow", "reasons": []}

test_frozen_denies_everything if {
	denied(object.union(base, {"frozen": true}), "frozen")
	denied(object.union(with_action({"type": "escalate"}), {"frozen": true}), "frozen")
}

test_unknown_action_is_denied if denied(with_action({"type": "delete_namespace"}), "unknown action type")

test_other_namespace_is_denied if {
	denied(with_action({"type": "restart_deployment", "namespace": "kube-system"}), "not allowed")
}

test_target_outside_blast_radius_is_denied if {
	denied(with_action({"type": "restart_deployment", "name": "emailservice"}), "outside the incident")
}

test_low_confidence_denies_writes_but_not_escalation if {
	low := object.union(base, {"incident": object.union(base.incident, {"confidence": 0.79})})
	denied(low, "below 0.8")
	denied(object.union(low, {"action": {"type": "open_pr", "files": [], "diff_lines": 1}}), "below 0.8")
	decide(object.union(low, {"action": {"type": "escalate"}})).decision == "allow"
	decide(object.union(low, {"action": {"type": "none"}})).decision == "allow"
}

test_confidence_at_threshold_is_allowed if {
	exact := object.union(base, {"incident": object.union(base.incident, {"confidence": 0.8})})
	decide(exact).decision == "allow"
}

test_fourth_write_is_denied if {
	busy := object.union(base, {"incident": object.union(base.incident, {"write_actions_so_far": 3})})
	denied(busy, "already 3 write actions")
	decide(object.union(busy, {"action": {"type": "escalate"}})).decision == "allow"
}

test_third_write_is_allowed if {
	two := object.union(base, {"incident": object.union(base.incident, {"write_actions_so_far": 2})})
	decide(two).decision == "allow"
}

test_repeat_within_window_is_denied_across_incidents if {
	recent := [{"type": "restart_deployment", "name": "frontend", "minutes_ago": 4, "incident_id": "other"}]
	denied(object.union(base, {"recent_actions": recent}), "already run on frontend 4 minutes ago")
}

test_repeat_after_window_or_other_target_is_allowed if {
	old := [{"type": "restart_deployment", "name": "frontend", "minutes_ago": 10}]
	decide(object.union(base, {"recent_actions": old})).decision == "allow"
	other := [{"type": "restart_deployment", "name": "cartservice", "minutes_ago": 1}]
	decide(object.union(base, {"recent_actions": other})).decision == "allow"
	different := [{"type": "scale_deployment", "name": "frontend", "minutes_ago": 1}]
	decide(object.union(base, {"recent_actions": different})).decision == "allow"
}

test_observe_mode_denies_actions if {
	denied(object.union(base, {"mode": "observe"}), "observe mode")
	decide(object.union(with_action({"type": "escalate"}), {"mode": "observe"})).decision == "allow"
}

# ------------------------------------------------------------- scale

scale(n) := with_action({"type": "scale_deployment", "replicas": n})

test_scale_within_bounds_is_allowed if {
	decide(scale(3)).decision == "allow"
	decide(scale(4)).decision == "allow"
	decide(scale(1)).decision == "allow"
}

test_scale_to_zero_is_denied if denied(scale(0), "between 1 and 5")

test_scale_above_five_is_denied if {
	from_four := object.union(scale(6), {"current": object.union(base.current, {"replicas": 4})})
	denied(from_four, "between 1 and 5")
}

test_scale_jump_is_denied if {
	denied(scale(5), "at most +2 or -1")
	from_five := object.union(scale(3), {"current": object.union(base.current, {"replicas": 5})})
	denied(from_five, "at most +2 or -1")
}

test_scale_stateful_needs_approval if {
	redis := object.union(scale(3), {"current": object.union(base.current, {"stateful": true})})
	decide(redis) == {"decision": "require_approval", "reasons": ["target is stateful"]}
}

test_scale_stateful_from_zero_needs_approval_not_denial if {
	# Scenario s07: Redis was scaled to 0 and must come back.
	redis := object.union(scale(1), {"current": {"replicas": 0, "stateful": true}})
	decide(redis).decision == "require_approval"
}

# --------------------------------------------------- patch_resources

patch(limits) := with_action({"type": "patch_resources", "container": "server", "limits": limits})

test_patch_up_to_double_is_allowed if {
	decide(patch({"memory": "256Mi"})).decision == "allow"
	decide(patch({"cpu": "400m"})).decision == "allow"
	decide(patch({"memory": "200Mi", "cpu": "300m"})).decision == "allow"
}

test_patch_more_than_double_needs_approval if {
	decide(patch({"memory": "512Mi"})) == {"decision": "require_approval", "reasons": ["memory limit more than doubles"]}
	decide(patch({"cpu": "500m"})).decision == "require_approval"
}

test_patch_above_cap_is_denied if {
	denied(patch({"memory": "2Gi"}), "above 1Gi")
	denied(patch({"cpu": "1500m"}), "above 1")
}

test_patch_at_cap_is_not_denied if {
	decide(patch({"memory": "1Gi", "cpu": "1"})).decision == "require_approval"
}

test_patch_lowering_is_denied if {
	denied(patch({"memory": "64Mi"}), "only be raised")
	denied(patch({"cpu": "100m"}), "only be raised")
}

test_patch_without_limits_is_denied if denied(patch({}), "must set a memory or CPU limit")

# ---------------------------------------------------------- rollback

rollback(rev) := with_action({"type": "rollback_deployment", "to_revision": rev})

test_rollback_to_proven_revision_is_allowed if decide(rollback(6)).decision == "allow"

test_rollback_to_unproven_revision_is_denied if denied(rollback(7), "never healthy")

test_rollback_to_unknown_revision_is_denied if denied(rollback(2), "never healthy")

# ----------------------------------------------------------- open_pr

pr(files, lines) := object.union(base, {"action": {"type": "open_pr", "title": "t", "files": files, "diff_lines": lines}})

test_pr_on_affected_files_is_allowed if {
	decide(pr(["online-boutique/overrides/frontend-image.yaml"], 12)).decision == "allow"
}

test_pr_touching_other_files_is_denied if {
	denied(pr(["online-boutique/overrides/frontend-image.yaml", "online-boutique/upstream.yaml"], 5), "does not belong")
}

test_large_pr_is_denied if {
	denied(pr(["online-boutique/overrides/frontend-image.yaml"], 51), "more than 50")
}

test_pr_does_not_count_as_a_cluster_write if {
	busy := object.union(pr(["online-boutique/overrides/frontend-image.yaml"], 5), {"incident": object.union(base.incident, {"write_actions_so_far": 3})})
	decide(busy).decision == "allow"
}

# ------------------------------------------------------------ reasons

test_all_deny_reasons_are_reported if {
	bad := object.union(scale(0), {"frozen": true})
	r := decide(bad)
	r.decision == "deny"
	count(r.reasons) >= 3
}

test_deny_wins_over_approval if {
	redis := object.union(scale(0), {"current": object.union(base.current, {"stateful": true})})
	decide(redis).decision == "deny"
}

# ---------------------------------------------------------- approval

test_approval_lifts_require_approval if {
	redis := object.union(scale(3), {"current": object.union(base.current, {"stateful": true}), "approved": true})
	decide(redis) == {"decision": "allow", "reasons": ["approved by a human: target is stateful"]}
}

test_approval_never_lifts_a_denial if {
	redis := object.union(scale(0), {"current": object.union(base.current, {"stateful": true}), "approved": true})
	decide(redis).decision == "deny"
	denied(object.union(base, {"frozen": true, "approved": true}), "frozen")
}

test_approved_flag_must_be_exactly_true if {
	redis := object.union(scale(3), {"current": object.union(base.current, {"stateful": true}), "approved": "yes"})
	decide(redis).decision == "require_approval"
}
