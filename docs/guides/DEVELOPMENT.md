# Development Workflow

How to develop on the Apple Mail MCP server. For *installing/configuring* the server as a user, see
the [README](../../README.md). For coding standards and the PR process, see
[CONTRIBUTING.md](../../CONTRIBUTING.md).

For Codex or Claude Code, start with [AGENTS.md](../../AGENTS.md). The
[Codex setup guide](CODEX.md) covers shared instructions and workflow automation.

## Environment

The project uses [`uv`](https://docs.astral.sh/uv/). One command sets up a virtualenv with all dev
dependencies:

```bash
uv sync --dev
```

Run anything in that environment with `uv run …` (e.g. `uv run pytest`, `uv run python -m
apple_mail_fast_mcp.server`). `uv sync` also installs the `apple-mail-pz-mcp` console script into
`.venv/bin/`.

## The canonical check: `make check-all`

`make check-all` is the single pre-push gate. It runs, stopping on the first failure:

| Step | Command | Notes |
|------|---------|-------|
| Lint | `uv run ruff check src/ tests/` | blocking |
| Format | `uv run ruff format --check src/ tests/` | blocking — run `make format` to fix |
| Type check | `uv run mypy src/` | blocking (strict) |
| Unit tests | `uv run pytest -m "not integration and not e2e and not benchmark"` | blocking; use `make coverage` for the separate 90% coverage gate |
| Complexity | `./scripts/check_complexity.sh` | blocking — see below |
| Version sync | `./scripts/check_version_sync.sh` | versions consistent across files |
| Client/server parity | `./scripts/check_client_server_parity.sh` | every public connector method is exposed |
| Doc drift | `./scripts/check_docs.sh` | tool docs, instruction links, and generated eval descriptions |

CI runs the same gates plus coverage (`.github/workflows/test.yml`).
**Integration, e2e, and benchmark tests are *not* in CI** (they need real Mail.app) — run those
locally; see [TESTING.md](TESTING.md).

## Core conventions

- **TDD always** — RED → GREEN → REFACTOR. Write the failing test first.
- **Backend + frontend together** — a feature touches *both* `mail_connector.py` (the connector
  method) and `server.py` (the MCP tool). The parity script (`check_client_server_parity.sh`) enforces
  that every public connector method is exposed as a tool.
- **Structured responses** — every tool returns `{"success": bool, …}`; errors carry `error` +
  `error_type`. No exception reaches the LLM.
- **Sanitize twice** — all user input goes through `sanitize_input()` then
  `escape_applescript_string()` before it touches AppleScript. See
  [SECURITY_CHECKLIST.md](SECURITY_CHECKLIST.md).
- **Touched AppleScript → integration test** — unit tests mock `_run_applescript()` and cannot catch
  AppleScript bugs (see [TESTING.md](TESTING.md)).

## Cyclomatic-complexity gate

`./scripts/check_complexity.sh` fails any function over **CC 20** (via `radon`, run through `uv`).
Genuinely-complex functions can be admitted via the per-function `ALLOWLIST` in that script — a
`(filename, qualified_name) → max_CC` map that ratchets (lowering an entry after a refactor is free;
raising one needs justification). See [COMPLEXITY.md](COMPLEXITY.md).

## Branch & changelog conventions

- Branch names: `{type}/issue-{num}-{description}` — e.g. `fix/issue-99-timeout`,
  `feature/issue-42-thread-support`.
- **`CHANGELOG.md` is only updated on release branches**, never on feature branches (the release
  skill owns it).
- **Bulk reformats are hidden from blame.** [`.git-blame-ignore-revs`](../../.git-blame-ignore-revs)
  lists them; GitHub honors it automatically, and locally it is a one-time opt-in:
  `git config blame.ignoreRevsFile .git-blame-ignore-revs`. That setting makes `git blame` fail
  ("could not open object name list") in any checkout without the file — every tag up to v0.14.2 —
  and no command-line flag overrides it; run `git config --unset blame.ignoreRevsFile` before
  blaming there. After a formatting-only PR merges, add its squash commit's full hash to that file.

## Common make targets

```bash
make test              # unit tests (~1s, mocked AppleScript)
make test-integration  # real Mail.app (needs a test account; MAIL_TEST_MODE=true)
make test-e2e          # MCP tool dispatch tests
make lint / format / typecheck
make coverage
make benchmark         # perf benchmarks (opt-in; needs real Mail) — see BENCHMARKING.md
make eval-descriptions # regenerate the blind-eval tool descriptions from the live server
```

## See also

- [ARCHITECTURE.md](../reference/ARCHITECTURE.md) — dispatch model, dual-emit IDs, drafts lifecycle, thread tiers
- [APPLESCRIPT_GOTCHAS.md](../reference/APPLESCRIPT_GOTCHAS.md) — JSON-via-ASObjC, escaping, known limits
- [TESTING.md](TESTING.md) — test categories, the manual-e2e policy, benchmarks, blind evals
- [TOOLS.md](../reference/TOOLS.md) — full tool reference
