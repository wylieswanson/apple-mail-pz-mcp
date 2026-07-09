#!/usr/bin/env python3
"""Multi-turn task-completion eval: how many round-trips does a task cost?

`run_eval.py` measures whether a model picks the right tool from a description,
in one turn, scored against its prose. That is tool *usability*. It cannot see
the thing this project claims to optimize — an agent's cost is dominated by
round-trips and by re-reading things it already fetched.

This harness runs the model to completion against the real MCP server (real
schemas, real validation, real coercion) with the AppleScript connector swapped
for an in-memory fixture, then grades the mailbox. It records:

    tool_calls    round-trips to the server — the metric that matters
    turns         model invocations
    input_tokens  the schema tax, paid once per turn, plus accumulated results
    output_tokens
    success       did the task actually get done
    over_budget   did it cost more calls than a competent agent needs

The model is injected as a `chat` callable, so the loop is testable with a
scripted fake and costs nothing to exercise in CI. Passing --model runs it for
real against OpenRouter.

    python evals/agent_tool_usability/task_eval.py --model anthropic/claude-sonnet-5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fake_mail import ACCOUNT, FakeMailConnector  # noqa: E402
from tasks import TASKS  # noqa: E402

MAX_TURNS = 12

SYSTEM_PROMPT = (
    "You manage the user's Apple Mail via the provided tools. "
    f"The user's only account is named {ACCOUNT!r}. "
    "Use as few tool calls as you can: prefer server-side filters over "
    "fetching and filtering yourself, and prefer one batched call over many "
    "single-item calls. When you have the answer, reply in plain text without "
    "calling further tools."
)


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResponse:
    """The subset of an OpenAI-shaped completion this loop needs."""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class Chat(Protocol):
    def __call__(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ChatResponse: ...


@dataclass
class TaskResult:
    id: str
    name: str
    success: bool
    tool_calls: int
    turns: int
    budget: int
    over_budget: bool
    input_tokens: int
    output_tokens: int
    call_sequence: list[str]
    answer: str
    hit_turn_limit: bool


async def _openai_tools(mcp: Any) -> list[dict[str, Any]]:
    """Render the live MCP tool schemas into OpenAI function-tool format."""
    from fastmcp import Client

    async with Client(mcp) as client:
        tools = await client.list_tools()
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema,
            },
        }
        for t in tools
    ]


async def run_task(
    task: dict[str, Any],
    chat: Chat,
    *,
    max_turns: int = MAX_TURNS,
) -> TaskResult:
    """Drive one task to completion against the real server + fake mailbox."""
    from fastmcp import Client

    from apple_mail_fast_mcp import server as server_module

    fake = FakeMailConnector()
    original = server_module.mail
    server_module.mail = fake  # type: ignore[assignment]
    try:
        tools = await _openai_tools(server_module.mcp)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task["prompt"]},
        ]
        tool_calls = 0
        turns = 0
        in_tok = out_tok = 0
        sequence: list[str] = []
        answer = ""
        hit_limit = False

        async with Client(server_module.mcp) as client:
            while True:
                if turns >= max_turns:
                    hit_limit = True
                    break
                response = chat(messages, tools)
                turns += 1
                in_tok += response.input_tokens
                out_tok += response.output_tokens

                if not response.tool_calls:
                    answer = response.content or ""
                    break

                messages.append(
                    {
                        "role": "assistant",
                        "content": response.content,
                        "tool_calls": [
                            {
                                "id": f"call_{turns}_{i}",
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": json.dumps(tc.arguments),
                                },
                            }
                            for i, tc in enumerate(response.tool_calls)
                        ],
                    }
                )
                for i, tc in enumerate(response.tool_calls):
                    tool_calls += 1
                    sequence.append(tc.name)
                    try:
                        result = await client.call_tool(tc.name, tc.arguments)
                        payload = json.dumps(result.data, default=str)
                    except Exception as exc:  # a rejected call still costs a round-trip
                        payload = json.dumps({"error": str(exc)})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": f"call_{turns}_{i}",
                            "content": payload,
                        }
                    )

        success = bool(task["succeeds"](fake, answer))
        return TaskResult(
            id=task["id"],
            name=task["name"],
            success=success,
            tool_calls=tool_calls,
            turns=turns,
            budget=task["budget"],
            over_budget=tool_calls > task["budget"],
            input_tokens=in_tok,
            output_tokens=out_tok,
            call_sequence=sequence,
            answer=answer,
            hit_turn_limit=hit_limit,
        )
    finally:
        server_module.mail = original  # type: ignore[assignment]


async def run_all(chat: Chat, tasks: list[dict[str, Any]] | None = None) -> list[TaskResult]:
    return [await run_task(t, chat) for t in (tasks or TASKS)]


def _openrouter_chat(model: str) -> Chat:
    """Real model, via OpenRouter. Imported lazily; `openai` is not a runtime dep."""
    from openai import OpenAI
    from run_eval import get_api_key

    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=get_api_key())

    def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatResponse:
        completion = client.chat.completions.create(
            model=model, messages=messages, tools=tools, temperature=0, max_tokens=2048
        )
        choice = completion.choices[0].message
        usage = completion.usage
        calls = [
            ToolCall(name=tc.function.name, arguments=json.loads(tc.function.arguments or "{}"))
            for tc in (choice.tool_calls or [])
        ]
        return ChatResponse(
            content=choice.content,
            tool_calls=calls,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )

    return chat


def _report(results: list[TaskResult]) -> int:
    print(f"\n{'task':24} {'ok':>3} {'calls':>6} {'budget':>7} {'in tok':>8} {'out tok':>8}")
    print("-" * 62)
    for r in results:
        flag = "yes" if r.success else "NO"
        over = "!" if r.over_budget else " "
        print(
            f"{r.id:24} {flag:>3} {r.tool_calls:5}{over} {r.budget:7} "
            f"{r.input_tokens:8,} {r.output_tokens:8,}"
        )
    print("-" * 62)
    passed = sum(r.success for r in results)
    over = sum(r.over_budget for r in results)
    total_calls = sum(r.tool_calls for r in results)
    budget = sum(r.budget for r in results)
    print(f"{passed}/{len(results)} succeeded | {total_calls} calls vs {budget} budgeted", end="")
    if over:
        print(f" | {over} over budget (marked !)")
    else:
        print()
    return 0 if passed == len(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True, help="OpenRouter model slug")
    parser.add_argument("--json", type=Path, help="write raw results here")
    args = parser.parse_args()

    results = asyncio.run(run_all(_openrouter_chat(args.model)))
    if args.json:
        args.json.write_text(json.dumps([asdict(r) for r in results], indent=2) + "\n")
        print(f"wrote {args.json}")
    return _report(results)


if __name__ == "__main__":
    raise SystemExit(main())
