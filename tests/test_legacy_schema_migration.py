from __future__ import annotations

import pytest

from app.storage import (
    CURRENT_SCHEMA_COLUMNS,
    CURRENT_SCHEMA_VERSION,
    connect,
    init_db,
    verify_audit_chain,
)


def _create_legacy_snapshot(path, *, extra_point_column: bool = False):
    conn = connect(str(path))

    extra = ", rogue_column text" if extra_point_column else ""

    conn.executescript(
        f"""
        create table demo_point_ledger (
            id integer primary key autoincrement,
            user_id text not null,
            market_id text,
            amount real not null,
            balance_after real not null,
            entry_type text not null,
            note text not null,
            created_at text not null default current_timestamp,
            balance_before real,
            reference_type text,
            reference_id text,
            idempotency_key text,
            request_id text
            {extra}
        );

        create table demo_audit_events (
            id integer primary key autoincrement,
            event_type text not null,
            user_id text,
            route text,
            request_id text,
            reference_type text,
            reference_id text,
            before_json text,
            after_json text,
            note text,
            created_at text not null default current_timestamp,
            previous_event_hash text,
            event_hash text,
            integrity_payload_json text
        );

        create table simulated_orders (
            id integer primary key autoincrement,
            user_id text not null,
            market_id text not null,
            outcome text not null,
            stake real not null,
            probability real not null,
            created_at text not null default current_timestamp,
            idempotency_key text,
            request_id text
        );

        create table simulated_positions (
            id integer primary key autoincrement,
            user_id text not null,
            market_id text not null,
            outcome text not null,
            stake real not null,
            probability real not null,
            estimated_return real not null,
            created_at text not null default current_timestamp,
            idempotency_key text,
            request_id text
        );
        """
    )

    conn.execute(
        """
        insert into demo_audit_events(
            event_type,
            user_id,
            route,
            request_id,
            reference_type,
            reference_id,
            before_json,
            after_json,
            note,
            created_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "demo_prediction_created",
            "participant-1",
            "/api/demo/predict",
            None,
            "simulated_position",
            "1",
            '{"balance":10000.0}',
            '{"balance":9990.0}',
            "legacy row",
            "2026-07-05 14:18:30",
        ),
    )

    conn.commit()
    return conn


def test_realistic_legacy_v0_migrates_and_backfills_audit_chain(tmp_path):
    conn = _create_legacy_snapshot(tmp_path / "legacy.sqlite3")

    assert conn.execute("pragma user_version").fetchone()[0] == 0

    before = conn.execute(
        "select * from demo_audit_events where id = 1"
    ).fetchone()

    original_fields = {
        key: before[key]
        for key in (
            "event_type",
            "user_id",
            "route",
            "request_id",
            "reference_type",
            "reference_id",
            "before_json",
            "after_json",
            "note",
            "created_at",
        )
    }

    assert verify_audit_chain(conn)["integrity_status"] == "partial_legacy_rows"

    init_db(conn)

    assert (
        conn.execute("pragma user_version").fetchone()[0]
        == CURRENT_SCHEMA_VERSION
    )
    assert conn.execute("pragma quick_check").fetchone()[0] == "ok"
    assert conn.execute("pragma foreign_key_check").fetchone() is None

    for table in (
        "demo_point_ledger",
        "demo_audit_events",
        "simulated_orders",
        "simulated_positions",
    ):
        actual = tuple(
            row["name"]
            for row in conn.execute(f'pragma table_info("{table}")')
        )
        expected = CURRENT_SCHEMA_COLUMNS[table]

        assert len(actual) == len(expected)
        assert set(actual) == set(expected)

    migrated = conn.execute(
        "select * from demo_audit_events where id = 1"
    ).fetchone()

    for key, value in original_fields.items():
        assert migrated[key] == value

    assert migrated["previous_event_hash"] == ""
    assert len(migrated["event_hash"]) == 64
    assert migrated["integrity_payload_json"]

    audit = verify_audit_chain(conn)

    assert audit == {
        "checked_count": 2,
        "verified_count": 2,
        "missing_hash_count": 0,
        "broken_count": 0,
        "first_broken_event_id": None,
        "integrity_status": "verified",
    }

    migration_event = conn.execute(
        """
        select *
        from demo_audit_events
        where id = 2
        """
    ).fetchone()

    assert migration_event["event_type"] == "audit_integrity_backfilled"
    assert migration_event["reference_type"] == "schema_migration"
    assert len(migration_event["previous_event_hash"]) == 64
    assert len(migration_event["event_hash"]) == 64

    conn.close()


def test_legacy_migration_still_rejects_structural_schema_difference(tmp_path):
    conn = _create_legacy_snapshot(
        tmp_path / "structural.sqlite3",
        extra_point_column=True,
    )

    with pytest.raises(
        RuntimeError,
        match="schema v1 invalid columns for demo_point_ledger",
    ):
        init_db(conn)

    # Entire migration transaction must roll back.
    assert conn.execute("pragma user_version").fetchone()[0] == 0

    row = conn.execute(
        "select event_hash from demo_audit_events where id = 1"
    ).fetchone()
    assert row["event_hash"] is None

    conn.close()


def test_legacy_migration_rejects_mixed_hashed_and_unhashed_audit_rows(tmp_path):
    conn = _create_legacy_snapshot(tmp_path / "mixed.sqlite3")

    conn.execute(
        """
        insert into demo_audit_events(
            event_type,
            created_at,
            previous_event_hash,
            event_hash,
            integrity_payload_json
        ) values (?, ?, ?, ?, ?)
        """,
        (
            "newer_event",
            "2026-07-06T00:00:00+00:00",
            "",
            "a" * 64,
            "{}",
        ),
    )
    conn.commit()

    with pytest.raises(
        RuntimeError,
        match="mixed legacy and hashed audit chain",
    ):
        init_db(conn)

    assert conn.execute("pragma user_version").fetchone()[0] == 0

    first = conn.execute(
        "select event_hash from demo_audit_events where id = 1"
    ).fetchone()
    assert first["event_hash"] is None

    conn.close()
