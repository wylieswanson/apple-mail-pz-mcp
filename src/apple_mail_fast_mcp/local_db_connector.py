"""Read-only Apple Mail Envelope Index search accelerator.

This module reads Mail.app's local SQLite Envelope Index in ``mode=ro`` and
emits the same metadata row shape as ``search_messages``. It is intentionally
metadata-only: full body/attachment search needs a separate private index over
``.emlx`` files.
"""

from __future__ import annotations

import glob
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from typing import Any
from urllib.parse import quote

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off", ""}
_DEFAULT_LIMIT = 999_999_999


class LocalDbUnavailableError(RuntimeError):
    """The local Mail database cannot be read on this machine/session."""


class LocalDbUnsupportedQueryError(RuntimeError):
    """The requested query needs data not available from the Envelope Index."""


@dataclass(frozen=True)
class LocalDbSearch:
    """Inputs supported by the Envelope Index search path."""

    account_uuid: str
    mailbox: str
    sender_contains: str | None = None
    subject_contains: str | None = None
    read_status: bool | None = None
    is_flagged: bool | None = None
    date_from: str | None = None
    date_to: str | None = None
    received_after: datetime | None = None
    limit: int | None = None


def local_db_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the opt-in local DB accelerator is enabled."""
    value = (env or os.environ).get("APPLE_MAIL_MCP_LOCAL_DB", "0")
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return False


def _default_envelope_index_path() -> Path:
    hits = sorted(glob.glob(os.path.expanduser("~/Library/Mail/V*/MailData/Envelope Index")))
    if not hits:
        raise LocalDbUnavailableError(
            "Apple Mail Envelope Index not found; Mail may not be configured "
            "or the host app may lack Full Disk Access."
        )
    return Path(hits[-1])


def _parse_iso_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO 8601 YYYY-MM-DD, got: {value!r}") from exc


def _local_midnight_ts(value: str, field: str) -> float:
    parsed = _parse_iso_date(value, field)
    return datetime.combine(parsed, dt_time.min).astimezone().timestamp()


def _local_next_midnight_ts(value: str, field: str) -> float:
    parsed = _parse_iso_date(value, field) + timedelta(days=1)
    return datetime.combine(parsed, dt_time.min).astimezone().timestamp()


def _received_iso(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone().isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _bare_message_id(value: str | None) -> str | None:
    if not value:
        return None
    stripped = value.strip()
    if stripped.startswith("<") and stripped.endswith(">"):
        stripped = stripped[1:-1]
    return stripped or None


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _mailbox_patterns(account_uuid: str, mailbox: str) -> list[str]:
    raw = f"%://{account_uuid}/{mailbox}%"
    encoded_mailbox = quote(mailbox, safe="/[]")
    encoded = f"%://{account_uuid}/{encoded_mailbox}%"
    if encoded == raw:
        return [raw]
    return [raw, encoded]


class LocalDbConnector:
    """Read Apple Mail's Envelope Index without touching Mail.app state."""

    def __init__(self, envelope_index_path: Path | None = None) -> None:
        self._envelope_index_path = envelope_index_path

    def _path(self) -> Path:
        override = os.getenv("APPLE_MAIL_MCP_LOCAL_DB_PATH")
        if override:
            return Path(override).expanduser()
        return self._envelope_index_path or _default_envelope_index_path()

    def _connect(self) -> sqlite3.Connection:
        path = self._path()
        if not path.exists():
            raise LocalDbUnavailableError(f"Apple Mail Envelope Index not found: {path}")
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise LocalDbUnavailableError(f"Cannot open Envelope Index read-only: {path}") from exc
        conn.row_factory = sqlite3.Row
        return conn

    def search_messages(self, query: LocalDbSearch) -> list[dict[str, Any]]:
        """Search metadata rows in the Envelope Index.

        Raises ``ValueError`` for invalid date inputs to match the existing
        connector contract. Operational SQLite failures are left as-is so the
        caller can fall back to AppleScript when the local schema differs.
        """
        sql = """
            SELECT
                m.message_id AS id,
                g.message_id_header AS rfc_message_id,
                s.subject AS subject,
                a.address AS sender_address,
                a.comment AS sender_name,
                m.date_received AS date_received,
                m.read AS read_status,
                m.flagged AS flagged,
                mb.url AS mailbox_url
            FROM messages m
            LEFT JOIN subjects s ON s.ROWID = m.subject
            LEFT JOIN addresses a ON a.ROWID = m.sender
            LEFT JOIN mailboxes mb ON mb.ROWID = m.mailbox
            LEFT JOIN message_global_data g ON g.message_id = m.message_id
            WHERE m.deleted = 0
        """
        params: list[Any] = []

        mailbox_patterns = _mailbox_patterns(query.account_uuid, query.mailbox)
        if len(mailbox_patterns) == 1:
            sql += " AND mb.url LIKE ?"
            params.append(mailbox_patterns[0])
        else:
            sql += " AND (mb.url LIKE ? OR mb.url LIKE ?)"
            params.extend(mailbox_patterns)

        if query.sender_contains:
            pattern = _like_pattern(query.sender_contains)
            sql += """
                AND (
                    LOWER(COALESCE(a.address, '')) LIKE LOWER(?) ESCAPE '\\'
                    OR LOWER(COALESCE(a.comment, '')) LIKE LOWER(?) ESCAPE '\\'
                )
            """
            params.extend([pattern, pattern])

        if query.subject_contains:
            sql += " AND LOWER(COALESCE(s.subject, '')) LIKE LOWER(?) ESCAPE '\\'"
            params.append(_like_pattern(query.subject_contains))

        if query.read_status is not None:
            sql += " AND m.read = ?"
            params.append(1 if query.read_status else 0)

        if query.is_flagged is not None:
            sql += " AND m.flagged = ?"
            params.append(1 if query.is_flagged else 0)

        if query.date_from is not None:
            sql += " AND m.date_received >= ?"
            params.append(_local_midnight_ts(query.date_from, "date_from"))

        if query.date_to is not None:
            sql += " AND m.date_received < ?"
            params.append(_local_next_midnight_ts(query.date_to, "date_to"))

        if query.received_after is not None:
            sql += " AND m.date_received >= ?"
            params.append(query.received_after.timestamp())

        sql += " ORDER BY m.date_received DESC LIMIT ?"
        params.append(int(query.limit) if query.limit is not None else _DEFAULT_LIMIT)

        with self._connect() as conn:
            return [self._row_to_message(row) for row in conn.execute(sql, params)]

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> dict[str, Any]:
        sender = row["sender_address"] or row["sender_name"] or ""
        return {
            "id": str(row["id"]),
            "rfc_message_id": _bare_message_id(row["rfc_message_id"]),
            "subject": row["subject"] or "",
            "sender": sender,
            "date_received": _received_iso(row["date_received"]),
            "read_status": bool(row["read_status"]),
            "flagged": bool(row["flagged"]),
        }
