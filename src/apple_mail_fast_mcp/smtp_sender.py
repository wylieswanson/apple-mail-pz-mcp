"""SMTP submission for wrapper-free immediate send (issue #322).

``create_draft(send_now=True)`` historically routed through Mail.app's
AppleScript ``tell theMessage to send``, whose ``content`` setter wraps the
body in an ``Apple-Mail-URLShareWrapper`` cite-blockquote that renders as a
quote on iOS (Mail.app bug FB11734014, #245). IMAP ``APPEND`` fixed the
draft case (#246/#292) but can only create drafts, never send. This module
submits a clean RFC 822 message (built by
:func:`apple_mail_fast_mcp.draft_builder.build_draft_mime`) over SMTP, so a
direct send never touches the AppleScript ``content`` setter.

Credential model: the account's IMAP app-password (see :mod:`keychain`) is
reused. For every provider we support (iCloud, Gmail, Yahoo, Outlook) the
same app-specific password authenticates both IMAP and SMTP submission, so
we deliberately reuse the existing Keychain entry rather than building a
parallel SMTP credential store (issue #322 "extend the keychain module").

``smtplib`` is the single point of external I/O for this module and the mock
boundary for unit tests, mirroring ``_run_applescript`` (AppleScript) and
``IMAPClient`` (IMAP).
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email import message_from_bytes
from email.message import Message
from email.policy import default as _default_policy
from email.utils import parseaddr

logger = logging.getLogger(__name__)

# Implicit-TLS submission port (RFC 8314). Anything else (587 submission,
# 25) is treated as explicit-TLS: connect cleartext, then STARTTLS.
_SMTP_SSL_PORT = 465

_DEFAULT_TIMEOUT_S = 30.0


class SmtpSender:
    """Submit a pre-built RFC 822 message over authenticated SMTP.

    One instance targets one account's outgoing server. The message bytes
    come from ``build_draft_mime`` (the same builder the clean IMAP draft
    path uses), so message construction is already correct and tested — this
    class only adds the send transport.
    """

    def __init__(
        self,
        host: str,
        port: int,
        email: str,
        password: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        """Store connection parameters.

        Args:
            host: SMTP server hostname (e.g. ``smtp.mail.me.com``).
            port: SMTP submission port. ``465`` uses implicit TLS
                (``SMTP_SSL``); any other value connects cleartext then
                issues ``STARTTLS``.
            email: SMTP AUTH username (the login credential). This is *not*
                necessarily the envelope ``MAIL FROM`` address — for a
                custom-domain iCloud account, or a Gmail/Outlook send-as
                alias, the login and the sending identity legitimately
                differ. The envelope-from is resolved separately in
                :meth:`send`.
            password: App-specific password (shared with IMAP; see module
                docstring).
            timeout: Socket timeout in seconds.
        """
        self._host = host
        self._port = port
        self._email = email
        self._password = password
        self._timeout = timeout

    def send(
        self,
        raw_message: bytes,
        recipients: list[str],
        *,
        envelope_from: str | None = None,
    ) -> None:
        """Authenticate and submit ``raw_message`` to ``recipients``.

        The ``Bcc`` header is stripped from the transmitted message — blind
        recipients are carried only in the envelope ``RCPT TO`` list, never
        on the wire — while the caller-supplied ``recipients`` list is used
        verbatim as the envelope recipients (it already includes any Bcc).

        Envelope ``MAIL FROM`` is the *sender's own address*, kept separate
        from the SMTP AUTH login (``self._email``). The two frequently
        differ: a custom-domain iCloud account authenticates as its Apple ID
        login (e.g. ``appleid@example.com``) but sends *as* its configured
        send-as address (e.g. ``you@example.com``); Gmail and Outlook send-as
        aliases behave the same way. Reusing the login as the envelope sender
        makes iCloud reject the message with ``550 5.7.0 From address is not
        one of your addresses`` (issue #322 / PR #404). When ``envelope_from``
        is ``None`` the address is taken from the message ``From:`` header —
        the same address the recipient sees.

        Once the server accepts the message (``send_message`` returns), any
        error raised while tearing the session down is swallowed rather than
        propagated. The notable case is a non-221 reply to ``QUIT``: on
        exiting the ``with`` block ``smtplib.SMTP.__exit__`` issues ``QUIT``
        and raises ``SMTPResponseException`` for any non-221 reply. Sending
        is non-idempotent, so letting that propagate would make the caller's
        AppleScript fallback deliver a *second* copy (PR #404 review). Errors
        *before* acceptance (connect, AUTH, refused recipient, rejected DATA)
        still propagate so the caller can fall back safely.

        Args:
            raw_message: A serialized RFC 822 message from
                ``build_draft_mime``.
            recipients: The full envelope recipient list (to + cc + bcc).
                Must be non-empty.
            envelope_from: Explicit envelope ``MAIL FROM`` address. When
                ``None``, it is parsed from the message ``From:`` header.

        Raises:
            ValueError: ``recipients`` is empty, or no envelope-from address
                could be resolved (no override and no parseable ``From:``).
            smtplib.SMTPException: An SMTP-level failure *before* the message
                was accepted (auth, refused recipient, protocol error).
                Callers that want graceful AppleScript fallback catch this
                plus ``OSError``.
            OSError: Connection failure (host unreachable, TLS error,
                timeout) before acceptance.
        """
        if not recipients:
            raise ValueError("SMTP send requires at least one recipient")

        msg = message_from_bytes(raw_message, policy=_default_policy)
        # A Bcc header must never travel on the wire; the envelope RCPT list
        # (passed explicitly below) is what actually delivers to blind
        # recipients. ``del`` is a no-op when no Bcc header is present.
        del msg["Bcc"]
        env_from = self._resolve_envelope_from(msg, envelope_from)

        context = ssl.create_default_context()
        accepted = False
        try:
            if self._port == _SMTP_SSL_PORT:
                with smtplib.SMTP_SSL(
                    self._host, self._port, timeout=self._timeout, context=context
                ) as client:
                    self._authenticate_and_send(client, msg, recipients, env_from)
                    accepted = True
            else:
                with smtplib.SMTP(
                    self._host, self._port, timeout=self._timeout
                ) as client:
                    client.ehlo()
                    client.starttls(context=context)
                    client.ehlo()
                    self._authenticate_and_send(client, msg, recipients, env_from)
                    accepted = True
        except (smtplib.SMTPException, OSError):
            # Not yet accepted → a real send failure; propagate so the caller
            # falls back to AppleScript. Already accepted → the message is
            # out; the only thing left to fail is session teardown (e.g. a
            # non-221 QUIT surfaced by ``SMTP.__exit__``). Swallow it — a
            # duplicate AppleScript send would otherwise follow (PR #404).
            if not accepted:
                raise
            logger.warning(
                "SMTP message accepted by %s but session teardown failed; "
                "not retrying, to avoid a duplicate send",
                self._host,
                exc_info=True,
            )

    def _resolve_envelope_from(self, msg: Message, override: str | None) -> str:
        """Resolve the envelope ``MAIL FROM`` address (issue #322 / PR #404).

        Precedence: an explicit ``override``, else the message ``From:``
        header. Either way the bare address is extracted with
        ``email.utils.parseaddr``, so a display-name form (``Name <addr>``)
        yields just ``addr``. This is deliberately independent of the SMTP
        AUTH login (``self._email``): the two legitimately differ for
        custom-domain iCloud and send-as-alias setups.

        Args:
            msg: The parsed message whose ``From:`` header is the fallback
                source of the envelope sender.
            override: An explicit envelope-from address, or ``None``.

        Returns:
            The bare envelope ``MAIL FROM`` address.

        Raises:
            ValueError: Neither an override nor a parseable ``From:`` address
                is available.
        """
        source = override if override else msg.get("From", "")
        address = parseaddr(source)[1]
        if not address:
            raise ValueError(
                "SMTP send requires an envelope-from address "
                "(no override and no parseable From: header)"
            )
        return address

    def _authenticate_and_send(
        self,
        client: smtplib.SMTP,
        msg: Message,
        recipients: list[str],
        envelope_from: str,
    ) -> None:
        """Log in with the AUTH credential and submit the message.

        The SMTP AUTH login (``self._email``) and the envelope ``MAIL FROM``
        (``envelope_from``) are passed as two distinct values — see
        :meth:`send` for why they legitimately differ. ``RCPT TO`` is the
        caller-supplied recipient list.
        """
        client.login(self._email, self._password)
        client.send_message(msg, from_addr=envelope_from, to_addrs=recipients)
        logger.debug(
            "SMTP send via %s:%d as %s to %d recipient(s)",
            self._host,
            self._port,
            envelope_from,
            len(recipients),
        )
