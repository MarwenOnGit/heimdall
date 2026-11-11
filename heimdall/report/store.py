"""Stored postmortems, and the search over them.

A postmortem is saved when its incident closes. Saved reports are what
`search_past_incidents` looks through, so this is also where the benchmark's
leakage rules live: a run must never be helped by a report about the same
scenario, and hold-out reports are never searchable at all.
"""

import logging
import math
import re
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from heimdall.core.audit import AuditEvent, AuditLog, EventType
from heimdall.core.models import IncidentState
from heimdall.report.postmortem import Postmortem, build

log = logging.getLogger("heimdall.report")

HOLDOUT = "holdout"
# A report is written when the incident stops moving on its own.
_REPORT_STATES = {
    IncidentState.MITIGATED,
    IncidentState.RESOLVED,
    IncidentState.ESCALATED,
    IncidentState.FAILED,
}


@dataclass(frozen=True)
class RunLabel:
    """Which benchmark scenario is being run, if any. Set by the harness."""

    scenario: str | None = None
    scenario_set: str | None = None


@dataclass(frozen=True)
class Stored:
    postmortem: Postmortem
    label: RunLabel


class PostmortemStore:
    def __init__(
        self, conn: sqlite3.Connection, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._conn = conn
        self._clock = clock

    # ------------------------------------------------------------ label

    def label(self) -> RunLabel:
        rows = self._conn.execute(
            "SELECT key, value FROM settings WHERE key IN ('run_scenario', 'run_set')"
        ).fetchall()
        values = {row["key"]: row["value"] or None for row in rows}
        return RunLabel(values.get("run_scenario"), values.get("run_set"))

    def set_label(self, label: RunLabel) -> None:
        for key, value in (("run_scenario", label.scenario), ("run_set", label.scenario_set)):
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value or ""),
            )

    # ---------------------------------------------------------- reports

    def save(self, postmortem: Postmortem, label: RunLabel | None = None) -> None:
        """Store a report. A later report for the same incident replaces the
        earlier one but keeps the label the incident was first saved under."""
        label = label or self.label()
        self._conn.execute(
            "INSERT INTO postmortems VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (incident_id) DO UPDATE SET generated_at = excluded.generated_at,"
            " data_json = excluded.data_json",
            (
                postmortem.incident_id,
                self._clock().isoformat(),
                postmortem.opened_at.isoformat(),
                label.scenario,
                label.scenario_set,
                postmortem.model_dump_json(),
            ),
        )

    def get(self, incident_id: str) -> Stored | None:
        row = self._conn.execute(
            "SELECT * FROM postmortems WHERE incident_id = ?", (incident_id,)
        ).fetchone()
        return _stored(row) if row else None

    def all(self) -> list[Stored]:
        rows = self._conn.execute("SELECT * FROM postmortems ORDER BY opened_at").fetchall()
        return [_stored(row) for row in rows]

    def searchable(self, current: RunLabel | None = None) -> list[Stored]:
        """The reports a search may use right now.

        Outside a benchmark run: every report except hold-out ones. During a
        run of scenario X: only reports from dev runs of other scenarios.
        Reports with no label are left out then, because nothing says they
        are not about X.
        """
        current = current or self.label()
        allowed = []
        for stored in self.all():
            label = stored.label
            if label.scenario_set == HOLDOUT:
                continue
            if current.scenario is not None and (
                label.scenario is None or label.scenario == current.scenario
            ):
                continue
            allowed.append(stored)
        return allowed


def _stored(row: sqlite3.Row) -> Stored:
    return Stored(
        Postmortem.model_validate_json(row["data_json"]),
        RunLabel(row["scenario"], row["scenario_set"]),
    )


class PostmortemWriter:
    """Writes the report when an incident reaches a resting state.

    Subscribed to the audit log, so it needs no call from the pipeline and
    cannot be forgotten on a new code path.
    """

    def __init__(self, audit: AuditLog, store: PostmortemStore) -> None:
        self._audit = audit
        self._store = store

    def on_event(self, event: AuditEvent) -> None:
        if event.event_type != EventType.STATE_TRANSITION or event.incident_id is None:
            return
        if event.payload.get("to") not in _REPORT_STATES:
            return
        try:
            self._store.save(build(event.incident_id, self._audit.events(event.incident_id)))
        except Exception:
            # A report that cannot be written must not stop the incident.
            log.exception("could not write the postmortem for %s", event.incident_id)


# --------------------------------------------------------------------- search

_WORD = re.compile(r"[a-z0-9]+")
_K1, _B = 1.5, 0.75


def tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("_", " "))


def document(pm: Postmortem) -> str:
    """The text a report is found by: what broke, where, and what was done."""
    parts = [*pm.services, *(a.alertname for a in pm.alerts)]
    for cause in pm.root_causes:
        parts += [cause.category, cause.summary]
        if cause.affected_resource:
            parts.append(cause.affected_resource.name)
    parts += [a.action for a in pm.actions]
    return " ".join(parts)


def bm25(query: str, documents: list[str]) -> list[float]:
    """Okapi BM25 score of every document for the query."""
    docs = [tokens(d) for d in documents]
    if not docs:
        return []
    average = sum(len(d) for d in docs) / len(docs) or 1.0
    frequency = Counter(term for d in docs for term in set(d))
    scores = []
    for doc in docs:
        counts = Counter(doc)
        score = 0.0
        for term in set(tokens(query)):
            if term not in counts:
                continue
            idf = math.log(1 + (len(docs) - frequency[term] + 0.5) / (frequency[term] + 0.5))
            tf = counts[term]
            score += idf * tf * (_K1 + 1) / (tf + _K1 * (1 - _B + _B * len(doc) / average))
        scores.append(score)
    return scores


def search(store: PostmortemStore, query: str, limit: int = 3) -> list[Stored]:
    candidates = store.searchable()
    scores = bm25(query, [document(s.postmortem) for s in candidates])
    ranked = sorted(zip(scores, candidates, strict=True), key=lambda pair: -pair[0])
    return [stored for score, stored in ranked[:limit] if score > 0]
