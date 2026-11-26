"""Telegram picks the in-app channel; we must take the offer it makes instead.

``auth.sendCode`` answers ``SentCodeTypeApp`` for most numbers used on a phone:
the code is dropped into the *service chat of another logged-in session*, and no
SMS or call is offered in that same answer. That channel is the one with the known
silent-withholding problem (Telethon #4730) - the request succeeds, the hash is
valid, and nothing ever arrives.

Telegram does not leave it there: ``auth.sentCode.next_type`` names the channel a
single ``auth.resendCode`` would use (SMS, usually). Resending once *is* how you
accept that offer, and it moves the code onto a channel that actually delivers -
without invalidating the first code or restarting the attempt.

The old behaviour told the owner to go and look in the service chat and tap
Resend themselves, which is why "no code" persisted.
"""

from __future__ import annotations

import pytest
from telethon.tl import types

from qna_bot.userbot.login import (
    DeliveryChannel,
    PhoneIssue,
    PhoneLoginSession,
    describe_delivery,
    parse_phone,
)


def _sent(code_type, *, next_type=None, phone_code_hash="hash-1"):
    return types.auth.SentCode(
        phone_code_hash=phone_code_hash,
        type=code_type,
        next_type=next_type,
        timeout=60,
    )


class _FakeClient:
    """A stand-in Telethon client that answers ``auth.resendCode``.

    ``request_code`` reaches the network through ``send_code_request`` (a
    stand-in rather than a real ``TelegramClient`` takes that path); the automatic
    channel switch goes through ``client(auth.ResendCodeRequest(...))``. A plain
    class with an ``async __call__`` mirrors the real client for both.
    """

    api_id = 12345
    api_hash = "a" * 32

    def __init__(self, code_type, next_type=None, *, resend_result=None, raises=None):
        self._sent = _sent(code_type, next_type=next_type)
        self._resend = resend_result or _sent(
            types.auth.SentCodeTypeSms(length=5), phone_code_hash="hash-1"
        )
        self._raises = raises
        self._phone_code_hash: dict[str, str] = {}
        self.requests: list[object] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    def is_connected(self) -> bool:
        return True

    async def send_code_request(self, phone: str):
        self.requests.append(("send_code", phone))
        return self._sent

    async def __call__(self, request):
        self.requests.append(request)
        if self._raises is not None:
            raise self._raises
        return self._resend

    def resends(self) -> int:
        return sum(1 for r in self.requests if type(r).__name__ == "ResendCodeRequest")


def _client(code_type, next_type=None, **kwargs) -> _FakeClient:
    return _FakeClient(code_type, next_type, **kwargs)


@pytest.fixture()
def notify():
    calls: list[str] = []

    async def _record(text: str) -> None:
        calls.append(text)

    _record.calls = calls  # type: ignore[attr-defined]
    return _record


@pytest.mark.asyncio
async def test_the_app_channel_is_switched_to_the_offered_one(monkeypatch, notify) -> None:
    """One automatic resend, onto the channel Telegram nominated."""
    client = _client(
        types.auth.SentCodeTypeApp(length=5),
        types.auth.CodeTypeSms(),
    )
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)
    session = PhoneLoginSession()
    session.set_notify(notify)

    result = await session.request_code(12345, "a" * 32, "+251912345678")

    assert result.delivery is not None
    assert result.delivery.channel is DeliveryChannel.SMS
    # Exactly one resend: a second would be rate-limited and would kill the code.
    assert client.resends() == 1


@pytest.mark.asyncio
async def test_an_sms_attempt_is_left_alone(monkeypatch) -> None:
    """No pointless resend when Telegram already chose a channel that delivers."""
    client = _client(types.auth.SentCodeTypeSms(length=5))
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)
    session = PhoneLoginSession()

    result = await session.request_code(12345, "a" * 32, "+251912345678")

    assert result.delivery.channel is DeliveryChannel.SMS
    assert client.resends() == 0


@pytest.mark.asyncio
async def test_the_switch_happens_once_per_attempt(monkeypatch) -> None:
    """A manual resend afterwards must not trigger another automatic one."""
    client = _client(
        types.auth.SentCodeTypeApp(length=5),
        types.auth.CodeTypeSms(),
    )
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)
    session = PhoneLoginSession()

    await session.request_code(12345, "a" * 32, "+251912345678")
    assert client.resends() == 1

    delivery = describe_delivery(_sent(types.auth.SentCodeTypeApp(length=5)))
    await session.escalate_after_app_channel(delivery)

    assert client.resends() == 1


@pytest.mark.asyncio
async def test_a_refused_switch_keeps_the_attempt_alive(monkeypatch, notify) -> None:
    """If Telegram refuses, the first code is still valid - never restart."""
    client = _client(
        types.auth.SentCodeTypeApp(length=5),
        types.auth.CodeTypeSms(),
    )
    client = _client(
        types.auth.SentCodeTypeApp(length=5),
        types.auth.CodeTypeSms(),
        raises=RuntimeError("SEND_CODE_UNAVAILABLE"),
    )
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)
    session = PhoneLoginSession()
    session.set_notify(notify)

    result = await session.request_code(12345, "a" * 32, "+251912345678")

    assert result.status.value in {"code_sent", "error"}
    assert session.pending is not None
    assert session.pending.phone_code_hash == "hash-1"


@pytest.mark.asyncio
async def test_the_owner_is_told_where_the_code_went(monkeypatch, notify) -> None:
    """When it stays on the app channel, say so instead of leaving them guessing."""
    client = _client(
        types.auth.SentCodeTypeApp(length=5),
        types.auth.CodeTypeSms(),
        resend_result=_sent(types.auth.SentCodeTypeApp(length=5)),
    )
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)
    session = PhoneLoginSession()
    session.set_notify(notify)

    await session.request_code(12345, "a" * 32, "+251912345678")

    assert notify.calls, "the owner must be told the code is inside the app"
    assert "service chat" in notify.calls[0]
    assert "SMS" in notify.calls[0]


def test_ethiopian_number_still_parses() -> None:
    """Guard against the switch work breaking the number that gets used."""
    assert parse_phone("+251912345678").issue is PhoneIssue.OK


def test_the_switch_uses_the_real_attempt_hash(monkeypatch) -> None:
    """The resend must carry the live hash, else Telegram rejects it."""
    import asyncio

    client = _client(types.auth.SentCodeTypeApp(length=5), types.auth.CodeTypeSms())
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)
    session = PhoneLoginSession()

    asyncio.run(session.request_code(12345, "a" * 32, "+251912345678"))

    request = next(r for r in client.requests if type(r).__name__ == 'ResendCodeRequest')
    assert type(request).__name__ == "ResendCodeRequest"
    assert request.phone_number == "+251912345678"
    assert request.phone_code_hash == "hash-1"
