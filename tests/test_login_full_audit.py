"""A full audit of the login path: every Telegram answer, every failure mode.

Each test here exists because the corresponding behaviour was either missing or
silently wrong, and "no login code ever arrives" could not be diagnosed from the
outside. They are deliberately written against observable behaviour.
"""

from __future__ import annotations

import base64
import ipaddress
import struct

import pytest
from telethon.tl.types import auth as T

from qna_bot.userbot.login import (
    DeliveryChannel,
    LoginStatus,
    PhoneIssue,
    PhoneLoginSession,
    describe_delivery,
    parse_phone,
)


def _sent(code_type, *, next_type=None, phone_code_hash="HASH-1"):
    return T.SentCode(
        phone_code_hash=phone_code_hash, type=code_type, next_type=next_type, timeout=60
    )


def _valid_session() -> str:
    raw = struct.pack(
        ">B4sH256s", 2, ipaddress.ip_address("149.154.167.51").packed, 443, b"\x33" * 256
    )
    return "1" + base64.urlsafe_b64encode(raw).decode()


# ---------------------------------------------------------------------------
# Phone numbers: a wrong normalisation asks Telegram for a different number
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("+251912345678", "+251912345678"),
        # The international form with the national trunk 0 kept - extremely common.
        # Sent as-is this is a *different* number, so the code goes nowhere.
        ("+2510912345678", "+251912345678"),
        ("+251 091 234 5678", "+251912345678"),
        ("+2540712345678", "+254712345678"),
        ("+919876543210", "+919876543210"),
        ("+15551234567", "+15551234567"),
        ("251912345678", "+251912345678"),
        ("00251912345678", "+251912345678"),
    ],
)
def test_numbers_are_normalised_without_the_trunk_zero(typed: str, expected: str) -> None:
    parsed = parse_phone(typed)
    assert parsed.issue is PhoneIssue.OK
    assert parsed.e164 == expected


def test_a_local_number_is_still_rejected_with_instructions() -> None:
    assert parse_phone("0912345678").issue is PhoneIssue.NO_COUNTRY_CODE


# ---------------------------------------------------------------------------
# Every SentCodeType Telegram can return must be understood
# ---------------------------------------------------------------------------


def test_firebase_sms_is_not_reported_as_unknown() -> None:
    """Android/Google-service numbers get this; it never becomes an SMS."""
    code_type = T.SentCodeTypeFirebaseSms(length=6, nonce=None, push_timeout=None)
    delivery = describe_delivery(_sent(code_type))
    assert delivery.channel is not DeliveryChannel.UNKNOWN
    assert "Google Play" in delivery.detail


@pytest.mark.parametrize(
    ("code_type", "channel"),
    [
        (T.SentCodeTypeApp(length=5), DeliveryChannel.TELEGRAM_APP),
        (T.SentCodeTypeSms(length=5), DeliveryChannel.SMS),
        (T.SentCodeTypeCall(length=5), DeliveryChannel.CALL),
        (T.SentCodeTypeFlashCall(pattern="1"), DeliveryChannel.FLASH_CALL),
        (T.SentCodeTypeMissedCall(prefix="9", length=5), DeliveryChannel.MISSED_CALL),
        (T.SentCodeTypeSmsWord(beginning="AB"), DeliveryChannel.SMS),
    ],
)
def test_each_delivery_type_is_identified(code_type, channel) -> None:
    assert describe_delivery(_sent(code_type)).channel is channel


def test_the_in_app_channel_with_no_fallback_is_recognised() -> None:
    """No SMS will be sent for this attempt - the UI must act on that."""
    from qna_bot.dashboard.handlers.wizard import _no_sms_will_come

    delivery = describe_delivery(_sent(T.SentCodeTypeApp(length=5), next_type=None))
    assert _no_sms_will_come(delivery) is True


def test_the_in_app_channel_with_an_sms_fallback_is_not_hopeless() -> None:
    from qna_bot.dashboard.handlers.wizard import _no_sms_will_come

    delivery = describe_delivery(
        _sent(T.SentCodeTypeApp(length=5), next_type=T.CodeTypeSms())
    )
    assert _no_sms_will_come(delivery) is False


def test_an_sms_attempt_is_never_called_hopeless() -> None:
    from qna_bot.dashboard.handlers.wizard import _no_sms_will_come

    assert _no_sms_will_come(describe_delivery(_sent(T.SentCodeTypeSms(length=5)))) is False


# ---------------------------------------------------------------------------
# A restart must not throw the attempt away
# ---------------------------------------------------------------------------


class _Client:
    api_id = 12345
    api_hash = "a" * 32

    def __init__(self) -> None:
        from telethon.sessions import StringSession

        self.session = StringSession(_valid_session())
        self._phone_code_hash: dict[str, str] = {}

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    def is_connected(self) -> bool:
        return True

    async def send_code_request(self, phone: str):
        return _sent(T.SentCodeTypeApp(length=5))

    async def sign_in(self, **kwargs):
        assert kwargs["phone_code_hash"] == "HASH-1"
        return object()


@pytest.mark.asyncio
async def test_a_restart_between_number_and_code_keeps_the_attempt(monkeypatch) -> None:
    """Telegram ties the hash to the connection; keeping its session resumes it."""
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: _Client())

    store: dict[str, str] = {}

    async def save(value):
        if value:
            store["s"] = value
        else:
            store.pop("s", None)

    async def load():
        return store.get("s")

    first = PhoneLoginSession()
    first.set_session_store(load, save)
    result = await first.request_code(12345, "a" * 32, "+251912345678")
    assert result.status is LoginStatus.CODE_SENT
    import asyncio

    await asyncio.sleep(0)  # let the save task run
    assert store.get("s"), "the temporary session must be kept for a restart"
    snapshot = first._last_snapshot
    assert snapshot is not None

    # -- the process is replaced here --

    second = PhoneLoginSession()
    second.set_session_store(load, save)
    second.seed_snapshot({**snapshot, "api_id": 12345, "api_hash": "a" * 32})
    assert await second.restore() is True

    done = await second.submit_code("12345")
    assert done.status is LoginStatus.OK


@pytest.mark.asyncio
async def test_restore_is_a_no_op_without_a_saved_attempt() -> None:
    session = PhoneLoginSession()

    async def load():
        return None

    session.set_session_store(load, None)
    assert await session.restore() is False
