#!/usr/bin/env python3
"""Measure the fixed token cost of the MCP tool surface.

Every request a host makes carries the whole `tools/list` payload — names,
descriptions, and JSON schemas — before the model does any work. That payload
is this project's standing tax on every conversation, and the LLM-efficiency
thesis is not falsifiable without measuring it.

Bytes are the metric of record: exact, deterministic, no tokenizer dependency.
The token column is an estimate (bytes / _BYTES_PER_TOKEN) shown for intuition
only; never gate on it.

Usage:
    scripts/schema_budget.py              # table for both modes
    scripts/schema_budget.py --json       # machine-readable
    scripts/schema_budget.py --check      # fail if over the committed baseline
    scripts/schema_budget.py --update     # re-record the baseline

`--check` is a one-way ratchet, like the complexity allowlist: adding a tool or
growing a description makes the budget visible in the diff rather than letting
it drift. When an increase is intended, run --update and say why in the PR.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE = REPO_ROOT / "evals" / "schema_budget.json"

# Rough chars-per-token for dense JSON + English prose. Used for the display
# column only; the gate compares bytes.
_BYTES_PER_TOKEN = 3.7

# Allowed growth over the recorded baseline before --check fails. Zero: any
# increase should be a deliberate, reviewed --update.
_TOLERANCE_BYTES = 0


def _measure_current_process() -> dict[str, Any]:
    """Measure the tool surface of the server as configured by this process."""
    from fastmcp import Client

    from apple_mail_fast_mcp.server import mcp

    async def _run() -> list[Any]:
        async with Client(mcp) as client:
            return await client.list_tools()

    tools = asyncio.run(_run())
    entries = []
    for tool in tools:
        payload = tool.model_dump(mode="json", exclude_none=True)
        blob = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        schema = payload.get("inputSchema") or {}
        entries.append(
            {
                "name": tool.name,
                "bytes": len(blob),
                "description_bytes": len(tool.description or ""),
                "params": len(schema.get("properties") or {}),
            }
        )
    entries.sort(key=lambda e: -e["bytes"])
    return {
        "tools": len(entries),
        "total_bytes": sum(e["bytes"] for e in entries),
        "entries": entries,
    }


def _measure(read_only: bool) -> dict[str, Any]:
    """Measure a mode in a subprocess.

    `_READ_ONLY` is resolved at import time (the @_tool decorator needs it to
    decide registration), so a single process cannot report both modes.
    """
    env = dict(os.environ)
    env["APPLE_MAIL_MCP_READ_ONLY"] = "1" if read_only else "0"
    with tempfile.TemporaryDirectory(prefix="schema-budget-") as tmp:
        # Keep template/draft state out of the user's real home while measuring.
        env["APPLE_MAIL_MCP_HOME"] = tmp
        out = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--_emit"],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
            check=True,
        )
    return json.loads(out.stdout)


def measure_all() -> dict[str, Any]:
    return {
        "read_only": _measure(read_only=True),
        "full": _measure(read_only=False),
    }


def _tokens(byte_count: int) -> int:
    return round(byte_count / _BYTES_PER_TOKEN)


def _print_table(result: dict[str, Any]) -> None:
    for mode in ("read_only", "full"):
        data = result[mode]
        label = "--read-only" if mode == "read_only" else "full surface"
        print(f"\n{label}: {data['tools']} tools")
        print(f"{'tool':30} {'params':>6} {'desc B':>8} {'schema B':>10}")
        print("-" * 58)
        for e in data["entries"]:
            print(f"{e['name']:30} {e['params']:6} {e['description_bytes']:8} {e['bytes']:10,}")
        total = data["total_bytes"]
        print("-" * 58)
        print(f"{'TOTAL':30} {'':6} {'':8} {total:10,}")
        print(f"{'':30} {'':6} {'':8} ~{_tokens(total):9,} tokens (est.)")


def _load_baseline() -> dict[str, Any]:
    if not BASELINE.exists():
        sys.exit(f"No baseline at {BASELINE}. Run: scripts/schema_budget.py --update")
    return json.loads(BASELINE.read_text())


def _write_baseline(result: dict[str, Any]) -> None:
    payload = {
        "_comment": (
            "Fixed token cost of the tools/list payload. Bytes are exact; a "
            "regression means every request to this server got more expensive. "
            "Regenerate deliberately with scripts/schema_budget.py --update."
        ),
        "read_only_bytes": result["read_only"]["total_bytes"],
        "read_only_tools": result["read_only"]["tools"],
        "full_bytes": result["full"]["total_bytes"],
        "full_tools": result["full"]["tools"],
        "per_tool_bytes": {
            e["name"]: e["bytes"]
            for e in sorted(result["full"]["entries"], key=lambda e: e["name"])
        },
    }
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Baseline written: {BASELINE.relative_to(REPO_ROOT)}")
    print(f"  read-only: {payload['read_only_bytes']:,} B ({payload['read_only_tools']} tools)")
    print(f"  full:      {payload['full_bytes']:,} B ({payload['full_tools']} tools)")


def _check(result: dict[str, Any]) -> int:
    base = _load_baseline()
    failures: list[str] = []
    for mode, key in (("read_only", "read_only_bytes"), ("full", "full_bytes")):
        actual = result[mode]["total_bytes"]
        allowed = base[key] + _TOLERANCE_BYTES
        delta = actual - base[key]
        status = "OK" if actual <= allowed else "OVER"
        sign = "+" if delta > 0 else ""
        print(f"  {status:4} {mode:10} {actual:7,} B  (baseline {base[key]:,} B, {sign}{delta:,})")
        if actual > allowed:
            failures.append(
                f"{mode} schema grew {delta:,} bytes (~{_tokens(delta):,} tokens) per request"
            )
    if failures:
        print("\nSchema budget exceeded:")
        for f in failures:
            print(f"  - {f}")
        print(
            "\nEvery request to this server now costs more. If that is "
            "intended (a new tool, a clearer description), re-record with:\n"
            "    scripts/schema_budget.py --update\n"
            "and justify the increase in the PR."
        )
        return 1
    print("\nSchema budget OK.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--check", action="store_true", help="fail if over baseline")
    parser.add_argument("--update", action="store_true", help="re-record the baseline")
    parser.add_argument("--_emit", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args._emit:
        # Inner subprocess: report only this process's mode.
        json.dump(_measure_current_process(), sys.stdout)
        return 0

    result = measure_all()
    if args.update:
        _write_baseline(result)
        return 0
    if args.check:
        print("Checking MCP tool schema budget...")
        return _check(result)
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
        return 0
    _print_table(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
