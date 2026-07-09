"""Read-only Apple Mail Envelope Index search accelerator.

This module reads Mail.app's local SQLite Envelope Index in ``mode=ro`` and
emits the same metadata row shape as ``search_messages``. It is intentionally
metadata-only: full body/attachment search needs a separate private index over
``.emlx`` files.
"""

from __future__ import annotations

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
_REQUIRED_TABLES = frozenset(
    {"messages", "subjects", "addresses", "mailboxes", "message_global_data"}
)


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


def _safe_exists(path: Path) -> tuple[bool, str | None]:
    try:
        return path.exists(), None
    except PermissionError as exc:
        return False, f"Permission denied: {exc}"
    except OSError as exc:
        return False, str(exc)


def _safe_readable(path: Path) -> bool:
    try:
        return os.access(path, os.R_OK)
    except OSError:
        return False


def _default_envelope_index_path() -> Path:
    mail_base = Path.home() / "Library" / "Mail"
    if not mail_base.exists():
        raise LocalDbUnavailableError(
            f"Apple Mail directory not found: {mail_base}. Mail.app may not be configured."
        )

    try:
        version_dirs = sorted(
            (
                child
                for child in mail_base.iterdir()
                if child.is_dir() and child.name.startswith("V") and child.name[1:].isdigit()
            ),
            key=lambda path: int(path.name[1:]),
        )
    except PermissionError as exc:
        raise LocalDbUnavailableError(
            f"Cannot list {mail_base}. Grant Full Disk Access to the parent app "
            "launching this process, then fully quit and reopen it."
        ) from exc

    candidates = [version / "MailData" / "Envelope Index" for version in version_dirs]
    for candidate in reversed(candidates):
        try:
            if candidate.exists():
                return candidate
        except PermissionError as exc:
            raise LocalDbUnavailableError(
                f"Cannot access {candidate}. Grant Full Disk Access to the parent app "
                "launching this process, then fully quit and reopen it."
            ) from exc

    scanned = ", ".join(str(candidate) for candidate in candidates) or str(mail_base / "V*")
    raise LocalDbUnavailableError(
        "Apple Mail Envelope Index not found. Checked: "
        f"{scanned}. Mail.app may not have synced mail locally yet."
    )


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
        stripped = stripped[1:-1].strip()
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


def _mailbox_url_where_clause(patterns: list[str]) -> tuple[str, list[Any]]:
    if len(patterns) == 1:
        return "url LIKE ?", [patterns[0]]
    return "(url LIKE ? OR url LIKE ?)", [patterns[0], patterns[1]]


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

    def diagnose(self, env: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Return a read-only health report for the local Envelope Index path."""
        env_map = env or os.environ
        report: dict[str, Any] = {
            "enabled": local_db_enabled(env_map),
            "env_value": env_map.get("APPLE_MAIL_MCP_LOCAL_DB"),
            "path_override": env_map.get("APPLE_MAIL_MCP_LOCAL_DB_PATH"),
            "mail_directory": str(Path.home() / "Library" / "Mail"),
            "mail_directory_exists": False,
            "mail_directory_readable": False,
            "mail_versions": [],
            "active_mail_version": None,
            "envelope_index_path": None,
            "envelope_index_exists": False,
            "envelope_index_readable": False,
            "sqlite_openable": False,
            "schema_ok": False,
            "missing_tables": sorted(_REQUIRED_TABLES),
            "available": False,
            "error": None,
            "recommendations": [],
        }

        path = self._diagnose_path(env_map, report)
        if path is not None:
            self._diagnose_sqlite(path, report)
        report["available"] = bool(report["sqlite_openable"] and report["schema_ok"])
        report["recommendations"] = self._diagnose_recommendations(report)
        return report

    def _diagnose_path(
        self, env: Mapping[str, str], report: dict[str, Any]
    ) -> Path | None:
        override = env.get("APPLE_MAIL_MCP_LOCAL_DB_PATH")
        if override:
            path = Path(override).expanduser()
            self._record_envelope_index_status(path, report)
            return path
        if self._envelope_index_path is not None:
            self._record_envelope_index_status(self._envelope_index_path, report)
            return self._envelope_index_path

        mail_base = Path.home() / "Library" / "Mail"
        exists, error = _safe_exists(mail_base)
        report["mail_directory_exists"] = exists
        if error is not None:
            report["error"] = error
            return None
        if not exists:
            report["error"] = f"Apple Mail directory not found: {mail_base}"
            return None

        try:
            version_dirs = sorted(
                (
                    child
                    for child in mail_base.iterdir()
                    if child.is_dir()
                    and child.name.startswith("V")
                    and child.name[1:].isdigit()
                ),
                key=lambda path: int(path.name[1:]),
            )
        except PermissionError as exc:
            report["error"] = (
                f"Cannot list {mail_base}. Grant Full Disk Access to the host app "
                "launching this process."
            )
            report["permission_error"] = str(exc)
            return None
        except OSError as exc:
            report["error"] = str(exc)
            return None

        report["mail_directory_readable"] = True
        report["mail_versions"] = [path.name for path in version_dirs]
        candidates = [version / "MailData" / "Envelope Index" for version in version_dirs]
        for candidate in reversed(candidates):
            exists, error = _safe_exists(candidate)
            if error is not None:
                report["error"] = error
                return None
            if exists:
                report["active_mail_version"] = candidate.parent.parent.name
                self._record_envelope_index_status(candidate, report)
                return candidate

        scanned = ", ".join(str(candidate) for candidate in candidates) or str(mail_base / "V*")
        report["error"] = f"Apple Mail Envelope Index not found. Checked: {scanned}"
        return None

    def _record_envelope_index_status(
        self, path: Path, report: dict[str, Any]
    ) -> None:
        report["envelope_index_path"] = str(path)
        exists, error = _safe_exists(path)
        report["envelope_index_exists"] = exists
        if error is not None:
            report["error"] = error
            return
        if exists:
            report["envelope_index_readable"] = _safe_readable(path)

    def _diagnose_sqlite(self, path: Path, report: dict[str, Any]) -> None:
        if not report["envelope_index_exists"]:
            return
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                report["sqlite_openable"] = True
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
                tables = {str(row[0]) for row in rows}
        except sqlite3.Error as exc:
            report["error"] = f"Cannot open Envelope Index read-only: {path}: {exc}"
            return
        missing = sorted(_REQUIRED_TABLES - tables)
        report["missing_tables"] = missing
        report["schema_ok"] = not missing

    @staticmethod
    def _diagnose_recommendations(report: dict[str, Any]) -> list[str]:
        recommendations: list[str] = []
        if not report["enabled"]:
            recommendations.append("Set APPLE_MAIL_MCP_LOCAL_DB=1 to enable local DB search.")
        if (
            not report["mail_directory_exists"]
            and report["path_override"] is None
            and report["envelope_index_path"] is None
        ):
            recommendations.append("Open Mail.app and confirm at least one account is configured.")
        if report["mail_directory_exists"] and not report["mail_directory_readable"]:
            recommendations.append(
                "Grant Full Disk Access to the host app launching this MCP server, "
                "then fully quit and reopen it."
            )
        if report["envelope_index_path"] and not report["envelope_index_exists"]:
            recommendations.append("Confirm Mail.app has downloaded local mail and rebuilt its index.")
        if report["envelope_index_exists"] and not report["sqlite_openable"]:
            recommendations.append(
                "Confirm the Envelope Index is readable by this process; Full Disk Access "
                "is the usual fix."
            )
        if report["sqlite_openable"] and not report["schema_ok"]:
            recommendations.append(
                "Apple Mail's Envelope Index schema differed from expectations; "
                "search will fall back to AppleScript."
            )
        if report["available"] and report["enabled"]:
            recommendations.append(
                "Local DB metadata search is available for non-body, non-attachment queries."
            )
        return recommendations

    def matching_mailbox_urls(
        self, account_uuid: str, mailbox: str, *, limit: int = 5
    ) -> list[str]:
        """Return matching mailbox URLs from the Envelope Index for diagnostics."""
        patterns = _mailbox_patterns(account_uuid, mailbox)
        where_clause, params = _mailbox_url_where_clause(patterns)
        sql = f"SELECT url FROM mailboxes WHERE {where_clause}"
        sql += " ORDER BY url LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            return [str(row["url"]) for row in conn.execute(sql, params)]

    def _matching_mailbox_ids(
        self, conn: sqlite3.Connection, account_uuid: str, mailbox: str
    ) -> list[int]:
        patterns = _mailbox_patterns(account_uuid, mailbox)
        where_clause, params = _mailbox_url_where_clause(patterns)
        sql = f"SELECT ROWID FROM mailboxes WHERE {where_clause}"
        return [int(row["ROWID"]) for row in conn.execute(sql, params)]

    def search_messages(self, query: LocalDbSearch) -> list[dict[str, Any]]:
        """Search metadata rows in the Envelope Index.

        Raises ``ValueError`` for invalid date inputs to match the existing
        connector contract. Operational SQLite failures are left as-is so the
        caller can fall back to AppleScript when the local schema differs.
        """
        with self._connect() as conn:
            mailbox_ids = self._matching_mailbox_ids(
                conn, query.account_uuid, query.mailbox
            )
            if not mailbox_ids:
                return []
            return self._search_messages_in_mailboxes(conn, query, mailbox_ids)

    def _search_messages_in_mailboxes(
        self,
        conn: sqlite3.Connection,
        query: LocalDbSearch,
        mailbox_ids: list[int],
    ) -> list[dict[str, Any]]:
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
                m.mailbox AS mailbox_id
            FROM messages m
            LEFT JOIN subjects s ON s.ROWID = m.subject
            LEFT JOIN addresses a ON a.ROWID = m.sender
            LEFT JOIN message_global_data g ON g.message_id = m.message_id
            WHERE m.deleted = 0
        """
        mailbox_placeholders = ", ".join("?" for _ in mailbox_ids)
        sql += f" AND m.mailbox IN ({mailbox_placeholders})"
        params: list[Any] = list(mailbox_ids)

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
