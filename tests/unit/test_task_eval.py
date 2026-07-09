"""The cost eval must distinguish a cheap agent from an expensive one.

Both agents below complete `batch-mark-read`. The old prose-scored eval would
pass them identically. The whole point of this harness is that it does not.

Scripted models keep this free to run in CI; `--model` drives the same loop
against a real one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

EVAL_DIR = Path(__file__).resolve().parents[2] / "evals" / "agent_tool_usability"
sys.path.insert(0, str(EVAL_DIR))

from fake_mail import FakeMailConnector  # noqa: E402
from task_eval import ChatResponse, ToolCall, run_task  # noqa: E402
from tasks import TASKS  # noqa: E402

BATCH_TASK = next(t for t in TASKS if t["id"] == "batch-mark-read")
COUNT_TASK = next(t for t in TASKS if t["id"] == "count-unread")

NEWSLETTER_IDS = ["msg-1", "msg-2", "msg-3"]


def _scripted(*turns: ChatResponse) -> Any:
    """A model that replays a fixed list of responses."""
    state = {"i": 0}

    def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatResponse:
        i = state["i"]
        state["i"] += 1
        return turns[min(i, len(turns) - 1)]

    return chat


class TestHarnessDrivesTheRealServer:
    async def test_tools_come_from_the_live_schema(self) -> None:
        """A misnamed tool must fail, proving we call the real server."""
        chat = _scripted(
            ChatResponse(tool_calls=[ToolCall("no_such_tool", {})]),
            ChatResponse(content="done"),
        )
        result = await run_task(COUNT_TASK, chat)
        # the bad call still cost a round-trip, and did not crash the loop
        assert result.tool_calls == 1
        assert result.call_sequence == ["no_such_tool"]

    async def test_real_validation_applies(self) -> None:
        """search_messages requires an account; the server rejects it for us."""
        chat = _scripted(
            ChatResponse(tool_calls=[ToolCall("search_messages", {"subject_contains": "x"})]),
            ChatResponse(content="0"),
        )
        result = await run_task(COUNT_TASK, chat)
        assert result.tool_calls == 1
        assert result.success is False  # answer "0" != 4


class TestCostIsMeasured:
    async def test_batching_agent_is_within_budget(self) -> None:
        chat = _scripted(
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        "search_messages",
                        {"account": "Work", "mailbox": "INBOX",
                         "sender_contains": "newsletter@example.com"},
                    )
                ],
                input_tokens=5000, output_tokens=60,
            ),
            ChatResponse(
                tool_calls=[
                    ToolCall("update_message",
                             {"message_ids": NEWSLETTER_IDS, "read_status": True})
                ],
                input_tokens=5600, output_tokens=40,
            ),
            ChatResponse(content="Marked 3 messages as read.", input_tokens=5700, output_tokens=12),
        )
        result = await run_task(BATCH_TASK, chat)
        assert result.success is True
        assert result.tool_calls == 2
        assert result.over_budget is False
        assert result.input_tokens == 16_300
        assert result.turns == 3

    async def test_one_at_a_time_agent_succeeds_but_blows_the_budget(self) -> None:
        """Same outcome, twice the round-trips. This is the distinction."""
        chat = _scripted(
            ChatResponse(
                tool_calls=[
                    ToolCall("search_messages",
                             {"account": "Work", "sender_contains": "newsletter@example.com"})
                ]
            ),
            ChatResponse(
                tool_calls=[
                    ToolCall("update_message", {"message_ids": [mid], "read_status": True})
                    for mid in NEWSLETTER_IDS
                ]
            ),
            ChatResponse(content="Marked them read."),
        )
        result = await run_task(BATCH_TASK, chat)
        assert result.success is True          # the mailbox is correct...
        assert result.tool_calls == 4          # ...and it cost double
        assert result.over_budget is True
        assert result.call_sequence == [
            "search_messages", "update_message", "update_message", "update_message",
        ]


class TestScoringGradesTheMailboxNotTheProse:
    async def test_claiming_success_without_doing_it_fails(self) -> None:
        chat = _scripted(ChatResponse(content="Done! I marked them all as read."))
        result = await run_task(BATCH_TASK, chat)
        assert result.success is False
        assert result.tool_calls == 0

    async def test_collateral_damage_fails_the_task(self) -> None:
        """Marking Alice's mail read too is a failure, however 'complete' it looks."""
        chat = _scripted(
            ChatResponse(
                tool_calls=[
                    ToolCall("update_message",
                             {"message_ids": [*NEWSLETTER_IDS, "msg-4"], "read_status": True})
                ]
            ),
            ChatResponse(content="Marked as read."),
        )
        result = await run_task(BATCH_TASK, chat)
        assert result.success is False

    async def test_runaway_agent_hits_the_turn_limit(self) -> None:
        chat = _scripted(
            ChatResponse(tool_calls=[ToolCall("list_accounts", {})])  # forever
        )
        result = await run_task(BATCH_TASK, chat, max_turns=4)
        assert result.hit_turn_limit is True
        assert result.turns == 4


class TestFixture:
    def test_seed_state_is_what_the_tasks_assume(self) -> None:
        mail = FakeMailConnector()
        unread = [m for m in mail.messages if not m["read_status"]]
        assert len(unread) == 4, "count-unread expects exactly 4 unread"
        newsletter = [m for m in mail.messages if "newsletter" in m["sender"]]
        assert len(newsletter) == 3

    def test_fixture_is_isolated_between_tasks(self) -> None:
        a, b = FakeMailConnector(), FakeMailConnector()
        a.update_message(["msg-1"], read_status=True)
        assert b._by_id("msg-1")["read_status"] is False

    @pytest.mark.parametrize("task", TASKS, ids=lambda t: t["id"])
    def test_every_task_declares_a_budget_and_a_reason(self, task: dict) -> None:
        assert task["budget"] >= 1
        assert task["why"].strip()
