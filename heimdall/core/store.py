"""SQLite storage. One file holds incidents, alerts and the audit log."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    seq          INTEGER PRIMARY KEY,
    timestamp    TEXT NOT NULL,
    incident_id  TEXT,
    actor        TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY,
    received_at  TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    status       TEXT NOT NULL,
    alertname    TEXT NOT NULL,
    labels_json  TEXT NOT NULL,
    raw_json     TEXT NOT NULL,
    confirmed    INTEGER NOT NULL,
    incident_id  TEXT
);
CREATE INDEX IF NOT EXISTS alerts_by_incident ON alerts (incident_id);

CREATE TABLE IF NOT EXISTS incidents (
    id           TEXT PRIMARY KEY,
    state        TEXT NOT NULL,
    namespace    TEXT,
    services_json TEXT NOT NULL,
    flapping     INTEGER NOT NULL DEFAULT 0,
    last_alert_at TEXT NOT NULL,
    opened_at    TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS incidents_by_state ON incidents (state);

CREATE TABLE IF NOT EXISTS actions (
    id              INTEGER PRIMARY KEY,
    incident_id     TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    type            TEXT NOT NULL,
    namespace       TEXT,
    name            TEXT,
    proposal_json   TEXT NOT NULL,
    decision        TEXT NOT NULL,
    status          TEXT NOT NULL,
    detail          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS actions_by_incident ON actions (incident_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the database in WAL mode and create the schema if needed.

    Transactions are managed explicitly (`BEGIN IMMEDIATE` ... `COMMIT`), so a
    writer always sees the latest committed row before adding the next one.
    """
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
