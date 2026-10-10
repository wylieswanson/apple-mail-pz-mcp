# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false
#
# imapclient ships without a py.typed marker, so Pyright/Pylance can't verify
# argument types against its public API. Mypy is configured to ignore missing
# imports for the imapclient package via [[tool.mypy.overrides]] in
# pyproject.toml; Pyright respects file-level pragmas instead. The three
# suppressed categories cover the false positives that arise when calling
# search() / fetch() with list-shaped criteria and reading Envelope/BodyData
# fields. Suppression is scoped to this file so unrelated type bugs elsewhere
# in the codebase still surface.
"""IMAPClient wrapper for read operations.

Stateless, per-call connection lifecycle. This module is deliberately
unaware of Mail.app, Keychain, and the MCP server. It takes fully-
resolved credentials and talks IMAP. Callers (tests here; the
delegation layer in #40 later) are responsible for correlating
Mail.app account name → (host, port, email) and fetching the
password via ``keychain.get_imap_password``.

See ``docs/plans/2026-04-23-imap-connector-design.md``.
"""

from __future__ import annotations

import base64
import binascii
import logging
import quopri
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime as _datetime
from datetime import timedelta as _timedelta
from datetime import timezone as _timezone
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.utils import collapse_rfc2231_value, decode_params, unquote
from typing import Any, cast
from urllib.parse import unquote as url_unquote

from imapclient import DRAFT, IMAPClient
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError, LoginError
from imapclient.response_types import Envelope

from .draft_builder import ForwardedAttachment, extract_attachment_payloads
from .exceptions import (
    MailAttachmentIndexError,
    MailImapMoveUnsupportedError,
    MailImapTrashNotFoundError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
)
from .utils import ALL_MAILBOXES, parse_rfc822_ids

logger = logging.getLogger(__name__)

# Strict ISO 8601 YYYY-MM-DD. Duplicated from mail_connector to break an
# otherwise-circular import: mail_connector.search_messages delegates to
# this module, so mail_connector has to import from imap_connector, and a
# reverse dependency would deadlock. The regex is trivial; duplication is
# preferable to reshuffling the module layout.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

CONNECT_TIMEOUT_S: float = 3.0
"""Per invariant 4 in imap-auth-options-decision.md: ≤3s so offline
fallback happens inside the graceful-degradation window without
waiting for TCP's default timeout. Bounds connect + login only — once
logged in the socket timeout is raised to OPERATION_TIMEOUT_S (#249)."""

OPERATION_TIMEOUT_S: float = 30.0
"""Socket read timeout for SEARCH/FETCH after login. imapclient's
``timeout=`` applies to every socket read, so the short CONNECT_TIMEOUT_S
would otherwise kill a legitimate server-side SEARCH (10–20s on a large
iCloud mailbox) mid-operation, silently dropping the IMAP fast path to the
slower AppleScript fallback. We connect fast (offline detection) then raise
the timeout for real work. (#249)"""


def _to_server_folder_name(path: str, delimiter: str, prefix: str) -> str:
    """Translate a Mail-style mailbox path to the IMAP server's name for it.

    Mail shows every mailbox as a ``/``-separated path and hides the server's
    personal namespace prefix, so ``To Delete/Spam`` in Mail is
    ``INBOX.To Delete.Spam`` on a Dovecot/Cyrus server with prefix
    ``INBOX.`` and delimiter ``.``. INBOX itself is never prefixed.

    Raises:
        ValueError: ``path`` is nested but the server has no hierarchy
            delimiter (a flat mailbox namespace).
    """
    if path.upper() == "INBOX":
        return path
    if "/" in path and not delimiter:
        raise ValueError(
            f"cannot create {path!r}: the server has no mailbox hierarchy"
        )
    name = path.replace("/", delimiter) if delimiter else path
    if prefix and not name.startswith(prefix):
        name = prefix + name
    return name


def _apply_operation_timeout(client: IMAPClient) -> None:
    """Raise the socket read timeout from the connect window to
    OPERATION_TIMEOUT_S, post-login. Call immediately after
    ``client.login(...)`` at every connection-open site. (#249)"""
    client.socket().settimeout(OPERATION_TIMEOUT_S)

POOL_IDLE_TIMEOUT_S: float = 270.0
"""Default pool idle threshold. iCloud and most providers drop IMAP
sessions after ~30 min idle. 270s = 4.5 min keeps us comfortably under
that while still amortizing connect cost across realistic interactive
bursts (a series of search → get_message → get_attachments calls within
a couple minutes of each other reuses one connection)."""

_FILTER_FETCH_CHUNK: int = 100
"""FETCH batch size when a limited search needs post-FETCH filtering
(has_attachment). Newest chunks are fetched first and the scan stops at
`limit` matches, so this bounds the per-round-trip cost, not the total
scan depth."""

_FLAG_SEEN = b"\\Seen"
_FLAG_FLAGGED = b"\\Flagged"
_FLAG_ANSWERED = b"\\Answered"


# ---------------------------------------------------------------------------
# Connection pool (issue #75)
# ---------------------------------------------------------------------------

@dataclass
class _PooledClient:
    """Tracks one cached IMAPClient: the connection, a lock that
    serializes its use across threads, and a monotonic timestamp for
    idle-timeout decisions."""

    client: IMAPClient
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_used: float = field(default_factory=time.monotonic)


# Exceptions that should invalidate a pooled connection. LoginError /
# IMAPClientError are direct protocol failures; OSError catches mid-
# session network drops (broken pipe, conn reset). MailMessageNotFoundError
# is NOT here — it's a clean "no match" answer, not a connection failure.
_POOL_INVALIDATE_EXCS: tuple[type[BaseException], ...] = (
    LoginError,
    IMAPClientError,
    OSError,
)


class ImapConnectionPool:
    """Reuses IMAPClient sessions across calls keyed by (host, email).

    Per the issue #75 design: opt-in (per-call lifecycle remains the
    default in :class:`ImapConnector` until a pool is explicitly
    passed). Lazy-reconnect on error and on idle timeout — if the
    cached client is stale, the next ``session()`` drops it and opens
    a fresh one transparently.

    Thread-safety:
    - The cache dict is guarded by ``_cache_lock``.
    - Each cached client has its own per-connection lock that
      ``session()`` holds for the duration of the yielded block.
      Concurrent calls to the same (host, email) serialize; concurrent
      calls to different keys run in parallel.
    """

    def __init__(self, *, idle_timeout_s: float = POOL_IDLE_TIMEOUT_S) -> None:
        self._cache: dict[tuple[str, str], _PooledClient] = {}
        self._cache_lock = threading.Lock()
        self._idle_timeout_s = idle_timeout_s

    @contextmanager
    def session(
        self,
        host: str,
        port: int,
        email: str,
        password: str,
        connect_timeout: float,
    ) -> Iterator[IMAPClient]:
        """Yield a logged-in IMAPClient for use; manage its lifecycle.

        On clean exit: bump ``last_used`` and keep the entry cached.

        On failure of one of ``_POOL_INVALIDATE_EXCS``: drop the entry
        (attempt logout, swallow logout errors) so the next call gets
        a fresh connection. The original exception still propagates;
        the orchestrator's existing fallback logic catches it.
        """
        key = (host, email)

        with self._cache_lock:
            entry = self._cache.get(key)
            stale = (
                entry is not None
                and time.monotonic() - entry.last_used > self._idle_timeout_s
            )
            if entry is None or stale:
                if entry is not None:
                    # Stale — try to be polite about closing it.
                    self._cache.pop(key, None)
                    try:
                        entry.client.logout()
                    except Exception:  # noqa: BLE001 — best effort
                        pass
                client = IMAPClient(
                    host, port=port, ssl=True, timeout=connect_timeout
                )
                client.login(email, password)
                _apply_operation_timeout(client)
                entry = _PooledClient(client=client)
                self._cache[key] = entry

        # Acquire the per-connection lock OUTSIDE the cache lock — we
        # don't want a slow operation on one connection blocking cache
        # reads for unrelated keys.
        with entry.lock:
            try:
                yield entry.client
            except _POOL_INVALIDATE_EXCS:
                # Connection-level failure: drop and propagate.
                with self._cache_lock:
                    # Only drop if it's still the same entry — another
                    # thread may have already reconnected.
                    if self._cache.get(key) is entry:
                        self._cache.pop(key, None)
                try:
                    entry.client.logout()
                except Exception:  # noqa: BLE001 — best effort
                    pass
                raise
            else:
                entry.last_used = time.monotonic()

    def close(self) -> None:
        """Log out all cached clients. Safe to call multiple times.

        Acquires each entry's per-connection lock before issuing
        ``logout()`` so an in-flight ``session()``-holder finishes its
        operation before we drop the connection underneath it (#171).
        Latent today because FastMCP is single-threaded, but the
        pool's per-entry locks are designed for future thread-safety
        and the atexit hook (#127) fires ``close()`` at interpreter
        shutdown when daemon threads may still be alive.
        """
        with self._cache_lock:
            entries = list(self._cache.values())
            self._cache.clear()
        for entry in entries:
            # Wait for any in-flight session() block to finish before
            # logging out — mirrors session()'s invalidation path.
            with entry.lock:
                try:
                    entry.client.logout()
                except Exception:  # noqa: BLE001 — best effort
                    pass

_IMAP_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _iso_to_imap_date(iso: str, field: str) -> str:
    if not _ISO_DATE_RE.match(iso):
        raise ValueError(
            f"{field} must be ISO 8601 YYYY-MM-DD, got: {iso!r}"
        )
    d = _date.fromisoformat(iso)
    return f"{d.day:02d}-{_IMAP_MONTHS[d.month - 1]}-{d.year}"


def _iso_to_imap_before(iso: str, field: str) -> str:
    """Upper-bound helper: IMAP BEFORE is exclusive; pass date + 1 day."""
    if not _ISO_DATE_RE.match(iso):
        raise ValueError(
            f"{field} must be ISO 8601 YYYY-MM-DD, got: {iso!r}"
        )
    d = _date.fromisoformat(iso) + _timedelta(days=1)
    return f"{d.day:02d}-{_IMAP_MONTHS[d.month - 1]}-{d.year}"


def _reject_control_chars(value: str, field: str) -> None:
    """Raise ValueError if ``value`` contains a C0 control character or DEL.

    IMAP command arguments and RFC 3501 quoted strings must not contain
    CR/LF. imapclient quotes string args but does NOT promote CR/LF-bearing
    values to literals (its 8-bit detector only fires on bytes > 127), so a
    raw CRLF embedded in a value is sent inline on the authenticated control
    channel and splits the command — letting following bytes be parsed as a
    new tagged command (IMAP command injection, CWE-93). Applied to every
    free-text value that becomes an IMAP command argument (SEARCH terms,
    Message-IDs, mailbox names) before it can reach the wire.
    """
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise ValueError(
                f"{field} contains a disallowed control character "
                f"(U+{ord(ch):04X}); control characters are not permitted "
                f"in IMAP command arguments"
            )


def _bracket_message_id(message_id: str) -> str:
    """Validate a Message-ID and return it in RFC 5322 bracketed form.

    Rejects control characters (see :func:`_reject_control_chars`), then
    strips surrounding whitespace / angle brackets and wraps the canonical
    bare id. Single chokepoint for every ``SEARCH HEADER "Message-ID"
    "<id>"`` construction so no call site can build a bracketed id without
    the CRLF-injection guard.
    """
    _reject_control_chars(message_id, "message_id")
    return f"<{_strip_brackets(message_id)}>"


# Max Message-IDs folded into a single OR SEARCH. Keeps the command well
# under server line-length limits while still collapsing the common
# bulk-mutation case (≤50 ids) into one round-trip. (#316)
_MSGID_SEARCH_CHUNK = 50


def _validate_message_ids(message_ids: list[str]) -> None:
    """Reject control characters in every Message-ID up front (the #254
    CRLF-injection guard), before any SELECT/capability work — so a crafted
    id is refused regardless of server capabilities or where resolution
    happens. ``_resolve_uids_batch`` re-applies the guard when it brackets
    ids, but the chokepoint must also fail closed early."""
    for mid in message_ids:
        _bracket_message_id(mid)


def _or_message_id_criteria(message_ids: list[str]) -> list[Any]:
    """Build an IMAP SEARCH criteria matching ANY of ``message_ids`` by
    ``HEADER "Message-ID"``, so a batch resolves in one round-trip. (#316)

    Each id is routed through :func:`_bracket_message_id` (the CRLF-injection
    chokepoint, #254). One id yields a bare ``HEADER`` clause; N ids fold
    into right-nested binary ``OR`` (IMAP's ``OR`` takes exactly two keys):
    ``["OR", a, ["OR", b, c]]``. Assumes ``message_ids`` is non-empty.
    """
    clauses = [
        ["HEADER", "Message-ID", _bracket_message_id(mid)]
        for mid in message_ids
    ]
    criteria: list[Any] = clauses[-1]
    for clause in reversed(clauses[:-1]):
        criteria = ["OR", clause, criteria]
    return criteria


def _build_search_criteria(
    sender_contains: str | None,
    subject_contains: str | None,
    read_status: bool | None,
    is_flagged: bool | None,
    date_from: str | None = None,
    date_to: str | None = None,
    body_contains: str | None = None,
    text_contains: str | None = None,
) -> list[Any]:
    """Translate ImapConnector.search_messages parameters to IMAP SEARCH criteria.

    Returns ``["ALL"]`` if no filters are supplied — IMAP SEARCH requires at
    least one criterion.
    """
    criteria: list[Any] = []
    if sender_contains:
        _reject_control_chars(sender_contains, "sender_contains")
        criteria.extend(["FROM", sender_contains])
    if subject_contains:
        _reject_control_chars(subject_contains, "subject_contains")
        criteria.extend(["SUBJECT", subject_contains])
    if read_status is True:
        criteria.append("SEEN")
    elif read_status is False:
        criteria.append("UNSEEN")
    if is_flagged is True:
        criteria.append("FLAGGED")
    elif is_flagged is False:
        criteria.append("UNFLAGGED")
    if date_from is not None:
        criteria.extend(["SINCE", _iso_to_imap_date(date_from, "date_from")])
    if date_to is not None:
        criteria.extend(["BEFORE", _iso_to_imap_before(date_to, "date_to")])
    if body_contains:
        _reject_control_chars(body_contains, "body_contains")
        criteria.extend(["BODY", body_contains])
    if text_contains:
        _reject_control_chars(text_contains, "text_contains")
        criteria.extend(["TEXT", text_contains])
    return criteria or ["ALL"]


def _search_charset(criteria: list[Any]) -> str | None:
    """Return ``"UTF-8"`` if any criterion value is non-ASCII, else ``None``.

    imapclient encodes ``str`` search criteria using the charset passed to
    ``IMAPClient.search()``, which defaults to ``us-ascii``. A non-ASCII term
    (e.g. a Korean keyword like ``"안내"``) under that default raises
    ``UnicodeEncodeError`` inside imaplib *before* the command is sent — never
    reaching the server, never matching anything.

    RFC 3501 §6.4.4 requires non-ASCII SEARCH keys to be sent under an explicit
    ``CHARSET``; imapclient emits ``SEARCH CHARSET UTF-8 ...`` (with the term as
    an 8-bit literal) when we pass ``charset="UTF-8"``. UTF-8 is near-universally
    supported; a server that rejects it answers ``BAD``/``NO``, which imapclient
    raises as ``IMAPClientError`` — caught by the orchestrator's AppleScript
    fallback.

    We only opt in to UTF-8 when a term actually needs it: pure-ASCII searches
    stay on the default us-ascii path for maximum server compatibility.
    """
    for item in criteria:
        if isinstance(item, str) and not item.isascii():
            return "UTF-8"
    return None


def _decode(b: bytes | bytearray | str | None) -> str:
    if b is None:
        return ""
    if isinstance(b, str):
        return b
    # bytes or bytearray — both have .decode().
    return b.decode("utf-8", errors="replace")


def _part_text(part: Any) -> str:
    """Decode a single non-multipart MIME part to text, honoring its
    transfer-encoding and charset (falling back to UTF-8/replace)."""
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return str(part.get_payload() or "")
        charset = part.get_content_charset() or "utf-8"
        return str(payload.decode(charset, errors="replace"))
    except (LookupError, ValueError, TypeError):
        try:
            return str(part.get_payload() or "")
        except (ValueError, TypeError):
            return ""


def _html_to_text(html: str) -> str:
    """Cheap HTML→text for the rare text/html-only message. Not a renderer —
    just enough to recover readable prose when there is no text/plain part."""
    import html as _html

    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p>", "\n\n", html)
    html = re.sub(r"<[^>]+>", "", html)
    return _html.unescape(html)


def _extract_text_body(raw: bytes | bytearray | None) -> str:
    """Parse a full RFC822 message (BODY[]) and return ONLY its
    human-readable text.

    This is the crux of the body-overflow fix: IMAP ``BODY[TEXT]`` hands back
    the entire multipart body with every attachment base64-inlined, so a 1.5 MB
    attachment becomes ~2 MB of ``content`` that overflows the MCP result. By
    walking the MIME tree here we return kilobytes of decoded prose with the
    attachment bytes dropped. Attachment *metadata* is derived separately from
    ``draft_builder.extract_attachment_payloads`` (the canonical walker that
    matches the BODYSTRUCTURE index contract), so this helper deliberately does
    not enumerate attachments.

    On any parse failure we degrade to a raw charset decode rather than raising
    — a body is never worth a hard error.
    """
    if not raw:
        return ""
    try:
        msg = message_from_bytes(bytes(raw), policy=policy.default)
    except (ValueError, TypeError):
        return _decode(raw)
    plains: list[str] = []
    htmls: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = part.get_content_disposition() or ""
        if part.get_filename() or disp == "attachment":
            continue  # attachment bytes never enter content
        ctype = part.get_content_type()
        if ctype == "text/plain":
            plains.append(_part_text(part))
        elif ctype == "text/html":
            htmls.append(_part_text(part))
    body = "\n".join(p for p in plains if p.strip())
    if not body.strip() and htmls:
        body = _html_to_text("\n".join(htmls))
    return body.replace("\r\n", "\n").strip()


def _decode_mime_header(raw: bytes | bytearray | str | None) -> str:
    """Decode an RFC 2047 encoded-word header value to its Unicode form.

    IMAP ``ENVELOPE`` returns Subject and address display-name fields as the
    raw header bytes. For non-ASCII content those are MIME encoded-words such
    as ``=?UTF-8?B?7JWI64K0?=`` — not the human-readable text. The AppleScript
    path hands back the value Mail.app has *already* decoded, so without this
    step the two paths disagree (the IMAP path leaked ``=?UTF-8?B?...?=`` for
    Korean subjects/senders — see the IMAP-delegation diagnosis, F3).

    Handles both shapes:
    - Proper encoded-words (pure ASCII on the wire) — decoded by
      ``decode_header``/``make_header``.
    - Servers that send raw UTF-8 in the header without encoding it — the
      initial UTF-8 decode recovers the text, and ``decode_header`` then finds
      no encoded-words and returns it unchanged. (Encoded-words are an ASCII
      subset, so decoding them as UTF-8 first is lossless.)

    Falls back to the best-effort string on any malformed input or unknown
    charset rather than raising — a display field is never worth a hard error.
    """
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        s = bytes(raw).decode("utf-8", errors="replace")
    else:
        s = raw
    try:
        return str(make_header(decode_header(s)))
    except (ValueError, LookupError):
        # ValueError: malformed encoded-word. LookupError: charset label the
        # codec registry doesn't know. Either way, return the raw string.
        return s


def _strip_brackets(s: str) -> str:
    s = s.strip()
    if s.startswith("<") and s.endswith(">"):
        return s[1:-1].strip()
    return s


def _flatten_thread_clusters(tree: Any) -> Iterator[set[int]]:
    """Yield each top-level cluster of a THREAD result as a flat set of UIDs.

    IMAPClient's ``client.thread()`` returns nested tuples per RFC 5256 —
    each top-level element is one thread, with possibly-nested replies
    representing the reply tree. For dispatch (#123 Tier 2) we don't care
    about the tree shape; we just need to know which UIDs belong to which
    cluster so we can intersect with the anchor's relevant_uids.

    Edge cases:
    - The result may be ``None`` if the server returned an empty THREAD
      response — we yield nothing.
    - Each leaf may be an int or a string-coerced int depending on the
      IMAP server / IMAPClient version; we coerce to int.
    """
    if not tree:
        return
    for top in tree:
        out: set[int] = set()
        _flatten_one(top, out)
        if out:
            yield out


def _flatten_one(node: Any, out: set[int]) -> None:
    """Recursive walk of a THREAD nested-tuple node, adding UIDs to ``out``."""
    if isinstance(node, (tuple, list)):
        for child in node:
            _flatten_one(child, out)
    else:
        try:
            out.add(int(node))
        except (TypeError, ValueError):
            pass


def _format_address(addr: Any) -> str:
    """Format one IMAP ENVELOPE address (name + mailbox@host) as a string.

    The display name is RFC 2047-decoded via ``_decode_mime_header`` so
    recipient/sender names come through in their human-readable Unicode form,
    consistent with how the subject is decoded (#392)."""
    name = _decode_mime_header(getattr(addr, "name", None))
    mailbox = _decode(getattr(addr, "mailbox", None))
    host = _decode(getattr(addr, "host", None))
    email = f"{mailbox}@{host}" if mailbox and host else mailbox or ""
    return f"{name} <{email}>" if name else email


def _format_sender(envelope: Envelope) -> str:
    from_ = envelope.from_ or ()
    if not from_:
        return ""
    return _format_address(from_[0])


def _format_address_list(addrs: Any) -> str:
    """Format an ENVELOPE address tuple (To/Cc) as a comma-joined string.
    Returns ``""`` when the field is absent — recipients were previously
    dropped entirely on the IMAP path; surfacing them closes that gap."""
    formatted = [_format_address(a) for a in (addrs or ())]
    return ", ".join(x for x in formatted if x)


def _bodystructure_extract_attachments(
    structure: Any,
) -> list[dict[str, Any]]:
    """Walk a BODYSTRUCTURE tuple and emit attachment metadata.

    A part counts as an attachment if any of:
    - Its disposition is ``attachment``.
    - It has a filename — a disposition ``filename`` or a content-type
      ``name``, in plain or RFC 2231 (``filename*``) form — whatever its
      disposition, including none. That is how inline images arrive:
      ``inline; filename=`` from Apple Mail, a bare ``name=`` with a
      Content-ID from Outlook.
    - It's a ``message/rfc822`` part (forwarded email — conventionally
      an attachment per RFC 2046 §5.2.1, and the case Mail.app's
      AppleScript surface sometimes silently drops).

    Returns dicts with keys: ``name``, ``mime_type``, ``size``,
    ``encoded_size``, ``downloaded``. On the IMAP path, BODYSTRUCTURE's
    size is the transfer-encoded body size for some providers, so
    ``encoded_size`` preserves that exact value and ``size`` reports the
    best decoded byte count available without fetching the part body.
    ``downloaded`` is always False on the IMAP path — BODYSTRUCTURE returns
    metadata only, not body bytes, and Mail.app's local cache state isn't
    observable from the protocol. Callers that need decoded bytes invoke
    ``get_attachment_content`` or ``save_attachments``.

    The byte-fetch paths index into this list by position.
    ``get_attachment_content`` uses the section-aware walker below;
    ``save_attachments`` still uses ``draft_builder.extract_attachment_payloads``
    for full raw messages. Both MUST apply the SAME inclusion predicate and
    document order. Keep the two in sync; the live integration test asserts
    they agree. The ``has_attachment`` search filter is this same walk.
    """
    return [
        {
            "name": item["name"],
            "mime_type": item["mime_type"],
            "size": item["size"],
            "encoded_size": item["encoded_size"],
            "downloaded": item["downloaded"],
        }
        for item in _bodystructure_attachment_infos(structure)
    ]


def _collapse_param(value: Any) -> str:
    """One ``decode_params`` value as text. A plain value is a quoted string;
    an RFC 2231 one is a ``(charset, language, quoted-text)`` triple.

    The quotes come off the raw text BEFORE the charset decode: quoting is
    byte-level, and Shift_JIS, Big5 and GBK all use 0x5C (backslash) as a
    trail byte, so unquoting afterwards corrupts those names.
    """
    if not isinstance(value, tuple):
        return str(collapse_rfc2231_value(value))
    charset, language, text = value
    return str(collapse_rfc2231_value((charset, language, unquote(text))))


def _salvage_param(pairs: list[tuple[str, str]], name: str) -> str | None:
    """The raw value of ``name`` (or its first ``name*…`` form), for a
    parameter list the RFC 2231 decoder refused. Best effort: an extended
    value loses its ``charset'lang'`` prefix and is percent-decoded as UTF-8."""
    for key, raw in pairs:
        if key == name or key.startswith(name + "*"):
            if key != name and raw.count("'") >= 2:
                raw = url_unquote(raw.split("'", 2)[2], errors="replace")
            return unquote(raw)
    return None


def _param_value(params: Any, name: str) -> str | None:
    """Decoded value of ``name`` in a flat BODYSTRUCTURE ``(k, v, k, v, ...)``
    param tuple, or ``None`` when absent.

    Servers hand parameters over as they were sent, so a non-ASCII filename
    arrives under ``filename*`` (RFC 2231) — or split over ``filename*0*``,
    ``filename*1*`` — and never under ``filename``. Other mailers put an
    RFC 2047 encoded-word in a plain ``filename``. Both decode here, to the
    name a MIME parse of the same part gives.

    Never raises. The parameters are whatever the sender wrote, and the
    stdlib decoder does raise on some of it (a ``filename*`` beside a
    ``filename*0``, a codec that isn't a text encoding, a continuation index
    too long to be an int). One such message must not fail every search and
    listing that includes it, so those fall back to the raw value.
    """
    if not isinstance(params, tuple):
        return None
    pairs: list[tuple[str, str]] = []
    for i in range(0, len(params) - 1, 2):
        k, v = params[i], params[i + 1]
        if isinstance(k, bytes) and isinstance(v, (bytes, bytearray)):
            pairs.append((_decode(k).lower(), _decode(v)))
    try:
        # decode_params treats slot 0 as the header's main value and skips it.
        for key, value in decode_params([("", ""), *pairs])[1:]:
            if key == name:
                return _decode_mime_header(_collapse_param(value))
        return None
    except (TypeError, ValueError, LookupError):
        salvaged = _salvage_param(pairs, name)
        return None if salvaged is None else _decode_mime_header(salvaged)


def _leaf_disposition(leaf: tuple[Any, ...]) -> tuple[bytes | None, Any]:
    """``(kind, params)`` of a leaf's disposition, or ``(None, ())``.

    The disposition is a ``(keyword, (params…) | NIL)`` pair in the extension
    data that follows the seven basic fields, at an index that varies with
    the part type (text adds 'lines'; message/rfc822 adds envelope + body +
    lines). So it is found by shape, from field 7 on — not by keyword, and
    not among the basic fields, where a content-type parameter that happens
    to be named ``attachment`` once passed for one. Any keyword counts:
    ``inline`` and ``attachment`` are only the usual two.
    """
    for elem in leaf[7:]:
        if (
            isinstance(elem, tuple)
            and 1 <= len(elem) <= 2
            and isinstance(elem[0], bytes)
            and (len(elem) == 1 or elem[1] is None or isinstance(elem[1], tuple))
        ):
            return elem[0].lower(), (elem[1] if len(elem) > 1 and elem[1] else ())
    return None, ()


def _leaf_attachment_info(
    leaf: tuple[Any, ...], section: str
) -> dict[str, Any] | None:
    """Attachment metadata for one leaf part, or ``None`` if it is body text.

    Spec positions: type, subtype, params, id, desc, encoding, size.
    """
    type_ = leaf[0] if isinstance(leaf[0], bytes) else b""
    subtype = leaf[1] if len(leaf) > 1 and isinstance(leaf[1], bytes) else b""
    ct_params = leaf[2] if len(leaf) > 2 else ()
    encoding = leaf[5] if len(leaf) > 5 else b""
    size_field = leaf[6] if len(leaf) > 6 else 0

    disp_kind, disp_params = _leaf_disposition(leaf)
    # Filename precedence: disposition's filename → content-type's `name`,
    # the first that is not blank (a whitespace-only name is no name).
    name = (
        (_param_value(disp_params, "filename") or "").strip()
        or (_param_value(ct_params, "name") or "").strip()
    )
    is_rfc822 = type_.lower() == b"message" and subtype.lower() == b"rfc822"
    if not (disp_kind == b"attachment" or name or is_rfc822):
        return None

    encoded_size = int(size_field) if isinstance(size_field, int) else 0
    return {
        "name": name,
        "mime_type": f"{_decode(type_)}/{_decode(subtype)}",
        "size": _decoded_size_from_bodystructure(encoding, encoded_size),
        "encoded_size": encoded_size,
        "downloaded": False,
        "section": section or "1",
        "encoding": _decode(encoding).lower(),
    }


def _bodystructure_attachment_infos(structure: Any) -> list[dict[str, Any]]:
    """Walk BODYSTRUCTURE and include private IMAP section metadata."""
    out: list[dict[str, Any]] = []

    def _child_section(prefix: str, index: int) -> str:
        return f"{prefix}.{index}" if prefix else str(index)

    def _walk(s: Any, section: str = "") -> None:
        if not isinstance(s, tuple) or not s:
            return

        # Multipart: IMAPClient groups the child parts in a LIST at
        # position 0 — ``([child1, child2, ...], subtype, params, ...)``.
        # (Real iCloud/Gmail BODYSTRUCTUREs take this shape; missing it
        # silently dropped attachments on every multipart message.)
        if isinstance(s[0], list):
            for index, child in enumerate(s[0], start=1):
                _walk(child, _child_section(section, index))
            return
        # Defensive: some inputs nest children as direct tuple elements.
        if isinstance(s[0], tuple):
            index = 1
            for child in s:
                if isinstance(child, tuple):
                    _walk(child, _child_section(section, index))
                    index += 1
            return

        info = _leaf_attachment_info(s, section)
        if info is not None:
            out.append(info)

    _walk(structure)
    return out


def _base64_body_octets(encoded_size: int) -> int:
    """Infer base64 character count from MIME transfer octets.

    BODYSTRUCTURE's size field is the transfer-encoded body size. MIME
    base64 bodies are commonly wrapped at 76 characters with CRLF line
    endings. Try the standard wrapped forms first, then fall back to an
    unwrapped multiple-of-four value.
    """
    if encoded_size <= 0:
        return 0

    def valid(n: int, lines: int) -> bool:
        return n > 0 and n % 4 == 0 and (lines - 1) * 76 < n <= lines * 76

    approx = max(1, encoded_size // 78)
    for lines in range(max(1, approx - 4), approx + 8):
        n = encoded_size - (2 * lines)
        if valid(n, lines):
            return n

        n = encoded_size - (2 * max(0, lines - 1))
        if valid(n, lines):
            return n

    if encoded_size % 4 == 0:
        return encoded_size
    return max(0, encoded_size - (encoded_size % 4))


def _decoded_size_from_bodystructure(encoding: Any, encoded_size: int) -> int:
    """Return decoded byte count when BODYSTRUCTURE makes it knowable enough."""
    if not isinstance(encoded_size, int) or encoded_size <= 0:
        return 0
    enc = _decode(encoding).lower()
    if enc == "base64":
        base64_octets = _base64_body_octets(encoded_size)
        if base64_octets <= 0:
            return 0
        # Padding is not exposed in BODYSTRUCTURE. One byte is the middle
        # candidate and matches the common PDF/image cases from Apple Mail
        # while staying within the exact decoded-size range of +/- 1 byte.
        return max(0, (base64_octets // 4) * 3 - 1)
    return encoded_size


def _decode_transfer_payload(payload: bytes, encoding: str) -> bytes:
    enc = encoding.strip().lower()
    if enc == "base64":
        try:
            return base64.b64decode(payload, validate=False)
        except (ValueError, binascii.Error):
            cleaned = re.sub(rb"\s+", b"", payload)
            return base64.b64decode(cleaned, validate=False)
    if enc in {"quoted-printable", "quotedprintable"}:
        return quopri.decodestring(payload)
    return payload


def _validate_payload_range(offset: int, max_bytes: int | None) -> None:
    if offset < 0:
        raise ValueError("offset must be greater than or equal to 0")
    if max_bytes is not None and max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer when provided")


def _range_end(
    total_size: int, *, offset: int, max_bytes: int | None
) -> int:
    if offset > total_size:
        raise ValueError(f"offset {offset} is beyond attachment size {total_size}")
    if max_bytes is None:
        return total_size
    return min(total_size, offset + max_bytes)


def _slice_decoded_payload(
    payload: bytes,
    *,
    offset: int,
    max_bytes: int | None,
) -> tuple[bytes, bool, int | None]:
    total_size = len(payload)
    end = _range_end(total_size, offset=offset, max_bytes=max_bytes)
    sliced = payload[offset:end]
    truncated = offset != 0 or end < total_size
    next_offset = end if end < total_size else None
    return sliced, truncated, next_offset


def _section_fetch_key(
    section: str,
    *,
    transfer_start: int | None = None,
    transfer_count: int | None = None,
) -> bytes:
    key = f"BODY.PEEK[{section}]"
    if transfer_start is not None and transfer_count is not None:
        key += f"<{transfer_start}.{transfer_count}>"
    return key.encode("ascii")


def _base64_transfer_offset(char_offset: int, base64_chars: int, encoded_size: int) -> int:
    """Map a base64 character offset to a MIME transfer-body octet offset.

    BODYSTRUCTURE exposes the transfer-encoded byte count. Apple Mail and
    common IMAP servers wrap MIME base64 at 76 chars with CRLF. For unwrapped
    bodies, or tiny one-line bodies with only a trailing CRLF, this collapses
    to the character offset.
    """
    if encoded_size == base64_chars:
        return char_offset
    return char_offset + 2 * (char_offset // 76)


def _base64_transfer_window(
    *,
    decoded_offset: int,
    decoded_count: int,
    encoded_size: int,
) -> tuple[int, int, int]:
    """Return ``(transfer_start, transfer_count, decoded_skip)`` for base64.

    The server can only slice transfer bytes. Align to a base64 quantum before
    the requested decoded offset, decode that small window, then discard
    ``decoded_skip`` bytes locally.
    """
    base64_chars = _base64_body_octets(encoded_size)
    start_group = decoded_offset // 3
    decoded_anchor = start_group * 3
    char_start = start_group * 4
    decoded_skip = decoded_offset - decoded_anchor
    decoded_needed = decoded_skip + decoded_count
    chars_needed = ((decoded_needed + 2) // 3) * 4
    char_end = min(base64_chars, char_start + chars_needed)

    transfer_start = _base64_transfer_offset(char_start, base64_chars, encoded_size)
    transfer_end = _base64_transfer_offset(char_end, base64_chars, encoded_size)
    transfer_count = max(0, min(encoded_size, transfer_end) - transfer_start)
    return transfer_start, transfer_count, decoded_skip


def _base64_decoded_size_from_tail(
    tail_payload: bytes,
    *,
    base64_chars: int,
    fallback: int,
) -> int:
    cleaned = re.sub(rb"\s+", b"", tail_payload)
    if not cleaned or base64_chars <= 0:
        return fallback
    padding = 2 if cleaned.endswith(b"==") else 1 if cleaned.endswith(b"=") else 0
    return max(0, (base64_chars // 4) * 3 - padding)


def _body_fetch_value(entry: dict[bytes, Any]) -> bytes | None:
    """Return the first BODY[...] value from a FETCH response entry."""
    for key, value in entry.items():
        if isinstance(key, bytes) and key.upper().startswith(b"BODY["):
            return bytes(value or b"")
    return None


def _bodystructure_has_attachment(structure: Any) -> bool:
    """True if the message has at least one attachment.

    This is the attachment listing's own walk, not a second predicate: the
    ``has_attachment`` search filter and the list a caller then reads can't
    disagree about a message.
    """
    return bool(_bodystructure_attachment_infos(structure))


def _envelope_to_dict(
    envelope: Envelope, flags: tuple[bytes, ...]
) -> dict[str, Any]:
    date = envelope.date
    if isinstance(date, _datetime):
        date_str = date.isoformat()
    else:
        date_str = _decode(date)
    # On the IMAP path, `id` and `rfc_message_id` are intentionally the
    # same value — both are the RFC 5322 Message-ID (bracketless). The
    # dual-emit (#148) lets cross-path consumers (e.g., callers feeding
    # this row to an AppleScript-only tool) always have an RFC id
    # available; on the AppleScript path the two diverge (`id` is the
    # Mail.app internal numeric id, `rfc_message_id` is the RFC form).
    rfc_id = _strip_brackets(_decode(envelope.message_id))
    return {
        "id": rfc_id,
        "rfc_message_id": rfc_id,
        "subject": _decode_mime_header(envelope.subject),
        "sender": _format_sender(envelope),
        "to": _format_address_list(envelope.to),
        "cc": _format_address_list(envelope.cc),
        "date_received": date_str,
        "read_status": _FLAG_SEEN in flags,
        "flagged": _FLAG_FLAGGED in flags,
    }


def _flag_names(flags: Any) -> list[str] | None:
    """A FETCH ``FLAGS`` value as strings, in the server's order — system
    flags and keywords alike. ``None`` when the server sent none at all."""
    if flags is None:
        return None
    return [_decode(flag) for flag in flags]


def _peek(section_key: bytes) -> bytes:
    """``BODY[...]`` → ``BODY.PEEK[...]``: the form of a section fetch that
    does not set ``\\Seen``.

    Every read asks this way. EXAMINE alone is not enough: iCloud sets
    ``\\Seen`` on a plain ``BODY[]`` even in a read-only session, which
    marked mail read behind the caller's back. The server answers under the
    plain ``BODY[...]`` key either way, so response lookups are unchanged.
    """
    return section_key.replace(b"BODY[", b"BODY.PEEK[", 1)


def _select_body_bytes(entry: dict[bytes, Any], body_key: bytes) -> bytes:
    """Pull the body section out of a FETCH entry. IMAPClient can key the
    returned section slightly differently than requested, so fall back to any
    ``BODY[...]`` that isn't the header block."""
    body_bytes = entry.get(body_key)
    if body_bytes is None:
        for k, v in entry.items():
            if (
                isinstance(k, bytes)
                and k.startswith(b"BODY[")
                and b"HEADER" not in k
            ):
                body_bytes = v
                break
    return body_bytes or b""


def _header_fields_bytes(entry: dict[bytes, Any]) -> bytes:
    """Pull the ``HEADER.FIELDS`` section bytes out of a FETCH entry,
    tolerating imapclient's ``BODY.PEEK[…]`` → ``BODY[…]`` key normalization
    (#415)."""
    for key, value in entry.items():
        if (
            isinstance(key, bytes)
            and b"HEADER.FIELDS" in key
            and isinstance(value, (bytes, bytearray))
        ):
            return bytes(value)
    return b""


def _anchor_from_fetch(entry: dict[bytes, Any]) -> dict[str, Any]:
    """Build a ``get_thread`` anchor from a FETCH of ENVELOPE +
    REFERENCES/IN-REPLY-TO headers (#415). References is not carried in the
    IMAP ENVELOPE, so it is parsed from the fetched header block."""
    envelope = entry.get(b"ENVELOPE")
    rfc_id = ""
    subject = ""
    in_reply_to = ""
    if envelope is not None:
        rfc_id = _strip_brackets(_decode(envelope.message_id))
        subject = _decode_mime_header(envelope.subject)
        in_reply_to = _strip_brackets(_decode(envelope.in_reply_to))
    references: list[str] = []
    header_bytes = _header_fields_bytes(entry)
    if header_bytes:
        parsed = message_from_bytes(header_bytes, policy=policy.default)
        references = parse_rfc822_ids(parsed.get("References", "") or "")
        if not in_reply_to:
            irt = parse_rfc822_ids(parsed.get("In-Reply-To", "") or "")
            in_reply_to = irt[0] if irt else ""
    return {
        "rfc_message_id": rfc_id,
        "subject": subject,
        "in_reply_to": in_reply_to or None,
        "references": references,
    }


def _payload_to_attachment_meta(fa: ForwardedAttachment) -> dict[str, Any]:
    """Map a ``draft_builder`` ForwardedAttachment tuple
    ``(filename, maintype, subtype, payload)`` to the IMAP attachment-metadata
    shape. ``downloaded`` is True — the bytes came from the full BODY[] parse,
    not from BODYSTRUCTURE metadata."""
    return {
        "name": fa[0],
        "mime_type": f"{fa[1]}/{fa[2]}",
        "size": len(fa[3]),
        "downloaded": True,
    }


def _build_get_message_result(
    entry: dict[bytes, Any],
    body_key: bytes,
    *,
    want_body: bool,
    text_mode: bool,
    include_attachments: bool,
) -> dict[str, Any]:
    """Assemble the get_message response dict from a single FETCH entry.

    Kept out of ``get_message`` itself so that method stays a thin
    fetch-orchestrator (and under the complexity budget). In text mode
    ``content`` is the decoded human-readable body and attachment metadata is
    derived from the same full ``BODY[]`` bytes via the canonical
    ``extract_attachment_payloads`` walker (which matches the BODYSTRUCTURE
    index contract); otherwise both fall back to the legacy behavior."""
    result = _envelope_to_dict(entry[b"ENVELOPE"], tuple(entry.get(b"FLAGS", ())))
    body_bytes = b""
    if want_body:
        body_bytes = _select_body_bytes(entry, body_key)
        result["content"] = (
            _extract_text_body(body_bytes) if text_mode else _decode(body_bytes)
        )
    else:
        result["content"] = ""
    if include_attachments:
        if want_body and text_mode:
            result["attachments"] = [
                _payload_to_attachment_meta(fa)
                for fa in extract_attachment_payloads(body_bytes)
            ]
        else:
            result["attachments"] = _bodystructure_extract_attachments(
                entry.get(b"BODYSTRUCTURE")
            )
    return result


class ImapConnector:
    def __init__(
        self,
        host: str,
        port: int,
        email: str,
        password: str,
        connect_timeout: float = CONNECT_TIMEOUT_S,
        *,
        pool: ImapConnectionPool | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._email = email
        self._password = password
        self._connect_timeout = connect_timeout
        self._pool = pool

    @contextmanager
    def _session(self) -> Iterator[IMAPClient]:
        """Yield a logged-in IMAPClient. Routes through the pool when
        one was provided at construction; otherwise opens and closes a
        fresh connection per call (the v0.5.0 behavior).

        All four public methods of this connector (search_messages,
        get_message, get_attachments, find_thread_members) use this
        helper so the pool / no-pool decision lives in exactly one place.
        """
        if self._pool is not None:
            with self._pool.session(
                self._host, self._port, self._email,
                self._password, self._connect_timeout,
            ) as client:
                yield client
            return

        client = IMAPClient(
            self._host, port=self._port, ssl=True, timeout=self._connect_timeout
        )
        try:
            client.login(self._email, self._password)
            _apply_operation_timeout(client)
            yield client
        finally:
            client.logout()

    def search_messages(
        self,
        mailbox: str = "INBOX",
        sender_contains: str | None = None,
        subject_contains: str | None = None,
        read_status: bool | None = None,
        is_flagged: bool | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachment: bool | None = None,
        limit: int | None = None,
        include_attachments: bool = False,
        body_contains: str | None = None,
        text_contains: str | None = None,
    ) -> list[dict[str, Any]]:
        # Validate and translate filters before opening a connection so that
        # invalid input fails fast without the TCP-connect + LOGIN round trip.
        criteria = _build_search_criteria(
            sender_contains,
            subject_contains,
            read_status,
            is_flagged,
            date_from,
            date_to,
            body_contains,
            text_contains,
        )
        _reject_control_chars(mailbox, "mailbox")

        charset = _search_charset(criteria)

        def search_selected(client: Any) -> list[dict[str, Any]]:
            return self._search_selected(
                client,
                criteria,
                charset,
                has_attachment=has_attachment,
                include_attachments=include_attachments,
                limit=limit,
            )

        with self._session() as client:
            if mailbox != ALL_MAILBOXES:
                client.select_folder(mailbox, readonly=True)
                return search_selected(client)

            return self._search_all_folders(client, search_selected, limit)

    def _search_all_folders(
        self,
        client: Any,
        search_selected: Callable[[Any], list[dict[str, Any]]],
        limit: int | None,
    ) -> list[dict[str, Any]]:
        """Run one search in every selectable folder; each row names its own.

        Rows merge by ISO date and ``limit`` keeps the newest. Each folder
        contributes its own newest ``limit`` by arrival (UID) order, so a
        broad query can miss an old-dated message that was filed recently
        behind newer-dated ones; a query that matches fewer than ``limit``
        per folder is exact.

        A message also visible in an ``\\All`` folder (Gmail's All Mail) is
        listed once, under its real folder — ``\\All`` folders are searched
        last and only their rows are ever dropped as duplicates. The same
        Message-ID in two real folders is two messages.
        """
        if limit == 0:
            return []
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for folder, is_all_mail in self._selectable_folders(client):
            try:
                info = client.select_folder(folder, readonly=True)
            except IMAPClientAbortError:
                raise  # the connection is gone, not this folder
            except IMAPClientError:
                continue  # this one won't open; the others still can
            if info.get(b"EXISTS") == 0:
                continue  # empty: skip the SEARCH round-trip
            for row in search_selected(client):
                rfc_id = str(row.get("rfc_message_id") or "")
                if is_all_mail and rfc_id and rfc_id in seen:
                    continue
                seen.add(rfc_id)
                row["mailbox"] = folder
                rows.append(row)
        rows.sort(key=lambda row: str(row.get("date_received") or ""))
        return rows[-limit:] if limit is not None else rows

    @staticmethod
    def _selectable_folders(client: Any) -> list[tuple[str, bool]]:
        """``(name, is_all_mail)`` for every folder that can be SELECTed,
        ``\\All`` folders last. Flags are matched in any case: servers spell
        ``\\Noselect`` more than one way."""
        normal: list[tuple[str, bool]] = []
        all_mail: list[tuple[str, bool]] = []
        for flags, _delim, name in client.list_folders():
            lowered = {bytes(flag).lower() for flag in flags}
            if lowered & {b"\\noselect", b"\\nonexistent"}:
                continue
            folder = name.decode("utf-8", errors="replace") if isinstance(name, bytes) else name
            if b"\\all" in lowered:
                all_mail.append((folder, True))
            else:
                normal.append((folder, False))
        return normal + all_mail

    def _search_selected(
        self,
        client: Any,
        criteria: list[Any],
        charset: str | None,
        *,
        has_attachment: bool | None,
        include_attachments: bool,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        """SEARCH + FETCH in the currently selected folder."""
        # Pass charset only when a non-ASCII term needs it — keeps ASCII
        # searches on the default us-ascii path (max server compatibility)
        # while letting Korean/CJK terms ride a CHARSET UTF-8 literal.
        uids = (
            client.search(criteria)
            if charset is None
            else client.search(criteria, charset)
        )
        # `limit` bounds MATCHING results. With no post-filter, every
        # candidate matches, so truncating the window up front is both
        # correct and the cheapest possible FETCH. With has_attachment
        # (only expressible by inspecting BODYSTRUCTURE after FETCH),
        # pre-truncating would change limit's meaning to "whichever of
        # the newest N happen to match" — silently dropping matches the
        # AppleScript path (per-match limit) would return. There the
        # walk below scans newest-first in bounded chunks instead.
        post_filter = has_attachment is not None
        if limit is not None and not post_filter:
            uids = uids[-limit:]

        if not uids:
            return []

        fetch_keys: list[bytes] = [b"ENVELOPE", b"FLAGS"]
        # BODYSTRUCTURE bundles into the same FETCH (no extra round-trip)
        # whether we need it for has_attachment filtering or for the
        # include_attachments output field — only fetch once.
        need_bodystructure = post_filter or include_attachments
        if need_bodystructure:
            fetch_keys.append(b"BODYSTRUCTURE")

        return self._collect_search_rows(
            client,
            uids,
            fetch_keys,
            has_attachment=has_attachment,
            include_attachments=include_attachments,
            limit=limit if post_filter else None,
        )

    def _collect_search_rows(
        self,
        client: Any,
        uids: list[int],
        fetch_keys: list[bytes],
        *,
        has_attachment: bool | None,
        include_attachments: bool,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        """FETCH candidate UIDs and build result rows, newest first.

        When ``limit`` is set (only passed for post-filtered searches),
        candidates are fetched in chunks of ``_FILTER_FETCH_CHUNK`` from
        the newest end and the walk stops as soon as ``limit`` rows match
        — so a mailbox where the newest messages match never pays for
        fetching the old ones. Results are returned oldest-first to match
        every other search path. ``limit=None`` degenerates to a single
        FETCH of all candidates (the pre-existing fast path).
        """
        chunk_size = _FILTER_FETCH_CHUNK if limit is not None else len(uids)
        newest_first: list[dict[str, Any]] = []
        for end in range(len(uids), 0, -chunk_size):
            chunk = uids[max(0, end - chunk_size) : end]
            fetched = client.fetch(chunk, fetch_keys)
            for uid in reversed(chunk):
                entry = fetched.get(uid)
                # A message can vanish between SEARCH and FETCH (expunged or
                # moved by a concurrent change — another client, a rule, or
                # our own move/delete on this mailbox). The server then omits
                # ENVELOPE for that UID. Skip it rather than crashing the
                # whole search. (#314)
                if entry is None or b"ENVELOPE" not in entry:
                    continue
                if has_attachment is not None:
                    has = _bodystructure_has_attachment(
                        entry.get(b"BODYSTRUCTURE")
                    )
                    if has_attachment is True and not has:
                        continue
                    if has_attachment is False and has:
                        continue
                row = _envelope_to_dict(
                    entry[b"ENVELOPE"], tuple(entry.get(b"FLAGS", ()))
                )
                if include_attachments:
                    row["attachments"] = _bodystructure_extract_attachments(
                        entry.get(b"BODYSTRUCTURE")
                    )
                newest_first.append(row)
                if limit is not None and len(newest_first) >= limit:
                    return list(reversed(newest_first))
        return list(reversed(newest_first))

    def get_message(
        self,
        message_id: str,
        mailbox: str = "INBOX",
        *,
        include_content: bool = True,
        headers_only: bool = False,
        include_attachments: bool = False,
        body_format: str = "text",
    ) -> dict[str, Any]:
        """Look up a single message by RFC 5322 Message-ID and return its
        envelope + flags, optionally with body content.

        ``message_id`` is matched against the ``Message-ID`` header via
        ``SEARCH HEADER "Message-ID" "<id>"``. The angle brackets are
        added if missing — RFC 5322 stores the ID in bracketed form and
        IMAP servers compare against the literal header value.

        Args:
            message_id: RFC 5322 Message-ID, with or without surrounding
                ``<>``. The bracketless form is what
                ``search_messages`` returns; either works as input.
            mailbox: Folder to look in. IMAP requires a SELECT before
                FETCH; cross-folder lookup is not in scope here (callers
                without a folder hint stay on the AppleScript path in
                the orchestrator above).
            include_content: When False, ``content`` is the empty string
                (matches the AppleScript path's behavior with the same
                flag).
            body_format: ``"text"`` (default) fetches the full message and
                returns ``content`` as decoded human-readable text only —
                attachment bytes are parsed out (their metadata still comes
                via ``include_attachments``). This is what keeps a message
                with a multi-MB attachment from overflowing the MCP result.
                ``"raw"`` preserves the legacy behavior: ``content`` is the
                undecoded ``BODY[TEXT]`` (the entire multipart body with
                base64 attachments inlined) — kept only as an escape hatch.
            headers_only: When True, fetches ``BODY[HEADER]`` instead of
                ``BODY[TEXT]`` — useful for preview-style callers who
                don't want the body. ``content`` is always returned as
                the empty string in this mode (the headers themselves
                are reflected via the envelope dict; we don't expose the
                raw RFC 822 header block).

        Returns:
            Dict with the same keys as the AppleScript ``get_message``
            output: ``id``, ``subject``, ``sender``, ``date_received``,
            ``read_status``, ``flagged``, ``content``.

        Raises:
            MailMessageNotFoundError: No message in ``mailbox`` matches
                the Message-ID. (The orchestrator's caller may then fall
                back to AppleScript.)
            IMAPClientError: Login / SELECT / SEARCH / FETCH failed.
        """
        bracketed = _bracket_message_id(message_id)
        _reject_control_chars(mailbox, "mailbox")

        fetch_keys: list[bytes] = [b"ENVELOPE", b"FLAGS"]
        want_body = include_content and not headers_only
        text_mode = body_format != "raw"
        # text mode parses the whole message server-side, so fetch BODY[] (full
        # RFC822); raw mode keeps the legacy header-less BODY[TEXT].
        body_key = b"BODY[]" if text_mode else b"BODY[TEXT]"
        if want_body:
            fetch_keys.append(_peek(body_key))
        elif headers_only:
            # We don't currently use the raw header block for anything in
            # the response (envelope already gives us subject/sender/date),
            # but requesting BODY[HEADER] is the spec-correct way to ask
            # the server for headers without paying for the body. Some
            # servers send less data this way; some don't care.
            fetch_keys.append(_peek(b"BODY[HEADER]"))
        # When we parse the full body (text mode), attachment metadata comes
        # from that parse — more reliable than BODYSTRUCTURE, which yields a
        # false-empty list on some real messages. Only pay for BODYSTRUCTURE
        # when we won't have the full body to parse (raw mode, or body skipped).
        have_full_body = want_body and text_mode
        if include_attachments and not have_full_body:
            fetch_keys.append(b"BODYSTRUCTURE")

        with self._session() as client:
            client.select_folder(mailbox, readonly=True)

            uids = client.search(["HEADER", "Message-ID", bracketed])
            if not uids:
                raise MailMessageNotFoundError(
                    f"Message-ID {message_id!r} not found in mailbox "
                    f"{mailbox!r} on {self._host}."
                )

            fetched = client.fetch(uids[:1], fetch_keys)
            entry = next(iter(fetched.values()), None)
            # The message matched SEARCH but vanished before FETCH (expunged
            # or moved by a concurrent change) — treat as not-found rather
            # than crashing on the missing ENVELOPE. (#314)
            if entry is None or b"ENVELOPE" not in entry:
                raise MailMessageNotFoundError(
                    f"Message-ID {message_id!r} vanished from mailbox "
                    f"{mailbox!r} between SEARCH and FETCH."
                )

            return _build_get_message_result(
                entry,
                body_key,
                want_body=want_body,
                text_mode=text_mode,
                include_attachments=include_attachments,
            )

    def fetch_raw_message(
        self, message_id: str, mailbox: str = "INBOX"
    ) -> bytes:
        """Fetch the full raw RFC 822 bytes of a message by Message-ID.

        Used to rebuild a clean reply/forward draft (#245 follow-up):
        parsing the raw original yields headers, body, and attachment
        payloads in a single fetch.

        Args:
            message_id: RFC 5322 Message-ID, with or without ``<>``.
            mailbox: Folder to SELECT and search. The caller supplies the
                seed message's folder (or defaults to INBOX); a miss
                raises so the orchestrator can fall back to AppleScript,
                which resolves the message across all folders.

        Raises:
            MailMessageNotFoundError: No message in ``mailbox`` matches.
            IMAPClientError: Login / SELECT / SEARCH / FETCH failed.
        """
        bracketed = _bracket_message_id(message_id)
        _reject_control_chars(mailbox, "mailbox")
        with self._session() as client:
            client.select_folder(mailbox, readonly=True)
            uids = client.search(["HEADER", "Message-ID", bracketed])
            if not uids:
                raise MailMessageNotFoundError(
                    f"Message-ID {message_id!r} not found in mailbox "
                    f"{mailbox!r} on {self._host}."
                )
            fetched = client.fetch(uids[:1], [_peek(b"BODY[]")])
            entry = next(iter(fetched.values()))
            raw = entry.get(b"BODY[]") or b""
            if not raw:
                raise MailMessageNotFoundError(
                    f"Message-ID {message_id!r} in {mailbox!r} returned an "
                    f"empty body on {self._host}."
                )
            return bytes(raw)

    def fetch_message_with_source(
        self, message_id: str, mailbox: str = "INBOX"
    ) -> tuple[bytes, dict[str, Any]]:
        """Fetch a message's raw RFC 822 bytes together with where they came
        from: the provenance an exported ``.eml`` is filed with.

        The source dict holds ``host``, ``mailbox``, ``uidvalidity`` and
        ``uid`` (which message, on which server), ``rfc822_size`` (the size
        the server states, to compare with the bytes received),
        ``flags_before`` / ``flags_after`` (the full flag list on either side
        of the fetch) and ``fetched_at`` (UTC, ISO 8601).

        The flags are read in their own commands, before and after the body.
        Flags returned in the same FETCH as the body already reflect that
        fetch, so they could not show whether it changed anything. That is
        two round-trips more than :meth:`fetch_raw_message`, which stays the
        path for callers that only want the bytes. A value the server did not
        give is ``None``; ``flags_after`` is ``None`` if the message was moved
        or expunged in between.

        Raises:
            MailMessageNotFoundError: No message in ``mailbox`` matches.
            IMAPClientError: Login / SELECT / SEARCH / FETCH failed.
        """
        bracketed = _bracket_message_id(message_id)
        _reject_control_chars(mailbox, "mailbox")
        with self._session() as client:
            info = client.select_folder(mailbox, readonly=True)
            uids = client.search(["HEADER", "Message-ID", bracketed])
            if not uids:
                raise MailMessageNotFoundError(
                    f"Message-ID {message_id!r} not found in mailbox "
                    f"{mailbox!r} on {self._host}."
                )
            uid = uids[0]
            before = client.fetch([uid], [b"FLAGS", b"RFC822.SIZE"]).get(uid) or {}
            fetched_at = _datetime.now(_timezone.utc).isoformat(timespec="seconds")
            raw = (client.fetch([uid], [_peek(b"BODY[]")]).get(uid) or {}).get(b"BODY[]")
            if not raw:
                raise MailMessageNotFoundError(
                    f"Message-ID {message_id!r} in {mailbox!r} returned an "
                    f"empty body on {self._host}."
                )
            after = client.fetch([uid], [b"FLAGS"]).get(uid)
            return bytes(raw), {
                "host": self._host,
                "mailbox": mailbox,
                "uidvalidity": info.get(b"UIDVALIDITY"),
                "uid": uid,
                "rfc822_size": before.get(b"RFC822.SIZE"),
                "flags_before": _flag_names(before.get(b"FLAGS")),
                "flags_after": _flag_names(after.get(b"FLAGS")) if after else None,
                "fetched_at": fetched_at,
            }

    def fetch_attachment_payload(
        self,
        message_id: str,
        attachment_index: int,
        mailbox: str = "INBOX",
        *,
        offset: int = 0,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Fetch and decode one attachment MIME part by BODYSTRUCTURE index.

        This avoids fetching the full RFC 822 message for
        ``get_attachment_content``. The attachment ordering and membership
        match ``get_attachments`` because both use the same BODYSTRUCTURE
        walker. When ``offset`` / ``max_bytes`` are supplied, base64 and
        raw-transfer parts use IMAP partial fetches so previews do not
        download the whole attachment body.
        """
        _validate_payload_range(offset, max_bytes)
        bracketed = _bracket_message_id(message_id)
        _reject_control_chars(mailbox, "mailbox")
        with self._session() as client:
            client.select_folder(mailbox, readonly=True)
            uids = client.search(["HEADER", "Message-ID", bracketed])
            if not uids:
                raise MailMessageNotFoundError(
                    f"Message-ID {message_id!r} not found in mailbox "
                    f"{mailbox!r} on {self._host}."
                )

            uid = uids[0]
            fetched = client.fetch([uid], [b"BODYSTRUCTURE"])
            entry = fetched.get(uid)
            infos = _bodystructure_attachment_infos(
                entry.get(b"BODYSTRUCTURE") if entry else None
            )
            if not 0 <= attachment_index < len(infos):
                raise MailAttachmentIndexError(
                    f"attachment_index {attachment_index} out of range: "
                    f"message has {len(infos)} attachment(s)."
                )

            info = infos[attachment_index]
            section = str(info["section"])
            encoding = str(info.get("encoding") or "")
            encoded_size = int(info.get("encoded_size") or 0)
            size_hint = int(info.get("size") or 0)

            def fetch_encoded(fetch_key: bytes) -> bytes:
                part_fetched = client.fetch([uid], [fetch_key])
                part_entry = part_fetched.get(uid) or {}
                encoded_payload = _body_fetch_value(part_entry)
                if encoded_payload is None:
                    raise MailMessageNotFoundError(
                        f"Attachment {attachment_index} of Message-ID "
                        f"{message_id!r} in {mailbox!r} returned an empty "
                        f"body on {self._host}."
                    )
                return encoded_payload

            def fetch_full_decoded() -> tuple[bytes, int]:
                payload = _decode_transfer_payload(
                    fetch_encoded(_section_fetch_key(section)),
                    encoding,
                )
                return payload, len(payload)

            enc = encoding.strip().lower()
            requested_range = offset != 0 or max_bytes is not None
            payload: bytes
            total_size: int
            if requested_range and enc == "base64" and encoded_size > 0:
                base64_chars = _base64_body_octets(encoded_size)
                tail_count = min(encoded_size, 96)
                tail_start = max(0, encoded_size - tail_count)
                tail_payload = fetch_encoded(
                    _section_fetch_key(
                        section,
                        transfer_start=tail_start,
                        transfer_count=tail_count,
                    )
                )
                total_size = _base64_decoded_size_from_tail(
                    tail_payload,
                    base64_chars=base64_chars,
                    fallback=size_hint,
                )
                end = _range_end(total_size, offset=offset, max_bytes=max_bytes)
                wanted = end - offset
                if wanted == 0:
                    payload = b""
                else:
                    transfer_start, transfer_count, decoded_skip = (
                        _base64_transfer_window(
                            decoded_offset=offset,
                            decoded_count=wanted,
                            encoded_size=encoded_size,
                        )
                    )
                    encoded_payload = fetch_encoded(
                        _section_fetch_key(
                            section,
                            transfer_start=transfer_start,
                            transfer_count=transfer_count,
                        )
                    )
                    decoded = _decode_transfer_payload(encoded_payload, encoding)
                    payload = decoded[decoded_skip : decoded_skip + wanted]
                    if len(payload) < wanted:
                        full_payload, total_size = fetch_full_decoded()
                        end = _range_end(
                            total_size, offset=offset, max_bytes=max_bytes
                        )
                        payload = full_payload[offset:end]
            elif (
                requested_range
                and enc in {"", "7bit", "8bit", "binary"}
                and size_hint > 0
            ):
                total_size = size_hint
                end = _range_end(total_size, offset=offset, max_bytes=max_bytes)
                wanted = end - offset
                if wanted == 0:
                    payload = b""
                else:
                    payload = _decode_transfer_payload(
                        fetch_encoded(
                            _section_fetch_key(
                                section,
                                transfer_start=offset,
                                transfer_count=wanted,
                            )
                        ),
                        encoding,
                    )
            else:
                full_payload, total_size = fetch_full_decoded()
                payload, _truncated, _next_offset = _slice_decoded_payload(
                    full_payload,
                    offset=offset,
                    max_bytes=max_bytes,
                )

            returned_end = offset + len(payload)
            truncated = offset != 0 or returned_end < total_size
            next_offset = returned_end if returned_end < total_size else None
            return {
                "name": info["name"] or "attachment",
                "mime_type": info["mime_type"],
                "size": total_size,
                "payload": payload,
                "content_offset": offset,
                "content_bytes_returned": len(payload),
                "content_truncated": truncated,
                "next_offset": next_offset,
            }

    def get_attachments(
        self,
        message_id: str,
        mailbox: str = "INBOX",
    ) -> list[dict[str, Any]]:
        """Return a list of attachment metadata dicts for a message.

        Looks the message up by RFC 5322 Message-ID via
        ``SEARCH HEADER "Message-ID" "<id>"``, then issues a single
        ``FETCH ... BODYSTRUCTURE`` and walks the MIME tree to extract
        attachment parts. One round-trip total for metadata, vs the
        AppleScript path's account×mailbox scan plus per-attachment
        property fetches.

        The IMAP path also surfaces attachment cases Mail.app's
        AppleScript layer drops silently — see issue #73 for the
        catalog (forwarded message/rfc822 parts, multipart/related
        inline images, Unicode filenames).

        Args:
            message_id: RFC 5322 Message-ID, with or without surrounding
                ``<>``. The bracketless form is what
                ``search_messages`` returns; either works as input.
            mailbox: Folder to look in. IMAP requires SELECT before
                FETCH; cross-folder discovery isn't in scope here
                (callers without a folder hint stay on the AppleScript
                path in the orchestrator above).

        Returns:
            List of dicts with keys ``name`` (str), ``mime_type`` (str),
            ``size`` (int), ``encoded_size`` (int), ``downloaded`` (bool,
            always False on this path). Empty list when the message has no
            attachments.

        Raises:
            MailMessageNotFoundError: No message in ``mailbox`` matches
                the Message-ID.
            IMAPClientError: Login / SELECT / SEARCH / FETCH failed.
        """
        bracketed = _bracket_message_id(message_id)
        _reject_control_chars(mailbox, "mailbox")

        with self._session() as client:
            client.select_folder(mailbox, readonly=True)

            uids = client.search(["HEADER", "Message-ID", bracketed])
            if not uids:
                raise MailMessageNotFoundError(
                    f"Message-ID {message_id!r} not found in mailbox "
                    f"{mailbox!r} on {self._host}."
                )

            fetched = client.fetch(uids[:1], [b"BODYSTRUCTURE"])
            entry = next(iter(fetched.values()))
            return _bodystructure_extract_attachments(
                entry.get(b"BODYSTRUCTURE")
            )

    def locate_message(self, message_id: str) -> dict[str, Any] | None:
        """Locate an RFC 5322 Message-ID and return where and *when* it is.

        Returns ``{"folder": str, "uid": int, "internaldate": datetime}`` for
        the first probed folder containing the Message-ID, or ``None`` when no
        probed folder has it.

        The ``internaldate`` is the point of this method (#432). Mail.app's
        ``message id`` property is UNINDEXED, so matching it in AppleScript
        loads every message in a mailbox and freezes Mail's UI. Mail *does*
        order messages newest-first with cheap positional access, so a caller
        that knows roughly WHEN a message arrived can binary-search to it in
        ~log2(n) property reads instead of scanning. This supplies that date
        from the server side, where the lookup IS indexed (measured 0.14s over
        61,880 messages).

        Shares the bounded probe-folder set with :meth:`resolve_anchor` — for
        locating, Gmail's All Mail is ideal rather than a liability, since it
        mirrors every message and so answers for any folder.

        Raises:
            IMAPClientError / OSError / LoginError: connection/auth failures,
                so the caller can distinguish "not there" from "could not
                check" (#425).
        """
        bracketed = _bracket_message_id(message_id)
        with self._session() as client:
            for folder in self._anchor_probe_folders(client):
                try:
                    client.select_folder(folder, readonly=True)
                    uids = client.search(["HEADER", "Message-ID", bracketed])
                except IMAPClientError as exc:
                    logger.debug(
                        "locate_message: skipping %s (%s)", folder, exc
                    )
                    continue
                if not uids:
                    continue
                try:
                    fetched = client.fetch([uids[0]], [b"INTERNALDATE"])
                except IMAPClientError:
                    continue
                entry = fetched.get(uids[0]) or {}
                internaldate = entry.get(b"INTERNALDATE")
                if internaldate is None:
                    continue
                return {
                    "folder": folder,
                    "uid": uids[0],
                    "internaldate": internaldate,
                }
        return None

    def resolve_anchor(self, message_id: str) -> dict[str, Any] | None:
        """Resolve an RFC 5322 Message-ID to a get_thread anchor via
        server-side, INDEXED ``SEARCH HEADER Message-ID`` (#415).

        This exists so ``get_thread`` never runs Mail.app's UNINDEXED
        all-mailbox ``whose message id`` scan, which loads every message and
        freezes Mail on a large account (a 33k-message INBOX takes >100s).
        IMAP ``SEARCH HEADER`` is server-indexed and instant.

        Probes a BOUNDED folder set — Gmail's ``\\All`` (All Mail mirrors every
        message) if present, else INBOX + Sent — and never lists/scans every
        folder or message. Returns an anchor dict
        ``{rfc_message_id, subject, in_reply_to, references}`` for the first
        probed folder that contains the Message-ID, or ``None`` if not found.
        The caller supplies ``account``.

        Raises:
            IMAPClientError / OSError / LoginError: connection/auth failures,
                so the caller can fall through to the next account.
        """
        bracketed = _bracket_message_id(message_id)
        with self._session() as client:
            for folder in self._anchor_probe_folders(client):
                try:
                    client.select_folder(folder, readonly=True)
                    uids = client.search(["HEADER", "Message-ID", bracketed])
                except IMAPClientError as exc:
                    logger.debug(
                        "resolve_anchor: skipping %s (%s)", folder, exc
                    )
                    continue
                if not uids:
                    continue
                try:
                    fetched = client.fetch(
                        [uids[0]],
                        [
                            b"ENVELOPE",
                            b"BODY.PEEK[HEADER.FIELDS "
                            b"(REFERENCES IN-REPLY-TO)]",
                        ],
                    )
                except IMAPClientError:
                    continue
                entry = fetched.get(uids[0])
                if entry:
                    return _anchor_from_fetch(entry)
        return None

    def _anchor_probe_folders(self, client: IMAPClient) -> list[str]:
        """Bounded folder set for #415 anchor resolution: Gmail All Mail
        (mirrors every message) if present, else INBOX + Sent. Never lists
        all folders — that's the cost we're removing."""
        all_mail = self._find_all_mail_folder(client)
        if all_mail:
            return [all_mail]
        return list(self._anchor_lookup_folders(client))

    def find_thread_members(
        self,
        anchor_rfc_message_id: str,
        anchor_references: list[str],
    ) -> list[dict[str, Any]]:
        """Return all messages in the anchor's thread across the account.

        Tiered, capability-detected dispatch:

        - **Tier 1 (Gmail X-GM-THRID via All Mail)** (#122) — when
          ``X-GM-EXT-1`` is advertised AND ``[Gmail]/All Mail`` is
          visible (``\\All`` SPECIAL-USE flag), look the conversation
          up by Gmail's 64-bit thread ID. ~5 round-trips,
          mailbox-count-independent.

        - **Tier 1.5 (Gmail X-GM-THRID per-mailbox)** (#125) — when
          ``X-GM-EXT-1`` is advertised but All Mail is hidden over IMAP
          (per-folder-size opt-out in Gmail Settings — common). Find
          the anchor's UID + X-GM-THRID in INBOX or Sent, then search
          every selectable folder by X-GM-THRID. ~2+M*2 round-trips
          (~186 on a 92-folder account vs ~1100 for BFS).

        - **Tier 2 (RFC 5256 THREAD)** (#123) — when ``THREAD=REFERENCES``
          or ``THREAD=REFS`` is advertised (Fastmail, some Dovecot;
          NOT Gmail typically, NOT iCloud). Per mailbox: a single
          THREAD command returns nested UID tuples representing the
          full thread structure; walk to find clusters containing
          relevant UIDs. ~M*2-4 round-trips depending on anchor
          locality.

        - **Tier 3 (header-search BFS)** — universal fallback. Iterates
          every mailbox; searches Message-ID / In-Reply-To /
          References for every known thread ID. Used on iCloud and
          any provider not covered above. Also used when a higher tier
          advertises but rejects mid-flight (server lied about its
          capabilities).

        A single ``_session()`` covers all tiers, so fall-through
        doesn't pay a second connect+login. With the pool (#75),
        all tiers share the cached connection.

        Args:
            anchor_rfc_message_id: RFC 5322 Message-ID of the thread anchor,
                bracketless (as returned by _strip_brackets).
            anchor_references: List of Message-IDs from the anchor's
                References header, bracketless, order preserved. Used by
                Tier 2 + Tier 3; Tier 1 / 1.5 don't need them
                (X-GM-THRID is the authoritative grouping).

        Returns:
            List of message dicts in the same shape as search_messages
            (``id``, ``subject``, ``sender``, ``date_received``,
            ``read_status``, ``flagged``), deduped by Message-ID, sorted
            chronologically ascending.
        """
        _reject_control_chars(anchor_rfc_message_id, "anchor_rfc_message_id")
        with self._session() as client:
            # Tier 1: Gmail X-GM-THRID via [Gmail]/All Mail
            if self._has_capability(client, b"X-GM-EXT-1"):
                tier1 = self._thread_via_xgm_thrid(client, anchor_rfc_message_id)
                if tier1 is not None:
                    return tier1

                # Tier 1.5: Gmail X-GM-THRID per-mailbox (when All Mail hidden)
                tier_1_5 = self._thread_via_xgm_per_mailbox(
                    client, anchor_rfc_message_id
                )
                if tier_1_5 is not None:
                    return tier_1_5

            # Tier 2: RFC 5256 THREAD (Fastmail, Dovecot)
            if (
                self._has_capability(client, b"THREAD=REFERENCES")
                or self._has_capability(client, b"THREAD=REFS")
            ):
                tier2 = self._thread_via_imap_thread(
                    client, anchor_rfc_message_id, anchor_references
                )
                if tier2 is not None:
                    return tier2

            # Tier 3: header-search BFS — universal fallback.
            return self._find_thread_members_bfs(
                client, anchor_rfc_message_id, anchor_references,
            )

    def delete_mailbox(self, name: str, *, allow_non_empty: bool = False) -> int:
        """Delete an IMAP mailbox via the IMAP DELETE command.

        Pre-flight: SELECT readonly to read the EXISTS count. Refuses if
        the mailbox has messages and ``allow_non_empty=False`` — surface
        as ``MailMailboxNotEmptyError`` from the caller.

        Args:
            name: Mailbox name (slash-separated for nested).
            allow_non_empty: When True, skips the message-count refusal
                and lets IMAP DELETE remove the mailbox along with its
                contents.

        Returns:
            The message count that was on the mailbox at the moment of
            delete (0 if empty, N if cascading).

        Raises:
            ValueError: If the mailbox is non-empty and the caller did
                not opt in to ``allow_non_empty``.
            imapclient.exceptions.IMAPClientError: Server-side error
                (mailbox doesn't exist, permission denied, etc.).
        """
        _reject_control_chars(name, "name")
        with self._session() as client:
            try:
                info = client.select_folder(name, readonly=True)
            except IMAPClientError:
                # Surface "mailbox doesn't exist" via the original IMAP error.
                raise
            count = int(info.get(b"EXISTS", 0))
            if count > 0 and not allow_non_empty:
                # CLOSE the readonly select before raising — leaves the
                # connection clean for any subsequent reuse via the pool.
                try:
                    client.close_folder()
                except IMAPClientError:
                    pass
                raise ValueError(
                    f"mailbox {name!r} is not empty ({count} messages); "
                    f"pass allow_non_empty=True to cascade-delete"
                )
            # IMAP DELETE requires the mailbox NOT to be selected on most
            # servers; CLOSE first.
            try:
                client.close_folder()
            except IMAPClientError:
                pass
            client.delete_folder(name)
            return count

    def rename_mailbox(self, old_name: str, new_name: str) -> None:
        """Rename a mailbox via the IMAP RENAME command.

        IMAP RENAME also serves as the move primitive — destination path
        with a different parent (e.g. ``"OldParent/Child"`` →
        ``"NewParent/Child"``) re-parents the mailbox in one operation.

        Args:
            old_name: Current mailbox name (slash-separated for nested).
            new_name: Destination name. Slash-separated; intermediate
                parents must exist (server behavior varies — most refuse
                to auto-create missing parents).

        Raises:
            imapclient.exceptions.IMAPClientError: Server-side error
                (source missing, destination exists, parent missing,
                permission denied, etc.).
        """
        _reject_control_chars(old_name, "old_name")
        _reject_control_chars(new_name, "new_name")
        with self._session() as client:
            client.rename_folder(old_name, new_name)

    def _folder_layout(self, client: IMAPClient) -> tuple[set[str], str, str]:
        """One LIST (plus NAMESPACE when advertised): the server's mailbox
        names, its hierarchy delimiter (``""`` for a flat server, which LISTs
        a NIL delimiter), and its personal namespace prefix."""
        present: set[str] = set()
        delimiter = ""
        for _flags, delim, raw_name in client.list_folders():
            present.add(
                raw_name.decode("utf-8", errors="replace")
                if isinstance(raw_name, (bytes, bytearray))
                else raw_name
            )
            if not delimiter and delim:
                delimiter = delim.decode() if isinstance(delim, bytes) else delim
        prefix = ""
        if self._has_capability(client, b"NAMESPACE"):
            try:
                personal = client.namespace()[0]
            except IMAPClientError:
                personal = None
            if personal:
                raw_prefix = personal[0][0]
                prefix = (
                    raw_prefix.decode() if isinstance(raw_prefix, bytes) else raw_prefix
                ) or ""
        return present, delimiter, prefix

    def ensure_folder(self, path: str) -> str:
        """Create the Mail-style mailbox ``path`` unless it exists, and
        return its server name. Used to materialize the delete-staging
        mailbox, synchronously on the server, so a MOVE into it right after
        doesn't bounce off a Mail.app sync lag."""
        _reject_control_chars(path, "path")
        with self._session() as client:
            present, delimiter, prefix = self._folder_layout(client)
            return self._ensure_folder(client, path, present, delimiter, prefix)

    @staticmethod
    def _ensure_folder(
        client: IMAPClient, path: str, present: set[str], delimiter: str, prefix: str
    ) -> str:
        if path in present:
            return path
        name = _to_server_folder_name(path, delimiter, prefix)
        if name not in present:
            client.create_folder(name)
            present.add(name)
        return name

    _STAGE_SUFFIX_LIMIT = 100

    def stage_mailbox(self, name: str, staging_path: str) -> str:
        """Move the mailbox ``name`` under ``staging_path`` (created if
        absent) with IMAP RENAME, which carries its messages and child
        mailboxes along (RFC 3501 §6.3.5). A taken destination gets
        `` 2``, `` 3``, … appended. Returns the new Mail-style path.

        Raises:
            MailMailboxNotFoundError: ``name`` doesn't exist on the server.
            ValueError: No free destination name, or a flat server.
            IMAPClientError: Server-side refusal.
        """
        _reject_control_chars(name, "name")
        _reject_control_chars(staging_path, "staging_path")
        with self._session() as client:
            present, delimiter, prefix = self._folder_layout(client)
            source = name if name in present else _to_server_folder_name(
                name, delimiter, prefix
            )
            if source not in present:
                raise MailMailboxNotFoundError(f"mailbox {name!r} not found")
            staging = _to_server_folder_name(staging_path, delimiter, prefix)
            if source == staging or (delimiter and source.startswith(staging + delimiter)):
                raise ValueError(
                    f"{name!r} is the staging mailbox or inside it; delete it "
                    "for real instead of staging it"
                )
            self._ensure_folder(client, staging_path, present, delimiter, prefix)
            leaf = name.rsplit("/", 1)[-1]
            for n in range(1, self._STAGE_SUFFIX_LIMIT + 1):
                staged = f"{staging_path}/{leaf}" if n == 1 else f"{staging_path}/{leaf} {n}"
                destination = _to_server_folder_name(staged, delimiter, prefix)
                if staged not in present and destination not in present:
                    client.rename_folder(source, destination)
                    return staged
            raise ValueError(
                f"no free name for {leaf!r} under {staging_path!r} after "
                f"{self._STAGE_SUFFIX_LIMIT} tries"
            )

    def _resolve_uids_batch(
        self,
        client: IMAPClient,
        message_ids: list[str],
    ) -> list[int]:
        """Resolve RFC 5322 Message-IDs to UIDs in the selected mailbox,
        batching the lookups into one ``OR HEADER "Message-ID"`` SEARCH per
        ``_MSGID_SEARCH_CHUNK`` ids instead of one SEARCH per id. (#316)

        Returns the matched UIDs (first-seen order, de-duplicated). Ids that
        don't resolve are silently skipped — callers act on the whole set,
        matching the AppleScript path's best-effort partial-success. Each id
        is validated via :func:`_bracket_message_id` (the #254 guard).
        """
        uids: list[int] = []
        seen: set[int] = set()
        for i in range(0, len(message_ids), _MSGID_SEARCH_CHUNK):
            chunk = message_ids[i:i + _MSGID_SEARCH_CHUNK]
            for uid in client.search(_or_message_id_criteria(chunk)):
                if uid not in seen:
                    seen.add(uid)
                    uids.append(uid)
        return uids

    def move_messages(
        self,
        message_ids: list[str],
        source_mailbox: str,
        destination_mailbox: str,
    ) -> int:
        """Move messages between mailboxes via IMAP. Issue #149.

        Resolves each RFC 5322 Message-ID to a UID via
        ``SEARCH HEADER "Message-ID" "<id>"`` against ``source_mailbox``,
        then issues one server-side move per session:

        - When ``MOVE`` (RFC 6851) is advertised: ``UID MOVE <uids> <dest>``
          — atomic, single round-trip after resolution.
        - Else when ``UIDPLUS`` (RFC 4315) is advertised: ``UID COPY`` +
          ``UID STORE +FLAGS (\\Deleted)`` + ``UID EXPUNGE``. The expunge
          is scoped to the just-deleted UIDs only — safe even if other
          ``\\Deleted``-flagged messages exist in the mailbox.
        - Else: raises :class:`MailImapMoveUnsupportedError`. A non-UIDPLUS
          unscoped EXPUNGE would remove all flagged messages in the
          mailbox, not just the ones we just moved.

        Args:
            message_ids: RFC 5322 Message-IDs, with or without surrounding
                ``<>``. Bracketless is what ``search_messages`` returns;
                either form works.
            source_mailbox: Mailbox the messages currently live in.
            destination_mailbox: Target mailbox.

        Returns:
            Count of resolved + moved messages. Message-IDs that don't
            resolve to a UID are silently skipped, matching the
            AppleScript path's best-effort partial-success behavior.

        Raises:
            MailImapMoveUnsupportedError: Server advertises neither MOVE
                nor UIDPLUS.
            IMAPClientError: SELECT / SEARCH / MOVE / COPY / STORE /
                EXPUNGE failed at the protocol level.
        """
        _validate_message_ids(message_ids)
        _reject_control_chars(source_mailbox, "source_mailbox")
        _reject_control_chars(destination_mailbox, "destination_mailbox")

        with self._session() as client:
            client.select_folder(source_mailbox, readonly=False)

            has_move = self._has_capability(client, b"MOVE")
            has_uidplus = self._has_capability(client, b"UIDPLUS")
            if not has_move and not has_uidplus:
                raise MailImapMoveUnsupportedError(
                    f"IMAP server at {self._host} advertises neither MOVE "
                    f"(RFC 6851) nor UIDPLUS (RFC 4315); cannot perform a "
                    f"safe scoped move"
                )

            uids = self._resolve_uids_batch(client, message_ids)
            if not uids:
                return 0

            if has_move:
                client.move(uids, destination_mailbox)
            else:
                client.copy(uids, destination_mailbox)
                client.add_flags(uids, [b"\\Deleted"], silent=True)
                client.uid_expunge(uids)
            return len(uids)

    # Conventional Trash folder names to fall back on when the IMAP
    # server doesn't advertise \\Trash via SPECIAL-USE (RFC 6154).
    # Order is informative — first match wins per `client.list_folders`.
    _CONVENTIONAL_TRASH_NAMES: tuple[str, ...] = (
        "Trash",
        "[Gmail]/Trash",
        "Deleted Messages",
        "Deleted Items",
    )

    # Conventional Drafts folder names to fall back on when the server
    # doesn't advertise \\Drafts via SPECIAL-USE (RFC 6154). Issue #245.
    _CONVENTIONAL_DRAFTS_NAMES: tuple[str, ...] = (
        "Drafts",
        "[Gmail]/Drafts",
        "INBOX.Drafts",
    )

    # Conventional Sent folder names to fall back on when the server
    # doesn't advertise \\Sent via SPECIAL-USE (RFC 6154). Issue #406.
    # "Sent Messages" is iCloud's name; "[Gmail]/Sent Mail" is Gmail's
    # (Gmail also advertises \\Sent, so the convention list is only a
    # safety net there). Order is informative — first match wins per
    # ``client.list_folders``.
    _CONVENTIONAL_SENT_NAMES: tuple[str, ...] = (
        "Sent",
        "Sent Messages",
        "[Gmail]/Sent Mail",
        "INBOX.Sent",
    )

    def append_draft(self, raw_message: bytes) -> str:
        """APPEND a pre-built RFC822 message to the account's Drafts
        folder with the ``\\Draft`` flag, and return the folder used.

        Bypasses Mail.app's AppleScript ``content`` setter, which wraps
        every body in an ``Apple-Mail-URLShareWrapper``
        ``<blockquote type="cite">`` that renders as a quote on iOS
        (Mail.app bug FB11734014, issue #245). The caller builds
        ``raw_message`` via :func:`apple_mail_fast_mcp.draft_builder.build_draft_mime`.
        """
        with self._session() as client:
            folder = self._find_drafts_folder(
                client
            ) or self._find_drafts_by_convention(client)
            if folder is None:
                raise MailMessageNotFoundError(
                    f"No Drafts folder found on {self._host} "
                    f"(no \\Drafts SPECIAL-USE flag and none of "
                    f"{list(self._CONVENTIONAL_DRAFTS_NAMES)} present)."
                )
            client.append(folder, raw_message, flags=[DRAFT])
            return folder

    @staticmethod
    def _find_drafts_folder(client: IMAPClient) -> str | None:
        """Return the Drafts folder name via the ``\\Drafts`` SPECIAL-USE
        flag (RFC 6154), or None if the server doesn't advertise it."""
        for flags, _delim, name in client.list_folders():
            if b"\\Drafts" in flags:
                if isinstance(name, (bytes, bytearray)):
                    return name.decode("utf-8", errors="replace")
                return cast(str, name)
        return None

    def _find_drafts_by_convention(self, client: IMAPClient) -> str | None:
        """Fall back to conventional Drafts names for servers that don't
        advertise SPECIAL-USE ``\\Drafts``. First match wins in
        :attr:`_CONVENTIONAL_DRAFTS_NAMES` order."""
        present: set[str] = set()
        for _flags, _delim, name in client.list_folders():
            if isinstance(name, (bytes, bytearray)):
                present.add(name.decode("utf-8", errors="replace"))
            else:
                present.add(name)
        for candidate in self._CONVENTIONAL_DRAFTS_NAMES:
            if candidate in present:
                return candidate
        return None

    def append_sent_copy(
        self, raw_message: bytes, *, answered: bool = False
    ) -> str:
        """APPEND a copy of an already-sent message to the Sent folder.

        Restores the Sent-mailbox copy that the SMTP send path (#322)
        dropped: Mail.app's AppleScript ``tell theMessage to send`` used to
        save a copy into Sent as a side effect, but the direct ``smtplib``
        submission bypasses that. This mirrors :meth:`append_draft` (#245)
        — same IMAP-APPEND transport, targeting the Sent folder instead of
        Drafts and flagging the copy as already-read (issue #406).

        The copy is flagged ``\\Seen`` (outgoing mail is not "unread" to its
        own sender) and, when ``answered`` is true, ``\\Answered`` — matching
        the flag a normal reply carries in the Sent mailbox.

        The Sent folder is resolved by the RFC 6154 ``\\Sent`` SPECIAL-USE
        flag first (Gmail advertises it, so ``[Gmail]/Sent Mail`` is found
        without hard-coding), then a conventional-name fallback that covers
        iCloud's ``Sent Messages`` and other providers.

        Args:
            raw_message: The serialized RFC 822 message that was sent (from
                ``build_draft_mime``; still carries any ``Bcc`` header, which
                is intentional — the Sent copy records blind recipients, as
                Mail.app's own Sent copy does).
            answered: True when the sent message was itself a reply.

        Returns:
            The Sent folder name the copy was APPENDed to.

        Raises:
            MailMessageNotFoundError: No Sent folder discoverable via
                SPECIAL-USE or conventional names. Callers treat this (like
                any failure here) as best-effort — the message is already
                delivered — and must not surface it as a send failure.
            IMAPClientError: Protocol-level APPEND failure.
        """
        flags: list[bytes] = [_FLAG_SEEN]
        if answered:
            flags.append(_FLAG_ANSWERED)
        with self._session() as client:
            folder = self._find_sent_folder(
                client
            ) or self._find_sent_by_convention(client)
            if folder is None:
                raise MailMessageNotFoundError(
                    f"No Sent folder found on {self._host} "
                    f"(no \\Sent SPECIAL-USE flag and none of "
                    f"{list(self._CONVENTIONAL_SENT_NAMES)} present)."
                )
            client.append(folder, raw_message, flags=flags)
            return folder

    def _find_sent_by_convention(self, client: IMAPClient) -> str | None:
        """Fall back to conventional Sent names for servers that don't
        advertise SPECIAL-USE ``\\Sent`` (e.g. iCloud's ``Sent Messages``).
        First match wins in :attr:`_CONVENTIONAL_SENT_NAMES` order (#406)."""
        present: set[str] = set()
        for _flags, _delim, name in client.list_folders():
            if isinstance(name, (bytes, bytearray)):
                present.add(name.decode("utf-8", errors="replace"))
            else:
                present.add(name)
        for candidate in self._CONVENTIONAL_SENT_NAMES:
            if candidate in present:
                return candidate
        return None

    def delete_messages(
        self,
        message_ids: list[str],
        source_mailbox: str,
    ) -> int:
        """Move messages to the account's Trash folder via IMAP. Issue #150.

        Mirrors :meth:`move_messages`'s capability dispatch but resolves
        the destination by discovering the server's Trash folder:

        - Prefer RFC 6154 SPECIAL-USE ``\\Trash``.
        - Fall back to a hard-coded list of conventional names
          (``Trash``, ``[Gmail]/Trash``, ``Deleted Messages``,
          ``Deleted Items``) by scanning the folder list.
        - Raise :class:`MailImapTrashNotFoundError` when neither finds
          anything; the orchestrator falls back to AppleScript.

        Same MOVE / UIDPLUS dispatch and partial-success semantics as
        :meth:`move_messages` (Message-IDs that don't resolve to a UID
        are silently skipped).

        Args:
            message_ids: RFC 5322 Message-IDs, with or without surrounding
                ``<>``.
            source_mailbox: Mailbox the messages currently live in.

        Returns:
            Count of resolved + moved messages.

        Raises:
            MailImapMoveUnsupportedError: Server advertises neither MOVE
                nor UIDPLUS.
            MailImapTrashNotFoundError: No Trash folder discoverable via
                SPECIAL-USE or conventional names.
            IMAPClientError: Protocol-level failure.
        """
        _validate_message_ids(message_ids)
        _reject_control_chars(source_mailbox, "source_mailbox")

        with self._session() as client:
            # #199 / #198: do all LIST traffic in AUTHENTICATED state.
            # Some servers (Exchange Online, older Dovecot) issue an
            # implicit CLOSE when LIST runs while a mailbox is SELECTED,
            # causing the subsequent SEARCH to fail with "No mailbox
            # selected". Resolve capabilities + Trash before SELECT so
            # all SELECT-state work happens in one uninterrupted window.
            has_move = self._has_capability(client, b"MOVE")
            has_uidplus = self._has_capability(client, b"UIDPLUS")
            if not has_move and not has_uidplus:
                raise MailImapMoveUnsupportedError(
                    f"IMAP server at {self._host} advertises neither MOVE "
                    f"(RFC 6851) nor UIDPLUS (RFC 4315); cannot perform a "
                    f"safe scoped move to Trash"
                )

            trash = self._find_trash_folder(client)
            if trash is None:
                trash = self._find_trash_by_convention(client)
            if trash is None:
                raise MailImapTrashNotFoundError(
                    f"IMAP server at {self._host} has no \\Trash "
                    f"SPECIAL-USE folder and none of "
                    f"{list(self._CONVENTIONAL_TRASH_NAMES)} were "
                    f"present in the folder listing"
                )

            client.select_folder(source_mailbox, readonly=False)

            uids = self._resolve_uids_batch(client, message_ids)
            if not uids:
                return 0

            if has_move:
                client.move(uids, trash)
            else:
                client.copy(uids, trash)
                client.add_flags(uids, [b"\\Deleted"], silent=True)
                client.uid_expunge(uids)
            return len(uids)

    def set_read_status(
        self,
        message_ids: list[str],
        source_mailbox: str,
        read: bool,
    ) -> int:
        """Mark messages as read (read=True) or unread (read=False) via
        IMAP UID STORE on the \\Seen flag. Issue #151.

        \\Seen is RFC 3501 base IMAP — no capability negotiation
        needed, works against every server. Idempotent at the server
        level (re-setting an already-set flag is a no-op), matching
        the AppleScript path's behavior.

        Args:
            message_ids: RFC 5322 Message-IDs, with or without
                surrounding angle brackets.
            source_mailbox: Mailbox the messages live in.
            read: True to mark read (+\\Seen), False to mark unread
                (-\\Seen).

        Returns:
            Count of resolved + updated messages. Message-IDs that
            don't resolve to a UID are silently skipped (matches
            AppleScript).

        Raises:
            IMAPClientError: SELECT / SEARCH / STORE failed at the
                protocol level.
        """
        _validate_message_ids(message_ids)
        _reject_control_chars(source_mailbox, "source_mailbox")

        with self._session() as client:
            client.select_folder(source_mailbox, readonly=False)

            uids = self._resolve_uids_batch(client, message_ids)
            if not uids:
                return 0

            if read:
                client.add_flags(uids, [b"\\Seen"], silent=True)
            else:
                client.remove_flags(uids, [b"\\Seen"], silent=True)
            return len(uids)

    def set_flagged_status(
        self,
        message_ids: list[str],
        source_mailbox: str,
        flagged: bool,
    ) -> int:
        """Set or clear the IMAP \\Flagged flag via UID STORE. Issue #152.

        Like \\Seen in #151, \\Flagged is RFC 3501 base IMAP — no
        capability negotiation needed, works against every server.
        Idempotent at the server level (re-setting an already-set flag
        is a no-op), matching the AppleScript path's behavior.

        Note that Mail.app's flag-color attributes (\\$MailFlagBit*
        user keywords) are Mail.app-specific and not in scope here.
        Callers who want a specific color must use the AppleScript
        path via ``update_message(flag_color=...)``. The bare
        \\Flagged set by this method renders as the default (red) flag
        in Mail.app — identical to today's ``update_message(flagged=True)``
        AppleScript path which sets ``flag index = 0`` and produces
        the same bare \\Flagged on the server (verified empirically
        against Gmail/Mail.app, 2026-05-12).

        Args:
            message_ids: RFC 5322 Message-IDs, with or without
                surrounding angle brackets.
            source_mailbox: Mailbox the messages live in.
            flagged: True to set \\Flagged, False to clear it.

        Returns:
            Count of resolved + updated messages. Message-IDs that
            don't resolve to a UID are silently skipped (matches
            AppleScript).

        Raises:
            IMAPClientError: SELECT / SEARCH / STORE failed at the
                protocol level.
        """
        _validate_message_ids(message_ids)
        _reject_control_chars(source_mailbox, "source_mailbox")

        with self._session() as client:
            client.select_folder(source_mailbox, readonly=False)

            uids = self._resolve_uids_batch(client, message_ids)
            if not uids:
                return 0

            if flagged:
                client.add_flags(uids, [b"\\Flagged"], silent=True)
            else:
                client.remove_flags(uids, [b"\\Flagged"], silent=True)
            return len(uids)

    @staticmethod
    def _has_capability(client: IMAPClient, name: bytes) -> bool:
        """True if ``name`` (e.g. ``b"X-GM-EXT-1"``) is in the post-login
        capability list. IMAPClient caches CAPABILITY across the session;
        this is a local lookup, no round trip."""
        return name in client.capabilities()

    @staticmethod
    def _find_all_mail_folder(client: IMAPClient) -> str | None:
        """Return the Gmail All Mail folder name via the ``\\All``
        SPECIAL-USE flag, or None if not found.

        Hardcoding ``[Gmail]/All Mail`` would break on localized Gmail
        accounts (e.g. ``[Google Mail]/Tutta la posta``); the SPECIAL-USE
        flag is the standard way to find it regardless of label."""
        for flags, _delim, name in client.list_folders():
            if b"\\All" in flags:
                if isinstance(name, (bytes, bytearray)):
                    return name.decode("utf-8", errors="replace")
                return cast(str, name)
        return None

    @staticmethod
    def _find_sent_folder(client: IMAPClient) -> str | None:
        """Return the Sent folder name via the ``\\Sent`` SPECIAL-USE flag,
        or None if not found. Used by Tier 1.5 (#125) as the second
        anchor-lookup target after INBOX — covers the common case of a
        thread anchored at a sent message — and by :meth:`append_sent_copy`
        (#406), which falls back to :meth:`_find_sent_by_convention` when
        this returns None."""
        for flags, _delim, name in client.list_folders():
            if b"\\Sent" in flags:
                if isinstance(name, (bytes, bytearray)):
                    return name.decode("utf-8", errors="replace")
                return cast(str, name)
        return None

    @staticmethod
    def _find_trash_folder(client: IMAPClient) -> str | None:
        """Return the Trash folder name via the ``\\Trash`` SPECIAL-USE
        flag (RFC 6154), or None if the server doesn't advertise it.
        Used by ``delete_messages`` (#150); falls back to
        :meth:`_find_trash_by_convention` when this returns None."""
        for flags, _delim, name in client.list_folders():
            if b"\\Trash" in flags:
                if isinstance(name, (bytes, bytearray)):
                    return name.decode("utf-8", errors="replace")
                return cast(str, name)
        return None

    def _find_trash_by_convention(
        self, client: IMAPClient
    ) -> str | None:
        """Fall back to a hard-coded list of conventional Trash names
        for servers that don't advertise SPECIAL-USE ``\\Trash`` (#150).
        Scans the folder listing once and returns the first conventional
        name present, in :attr:`_CONVENTIONAL_TRASH_NAMES` order."""
        present: set[str] = set()
        for _flags, _delim, name in client.list_folders():
            if isinstance(name, (bytes, bytearray)):
                present.add(name.decode("utf-8", errors="replace"))
            else:
                present.add(name)
        for candidate in self._CONVENTIONAL_TRASH_NAMES:
            if candidate in present:
                return candidate
        return None

    @staticmethod
    def _merge_envelope_fetch_into(
        fetched: dict[int, dict[bytes, Any]],
        collected: dict[str, dict[str, Any]],
    ) -> None:
        """Merge a ``client.fetch([ENVELOPE, FLAGS])`` response into the
        cross-mailbox ``collected`` dict keyed by stripped
        rfc_message_id. Skips entries with no envelope or no
        message_id; first occurrence per id wins (dedup across
        mailboxes).

        Shared between Tier 1.5 (``_thread_via_xgm_per_mailbox``) and
        Tier 2 (``_thread_via_imap_thread``) — both run the identical
        per-mailbox loop after FETCH. (#194 / #195)
        """
        for fetch_entry in fetched.values():
            envelope = fetch_entry.get(b"ENVELOPE")
            if envelope is None:
                continue
            raw_msgid = getattr(envelope, "message_id", None)
            if not raw_msgid:
                continue
            clean_msgid = _strip_brackets(_decode(raw_msgid))
            if clean_msgid in collected:
                continue
            fetch_flags = tuple(fetch_entry.get(b"FLAGS", ()) or ())
            collected[clean_msgid] = _envelope_to_dict(
                envelope, fetch_flags
            )

    def _thread_via_xgm_thrid(
        self,
        client: IMAPClient,
        anchor_rfc_message_id: str,
    ) -> list[dict[str, Any]] | None:
        """Tier 1 (Gmail X-GM-THRID) implementation. Returns thread member
        dicts on success, OR ``None`` when the strategy can't run (no
        ``\\All`` folder, anchor not in All Mail, X-GM-THRID query
        rejected mid-flight). On None, the caller falls through to Tier 3.

        Returning None (rather than raising) keeps the dispatcher's
        control flow clean — exceptions are reserved for failures the
        orchestrator's ``_IMAP_FALLBACK_EXCS`` should observe."""
        all_mail = self._find_all_mail_folder(client)
        if all_mail is None:
            return None

        client.select_folder(all_mail, readonly=True)

        bracketed = _bracket_message_id(anchor_rfc_message_id)
        try:
            anchor_uids = client.search(["HEADER", "Message-ID", bracketed])
        except IMAPClientError:
            return None
        if not anchor_uids:
            return None

        try:
            thrid_fetch = client.fetch(anchor_uids[:1], [b"X-GM-THRID"])
        except IMAPClientError:
            return None
        thrid_entry: dict[bytes, Any] = next(iter(thrid_fetch.values()), {})
        thrid = thrid_entry.get(b"X-GM-THRID")
        if thrid is None:
            return None

        try:
            thread_uids = client.search(["X-GM-THRID", str(thrid)])
        except IMAPClientError:
            return None
        if not thread_uids:
            return None

        fetched = client.fetch(thread_uids, [b"ENVELOPE", b"FLAGS"])

        collected: dict[str, dict[str, Any]] = {}
        for fetch_entry in fetched.values():
            envelope = fetch_entry.get(b"ENVELOPE")
            if envelope is None:
                continue
            raw_msgid = getattr(envelope, "message_id", None)
            if not raw_msgid:
                continue
            clean_msgid = _strip_brackets(_decode(raw_msgid))
            if clean_msgid in collected:
                continue
            flags = tuple(fetch_entry.get(b"FLAGS", ()) or ())
            collected[clean_msgid] = _envelope_to_dict(envelope, flags)

        return sorted(
            collected.values(),
            key=lambda m: m.get("date_received") or "",
        )

    def _thread_via_xgm_per_mailbox(
        self,
        client: IMAPClient,
        anchor_rfc_message_id: str,
    ) -> list[dict[str, Any]] | None:
        """Tier 1.5 (Gmail X-GM-THRID per-mailbox, #125).

        Used when ``X-GM-EXT-1`` is advertised but ``[Gmail]/All Mail`` is
        hidden over IMAP (Gmail Settings → Forwarding and POP/IMAP →
        Folder size limits). The per-mailbox X-GM-THRID search covers
        the same conversation-grouping the All Mail path would, just with
        ~M*2 round-trips instead of ~5.

        Returns ``None`` (not raise) when the strategy can't find the
        anchor or the X-GM-THRID query gets rejected — dispatcher then
        falls through to Tier 2 / Tier 3.
        """
        # Step 1: locate the anchor's UID. Try INBOX first, then \\Sent.
        bracketed = _bracket_message_id(anchor_rfc_message_id)
        anchor_uid: int | None = None
        for folder_name in self._anchor_lookup_folders(client):
            try:
                client.select_folder(folder_name, readonly=True)
            except IMAPClientError:
                continue
            try:
                uids = client.search(["HEADER", "Message-ID", bracketed])
            except IMAPClientError:
                continue
            if uids:
                anchor_uid = uids[0]
                break

        if anchor_uid is None:
            # Anchor not in INBOX or Sent → fall through to Tier 2 / Tier 3.
            return None

        # Step 2: get the anchor's X-GM-THRID.
        try:
            thrid_fetch = client.fetch([anchor_uid], [b"X-GM-THRID"])
        except IMAPClientError:
            return None
        thrid_entry: dict[bytes, Any] = next(iter(thrid_fetch.values()), {})
        thrid = thrid_entry.get(b"X-GM-THRID")
        if thrid is None:
            return None

        # Step 3: iterate every selectable folder, collect cluster UIDs by
        # X-GM-THRID, FETCH ENVELOPE+FLAGS for matches.
        collected: dict[str, dict[str, Any]] = {}
        thrid_str = str(thrid)
        for flags, _delim, raw_name in client.list_folders():
            if b"\\Noselect" in flags:
                continue
            if isinstance(raw_name, (bytes, bytearray)):
                folder_name = raw_name.decode("utf-8", errors="replace")
            else:
                folder_name = cast(str, raw_name)
            try:
                client.select_folder(folder_name, readonly=True)
            except IMAPClientError as exc:
                logger.debug(
                    "Tier 1.5: skipping mailbox %s (SELECT rejected): %s",
                    folder_name, exc,
                )
                continue
            try:
                uids = client.search(["X-GM-THRID", thrid_str])
            except IMAPClientError as exc:
                logger.debug(
                    "Tier 1.5: skipping mailbox %s (X-GM-THRID search "
                    "rejected): %s",
                    folder_name, exc,
                )
                continue
            if not uids:
                continue
            try:
                fetched = client.fetch(uids, [b"ENVELOPE", b"FLAGS"])
            except IMAPClientError:
                continue
            self._merge_envelope_fetch_into(fetched, collected)

        return sorted(
            collected.values(),
            key=lambda m: m.get("date_received") or "",
        )

    def _anchor_lookup_folders(
        self, client: IMAPClient
    ) -> Iterator[str]:
        """Yield folder names to try in order when looking up the
        anchor's UID for Tier 1.5: INBOX first, then ``\\Sent`` (if
        distinct). Covers ~95% of cases per #125."""
        yield "INBOX"
        sent = self._find_sent_folder(client)
        if sent and sent != "INBOX":
            yield sent

    def _thread_via_imap_thread(
        self,
        client: IMAPClient,
        anchor_rfc_message_id: str,
        anchor_references: list[str],
    ) -> list[dict[str, Any]] | None:
        """Tier 2 (RFC 5256 THREAD, #123).

        Used when the server advertises ``THREAD=REFERENCES`` or
        ``THREAD=REFS`` (Fastmail, some Dovecot deployments). Per
        mailbox: SELECT, narrow-search for any UID containing the
        anchor's Message-ID or referencing it, then THREAD on the
        full mailbox to collect the cluster(s) those UIDs belong to,
        then FETCH ENVELOPE+FLAGS.

        Returns ``None`` (not raise) when THREAD gets rejected
        mid-flight — dispatcher then falls through to Tier 3.
        """
        bracketed = _bracket_message_id(anchor_rfc_message_id)
        # Pick the algorithm name the server actually advertises.
        if self._has_capability(client, b"THREAD=REFERENCES"):
            algo = "REFERENCES"
        elif self._has_capability(client, b"THREAD=REFS"):
            algo = "REFS"
        else:
            return None

        collected: dict[str, dict[str, Any]] = {}
        for flags, _delim, raw_name in client.list_folders():
            if b"\\Noselect" in flags:
                continue
            if isinstance(raw_name, (bytes, bytearray)):
                folder_name = raw_name.decode("utf-8", errors="replace")
            else:
                folder_name = cast(str, raw_name)
            try:
                client.select_folder(folder_name, readonly=True)
            except IMAPClientError as exc:
                logger.debug(
                    "Tier 2: skipping mailbox %s (SELECT rejected): %s",
                    folder_name, exc,
                )
                continue
            # Narrow-search: anchor UID + sibling-replies in this mailbox.
            try:
                anchor_uids = client.search(
                    ["HEADER", "Message-ID", bracketed]
                )
                ref_uids = client.search(
                    ["HEADER", "References", bracketed]
                )
            except IMAPClientError:
                continue
            relevant_uids = set(anchor_uids) | set(ref_uids)
            if not relevant_uids:
                continue
            # Run THREAD; walk tree for clusters intersecting relevant_uids.
            try:
                tree = client.thread(
                    algorithm=algo, criteria="ALL", charset="UTF-8"
                )
            except IMAPClientError as exc:
                logger.debug(
                    "Tier 2: THREAD rejected on mailbox %s: %s. "
                    "Falling through to Tier 3.",
                    folder_name, exc,
                )
                # Mid-flight rejection — fall through to Tier 3 BFS,
                # which produces a *complete* thread by re-walking via
                # header search.
                #
                # Why THREAD-failure aborts and SELECT/search/fetch-
                # failures just `continue` (#172):
                #
                # - SELECT/search/fetch failures are bounded to one
                #   mailbox. Skipping that mailbox loses a few known-
                #   relevant UIDs but doesn't cast doubt on data
                #   already collected from earlier mailboxes.
                # - THREAD failure indicates capability-level
                #   unreliability: the server advertised THREAD but
                #   can't actually perform it on at least some
                #   mailboxes. That means THREAD output from earlier
                #   mailboxes may also have been silently wrong
                #   (truncated trees, missing references). Falling
                #   through to Tier 3 BFS re-validates the whole
                #   thread.
                #
                # We intentionally discard any partial `collected`
                # from earlier mailboxes here.
                return None
            cluster_uids: set[int] = set()
            for cluster in _flatten_thread_clusters(tree):
                if cluster & relevant_uids:
                    cluster_uids |= cluster
            if not cluster_uids:
                continue
            try:
                fetched = client.fetch(
                    sorted(cluster_uids), [b"ENVELOPE", b"FLAGS"]
                )
            except IMAPClientError:
                continue
            self._merge_envelope_fetch_into(fetched, collected)

        if not collected:
            # No mailbox in the account had any matching message — Tier 2
            # didn't help. Fall through.
            return None

        return sorted(
            collected.values(),
            key=lambda m: m.get("date_received") or "",
        )

    @staticmethod
    def _find_thread_members_bfs(
        client: IMAPClient,
        anchor_rfc_message_id: str,
        anchor_references: list[str],
    ) -> list[dict[str, Any]]:
        """Tier 3: per-mailbox header-search BFS. Universal fallback —
        works against any IMAP server that supports basic SEARCH HEADER
        (RFC 3501).

        For each mailbox, searches the Message-ID / In-Reply-To /
        References headers for every known thread ID (the anchor plus
        its known ancestors). A single pass suffices because well-formed
        replies copy the entire References chain of their parent — so
        searching on the anchor's Message-ID against the References
        header captures all descendants regardless of tree depth.

        Cost: M × N × 3 SEARCHes + M SELECTs + up to M FETCHes, where
        M is mailbox count and N is the size of the known-IDs set.
        Slow on accounts with many mailboxes; #122 (X-GM-THRID) is the
        Gmail-specific optimization, #123 (RFC 5256 THREAD) the more
        general one."""
        known_ids: set[str] = {anchor_rfc_message_id} | set(anchor_references)
        mailboxes = client.list_folders()
        collected: dict[str, dict[str, Any]] = {}

        for _flags, _delimiter, raw_name in mailboxes:
            # imapclient returns names as str when its decoder succeeds,
            # bytes/bytearray on failure. Coerce to str either way.
            if isinstance(raw_name, (bytes, bytearray)):
                mailbox_name = raw_name.decode("utf-8", errors="replace")
            else:
                mailbox_name = raw_name

            try:
                client.select_folder(mailbox_name, readonly=True)
            except IMAPClientError as exc:
                # Some mailboxes (e.g. Gmail smart labels) reject SELECT.
                # Matches the AppleScript path's precedent of skipping
                # them silently.
                logger.debug(
                    "find_thread_members: skipping mailbox %s: %s",
                    mailbox_name, exc,
                )
                continue

            # Search for each known id across three header types. IMAP
            # returns UIDs whose specified header contains the given
            # substring; each search is server-side and indexed.
            uids_found: set[int] = set()
            for id_ in known_ids:
                id_quoted = _bracket_message_id(id_)
                for header in ("Message-ID", "In-Reply-To", "References"):
                    try:
                        uids = client.search(["HEADER", header, id_quoted])
                    except IMAPClientError as exc:
                        logger.debug(
                            "find_thread_members: search failed in %s for "
                            "%s=%s: %s",
                            mailbox_name, header, id_quoted, exc,
                        )
                        continue
                    uids_found.update(uids)

            if not uids_found:
                continue

            fetched = client.fetch(
                list(uids_found), [b"ENVELOPE", b"FLAGS"]
            )
            for fetch_entry in fetched.values():
                envelope = fetch_entry.get(b"ENVELOPE")
                if envelope is None:
                    continue
                raw_msgid = getattr(envelope, "message_id", None)
                if not raw_msgid:
                    continue
                clean_msgid = _strip_brackets(_decode(raw_msgid))
                if clean_msgid in collected:
                    continue
                flags = tuple(fetch_entry.get(b"FLAGS", ()) or ())
                collected[clean_msgid] = _envelope_to_dict(envelope, flags)

        return sorted(
            collected.values(),
            key=lambda m: m.get("date_received") or "",
        )
