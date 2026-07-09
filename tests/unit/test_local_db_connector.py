"""Tests for the read-only Envelope Index search accelerator."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from apple_mail_fast_mcp.local_db_connector import (
    LocalDbConnector,
    LocalDbSearch,
    local_db_enabled,
)


def _build_envelope_index(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE subjects (
            ROWID INTEGER PRIMARY KEY,
            subject TEXT
        );
        CREATE TABLE addresses (
            ROWID INTEGER PRIMARY KEY,
            address TEXT,
            comment TEXT
        );
        CREATE TABLE mailboxes (
            ROWID INTEGER PRIMARY KEY,
            url TEXT
        );
        CREATE TABLE message_global_data (
            message_id INTEGER PRIMARY KEY,
            message_id_header TEXT
        );
        CREATE TABLE messages (
            message_id INTEGER,
            subject INTEGER,
            sender INTEGER,
            mailbox INTEGER,
            date_received REAL,
            read INTEGER,
            flagged INTEGER,
            deleted INTEGER
        );
    """)
    conn.executemany(
        "INSERT INTO subjects(ROWID, subject) VALUES (?, ?)",
        [(1, "Quarterly Budget"), (2, "Dinner Plans"), (3, "Budget Archive")],
    )
    conn.executemany(
        "INSERT INTO addresses(ROWID, address, comment) VALUES (?, ?, ?)",
        [
            (1, "alice@example.com", "Alice"),
            (2, "bob@example.com", "Bob"),
        ],
    )
    conn.executemany(
        "INSERT INTO mailboxes(ROWID, url) VALUES (?, ?)",
        [
            (1, "imap://ACC-1/INBOX"),
            (2, "imap://ACC-1/Sent%20Messages"),
            (3, "imap://ACC-2/INBOX"),
        ],
    )
    conn.executemany(
        "INSERT INTO message_global_data(message_id, message_id_header) VALUES (?, ?)",
        [(111, "<rfc-111@example.com>"), (222, "rfc-222@example.com")],
    )
    conn.executemany(
        """
        INSERT INTO messages(
            ROWID, message_id, subject, sender, mailbox, date_received,
            read, flagged, deleted
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                10,
                111,
                1,
                1,
                1,
                datetime(2026, 5, 10, 12, tzinfo=timezone.utc).timestamp(),
                0,
                1,
                0,
            ),
            (
                11,
                222,
                2,
                2,
                1,
                datetime(2026, 5, 9, 12, tzinfo=timezone.utc).timestamp(),
                1,
                0,
                0,
            ),
            (
                12,
                333,
                3,
                1,
                2,
                datetime(2026, 5, 8, 12, tzinfo=timezone.utc).timestamp(),
                0,
                0,
                0,
            ),
            (
                13,
                444,
                1,
                1,
                3,
                datetime(2026, 5, 7, 12, tzinfo=timezone.utc).timestamp(),
                0,
                0,
                0,
            ),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def envelope_index(tmp_path: Path) -> Path:
    path = tmp_path / "Envelope Index"
    _build_envelope_index(path)
    return path


def test_local_db_enabled_parses_opt_in_values() -> None:
    assert local_db_enabled({"APPLE_MAIL_MCP_LOCAL_DB": "1"}) is True
    assert local_db_enabled({"APPLE_MAIL_MCP_LOCAL_DB": "true"}) is True
    assert local_db_enabled({"APPLE_MAIL_MCP_LOCAL_DB": "0"}) is False
    assert local_db_enabled({}) is False


def test_search_messages_filters_and_matches_common_shape(envelope_index: Path) -> None:
    connector = LocalDbConnector(envelope_index)

    rows = connector.search_messages(
        LocalDbSearch(
            account_uuid="ACC-1",
            mailbox="INBOX",
            subject_contains="budget",
            sender_contains="alice",
            read_status=False,
            is_flagged=True,
            limit=10,
        )
    )

    assert rows == [
        {
            "id": "111",
            "rfc_message_id": "rfc-111@example.com",
            "subject": "Quarterly Budget",
            "sender": "alice@example.com",
            "date_received": rows[0]["date_received"],
            "read_status": False,
            "flagged": True,
        }
    ]
    assert rows[0]["date_received"].startswith("2026-05-10")


def test_search_messages_filters_encoded_mailbox_names(envelope_index: Path) -> None:
    connector = LocalDbConnector(envelope_index)

    rows = connector.search_messages(
        LocalDbSearch(account_uuid="ACC-1", mailbox="Sent Messages", limit=10)
    )

    assert [row["id"] for row in rows] == ["333"]


def test_search_messages_rejects_bad_dates(envelope_index: Path) -> None:
    connector = LocalDbConnector(envelope_index)

    with pytest.raises(ValueError, match="date_from"):
        connector.search_messages(
            LocalDbSearch(account_uuid="ACC-1", mailbox="INBOX", date_from="last week")
        )
