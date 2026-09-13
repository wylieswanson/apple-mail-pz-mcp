"""Unit tests for the SMTP submission transport (issue #322).

The mock boundary is ``smtplib`` — the SMTP equivalent of the connector's
``_run_applescript`` / ``IMAPClient`` boundaries. No test opens a real
socket or touches real credentials.
"""

import smtplib
from email import message_from_bytes
from email.policy import default as _default_policy
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_fast_mcp.smtp_sender import SmtpSender


def _raw_with_bcc() -> bytes:
    return (
        b"From: Fred <fred@example.com>\r\n"
        b"To: alice@example.net\r\n"
        b"Cc: carol@example.org\r\n"
        b"Bcc: secret@example.com\r\n"
        b"Subject: Hi\r\n"
        b"\r\n"
        b"Body text.\r\n"
    )


class TestConstructor:
    def test_stores_credentials(self) -> None:
        sender = SmtpSender("smtp.example.com", 587, "u@example.com", "pw")
        assert sender._host == "smtp.example.com"
        assert sender._port == 587
        assert sender._email == "u@example.com"
        assert sender._password == "pw"

    def test_default_timeout(self) -> None:
        sender = SmtpSender("h", 587, "u", "p")
        assert sender._timeout == 30.0

    def test_custom_timeout(self) -> None:
        sender = SmtpSender("h", 587, "u", "p", timeout=5.0)
        assert sender._timeout == 5.0


class TestStartTls:
    @patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP")
    def test_port_587_uses_starttls_then_login_and_send(
        self, mock_smtp: MagicMock
    ) -> None:
        client = mock_smtp.return_value.__enter__.return_value
        sender = SmtpSender("smtp.example.com", 587, "u@example.com", "pw")

        sender.send(_raw_with_bcc(), ["alice@example.net", "secret@example.com"])

        mock_smtp.assert_called_once()
        assert mock_smtp.call_args.args[0] == "smtp.example.com"
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("u@example.com", "pw")
        client.send_message.assert_called_once()

    @patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP")
    def test_envelope_from_is_message_from_not_login_and_recipients_explicit(
        self, mock_smtp: MagicMock
    ) -> None:
        """Regression for #322 / PR #404: the envelope ``MAIL FROM`` is the
        message's own ``From:`` address (``fred@example.com``), NOT the SMTP
        AUTH login (``u@example.com``). Reusing the login as the envelope
        sender is what made custom-domain iCloud reject the send with
        ``550 5.7.0 From address is not one of your addresses``. ``RCPT TO``
        is the caller's explicit recipient list."""
        client = mock_smtp.return_value.__enter__.return_value
        sender = SmtpSender("smtp.example.com", 587, "u@example.com", "pw")
        recipients = ["alice@example.net", "carol@example.org", "secret@example.com"]

        sender.send(_raw_with_bcc(), recipients)

        # AUTH login and envelope-from are two distinct values.
        client.login.assert_called_once_with("u@example.com", "pw")
        kwargs = client.send_message.call_args.kwargs
        assert kwargs["from_addr"] == "fred@example.com"
        assert kwargs["from_addr"] != "u@example.com"
        assert kwargs["to_addrs"] == recipients

    @patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP")
    def test_envelope_from_strips_display_name(
        self, mock_smtp: MagicMock
    ) -> None:
        """A ``Display Name <addr>`` From header yields the bare address for
        the envelope (``_resolve_account_to_sender`` may return that form)."""
        client = mock_smtp.return_value.__enter__.return_value
        raw = (
            b"From: Fred Masi <fred@example.com>\r\n"
            b"To: alice@example.net\r\n"
            b"Subject: Hi\r\n\r\nx\r\n"
        )
        sender = SmtpSender("smtp.example.com", 587, "u@example.com", "pw")

        sender.send(raw, ["alice@example.net"])

        assert client.send_message.call_args.kwargs["from_addr"] == "fred@example.com"

    @patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP")
    def test_explicit_envelope_from_overrides_header(
        self, mock_smtp: MagicMock
    ) -> None:
        """An explicit ``envelope_from`` wins over the ``From:`` header."""
        client = mock_smtp.return_value.__enter__.return_value
        sender = SmtpSender("smtp.example.com", 587, "u@example.com", "pw")

        sender.send(
            _raw_with_bcc(),
            ["alice@example.net"],
            envelope_from="Alias <alias@example.com>",
        )

        assert client.send_message.call_args.kwargs["from_addr"] == "alias@example.com"

    def test_missing_from_and_no_override_raises(self) -> None:
        """No override and no ``From:`` header → an explicit ValueError rather
        than a silent, wrong envelope sender."""
        sender = SmtpSender("h", 587, "u@example.com", "pw")
        with pytest.raises(ValueError, match="envelope-from"):
            sender.send(b"To: a@example.net\r\nSubject: x\r\n\r\nx", ["a@example.net"])

    @patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP")
    def test_bcc_header_stripped_from_wire_message(
        self, mock_smtp: MagicMock
    ) -> None:
        client = mock_smtp.return_value.__enter__.return_value
        sender = SmtpSender("smtp.example.com", 587, "u@example.com", "pw")

        sender.send(_raw_with_bcc(), ["alice@example.net", "secret@example.com"])

        sent_msg = client.send_message.call_args.args[0]
        assert sent_msg["Bcc"] is None
        # To/Cc must still be present on the wire message.
        assert sent_msg["To"] is not None


class TestSsl:
    @patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP_SSL")
    @patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP")
    def test_port_465_uses_ssl_no_starttls(
        self, mock_smtp: MagicMock, mock_ssl: MagicMock
    ) -> None:
        client = mock_ssl.return_value.__enter__.return_value
        sender = SmtpSender("smtp.example.com", 465, "u@example.com", "pw")

        sender.send(_raw_with_bcc(), ["alice@example.net"])

        mock_ssl.assert_called_once()
        mock_smtp.assert_not_called()
        client.starttls.assert_not_called()
        client.login.assert_called_once_with("u@example.com", "pw")
        client.send_message.assert_called_once()


class TestValidation:
    def test_empty_recipients_raises(self) -> None:
        sender = SmtpSender("h", 587, "u", "p")
        with pytest.raises(ValueError, match="recipient"):
            sender.send(b"From: a@example.com\r\n\r\nx", [])

    @patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP")
    def test_wire_message_is_parseable(self, mock_smtp: MagicMock) -> None:
        """Regression guard: the object handed to send_message is a real
        parsed EmailMessage, not the raw bytes."""
        client = mock_smtp.return_value.__enter__.return_value
        sender = SmtpSender("h", 587, "u@example.com", "p")

        sender.send(_raw_with_bcc(), ["alice@example.net"])

        sent = client.send_message.call_args.args[0]
        # Re-serialize round-trips cleanly.
        reparsed = message_from_bytes(sent.as_bytes(), policy=_default_policy)
        assert reparsed["Subject"] == "Hi"


class TestTeardownAfterAccept:
    """PR #404: once the server has accepted the message (``send_message``
    returned), a failure during session teardown — most notably a non-221
    reply to ``QUIT`` that ``SMTP.__exit__`` raises as ``SMTPResponseException``
    — must NOT propagate, or the caller would fall back to AppleScript and
    send a duplicate. Failures *before* acceptance must still propagate."""

    @patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP")
    def test_non_221_quit_after_accept_is_swallowed(
        self, mock_smtp: MagicMock
    ) -> None:
        """DATA→250 then a non-221 QUIT: ``send`` returns normally (no
        exception surfaces), so the caller never retries via AppleScript."""
        client = mock_smtp.return_value.__enter__.return_value
        # Exiting the `with` (post-accept) raises, as real __exit__ does on a
        # non-221 QUIT reply.
        mock_smtp.return_value.__exit__.side_effect = smtplib.SMTPResponseException(
            421, b"4.7.0 closing transmission channel"
        )
        sender = SmtpSender("smtp.example.com", 587, "u@example.com", "pw")

        # Must not raise — the message was accepted.
        sender.send(_raw_with_bcc(), ["alice@example.net"])

        client.send_message.assert_called_once()

    @patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP_SSL")
    @patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP")
    def test_non_221_quit_after_accept_is_swallowed_ssl(
        self, mock_smtp: MagicMock, mock_ssl: MagicMock
    ) -> None:
        """Same guarantee on the implicit-TLS (:465) path."""
        client = mock_ssl.return_value.__enter__.return_value
        mock_ssl.return_value.__exit__.side_effect = smtplib.SMTPResponseException(
            421, b"4.7.0 closing transmission channel"
        )
        sender = SmtpSender("smtp.example.com", 465, "u@example.com", "pw")

        sender.send(_raw_with_bcc(), ["alice@example.net"])

        client.send_message.assert_called_once()

    @patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP")
    def test_send_message_failure_before_accept_propagates(
        self, mock_smtp: MagicMock
    ) -> None:
        """A pre-acceptance failure (DATA rejected) must propagate so the
        caller can fall back to AppleScript — the message was never sent."""
        client = mock_smtp.return_value.__enter__.return_value
        client.send_message.side_effect = smtplib.SMTPRecipientsRefused(
            {"alice@example.net": (550, b"5.1.1 no such user")}
        )
        sender = SmtpSender("smtp.example.com", 587, "u@example.com", "pw")

        with pytest.raises(smtplib.SMTPException):
            sender.send(_raw_with_bcc(), ["alice@example.net"])

    @patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP")
    def test_auth_failure_before_accept_propagates(
        self, mock_smtp: MagicMock
    ) -> None:
        """An AUTH failure is pre-acceptance and must propagate."""
        client = mock_smtp.return_value.__enter__.return_value
        client.login.side_effect = smtplib.SMTPAuthenticationError(
            535, b"bad creds"
        )
        sender = SmtpSender("smtp.example.com", 587, "u@example.com", "pw")

        with pytest.raises(smtplib.SMTPException):
            sender.send(_raw_with_bcc(), ["alice@example.net"])
        client.send_message.assert_not_called()
