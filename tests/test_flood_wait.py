"""A FloodWait is an instruction, not a hint.

Two bugs combined to make a rate limit unbounded:

1. Only ``getattr(exc, "seconds")`` was read. Telethon does not always populate it -
   when the number is only in the error text, the bot printed "Please wait and try
   again" with no time at all. An 18-hour block looked like a glitch.
2. Nothing stopped the bot asking again. ``_recover_stale_hash`` transparently
   requested a *new* code whenever a code was rejected, and every "Try Again" did
   the same - and Telegram lengthens the block for each request made inside the
   window. Retrying is what turns minutes into most of a day.
"""

from __future__ import annotations

import pytest
from telethon.errors import FloodPremiumWaitError, FloodWaitError
from telethon.tl.types import auth as T

from qna_bot.userbot.login import (
    LoginStatus,
    PhoneLoginSession,
    _flood_result,
    _flood_seconds,
    humanise_seconds,
)

# ---------------------------------------------------------------------------
# Reading the number Telegram gave us
# ---------------------------------------------------------------------------


def test_seconds_come_from_the_attribute() -> None:
    assert _flood_seconds(FloodWaitError(request=None, capture=67746)) == 67746


def test_seconds_come_from_the_message_when_the_attribute_is_empty() -> None:
    """Some paths leave ``.seconds`` at 0 and keep the number only in the text."""

    class E(Exception):
        def __init__(self) -> None:
            super().__init__("A wait of 67746 seconds is required (caused by X)")

    assert _flood_seconds(E()) == 67746


def test_seconds_come_from_an_underscore_error_name() -> None:
    class E(Exception):
        def __init__(self) -> None:
            super().__init__("FLOOD_WAIT_3600")

    assert _flood_seconds(E()) == 3600


def test_premium_wait_is_understood_too() -> None:
    """It is not a subclass of FloodWaitError, so it needs its own handling."""
    assert _flood_seconds(FloodPremiumWaitError(request=None, capture=999)) == 999


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (45, "45 seconds"),
        (90, "1 minute"),
        (3600, "1 hour"),
        (67746, "18 hours 49 minutes"),
        (172800, "2 days"),
    ],
)
def test_the_wait_is_stated_in_words(seconds: int, expected: str) -> None:
    """'67746s' is not something a person can plan around."""
    assert humanise_seconds(seconds) == expected


def test_the_message_never_omits_the_wait() -> None:
    result = _flood_result(FloodWaitError(request=None, capture=67746), "login code request")
    assert "18 hours 49 minutes" in result.message
    assert result.retry_after == 67746


def test_an_unknown_wait_still_produces_a_usable_instruction() -> None:
    result = _flood_result(Exception("boom"), "login code request")
    assert result.retry_after and result.retry_after > 0
    assert "wait" in result.message.lower()


# ---------------------------------------------------------------------------
# The bot must stop asking
# ---------------------------------------------------------------------------


class _FloodingClient:
    api_id = 12345
    api_hash = "a" * 32

    def __init__(self, seconds: int) -> None:
        self.seconds = seconds
        self.requests = 0
        self._phone_code_hash: dict[str, str] = {}

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    def is_connected(self) -> bool:
        return True

    async def send_code_request(self, phone: str):
        self.requests += 1
        raise FloodWaitError(request=None, capture=self.seconds)


@pytest.mark.asyncio
async def test_tapping_try_again_does_not_touch_telegram(monkeypatch) -> None:
    """Every request inside the window lengthens the block. None may be sent."""
    client = _FloodingClient(67746)
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)
    session = PhoneLoginSession()

    first = await session.request_code(12345, "a" * 32, "+251912345678")
    assert first.status is LoginStatus.ERROR
    assert client.requests == 1

    for _ in range(5):
        again = await session.request_code(12345, "a" * 32, "+251912345678")
        assert again.status is LoginStatus.ERROR

    assert client.requests == 1, "the bot kept asking Telegram inside the block"


@pytest.mark.asyncio
async def test_the_block_is_reported_with_the_remaining_time(monkeypatch) -> None:
    client = _FloodingClient(3600)
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)
    session = PhoneLoginSession()

    await session.request_code(12345, "a" * 32, "+251912345678")
    again = await session.request_code(12345, "a" * 32, "+251912345678")

    assert "rate-limiting" in again.message
    assert "minute" in again.message
    assert again.retry_after and again.retry_after > 0


@pytest.mark.asyncio
async def test_resend_is_refused_inside_the_block(monkeypatch) -> None:
    client = _FloodingClient(7200)
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)
    session = PhoneLoginSession()

    await session.request_code(12345, "a" * 32, "+251912345678")
    # A live attempt is needed for resend to have anything to resend.
    session._pending = None
    result = await session.resend_code()
    assert result.status is LoginStatus.ERROR


@pytest.mark.asyncio
async def test_the_automatic_stale_hash_retry_is_suppressed(monkeypatch) -> None:
    """This retry used to request a fresh code on every rejected code."""
    client = _FloodingClient(600)
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)
    session = PhoneLoginSession()

    await session.request_code(12345, "a" * 32, "+251912345678")
    before = client.requests

    session._pending = object()  # type: ignore[assignment]
    result = await session._recover_stale_hash("12345")

    assert result.status is LoginStatus.ERROR
    assert client.requests == before


@pytest.mark.asyncio
async def test_a_normal_attempt_is_unaffected(monkeypatch) -> None:
    """The guard must not block the ordinary path."""

    class Client(_FloodingClient):
        async def send_code_request(self, phone: str):
            self.requests += 1
            return T.SentCode(
                phone_code_hash="H1",
                type=T.SentCodeTypeSms(length=5),
                next_type=None,
                timeout=60,
            )

    client = Client(0)
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)
    session = PhoneLoginSession()

    result = await session.request_code(12345, "a" * 32, "+251912345678")
    assert result.status is LoginStatus.CODE_SENT
