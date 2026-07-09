#!/usr/bin/env python3
"""Benchmark the opt-in local Mail Envelope Index search path.

This script resolves the Mail.app account UUID dynamically, then times the
direct ``LocalDbConnector`` search. Optional flags compare against IMAP or
AppleScript, but those are disabled by default because AppleScript can be very
slow on large mailboxes.

Examples:
    uv run python scripts/benchmark_local_db_search.py --account iCloud
    uv run python scripts/benchmark_local_db_search.py --account Google --subject invoice
    uv run python scripts/benchmark_local_db_search.py --account Google --include-imap
"""

from __future__ import annotations

import argparse
import statistics
import time
from collections.abc import Callable
from typing import Any

from apple_mail_fast_mcp.local_db_connector import LocalDbConnector, LocalDbSearch
from apple_mail_fast_mcp.mail_connector import AppleMailConnector


def _timed(fn: Callable[[], list[dict[str, Any]]], runs: int) -> tuple[float, int, str]:
    times: list[float] = []
    count = -1
    for _ in range(runs):
        started = time.perf_counter()
        try:
            rows = fn()
        except Exception as exc:  # noqa: BLE001 - benchmark reports all failures.
            elapsed = time.perf_counter() - started
            return elapsed, -1, f"{type(exc).__name__}: {exc}"
        times.append(time.perf_counter() - started)
        count = len(rows)
    return statistics.median(times), count, ""


def _resolve_account(connector: AppleMailConnector, account: str | None) -> tuple[str, str]:
    accounts = connector.list_accounts()
    if not accounts:
        raise SystemExit("No Mail.app accounts found.")

    if account is None:
        first = accounts[0]
        return str(first["name"]), str(first["id"])

    for row in accounts:
        name = str(row.get("name") or "")
        row_id = str(row.get("id") or "")
        if account in (name, row_id):
            return name or account, row_id or account

    names = ", ".join(str(row.get("name") or row.get("id")) for row in accounts)
    raise SystemExit(f"Account {account!r} not found. Available accounts: {names}")


def _print_result(label: str, elapsed_s: float, count: int, error: str) -> None:
    if error:
        print(f"{label:<18} ERR after {elapsed_s * 1000:8.1f} ms  {error}")
        return
    print(f"{label:<18} {elapsed_s * 1000:8.1f} ms  rows={count}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--account", help="Mail.app account name or UUID. Defaults to first account."
    )
    parser.add_argument("--mailbox", default="INBOX", help="Mailbox name. Default: INBOX.")
    parser.add_argument(
        "--subject", default="invoice", help="Subject substring for the second query."
    )
    parser.add_argument("--sender", help="Optional sender substring for a third query.")
    parser.add_argument(
        "--limit", type=int, default=50, help="Result limit per query. Default: 50."
    )
    parser.add_argument("--runs", type=int, default=3, help="Local DB runs per query. Default: 3.")
    parser.add_argument(
        "--include-imap",
        action="store_true",
        help="Also time the direct IMAP path if credentials are configured.",
    )
    parser.add_argument(
        "--include-applescript",
        action="store_true",
        help="Also time AppleScript once per query. Can be slow on large mailboxes.",
    )
    args = parser.parse_args()

    connector = AppleMailConnector(timeout=180, local_db_enabled=False)
    account_name, account_uuid = _resolve_account(connector, args.account)
    local_db = LocalDbConnector()

    queries: list[tuple[str, dict[str, Any]]] = [
        ("recent", {"limit": args.limit}),
        ("subject", {"subject_contains": args.subject, "limit": args.limit}),
    ]
    if args.sender:
        queries.append(("sender", {"sender_contains": args.sender, "limit": args.limit}))

    print(f"Account: {account_name} ({account_uuid})")
    print(f"Mailbox: {args.mailbox}")
    print(f"Local DB runs/query: {args.runs}")
    print()

    for query_name, query in queries:
        print(f"[{query_name}] {query}")
        local_search = LocalDbSearch(
            account_uuid=account_uuid,
            mailbox=args.mailbox,
            sender_contains=query.get("sender_contains"),
            subject_contains=query.get("subject_contains"),
            read_status=query.get("read_status"),
            is_flagged=query.get("is_flagged"),
            date_from=query.get("date_from"),
            date_to=query.get("date_to"),
            limit=query.get("limit"),
        )
        _print_result(
            "local-db",
            *_timed(
                lambda search=local_search: local_db.search_messages(search),
                args.runs,
            ),
        )

        if args.include_imap:
            imap_query = dict(query)
            _print_result(
                "imap",
                *_timed(
                    lambda query=imap_query: connector._imap_search(
                        account=account_name, mailbox=args.mailbox, **query
                    ),
                    1,
                ),
            )

        if args.include_applescript:
            applescript_query = dict(query)
            _print_result(
                "applescript",
                *_timed(
                    lambda query=applescript_query: connector._search_messages_applescript(
                        account=account_name,
                        mailbox=args.mailbox,
                        **query,
                    ),
                    1,
                ),
            )
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
