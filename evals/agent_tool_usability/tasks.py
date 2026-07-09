"""Task-completion scenarios for the cost eval.

These differ from `scenarios.py` in what they ask and how they score.
`scenarios.py` asks "would the model pick the right tool?" and grades the
model's prose after one turn. These run the model to completion against a real
MCP server and grade the *mailbox*.

Each task carries a `budget`: the number of tool calls a competent agent needs.
It is the LLM-efficiency thesis written as a number. A task that succeeds in
2 calls and a task that succeeds in 7 both "pass" the old eval; only one of
them is cheap.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fake_mail import FakeMailConnector


def _all_newsletter_read(mail: FakeMailConnector) -> bool:
    return all(
        m["read_status"]
        for m in mail.messages
        if "newsletter@example.com" in m["sender"]
    )


def _nothing_else_changed(mail: FakeMailConnector) -> bool:
    """Alice's and Bob's messages must keep their original read state."""
    others = {m["id"]: m["read_status"] for m in mail.messages if "newsletter" not in m["sender"]}
    return others == {"msg-4": False, "msg-5": True}


def _mentions(answer: str, *needles: str) -> bool:
    lowered = answer.lower()
    return all(n.lower() in lowered for n in needles)


TASKS: list[dict[str, Any]] = [
    {
        "id": "count-unread",
        "name": "Count unread in inbox",
        "prompt": "How many unread messages are in my Work inbox?",
        # list_accounts is optional; a good agent may go straight to search.
        "budget": 2,
        "succeeds": lambda mail, answer: _mentions(answer, "4"),
        "why": (
            "Four unread messages seeded. Tests whether the model filters "
            "server-side with read_status=False instead of fetching everything "
            "and counting in-context."
        ),
    },
    {
        "id": "batch-mark-read",
        "name": "Mark a sender's mail read",
        "prompt": (
            "Mark every message from newsletter@example.com in my Work inbox "
            "as read. Leave everything else alone."
        ),
        # search_messages once, update_message once with all three ids.
        "budget": 2,
        "succeeds": lambda mail, answer: _all_newsletter_read(mail)
        and _nothing_else_changed(mail),
        "why": (
            "THE discriminating task. update_message takes a list of ids, so a "
            "batching agent finishes in 2 calls. An agent that calls "
            "update_message once per message takes 4 and scales with inbox size. "
            "Both 'succeed'; only one is affordable."
        ),
    },
    {
        "id": "newest-from-sender",
        "name": "Newest message from a sender",
        "prompt": "What is the subject of the most recent message from Alice in my Work inbox?",
        "budget": 2,
        "succeeds": lambda mail, answer: _mentions(answer, "Q3 planning"),
        "why": (
            "search_messages sorts by date descending and takes a limit, so the "
            "answer is one filtered call. Tests whether the model uses "
            "sender_contains + limit rather than listing the inbox."
        ),
    },
]


TaskSucceeds = Callable[[FakeMailConnector, str], bool]
