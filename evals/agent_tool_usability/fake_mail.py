"""A deterministic in-memory mailbox for cost evals.

The task eval drives the *real* MCP server — real schemas, real validation,
real argument coercion, real response bounding — with only the AppleScript
connector swapped out. Anything less would measure a mock instead of the tool
surface we ship.

State is mutable on purpose: a task like "mark everything from X as read" is
scored by inspecting this fixture afterwards, not by grading the model's prose.
"""

from __future__ import annotations

import copy
from typing import Any

ACCOUNT = "Work"

# A small, adversarial-by-design inbox: two senders, mixed read state, one
# thread. Enough that a batching agent and a one-at-a-time agent produce
# visibly different call counts.
_SEED_MESSAGES: list[dict[str, Any]] = [
    {
        "id": "msg-1", "subject": "Weekly digest", "sender": "newsletter@example.com",
        "date_received": "2026-07-01T09:00:00Z", "read_status": False,
        "is_flagged": False, "mailbox": "INBOX", "content": "Top stories this week.",
    },
    {
        "id": "msg-2", "subject": "Weekly digest", "sender": "newsletter@example.com",
        "date_received": "2026-07-02T09:00:00Z", "read_status": False,
        "is_flagged": False, "mailbox": "INBOX", "content": "More stories.",
    },
    {
        "id": "msg-3", "subject": "Weekly digest", "sender": "newsletter@example.com",
        "date_received": "2026-07-03T09:00:00Z", "read_status": False,
        "is_flagged": False, "mailbox": "INBOX", "content": "Even more stories.",
    },
    {
        "id": "msg-4", "subject": "Q3 planning", "sender": "alice@example.com",
        "date_received": "2026-07-04T14:30:00Z", "read_status": False,
        "is_flagged": False, "mailbox": "INBOX", "content": "Draft plan attached.",
    },
    {
        "id": "msg-5", "subject": "Re: Q3 planning", "sender": "bob@example.com",
        "date_received": "2026-07-05T10:15:00Z", "read_status": True,
        "is_flagged": False, "mailbox": "INBOX", "content": "Looks good to me.",
    },
]

_THREADS = {"msg-4": ["msg-4", "msg-5"], "msg-5": ["msg-4", "msg-5"]}


class FakeMailConnector:
    """Implements the connector surface the evaluated tools actually call."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = copy.deepcopy(_SEED_MESSAGES)
        self.calls: list[str] = []

    # -- helpers ---------------------------------------------------------
    def _by_id(self, message_id: str) -> dict[str, Any] | None:
        return next((m for m in self.messages if m["id"] == message_id), None)

    # -- connector API ---------------------------------------------------
    def list_accounts(self) -> list[dict[str, Any]]:
        self.calls.append("list_accounts")
        return [
            {
                "id": "11111111-2222-3333-4444-555555555555",
                "name": ACCOUNT,
                "email_addresses": ["me@example.com"],
                "account_type": "imap",
                "enabled": True,
            }
        ]

    def list_mailboxes(self, account: str) -> list[dict[str, Any]]:
        self.calls.append("list_mailboxes")
        return [{"name": "INBOX"}, {"name": "Archive"}]

    def search_messages(
        self,
        account: str,
        mailbox: str = "INBOX",
        sender_contains: str | None = None,
        subject_contains: str | None = None,
        read_status: bool | None = None,
        is_flagged: bool | None = None,
        limit: int | None = None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        self.calls.append("search_messages")
        rows = [m for m in self.messages if m["mailbox"] == mailbox]
        if sender_contains:
            rows = [m for m in rows if sender_contains.lower() in m["sender"].lower()]
        if subject_contains:
            rows = [m for m in rows if subject_contains.lower() in m["subject"].lower()]
        if read_status is not None:
            rows = [m for m in rows if m["read_status"] is read_status]
        if is_flagged is not None:
            rows = [m for m in rows if m["is_flagged"] is is_flagged]
        rows.sort(key=lambda m: m["date_received"], reverse=True)
        if limit:
            rows = rows[:limit]
        return [copy.deepcopy(m) for m in rows]

    def get_message(self, message_id: str, **_kwargs: Any) -> dict[str, Any] | None:
        self.calls.append("get_message")
        found = self._by_id(message_id)
        return copy.deepcopy(found) if found else None

    def get_thread(self, message_id: str) -> list[dict[str, Any]]:
        self.calls.append("get_thread")
        ids = _THREADS.get(message_id, [message_id])
        return [copy.deepcopy(m) for m in self.messages if m["id"] in ids]

    def update_message(
        self,
        message_ids: list[str],
        *,
        read_status: bool | None = None,
        flagged: bool | None = None,
        **_kwargs: Any,
    ) -> int:
        self.calls.append("update_message")
        updated = 0
        for mid in message_ids:
            msg = self._by_id(mid)
            if msg is None:
                continue
            if read_status is not None:
                msg["read_status"] = read_status
            if flagged is not None:
                msg["is_flagged"] = flagged
            updated += 1
        return updated

    def diagnose_mail_access(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append("diagnose_mail_access")
        return {"local_db": {"available": False}, "recommendations": []}
