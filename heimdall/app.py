"""Wires the components together from configuration."""

import sqlite3
from dataclasses import dataclass

import httpx
from fastapi import FastAPI

from heimdall.agent.setup import (
    build_approvals,
    build_investigator,
    build_llm,
    build_pipeline,
    read_only_kube,
)
from heimdall.agent.worker import Worker
from heimdall.api import create_app
from heimdall.config import Config
from heimdall.core import anchor as anchoring
from heimdall.core.actions import ActionStore
from heimdall.core.approvals import Approval
from heimdall.core.audit import AuditLog
from heimdall.core.confirm import PrometheusConfirmer
from heimdall.core.incidents import IncidentManager
from heimdall.core.revisions import RevisionTracker, RevisionWatcher
from heimdall.core.store import connect
from heimdall.report.store import PostmortemStore, PostmortemWriter
from heimdall.tools.dependencies import DependencyGraph

ANCHOR_REPO = "heimdall/audit-anchors"


@dataclass
class Heimdall:
    config: Config
    conn: sqlite3.Connection
    audit: AuditLog
    incidents: IncidentManager
    anchorer: anchoring.Anchorer | None
    gitea_anchors: anchoring.GiteaAnchorPublisher | None
    api: FastAPI
    worker: Worker | None = None
    revisions: RevisionWatcher | None = None


def gitea_anchor_publisher(config: Config) -> anchoring.GiteaAnchorPublisher | None:
    if not (config.gitea_url and config.gitea_token):
        return None
    client = httpx.Client(
        base_url=config.gitea_url,
        headers={"Authorization": f"token {config.gitea_token}"},
        timeout=15,
    )
    return anchoring.GiteaAnchorPublisher(client, ANCHOR_REPO)


def build(config: Config) -> Heimdall:
    conn = connect(config.db_path)
    audit = AuditLog(conn)
    incidents = IncidentManager(conn, audit, DependencyGraph.load(config.dependencies_path))

    gitea_anchors = gitea_anchor_publisher(config)
    anchorer = None
    if config.audit_key_path:
        publishers: list[anchoring.AnchorPublisher] = []
        if config.anchor_file:
            publishers.append(anchoring.FileAnchorPublisher(config.anchor_file))
        if gitea_anchors:
            publishers.append(gitea_anchors)
        anchorer = anchoring.Anchorer(
            audit,
            anchoring.load_private_key(config.audit_key_path),
            publishers,
            every=config.anchor_every_events,
        )

    postmortems = PostmortemStore(conn)
    audit.subscribe(PostmortemWriter(audit, postmortems).on_event)

    confirmer = PrometheusConfirmer(httpx.Client(base_url=config.prometheus_url, timeout=10))
    # Pick up anything a previous run left unfinished.
    incidents.reconcile()

    approvals = build_approvals(config, conn)
    worker = None
    pipeline = None
    revisions = None
    llm = build_llm(config)
    if llm is not None:
        investigator = build_investigator(config, audit, llm, postmortems)
        actions = ActionStore(conn)
        # Its own connection: the tracker writes from a background thread.
        tracker = RevisionTracker(
            read_only_kube(config), connect(config.db_path), config.namespaces
        )
        revisions = RevisionWatcher(tracker)
        pipeline = build_pipeline(config, audit, llm, incidents, actions, approvals, tracker)
        worker = Worker(
            incidents,
            investigator,
            conn,
            settle_seconds=config.settle_seconds,
            pipeline=pipeline,
            actions=actions,
        )

    def on_decision(approval: Approval, approved: bool) -> None:
        if pipeline is not None:
            pipeline.resume(approval, approved=approved)

    api = create_app(
        alert_token=config.alert_token,
        manager=incidents,
        confirmer=confirmer,
        approvals=approvals if pipeline is not None else None,
        on_decision=on_decision,
    )
    return Heimdall(config, conn, audit, incidents, anchorer, gitea_anchors, api, worker, revisions)
