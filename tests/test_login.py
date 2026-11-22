"""Tests for the phone-login engine: number parsing, code-delivery reporting and recovery.

These cover the failures that made the wizard look like "Telegram never sent a
code": wrongly formatted numbers passed straight through, codes delivered
somewhere other than a Telegram chat being described as "sent via Telegram",
stale ``phone_code_hash`` values, and disconnected login clients.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberFloodError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import auth as auth_types

from qna_bot.userbot.login import (
    CodeDelivery,
    DeliveryChannel,
    LoginStatus,
    PhoneIssue,
    PhoneLoginSession,
    describe_delivery,
    parse_phone,
    translate_send_error,
    translate_signin_error,
)

# ---------------------------------------------------------------------------
# Phone normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+251912345678", "+251912345678"),
        ("251912345678", "+251912345678"),
        ("00251912345678", "+251912345678"),
        ("+251 91 234 5678", "+251912345678"),
        ("+251-91-234-5678", "+251912345678"),
        ("+1 (555) 123-4567", "+15551234567"),
        ("  +44 7700 900123  ", "+447700900123"),
        ("tel:+251912345678", "+251912345678"),
    ],
)
def test_parse_phone_accepts_international_formats(raw: str, expected: str) -> None:
    parsed = parse_phone(raw)
    assert parsed.ok, parsed.hint
    assert parsed.e164 == expected


def test_parse_phone_rejects_local_number_with_country_code_hint() -> None:
    """The old flow forwarded '0912345678' to Telegram and returned a generic error."""
    parsed = parse_phone("0912345678")
    assert not parsed.ok
    assert parsed.issue is PhoneIssue.NO_COUNTRY_CODE
    assert "country code" in parsed.hint.lower()
    # Must NOT be guessed as an Indian (+91) number
    assert parsed.e164 == ""


def test_parse_phone_rejects_garbage() -> None:
    assert parse_phone("not a number").issue is PhoneIssue.NOT_A_NUMBER
    assert parse_phone("").issue is PhoneIssue.EMPTY


def test_parse_phone_rejects_too_short_and_too_long() -> None:
    assert parse_phone("+2511").issue is PhoneIssue.TOO_SHORT
    assert parse_phone("+2519123456789012345").issue is PhoneIssue.TOO_LONG


def test_parse_phone_rejects_unknown_country_code() -> None:
    parsed = parse_phone("+999912345678")
    assert not parsed.ok
    assert parsed.issue is PhoneIssue.NO_COUNTRY_CODE


# ---------------------------------------------------------------------------
# Code delivery reporting
# ---------------------------------------------------------------------------


def test_describe_app_delivery_explains_telegram_service_message() -> None:
    """Most real accounts get the code *inside* Telegram, not by SMS."""
    delivery = describe_delivery(auth_types.SentCode(type=auth_types.SentCodeTypeApp(length=5), phone_code_hash="h"))
    assert delivery.channel is DeliveryChannel.TELEGRAM_APP
    assert delivery.length == 5
    text = (delivery.title + delivery.detail).lower()
    assert "service message" in text or "service chat" in text
    assert "sms" in text  # explicitly rules out waiting for an SMS


def test_describe_sms_and_call_and_email_delivery() -> None:
    sms = describe_delivery(auth_types.SentCode(type=auth_types.SentCodeTypeSms(length=5), phone_code_hash="h"))
    assert sms.channel is DeliveryChannel.SMS and sms.length == 5

    call = describe_delivery(auth_types.SentCode(type=auth_types.SentCodeTypeCall(length=4), phone_code_hash="h"))
    assert call.channel is DeliveryChannel.CALL

    email = describe_delivery(
        auth_types.SentCode(
            type=auth_types.SentCodeTypeEmailCode(email_pattern="a***@gmail.com", length=6),
            phone_code_hash="h",
        )
    )
    assert email.channel is DeliveryChannel.EMAIL
    assert "a***@gmail.com" in email.detail


def test_describe_email_setup_required_flags_action() -> None:
    delivery = describe_delivery(
        auth_types.SentCode(type=auth_types.SentCodeTypeSetUpEmailRequired(), phone_code_hash="h")
    )
    assert delivery.channel is DeliveryChannel.SETUP_REQUIRED
    assert delivery.action_required is True
    assert "login email" in delivery.detail.lower()


def test_describe_unknown_delivery_is_still_actionable() -> None:
    delivery = describe_delivery(object())
    assert delivery.channel is DeliveryChannel.UNKNOWN
    assert "resend" in delivery.detail.lower()


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def test_flood_wait_is_reported_with_seconds() -> None:
    res = translate_send_error(FloodWaitError(request=None, capture=42), "+251912345678")
    assert res.status is LoginStatus.ERROR
    assert res.retry_after == 42
    assert "42" in res.message


def test_phone_number_flood_suggests_resend_instead_of_retyping() -> None:
    res = translate_send_error(PhoneNumberFloodError(request=None), "+251912345678")
    assert "too many times" in res.message.lower()
    assert "resend" in res.message.lower()


def test_invalid_number_message_includes_country_code_guidance() -> None:
    res = translate_send_error(PhoneNumberInvalidError(request=None), "+251912345678")
    assert "country code" in res.message.lower()


def test_banned_number_is_explicit() -> None:
    res = translate_send_error(PhoneNumberBannedError(request=None), "+251912345678")
    assert "banned" in res.message.lower()


def test_invalid_api_hash_points_to_my_telegram_org() -> None:
    res = translate_send_error(ApiIdInvalidError(request=None), "+251912345678")
    assert "my.telegram.org" in res.message


def test_signin_errors_are_specific() -> None:
    assert translate_signin_error(PhoneCodeInvalidError(request=None)).status is LoginStatus.ERROR
    assert "wrong login code" in translate_signin_error(PhoneCodeInvalidError(request=None)).message.lower()

    expired = translate_signin_error(PhoneCodeExpiredError(request=None))
    assert "expired" in expired.message.lower()

    two_fa = translate_signin_error(SessionPasswordNeededError(request=None))
    assert two_fa.status is LoginStatus.TWO_FA_REQUIRED

    bad_pw = translate_signin_error(PasswordHashInvalidError(request=None))
    assert "2fa" in bad_pw.message.lower() or "password" in bad_pw.message.lower()


# ---------------------------------------------------------------------------
# Session state machine (fake Telethon client)
# ---------------------------------------------------------------------------


def _fake_client(sent_code: object, *, sign_in_side_effects: list | None = None) -> MagicMock:
    """A stand-in Telethon client. ``__call__`` answers ResendCodeRequest with ``sent_code``."""
    client = MagicMock()
    client.is_connected = MagicMock(return_value=True)
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.send_code_request = AsyncMock(return_value=sent_code)
    client.sign_in = AsyncMock(side_effect=sign_in_side_effects)
    client.session = MagicMock()
    client.session.save = MagicMock(return_value="1BVtsOK_fake_session")
    return client


def _sent_app(phone_code_hash: str = "hash-a", length: int = 5) -> object:
    return auth_types.SentCode(
        type=auth_types.SentCodeTypeApp(length=length), phone_code_hash=phone_code_hash
    )


@pytest.mark.asyncio
async def test_request_code_reports_delivery_and_normalises_phone(monkeypatch) -> None:
    session = PhoneLoginSession()
    client = _fake_client(_sent_app())
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    try:
        res = await session.request_code(12345, "abc123", "0912345678")
        # Local number is rejected *before* bothering Telegram
        assert res.status is LoginStatus.ERROR
        client.send_code_request.assert_not_called()

        res = await session.request_code(12345, "abc123", "+251 91 234 5678")
        assert res.status is LoginStatus.CODE_SENT
        assert res.delivery is not None
        assert res.delivery.channel is DeliveryChannel.TELEGRAM_APP
        client.send_code_request.assert_awaited_once_with("+251912345678")
        assert session.pending is not None
        assert session.pending.phone == "+251912345678"
        assert session.pending.phone_code_hash == "hash-a"
        assert session.pending.code_length == 5
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_successful_code_login_reaches_ok(monkeypatch) -> None:
    session = PhoneLoginSession()
    client = _fake_client(_sent_app())
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    await session.request_code(12345, "abc123", "+251912345678")
    res = await session.submit_code("12345")

    assert res.status is LoginStatus.OK
    client.sign_in.assert_awaited_once_with(
        phone="+251912345678", code="12345", phone_code_hash="hash-a"
    )
    assert session.take_authorized_client() is not None


@pytest.mark.asyncio
async def test_wrong_code_length_is_rejected_without_calling_telegram(monkeypatch) -> None:
    session = PhoneLoginSession()
    client = _fake_client(_sent_app(length=5))
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    await session.request_code(12345, "abc123", "+251912345678")
    res = await session.submit_code("123")

    assert res.status is LoginStatus.ERROR
    assert "5 digits" in res.message
    client.sign_in.assert_not_called()


@pytest.mark.asyncio
async def test_2fa_is_detected_after_code_acceptance(monkeypatch) -> None:
    session = PhoneLoginSession()
    client = _fake_client(
        _sent_app(), sign_in_side_effects=[SessionPasswordNeededError(request=None), None]
    )
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    await session.request_code(12345, "abc123", "+251912345678")
    res = await session.submit_code("12345")
    assert res.status is LoginStatus.TWO_FA_REQUIRED
    assert session.pending is not None and session.pending.code_verified is True

    res = await session.submit_password("my-cloud-password")
    assert res.status is LoginStatus.OK


@pytest.mark.asyncio
async def test_stale_hash_is_recovered_transparently(monkeypatch) -> None:
    """A resend or reconnect can invalidate the stored hash; the same code should still work.

    Before the fix this surfaced as a bare 'Sign-in failed' error and the owner
    had to start over, which is why it looked like no code ever arrived.
    """
    session = PhoneLoginSession()
    client = _fake_client(_sent_app(phone_code_hash="hash-old"))
    client.send_code_request = AsyncMock(
        side_effect=[_sent_app("hash-old"), _sent_app("hash-new")]
    )
    client.sign_in = AsyncMock(side_effect=[PhoneCodeExpiredError(request=None), MagicMock()])
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    await session.request_code(12345, "abc123", "+251912345678")
    res = await session.submit_code("12345")

    assert res.status is LoginStatus.OK
    # retried once with the refreshed hash
    assert client.sign_in.await_args_list[-1].kwargs["phone_code_hash"] == "hash-new"


@pytest.mark.asyncio
async def test_resend_falls_back_when_hash_is_dead(monkeypatch) -> None:
    session = PhoneLoginSession()
    client = _fake_client(_sent_app("hash-old"))
    client.side_effect = PhoneCodeExpiredError(request=None)  # __call__ -> ResendCodeRequest
    client.send_code_request = AsyncMock(
        side_effect=[_sent_app("hash-old"), _sent_app("hash-fresh")]
    )
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    await session.request_code(12345, "abc123", "+251912345678")
    res = await session.resend_code()

    assert res.status is LoginStatus.CODE_SENT
    assert session.pending is not None
    assert session.pending.phone_code_hash == "hash-fresh"


@pytest.mark.asyncio
async def test_resend_flood_is_reported_without_losing_the_session(monkeypatch) -> None:
    session = PhoneLoginSession()
    client = _fake_client(_sent_app())
    client.side_effect = FloodWaitError(request=None, capture=30)  # ResendCodeRequest
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    await session.request_code(12345, "abc123", "+251912345678")
    res = await session.resend_code()

    assert res.status is LoginStatus.ERROR
    assert res.retry_after == 30
    assert session.pending is not None  # still usable after the wait


@pytest.mark.asyncio
async def test_lost_session_messages_are_actionable() -> None:
    session = PhoneLoginSession()

    res = await session.submit_code("12345")
    assert res.status is LoginStatus.ERROR
    assert "phone number again" in res.message.lower()

    res = await session.resend_code()
    assert res.status is LoginStatus.ERROR
    assert "phone number again" in res.message.lower()

    res = await session.submit_password("x")
    assert res.status is LoginStatus.ERROR


@pytest.mark.asyncio
async def test_cancel_releases_the_temporary_client(monkeypatch) -> None:
    session = PhoneLoginSession()
    client = _fake_client(_sent_app())
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    await session.request_code(12345, "abc123", "+251912345678")
    await session.close()

    client.disconnect.assert_awaited()
    assert session.pending is None
    assert session.has_pending is False


@pytest.mark.asyncio
async def test_offline_client_is_reconnected_before_signin(monkeypatch) -> None:
    """The login client must be reconnected instead of failing the correct code."""
    session = PhoneLoginSession()
    client = _fake_client(_sent_app())
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    await session.request_code(12345, "abc123", "+251912345678")
    client.is_connected = MagicMock(return_value=False)

    res = await session.submit_code("12345")
    assert res.status is LoginStatus.OK
    client.connect.assert_awaited()


def test_login_result_carries_delivery_for_ui() -> None:
    delivery = CodeDelivery(DeliveryChannel.SMS, "Code sent by SMS", "check messages", 5)
    assert delivery.length == 5


# ---------------------------------------------------------------------------
# Persistence across restarts (Render Free Tier restarts mid-login)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_attempt_is_persisted_and_cleared(monkeypatch) -> None:
    session = PhoneLoginSession()
    saved: list[dict | None] = []
    cleared: list[bool] = []

    async def persist(snapshot):
        saved.append(snapshot)

    async def clear():
        cleared.append(True)

    session.set_persistence(persist, clear)
    client = _fake_client(_sent_app())
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    await session.request_code(12345, "abc123", "+251 91 234 5678")
    assert saved and saved[-1]["phone"] == "+251912345678"
    assert saved[-1]["phone_code_hash"] == "hash-a"

    await session.submit_code("12345")
    assert cleared, "a completed login must clear the persisted attempt"


@pytest.mark.asyncio
async def test_pending_login_settings_are_never_exported(tmp_path) -> None:
    """tg_pending_login is ephemeral and must not leak into BOT_STATE_B64."""
    from qna_bot.storage.crypto import SecretCipher
    from qna_bot.storage.database import Database

    db = Database(tmp_path / "bot.db", SecretCipher.from_env_or_file(None, tmp_path / ".key"))
    await db.connect()
    try:
        await db.set_setting("tg_pending_login", {"phone": "+251912345678"})
        await db.set_setting("owner_id", 42)
        exported = await db.export_state_b64()

        db2 = Database(tmp_path / "b2.db", SecretCipher.from_env_or_file(None, tmp_path / ".k2"))
        await db2.connect()
        try:
            await db2.import_state_b64(exported)
            assert await db2.get_setting("owner_id") == 42
            assert await db2.get_setting("tg_pending_login") is None
        finally:
            await db2.close()
    finally:
        await db.close()
