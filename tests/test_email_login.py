"""Login by email code: the channel Telegram still delivers to third-party apps.

Why this exists, in the order the problem was discovered:

1. **OTP never arrives.** ``sendCode`` answers ``SentCodeTypeApp`` with a valid
   hash and nothing is ever delivered — SMS was withdrawn from third-party
   clients in Feb 2023, and since early 2026 the in-app code is often withheld
   too (Telethon #4730).

2. **The one-tap deep link cannot work.** A user's own screenshot of tapping it
   shows Telegram's answer: "This code can be used to allow someone to log in to
   your Telegram account. To confirm Telegram login, please go to Settings >
   Devices > Scan QR and scan the code." TDLib documents that as deliberate:
   ``internalLinkTypeQrCodeAuthentication`` — "it must be scanned from QR-code
   using in-app camera". So the QR route always needs a camera and a second
   screen, on every client, and no link can bypass it.

3. **Email is the answer.** Telegram's authorization docs list
   ``auth.sentCodeTypeEmailCode`` as a first-class delivery channel for
   third-party apps, and ``account.sendVerifyEmailCode`` with an
   ``emailVerifyPurposeLoginSetup`` purpose is legal *during* a login. Telegram's
   own mail servers send it, so nothing depends on an app session or an SMS
   gateway.

These tests pin that flow, including the part that is easy to get wrong:
``account.verifyEmail`` does **not** log you in — it returns the next
``auth.sentCode``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.tl import types

from qna_bot.userbot.login import (
    LoginStatus,
    PhoneLoginSession,
    describe_delivery,
)


def _sent(phone_code_hash: str = "hash-a", *, length: int = 5) -> types.auth.SentCode:
    return types.auth.SentCode(
        type=types.auth.SentCodeTypeApp(length=length), phone_code_hash=phone_code_hash
    )


def _client_with_login() -> MagicMock:
    client = MagicMock()
    client.is_connected = MagicMock(return_value=True)
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.send_code_request = AsyncMock(return_value=_sent())
    client.sign_in = AsyncMock()
    client.session = MagicMock()
    client.session.save = MagicMock(return_value="1BVtsOK_fake")
    return client


async def _pending_session(monkeypatch, client) -> PhoneLoginSession:
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)
    session = PhoneLoginSession()
    res = await session.request_code(12345, "abc123", "+251912345678")
    assert res.status is LoginStatus.CODE_SENT
    return session


# ---------------------------------------------------------------------------
# Asking Telegram to route the code to an inbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_switch_sends_a_verify_email_request(monkeypatch) -> None:
    client = _client_with_login()
    sent_email = types.account.SentEmailCode(email_pattern="y***@gmail.com", length=6)
    client.side_effect = AsyncMock(return_value=sent_email)
    session = await _pending_session(monkeypatch, client)
    try:
        res = await session.use_login_email("you@gmail.com")
        assert res.status is LoginStatus.CODE_SENT
        assert "y***@gmail.com" in res.message
        assert "6 digits" in res.message
        assert session.pending is not None
        assert session.pending.email_setup_length == 6
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_email_switch_rejects_a_non_address(monkeypatch) -> None:
    client = _client_with_login()
    session = await _pending_session(monkeypatch, client)
    client.side_effect = AsyncMock()
    try:
        res = await session.use_login_email("not-an-email")
        assert res.status is LoginStatus.ERROR
        assert "email" in res.message.lower()
        client.side_effect.assert_not_called()
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_email_switch_needs_a_login_in_progress() -> None:
    session = PhoneLoginSession()
    res = await session.use_login_email("you@gmail.com")
    assert res.status is LoginStatus.ERROR
    assert "phone number" in res.message.lower()


@pytest.mark.asyncio
async def test_email_switch_reports_telegram_refusal_by_name(monkeypatch) -> None:
    """The fix differs per refusal, so Telegram's own reason must survive."""
    client = _client_with_login()

    class EmailNotAllowedError(Exception):
        pass

    client.side_effect = AsyncMock(side_effect=EmailNotAllowedError("nope"))
    session = await _pending_session(monkeypatch, client)
    try:
        res = await session.use_login_email("you@gmail.com")
        assert res.status is LoginStatus.ERROR
        assert "will not accept an email code" in res.message
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_email_switch_flood_wait_keeps_the_session(monkeypatch) -> None:
    client = _client_with_login()
    client.side_effect = AsyncMock(side_effect=FloodWaitError(request=None, capture=90))
    session = await _pending_session(monkeypatch, client)
    try:
        res = await session.use_login_email("you@gmail.com")
        assert res.status is LoginStatus.ERROR
        assert res.retry_after == 90
        assert session.pending is not None
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# verifyEmail returns the NEXT sentCode, not a finished login
# ---------------------------------------------------------------------------


def _verified_login(next_hash: str = "hash-email") -> types.account.EmailVerifiedLogin:
    return types.account.EmailVerifiedLogin(
        email="you@gmail.com",
        sent_code=types.auth.SentCode(
            type=types.auth.SentCodeTypeEmailCode(email_pattern="y***@gmail.com", length=5),
            phone_code_hash=next_hash,
        ),
    )


@pytest.mark.asyncio
async def test_email_code_verification_asks_for_the_login_code(monkeypatch) -> None:
    """The subtle part: verifying the email does not sign you in.

    Telegram answers with a fresh ``auth.sentCode`` — the code for the next step.
    Treating that as success would leave the owner logged out and confused.
    """
    client = _client_with_login()
    client.side_effect = AsyncMock(return_value=_verified_login())
    session = await _pending_session(monkeypatch, client)
    try:
        await session.use_login_email("you@gmail.com")
        res = await session.submit_email_login_code("123456")

        assert res.status is LoginStatus.CODE_SENT
        assert "Email verified" in res.message
        assert session.pending is not None
        # The stale hash must be replaced or the next sign_in fails.
        assert session.pending.phone_code_hash == "hash-email"
        assert session.pending.delivery is not None
        assert session.pending.delivery.length == 5
        # Nothing was finalized silently.
        assert session.pending is not None
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_email_code_wrong_length_is_caught_locally(monkeypatch) -> None:
    client = _client_with_login()
    sent_email = types.account.SentEmailCode(email_pattern="y***@gmail.com", length=6)
    client.side_effect = AsyncMock(return_value=sent_email)
    session = await _pending_session(monkeypatch, client)
    try:
        await session.use_login_email("you@gmail.com")
        assert session.pending is not None
        assert session.pending.email_setup_length == 6
        client.side_effect = AsyncMock()
        res = await session.submit_email_login_code("123")  # 6 expected
        assert res.status is LoginStatus.ERROR
        assert "6 digits" in res.message
        client.side_effect.assert_not_called()
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_email_code_surfaces_2fa(monkeypatch) -> None:
    client = _client_with_login()
    client.side_effect = AsyncMock(side_effect=SessionPasswordNeededError(request=None))
    session = await _pending_session(monkeypatch, client)
    try:
        await session.use_login_email("you@gmail.com")
        res = await session.submit_email_login_code("123456")
        assert res.status is LoginStatus.TWO_FA_REQUIRED
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_email_code_reports_a_bad_code(monkeypatch) -> None:
    from telethon.errors import CodeInvalidError

    client = _client_with_login()
    client.side_effect = AsyncMock(side_effect=CodeInvalidError(request=None))
    session = await _pending_session(monkeypatch, client)
    try:
        await session.use_login_email("you@gmail.com")
        res = await session.submit_email_login_code("999999")
        assert res.status is LoginStatus.ERROR
        assert "code" in res.message.lower()
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# The delivery descriptor for an email code
# ---------------------------------------------------------------------------


def test_email_delivery_is_described_as_an_inbox() -> None:
    delivery = describe_delivery(
        types.auth.SentCode(
            type=types.auth.SentCodeTypeEmailCode(email_pattern="y***@gmail.com", length=5),
            phone_code_hash="h",
        )
    )
    assert "email" in delivery.title.lower()
    assert "y***@gmail.com" in delivery.detail
    assert delivery.length == 5
    assert delivery.action_required is False


def test_email_setup_required_channel_is_distinct() -> None:
    """``SetUpEmailRequired`` means *nothing* was sent; it is not an email code."""
    from qna_bot.userbot.login import DeliveryChannel

    delivery = describe_delivery(
        types.auth.SentCode(
            type=types.auth.SentCodeTypeSetUpEmailRequired(), phone_code_hash="h"
        )
    )
    assert delivery.channel is DeliveryChannel.SETUP_REQUIRED
    assert delivery.action_required is True
    assert "login email" in delivery.detail.lower()


# ---------------------------------------------------------------------------
# Telegram's refusals, each with the fix that matches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_refusals_are_named_not_generic(monkeypatch) -> None:
    """A generic "it failed" is what made this bug survive three attempts."""
    from telethon.errors import EmailInvalidError, EmailVerifyExpiredError

    client = _client_with_login()
    session = await _pending_session(monkeypatch, client)
    try:
        client.side_effect = AsyncMock(side_effect=EmailInvalidError(request=None))
        # A well-formed address so the local check passes and Telegram's verdict is
        # what gets reported.
        res = await session.use_login_email("you@gmail.com")
        assert "rejected that email address" in res.message

        client.side_effect = AsyncMock(side_effect=EmailVerifyExpiredError(request=None))
        res = await session.use_login_email("you@gmail.com")
        assert "expired" in res.message.lower()
    finally:
        await session.close()


def test_manager_exposes_the_email_login_path() -> None:
    """The wizard talks to the manager, so both methods must exist there."""
    import inspect

    from qna_bot.userbot.manager import UserbotManager

    assert hasattr(UserbotManager, "use_login_email")
    assert hasattr(UserbotManager, "submit_email_login_code")
    params = list(inspect.signature(UserbotManager.use_login_email).parameters)
    assert params == ["self", "email"]
