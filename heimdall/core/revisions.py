"""The revision tracker: how long each revision of a Deployment was Available.

Kubernetes keeps old ReplicaSets but not how long each one served. The policy
only allows a rollback to a revision that was healthy for a while, so Heimdall
measures it: at every look, a Deployment that is fully rolled out and
Available has the time since the previous look credited to its current
revision. A revision that never became healthy is never credited.

Time is only credited across short gaps. If Heimdall was down, nobody was
watching, and the gap counts for nothing.
"""

import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from heimdall.tools.kube import KubeClient, KubeError

log = logging.getLogger("heimdall.revisions")

REVISION_ANNOTATION = "deployment.kubernetes.io/revision"


def is_available(deployment: dict[str, Any]) -> bool:
    """True when every wanted replica runs the current revision and is available."""
    wanted = deployment.get("spec", {}).get("replicas", 1)
    status = deployment.get("status", {})
    conditions = {c.get("type"): c.get("status") for c in status.get("conditions", [])}
    return bool(
        wanted >= 1
        and conditions.get("Available") == "True"
        and status.get("updatedReplicas", 0) >= wanted
        and status.get("availableReplicas", 0) >= wanted
        and status.get("observedGeneration", 0) >= deployment["metadata"].get("generation", 0)
    )


class RevisionTracker:
    def __init__(
        self,
        kube: KubeClient,
        conn: sqlite3.Connection,
        namespaces: frozenset[str] | set[str],
        *,
        max_gap_seconds: float = 90,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._kube = kube
        self._conn = conn
        self._namespaces = sorted(namespaces)
        self._max_gap = max_gap_seconds
        self._clock = clock

    def observe(self) -> None:
        """Look at every Deployment once and credit the healthy ones."""
        now = self._clock()
        for namespace in self._namespaces:
            listing = self._kube.get(f"/apis/apps/v1/namespaces/{namespace}/deployments")
            for deployment in listing.get("items", []):
                self._observe(namespace, deployment, now)

    def _observe(self, namespace: str, deployment: dict[str, Any], now: datetime) -> None:
        metadata = deployment["metadata"]
        revision = metadata.get("annotations", {}).get(REVISION_ANNOTATION)
        if revision is None:
            return
        key = (namespace, metadata["name"], int(revision))
        row = self._conn.execute(
            "SELECT last_available_at FROM revision_availability"
            " WHERE namespace = ? AND deployment = ? AND revision = ?",
            key,
        ).fetchone()
        available = is_available(deployment)
        credit = 0.0
        if available and row is not None and row["last_available_at"]:
            gap = (now - datetime.fromisoformat(row["last_available_at"])).total_seconds()
            if 0 < gap <= self._max_gap:
                credit = gap
        # `last_available_at` is cleared while the revision is unhealthy, so
        # the time it spent broken is never credited when it recovers.
        self._conn.execute(
            "INSERT INTO revision_availability (namespace, deployment, revision, first_seen_at,"
            " available_seconds, last_available_at) VALUES (?, ?, ?, ?, 0, ?)"
            " ON CONFLICT (namespace, deployment, revision) DO UPDATE SET"
            " available_seconds = available_seconds + ?, last_available_at = ?",
            (*key, now.isoformat(), now.isoformat() if available else None, credit,
             now.isoformat() if available else None),
        )  # fmt: skip

    def available_minutes(self, namespace: str, deployment: str) -> dict[int, float]:
        """Measured minutes of availability for every revision seen so far."""
        rows = self._conn.execute(
            "SELECT revision, available_seconds FROM revision_availability"
            " WHERE namespace = ? AND deployment = ?",
            (namespace, deployment),
        ).fetchall()
        return {row["revision"]: round(row["available_seconds"] / 60, 1) for row in rows}


class RevisionWatcher:
    """Runs the tracker in the background."""

    def __init__(self, tracker: RevisionTracker, poll_seconds: float = 30) -> None:
        self._tracker = tracker
        self._poll = poll_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="revisions", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tracker.observe()
            except (KubeError, sqlite3.Error, OSError):
                log.exception("could not record revision availability")
            self._stop.wait(self._poll)
