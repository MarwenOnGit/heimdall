import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from heimdall.core import anchor as anchoring
from heimdall.core.audit import GENESIS_HASH, AuditLog, EventType
from heimdall.core.store import connect


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "heimdall.sqlite")


@pytest.fixture
def log(conn: sqlite3.Connection) -> AuditLog:
    return AuditLog(conn, clock=lambda: datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC))


def fill(log: AuditLog, n: int) -> None:
    for i in range(n):
        log.append(EventType.TOOL_CALL, {"tool": "list_pods", "i": i}, actor="investigator",
                   incident_id="inc_1")  # fmt: skip


def test_events_are_chained_from_genesis(log: AuditLog) -> None:
    first = log.append(EventType.ALERT_RECEIVED, {"alertname": "X"}, actor="ingest")
    second = log.append(EventType.TOOL_CALL, {"tool": "list_pods"}, actor="investigator")

    assert (first.seq, second.seq) == (1, 2)
    assert first.prev_hash == GENESIS_HASH
    assert second.prev_hash == first.hash
    assert log.verify_chain() is None


def test_hash_does_not_depend_on_payload_key_order(tmp_path: Path) -> None:
    clock = lambda: datetime(2026, 1, 1, tzinfo=UTC)  # noqa: E731
    a = AuditLog(connect(tmp_path / "a.sqlite"), clock)
    b = AuditLog(connect(tmp_path / "b.sqlite"), clock)

    ha = a.append(EventType.DIAGNOSIS, {"x": 1, "y": [1.5, "é"]}, actor="t").hash
    hb = b.append(EventType.DIAGNOSIS, {"y": [1.5, "é"], "x": 1}, actor="t").hash

    assert ha == hb


def test_unhashable_payload_is_rejected_and_nothing_is_written(log: AuditLog) -> None:
    with pytest.raises(Exception):  # noqa: B017 - rfc8785 raises its own error types
        log.append(EventType.TOOL_RESULT, {"value": math.nan}, actor="t")
    assert log.head() is None


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        ('UPDATE audit_log SET payload_json = \'{"tool":"delete_all"}\' WHERE seq = 3', "hash"),
        ("UPDATE audit_log SET actor = 'human' WHERE seq = 3", "hash"),
        ("UPDATE audit_log SET timestamp = '2020-01-01T00:00:00' WHERE seq = 3", "hash"),
        ("DELETE FROM audit_log WHERE seq = 3", "missing"),
        ("UPDATE audit_log SET prev_hash = hash WHERE seq = 3", "prev_hash"),
    ],
)
def test_tampering_is_detected_at_the_right_row(
    log: AuditLog, conn: sqlite3.Connection, statement: str, reason: str
) -> None:
    fill(log, 5)
    conn.execute(statement)

    problem = log.verify_chain()

    assert problem is not None
    assert problem.seq == 3
    assert reason in problem.reason


def test_events_can_be_read_back_per_incident(log: AuditLog) -> None:
    log.append(EventType.ALERT_RECEIVED, {"a": 1}, actor="ingest", incident_id="inc_1")
    log.append(EventType.ALERT_RECEIVED, {"a": 2}, actor="ingest", incident_id="inc_2")

    assert [e.payload for e in log.events("inc_2")] == [{"a": 2}]
    assert len(list(log.events())) == 2


def recompute_chain(conn: sqlite3.Connection) -> None:
    """What an attacker with database access would do after editing a row."""
    from heimdall.core.audit import compute_hash

    prev = GENESIS_HASH
    import json

    for row in conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall():
        new_hash = compute_hash(
            seq=row["seq"], timestamp=row["timestamp"], incident_id=row["incident_id"],
            actor=row["actor"], event_type=row["event_type"],
            payload=json.loads(row["payload_json"]), prev_hash=prev,
        )  # fmt: skip
        conn.execute("UPDATE audit_log SET prev_hash = ?, hash = ? WHERE seq = ?",
                     (prev, new_hash, row["seq"]))  # fmt: skip
        prev = new_hash


def test_rewritten_chain_passes_chain_check_but_fails_against_anchors(
    log: AuditLog, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    key = anchoring.generate_key()
    anchor_file = tmp_path / "anchors.jsonl"
    anchorer = anchoring.Anchorer(log, key, [anchoring.FileAnchorPublisher(anchor_file)])
    fill(log, 5)
    anchorer.anchor_now()
    anchors = anchoring.read_anchor_file(anchor_file)
    assert anchoring.verify(log, anchors, key.public_key()) is None

    conn.execute('UPDATE audit_log SET payload_json = \'{"tool":"other"}\' WHERE seq = 2')
    recompute_chain(conn)

    assert log.verify_chain() is None  # the chain alone cannot see it
    problem = anchoring.verify(log, anchors, key.public_key())
    assert problem is not None
    assert problem.seq == 5
    assert "rewritten" in problem.reason


def test_truncated_log_fails_against_anchors(
    log: AuditLog, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    key = anchoring.generate_key()
    anchor_file = tmp_path / "anchors.jsonl"
    anchorer = anchoring.Anchorer(log, key, [anchoring.FileAnchorPublisher(anchor_file)])
    fill(log, 5)
    anchorer.anchor_now()

    conn.execute("DELETE FROM audit_log WHERE seq >= 4")

    assert log.verify_chain() is None
    problem = anchoring.verify(log, anchoring.read_anchor_file(anchor_file), key.public_key())
    assert problem is not None
    assert "truncated" in problem.reason


def test_anchor_signed_by_another_key_is_rejected(log: AuditLog, tmp_path: Path) -> None:
    key, other = anchoring.generate_key(), anchoring.generate_key()
    fill(log, 2)
    head = log.head()
    assert head is not None
    forged = anchoring.sign(head, other)

    problem = anchoring.verify(log, [forged], key.public_key())

    assert problem is not None
    assert "signature" in problem.reason


def test_anchors_are_published_every_n_events(log: AuditLog, tmp_path: Path) -> None:
    anchor_file = tmp_path / "anchors.jsonl"
    anchoring.Anchorer(
        log, anchoring.generate_key(), [anchoring.FileAnchorPublisher(anchor_file)], every=3
    )
    fill(log, 7)

    anchors = anchoring.read_anchor_file(anchor_file)
    # Anchors after the 3rd and 6th tool call. Anchor events themselves are
    # rows in the log (seq 4 and 8) but do not count towards the interval.
    assert [a.seq for a in anchors] == [3, 7]
    assert [e.event_type for e in log.events()].count("anchor") == 2
    assert log.verify_chain() is None


def test_keys_round_trip_through_pem(tmp_path: Path) -> None:
    key = anchoring.generate_key()
    path = tmp_path / "audit.key"
    path.write_bytes(anchoring.private_key_pem(key))

    loaded = anchoring.load_private_key(path)
    public = anchoring.load_public_key(anchoring.public_key_pem(key.public_key()))

    signature = loaded.sign(b"x")
    public.verify(signature, b"x")
