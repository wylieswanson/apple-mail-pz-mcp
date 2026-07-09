# Apple Mail PingZero MCP Server

[![Tests](https://github.com/wylieswanson/apple-mail-pz-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/wylieswanson/apple-mail-pz-mcp/actions/workflows/test.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

An MCP server that provides programmatic access to Apple Mail, enabling AI assistants like Claude to read, send, search, and manage emails on macOS.

> **Built on [`apple-mail-fast-mcp`](https://github.com/s-morgan-jeffries/apple-mail-fast-mcp) by Morgan Jeffries**, MIT-licensed, whose architecture this project inherits wholesale. See [Credits and origins](#credits-and-origins).

> ⚠️ **Pre-1.0 — expect breaking changes.** The MCP tool surface (tool names, parameters, return shapes) is still evolving as the project matures. Pin to a specific version (for example, `apple-mail-pz-mcp==0.10.2`) and review the [CHANGELOG](CHANGELOG.md) before upgrading.

## Tools (26)

Grouped by lifecycle (12 read-only, 14 mutating):

- **Discovery** — `diagnose_mail_access`, `list_accounts`, `list_mailboxes`, `list_rules`, `list_templates`: inspect access/search health and enumerate what's configured (no external cache — call per account).
- **Read** — `search_messages`, `get_messages`, `get_thread`, `get_statistics`, `get_attachment_content`, `get_template`, `render_template`: read messages/threads, aggregate inbox stats, pull an attachment's content inline, and render templates.
- **Message actions** — `update_message` (read/flag/move in one pass), `delete_messages` (→ Trash), `save_attachments` (to disk, byte-capped).
- **Drafts** — `create_draft` (new / reply / forward, optionally `send_now`), `update_draft`, `delete_draft`.
- **Mailbox CRUD** — `create_mailbox`, `update_mailbox` (rename or move), `delete_mailbox`.
- **Rules** — `create_rule`, `update_rule`, `delete_rule`.
- **Templates (write)** — `save_template`, `delete_template`.

Destructive operations (`delete_*`, `create_rule` with move/forward/delete actions, `create_draft` with `send_now=true`) prompt for confirmation via MCP elicitation. See [docs/reference/TOOLS.md](docs/reference/TOOLS.md) for full parameters and return shapes.

## Prerequisites

- macOS 10.15 (Catalina) or later
- Python 3.10 or later
- Apple Mail configured with at least one account
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Installation

> **All 26 tools install by default.** Destructive tools confirm each action through
> [MCP elicitation](#read-only-mode-and-the-elicitation-caveat) and *fail closed* if the
> host cannot prompt — so on Claude Desktop and Cowork, which don't implement elicitation
> yet, those tools return a confirmation error instead of acting. If you'd rather not see
> them at all, run with `--read-only` (or tick **Read-only mode** when installing the
> `.mcpb`).

### Claude Desktop — install from file (`.mcpb`)

The lowest-friction path for Claude Desktop: grab the `apple-mail-pz-mcp-<version>.mcpb`
bundle from the [Releases](https://github.com/wylieswanson/apple-mail-pz-mcp/releases)
page and open it (or drag it into **Settings → Extensions**). Claude Desktop manages Python
and dependencies for you via `uv` — no manual venv, no config JSON to hand-edit. macOS only.

To build the bundle yourself: `./scripts/build-mcpb.sh` → `dist/apple-mail-pz-mcp-<version>.mcpb`
(requires Node for the `mcpb` packer).

### Claude Code — install as a plugin

One command in Claude Code, no config JSON:

```
/plugin marketplace add wylieswanson/apple-mail-pz-mcp
/plugin install apple-mail-pz@apple-mail-pz-mcp
```

Claude Code launches the server via `uv run` from the plugin directory (resolves dependencies
from the bundled `pyproject.toml`/`uv.lock` — no PyPI needed), so you only need `uv` installed.
macOS only. See [`docs/reference/TOOLS.md`](docs/reference/TOOLS.md) for IMAP setup and the
read/write split.

### Codex CLI

Add to `~/.codex/config.toml` (or run `codex mcp add`). Point at the installed console
script rather than `uv run --directory …`: Codex's `startup_timeout_sec` defaults to **10
seconds**, and a cold `uv` dependency resolve will exceed it.

```toml
[mcp_servers.apple-mail]
command = "apple-mail-pz-mcp"          # or the absolute path from `which apple-mail-pz-mcp`
env = { APPLE_MAIL_MCP_LOCAL_DB = "1" }
startup_timeout_sec = 30

# Read-only if you want it — then `default_tools_approval_mode = "auto"` is safe,
# because every exposed tool is read-only.
# args = ["--read-only"]
# default_tools_approval_mode = "auto"
```

Two Codex-specific notes. Elicitation landed around **v0.119**; on older builds the write
tools fail closed exactly as they do on Cowork. And Codex's per-tool `approval_mode` means
you do **not** need the two-connector split below — set `approval_mode` per tool instead.

### pip / uvx (any MCP client)

Published on [PyPI](https://pypi.org/project/apple-mail-pz-mcp/):

```bash
uvx apple-mail-pz-mcp          # zero-install, run on demand
pip install apple-mail-pz-mcp  # or install the console script
```

Then point your MCP client at it — the config is a one-liner (no absolute paths):

```json
{
  "mcpServers": {
    "apple-mail": { "command": "uvx", "args": ["apple-mail-pz-mcp"] }
  }
}
```

### From source (development)

```bash
git clone https://github.com/wylieswanson/apple-mail-pz-mcp.git
cd apple-mail-pz-mcp
uv sync --dev
```

## Configuration

> Skip this section if you installed the `.mcpb` bundle — it wires up Claude Desktop for you.
> The manual config below is for source installs.

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`). `uv sync` installs a console script at `.venv/bin/apple-mail-pz-mcp`; point Claude Desktop at its **absolute path** — it's the most reliable form under Claude Desktop's restricted spawn environment (no reliance on `uv` being on `PATH`):

```json
{
  "mcpServers": {
    "apple-mail": {
      "command": "/path/to/apple-mail-pz-mcp/.venv/bin/apple-mail-pz-mcp"
    }
  }
}
```

(Equivalent alternative if you prefer driving it through uv: `"command": "uv", "args": ["--directory", "/path/to/apple-mail-pz-mcp", "run", "apple-mail-pz-mcp"]`.)

### Read-only mode and the elicitation caveat

Every destructive tool confirms the operation through **MCP elicitation** and *fails closed*: if the client can't prompt the user, the tool returns `error_type: "confirmation_required"` rather than proceeding. That is a deliberate security property — an earlier version silently proceeded, which was a real bypass of the confirmation gate.

The consequence is that the write tools only *function* on a host that implements elicitation:

| Host | Elicitation | Write tools |
|---|---|---|
| Claude Code | Yes | Work |
| Codex CLI ≥ ~v0.119 | Yes | Work |
| Codex CLI, older | No | Fail closed |
| Claude Desktop / Cowork | No ([`claude-ai-mcp#153`](https://github.com/anthropics/claude-ai-mcp/issues/153)) | Fail closed |

They are still installed by default, because that's a host limitation rather than a property of this server, and hosts are fixing it. If you'd rather the model not see tools it cannot use on your host, start the server in read-only mode — it exposes the 12 read tools and skips registering the 14 mutating ones:

```bash
apple-mail-pz-mcp --read-only            # flag
APPLE_MAIL_MCP_READ_ONLY=1 apple-mail-pz-mcp   # or env var, for hosts that only pass env
```

The `.mcpb` bundle surfaces this as a **Read-only mode** checkbox at install time. Whatever you do, don't work around the gate by making confirmation pass silently.

### Optional: split read / write servers

Claude Desktop prompts per-tool for permission. If you want to **batch-approve the 12 read tools** (diagnose / list / search / get) and still gate the 14 mutating tools per call, run the connector twice — once with `--read-only`, once without — under two separate `mcpServers` entries:

```json
{
  "mcpServers": {
    "apple-mail-read": {
      "command": "/path/to/apple-mail-pz-mcp/.venv/bin/apple-mail-pz-mcp",
      "args": ["--read-only"]
    },
    "apple-mail-write": {
      "command": "/path/to/apple-mail-pz-mcp/.venv/bin/apple-mail-pz-mcp"
    }
  }
}
```

The `--read-only` server exposes only the 12 read tools, so Claude Desktop's per-server permission UI naturally groups them. The full server still gates writes individually. Trade-off: 2× connector processes. See [`docs/reference/TOOLS.md`](docs/reference/TOOLS.md) for the per-tool classification and a note on MCP annotation hints (`readOnlyHint` / `destructiveHint` / `idempotentHint`) which forward-compatible hosts may use to provide the same UX without the split.

## What version am I running?

Three ways to ask, depending on who's asking.

From a shell — reports the release, the commit it was built from, when that commit was made, and (for an installed build) when the wheel was built:

```console
$ apple-mail-pz-mcp --version
apple-mail-pz-mcp 0.10.2 | commit 0ef7dd33b850 | committed 2026-07-09T14:40:02-07:00 | built 2026-07-09T22:29:46+00:00
```

From an MCP host — the server reports its version in `serverInfo` at initialize, so hosts that show connector versions will display it without any tool call.

From an agent mid-conversation — `diagnose_mail_access` returns a `server` block:

```json
{
  "version": "0.10.2",
  "commit": "0ef7dd33b850",
  "commit_date": "2026-07-09T14:40:02-07:00",
  "built_at": "2026-07-09T22:29:46+00:00",
  "dirty": false,
  "source": "build",
  "read_only": false
}
```

`source` tells you how much to trust the commit: `build` means it was frozen into the wheel at build time, `git` means it was read live from a source checkout (and `dirty` says whether that tree had uncommitted changes), and `unknown` means the package was installed from an sdist built outside a repo — in which case the commit is genuinely unrecoverable and the server says so rather than guessing.

## Permissions

On first run, macOS will prompt for Automation access. Grant permission in:
**System Settings > Privacy & Security > Automation > Terminal (or your IDE)**

## Experimental: local Mail index accelerator

This fork can optionally accelerate `search_messages` metadata queries by
reading Apple Mail's local Envelope Index SQLite database in read-only mode:

```json
{
  "mcpServers": {
    "apple-mail-read": {
      "command": "/path/to/apple-mail-pz-mcp/.venv/bin/apple-mail-pz-mcp",
      "args": ["--read-only"],
      "env": { "APPLE_MAIL_MCP_LOCAL_DB": "1" }
    }
  }
}
```

What it covers today: account/mailbox-scoped `search_messages` filters for
sender, subject, read/unread, flagged, dates, `received_within_hours`, and
limit. Supported metadata-only queries prefer the local DB first, then fall
back to IMAP or AppleScript. Body/text search, `has_attachment`, and
attachment metadata still use IMAP when configured or AppleScript fallback.

This path requires Full Disk Access for the host app because it reads
`~/Library/Mail/V*/MailData/Envelope Index`. If the local database is missing,
unreadable, or has an unexpected schema, the connector falls back to
AppleScript. The database is opened with `mode=ro`; the connector never writes
to Mail's store. When `APPLE_MAIL_MCP_LOCAL_DB=1` is set, the server emits a
one-time startup warning if the Envelope Index is unavailable so you can catch
Full Disk Access problems without waiting for a slow fallback query.

Run `diagnose_mail_access(account="iCloud", mailbox="INBOX")` from your MCP
client to see whether the running process can read Mail's store and which
search backends are configured. `search_messages` responses also include a
`search_backend` field (`imap`, `local-db`, `applescript`, or `source`) and
`search_elapsed_ms`. If diagnostics report `mail_directory_readable: false`,
grant Full Disk Access to the exact app that launches the server (Claude
Desktop, iTerm, Terminal, or your IDE), then fully quit and reopen that app.

## Optional: faster search via IMAP

`search_messages` works out of the box via AppleScript. For large mailboxes (thousands of messages), AppleScript's `whose` clause can take 1–5 seconds per query. If you want faster server-side search, you can enable IMAP delegation per account by adding a Keychain entry.

**How it works.** If credentials exist for an account, the server uses IMAP (fast, server-side SEARCH). Otherwise — or on any IMAP failure (offline, wrong password, timeout) — it silently falls back to AppleScript. You never lose functionality; you only gain speed when IMAP is configured and reachable. The normal opt-in is a Keychain entry (below); an environment-variable fallback ([further down](#environment-variable-fallback-uvx--headless--ci)) covers contexts where the Keychain isn't usable.

**One-time setup per account — the `setup-imap` subcommand walks you through it:**

```bash
apple-mail-pz-mcp setup-imap --account iCloud
```

Substitute the Mail.app account name exactly — whatever it's labeled in Mail.app (e.g. `iCloud`, `Gmail`, `"Yahoo!"`). The guided flow (#384):

- **detects your provider** from the account's IMAP host and points you at the right app-password page — **iCloud** ([account.apple.com](https://account.apple.com/account/manage) → App-Specific Passwords), **Gmail** ([myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)), **Yahoo**, **Outlook**, **Fastmail** (generic guidance for anything else) — with the provider's 2FA steps, and **offers to open the page** in your browser;
- explains that this is a **scoped, revocable app-specific password** — limited to that one account — unlike granting full disk access;
- looks up the account's primary email from Mail.app (override with `--email`, which is **persisted** so runtime uses the same login — see the iCloud quirk below);
- prompts via `getpass` so the password never lands in shell history;
- writes to Keychain at `apple-mail-fast-mcp.imap.<account>` (idempotent — re-running with a new password updates the entry; pre-rename `apple-mail-mcp.imap.` entries still resolve via a read-through fallback removed at 1.0.0);
- opens an IMAP connection and runs a real LOGIN to confirm the password works. On rejection it **rolls back the Keychain entry and lets you paste again** (up to 3 tries) so a bad password never leaves a broken item behind.

If you see a one-time "security wants to use the 'login' keychain" prompt on the next IMAP-backed call, click **Always Allow**.

To remove the entry later: `apple-mail-pz-mcp setup-imap --account iCloud --uninstall`.

### Environment-variable fallback (uvx / headless / CI)

Some contexts have no usable Keychain: `uvx` runs (ephemeral binary paths break the Keychain ACL, causing re-prompts or failures), Docker / CI (no Keychain at all), and background services (the ACL prompt blocks forever with no UI attached). For those, you can supply the IMAP password via an environment variable instead:

```
APPLE_MAIL_MCP_IMAP_PASSWORD_<SUFFIX>
```

`<SUFFIX>` is the Mail.app account name **uppercased**, with each run of non-alphanumeric characters collapsed to a single underscore and leading/trailing underscores trimmed:

| Account name | Environment variable |
|---|---|
| `iCloud` | `APPLE_MAIL_MCP_IMAP_PASSWORD_ICLOUD` |
| `Gmail` | `APPLE_MAIL_MCP_IMAP_PASSWORD_GMAIL` |
| `Yahoo!` | `APPLE_MAIL_MCP_IMAP_PASSWORD_YAHOO` |
| `My Gmail` | `APPLE_MAIL_MCP_IMAP_PASSWORD_MY_GMAIL` |

When set to a non-empty value, the env var is used **in preference to** any Keychain entry for that account (it's checked first, with no `security` shell-out). An empty or whitespace-only value is ignored and the Keychain path is used. The lookup composes with the name↔UUID fallback, so an env var keyed on the account name is still found when a caller passes the account's UUID.

> ⚠️ **Security tradeoff.** Environment variables are far less private than the Keychain — they're visible via `ps -E`, `launchctl getenv`, `/proc`-style introspection, and process crash dumps, and they're easy to leak into logs or shell history. **Use this only when the Keychain genuinely isn't an option** (uvx, Docker, CI, headless). For Claude Desktop and standard local installs, stick with `setup-imap` + Keychain.
>
> Caveat: the name→suffix mapping isn't reversible — `Yahoo!` and `Yahoo` both map to `YAHOO`, and an account name with no ASCII letters/digits has no env-var form (use the Keychain for those).

**Verifying the setup.** The `setup-imap` command does this for you. If you want to spot-check post-hoc:
```bash
uv run python -c "from apple_mail_fast_mcp.mail_connector import AppleMailConnector; \
    print(AppleMailConnector().search_messages(account='<ACCOUNT_NAME>', limit=1))"
```
If IMAP is working, the call returns in ~1 second. If it logs a WARNING about falling back (visible with `--log-level=DEBUG`), check that the account name matches Mail.app's account name exactly and that the email in your Keychain entry matches what `email addresses of account` returns.

**Known provider quirks.**

- **iCloud:** the IMAP server accepts `@icloud.com` / `@me.com` aliases as LOGIN username, not the Apple ID email. The server (and `setup-imap`) reads `email addresses of account` from Mail.app for that reason. If your iCloud Apple ID is a *third-party* address (e.g. a `@gmail.com` Apple ID) **and** Mail.app reports no `@icloud.com` address for the account, auto-detection can't find the right login — `setup-imap` will fail with a hint to re-run with `--email <your @icloud.com/@me.com address>`. That `--email` value is **persisted** (in `~/.apple_mail_mcp/imap_login_overrides.json`) so runtime resolution uses the same login (#341). It's a general override — use it for any account whose auto-detected IMAP login is wrong.
- **Yahoo:** app passwords have been progressively deprecated; the option may not be available for all accounts. If Yahoo's account-security page doesn't show the option, IMAP setup isn't possible for that account and AppleScript is the only path.
- **Gmail:** requires 2-Step Verification enabled. If your Google Workspace admin has disabled app passwords at the tenant level, IMAP setup isn't possible for that account.
- **Gmail thread retrieval — All Mail visibility tradeoff.** `find_thread_members` (used internally by thread-aware queries) is fastest when `[Gmail]/All Mail` is exposed over IMAP — that path is ~5 round-trips, mailbox-count-independent. Many users hide All Mail (Gmail Settings → Forwarding and POP/IMAP → Folder size limits → "Do not show in IMAP") because it duplicates every message. When hidden, the connector falls back to a per-mailbox X-GM-THRID iteration (still ~6× faster than the universal BFS, but proportional to your label count — ~25s on a 92-label account). Expose All Mail if you want the headline speed; keep it hidden if you prefer the cleaner IMAP folder list.

**Write operations** (`create_draft`, `update_draft`, including the `send_now=true` send path) always use AppleScript regardless of IMAP configuration — these need Mail.app's compose UI.

## Development

```bash
# Setup
uv sync --dev

# Common commands
make test              # Run unit tests
make lint              # Lint with ruff
make typecheck         # Type check with mypy
make check-all         # All checks (lint, typecheck, test, complexity, version-sync, parity)
make coverage          # Coverage report
make test-integration  # Integration tests (requires Mail.app)

# Validation scripts
./scripts/check_version_sync.sh          # Version consistency
./scripts/check_client_server_parity.sh  # Connector-server alignment
./scripts/check_complexity.sh            # Cyclomatic complexity
./scripts/check_applescript_safety.sh    # AppleScript safety audit
```

### Branch Convention

`{type}/issue-{num}-{description}` — e.g., `feature/issue-42-thread-support`

## Architecture

```
server.py (FastMCP tools — thin orchestration, validation, elicitation gates)
  -> mail_connector.py (dispatch + domain logic)
     -> AppleScript path:  subprocess.run(["osascript", ...]) -> Apple Mail.app   (universal baseline)
     -> IMAP fast path:    imap_connector.py -> the account's IMAP server          (when hinted + Keychain creds)
```

**Dispatch model.** AppleScript is the always-available baseline. When a read/mutation call supplies
an `account` (and, where relevant, `mailbox`) hint **and** the account has Keychain IMAP credentials,
the connector takes a server-side IMAP fast path; on any IMAP failure it falls back to AppleScript, so
you never lose functionality — you only gain speed. See
[docs/reference/ARCHITECTURE.md](docs/reference/ARCHITECTURE.md) for the full dispatch model, the
dual-emit message-ID scheme, the drafts lifecycle, and the IMAP thread tiers.

- **server.py** — MCP tool registration, input validation, confirmation (elicitation) gates, response formatting
- **mail_connector.py** — AppleScript generation/execution + IMAP-fast-path dispatch
- **imap_connector.py** — IMAP client + connection pool (search, fetch, bulk-mutation fast paths)
- **security.py** — Input sanitization, audit logging, confirmation flows
- **utils.py** — Pure functions: escaping, parsing, validation
- **exceptions.py** — Typed exception hierarchy

## Security

- Local execution only (no cloud processing)
- Uses existing Mail.app authentication; IMAP app-passwords (opt-in) live in the macOS Keychain, never in the repo or config
- All inputs sanitized and AppleScript-escaped (defense against AppleScript injection)
- Destructive operations require user confirmation via MCP elicitation; rate limits + audit logging on top
- `save_attachments` is byte-capped (per-attachment + aggregate) against disk-fill DoS

Docs:
- [SECURITY.md](SECURITY.md) — vulnerability-reporting policy
- [docs/SECURITY.md](docs/SECURITY.md) — user-facing security posture & privacy
- [docs/guides/THREAT_MODEL.md](docs/guides/THREAT_MODEL.md) — STRIDE trust-boundary analysis
- [docs/guides/SECURITY_CHECKLIST.md](docs/guides/SECURITY_CHECKLIST.md) — per-feature contributor checklist

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development workflow, coding standards, and PR process.

## Credits and origins

This project is a continuation of **[`apple-mail-fast-mcp`](https://github.com/s-morgan-jeffries/apple-mail-fast-mcp)**, created by **Morgan Jeffries** and released under the MIT License. Effectively all of the engineering that makes this server work is theirs:

- the AppleScript connector and its hard-won [gotchas](docs/reference/APPLESCRIPT_GOTCHAS.md) — JSON emission via ASObjC, the `|name|:` record-key quirk, `whose`-clause search;
- the IMAP fast path, connection pooling, and Gmail `X-GM-THRID` thread strategies;
- the security model — double sanitization, path-traversal-safe name validation, rate limiting, audit logging, and the fail-closed confirmation gate on every destructive tool;
- the test discipline: 1544 unit, 35 e2e, and 62 integration tests, and the validation scripts that keep the docs honest.

That project itself succeeded `apple-mail-mcp`; the lineage is preserved in the [CHANGELOG](CHANGELOG.md) and in the Keychain read-through fallbacks, which still resolve credentials written under both earlier names.

**What's different here.** `apple-mail-pz-mcp` (PingZero) evolves the tool surface for **LLM efficiency** rather than for human API aesthetics. The working thesis is that an agent's cost is dominated by round-trips and by tokens spent re-reading things it already fetched, so the areas of divergence are:

- **Fewer round-trips per task** — richer single-call tools over chatty primitives, so a mailbox triage is one call, not twelve.
- **Tighter payloads** — bounded bodies, bounded attachments, and response shapes that omit what the model won't use.
- **Predictable degradation across MCP hosts** — the tool surface must behave the same whether the host supports elicitation and sends real JSON types (Claude Code) or supports neither (Cowork, older Codex). See the client-compatibility matrix in [AGENTS.md](AGENTS.md#mcp-client-compatibility).
- **Read-only by default** — the shipped plugin and `.mcpb` bundle launch with `--read-only`, so the 14 mutating tools are opt-in rather than opt-out.

This is an independent fork, not a staging area for upstream. Nothing here presumes upstream wants any of it back — though anyone, upstream included, is free to take any of it under the MIT License. If you build on this in turn, the same courtesy applies: keep Morgan's copyright notice, because most of this code is theirs.

## License

[MIT](LICENSE) — Copyright (c) 2025 Morgan. The original copyright notice is retained unmodified; this project adds no separate copyright claim.
