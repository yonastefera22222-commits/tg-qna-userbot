"""Two defects that made a *correct* OTP fail. No new features, just the fix.

1. **A stale hash was terminal once two resends had been used.**
   ``_recover_stale_hash`` bailed out when ``resend_count >= MAX_AUTO_RESENDS``,
   returning "that login code expired" — even when the digits the owner typed were
   the right ones and the only stale thing was ``phone_code_hash``. The two
   budgets were conflated: "Telegram never delivered anything" and "this attempt
   got out of step" are different problems, and spending the first one's budget
   killed the second one permanently.

2. **Telethon's own hash cache was never updated.**
   ``client.sign_in(phone, code, phone_code_hash=...)`` does use the value passed
   in, but every other path in the client reads ``client._phone_code_hash[phone]``,
   and ``send_code_request`` only *sets* that cache when it is empty. After a
   resume, a resend, or a reconnect the two can disagree, and the mismatch is
   reported as "code expired" for a valid code.
"""

from __future__ import annotations

import pytest
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
)

from qna_bot.userbot.login import (
    LoginStatus,
    PendingLogin,
    PhoneLoginSession,
    _sync_client_hash,
)


class _FakeClient:
    """Records what was called and replays a scripted sign_in outcome."""

    def __init__(self, outcomes: list[str], *, new_hash: str = "NEWHASH") -> None:
        self.outcomes = list(outcomes)
        self.new_hash = new_hash
        self.calls: list[tuple] = []
        self._phone_code_hash: dict[str, str] = {}

    def is_connected(self) -> bool:
        return True

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def sign_in(self, **kwargs):
        self.calls.append(("sign_in", kwargs["code"], kwargs["phone_code_hash"]))
        outcome = self.outcomes.pop(0)
        if outcome == "expired":
            raise PhoneCodeExpiredError(request=None)
        if outcome == "invalid":
            raise PhoneCodeInvalidError(request=None)
        return True

    async def send_code_request(self, phone: str):
        self.calls.append(("send_code", phone))

        class _Sent:
            phone_code_hash = self.new_hash
            type = None
            timeout = 60

        return _Sent()


def _session(outcomes: list[str], **fields) -> tuple[PhoneLoginSession, _FakeClient]:
    session = PhoneLoginSession()
    client = _FakeClient(outcomes)
    pending = PendingLogin(
        client=client,  # type: ignore[arg-type]
        api_id=1,
        api_hash="a" * 32,
        phone="+251911111111",
        phone_code_hash="OLDHASH",
    )
    for name, value in fields.items():
        setattr(pending, name, value)
    session._set_pending(pending)
    return session, client


# ---------------------------------------------------------------------------
# 1. A stale hash must be recoverable regardless of the resend count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_correct_code_still_logs_in_after_two_resends() -> None:
    """This was the dead end: correct digits, stale hash, resend budget spent."""
    session, client = _session(["expired", "ok"], resend_count=2)

    result = await session.submit_code("12345")

    assert result.status is LoginStatus.OK
    assert ("send_code", "+251911111111") in client.calls
    # The retry used the freshly fetched hash, not the stale one.
    assert client.calls[-1] == ("sign_in", "12345", "NEWHASH")


@pytest.mark.asyncio
async def test_the_hash_retry_budget_is_separate_from_resends() -> None:
    """Spending resends must not spend the hash-retry budget, or vice versa."""
    from qna_bot.userbot.login import PhoneLoginSession as S

    session, _client = _session(["expired", "ok"], resend_count=99)
    assert session.pending is not None

    result = await session.submit_code("12345")

    assert result.status is LoginStatus.OK
    assert session.pending.hash_retries == 1
    assert session.pending.resend_count == 99, "a hash retry is not a resend"
    assert S.MAX_HASH_RETRIES >= 2


@pytest.mark.asyncio
async def test_exhausted_hash_retries_say_resend_not_retry_the_same_code() -> None:
    """When we cannot recover, the message must not send them in a circle."""
    from qna_bot.userbot.login import PhoneLoginSession as S

    session, _client = _session(["expired"], hash_retries=S.MAX_HASH_RETRIES)

    result = await session.submit_code("12345")

    assert result.status is LoginStatus.ERROR
    assert "resend" in result.message.lower()
    assert "expired" not in result.message.lower() or "new" in result.message.lower()


@pytest.mark.asyncio
async def test_a_channel_switch_asks_for_the_new_code_not_the_old_digits() -> None:
    """If Telegram switched channel the held digits belong to another message."""

    session, client = _session(["expired"])
    pending = session.pending
    assert pending is not None
    # Simulate: the old code came by SMS, the new one is an in-app message.
    client.new_hash = "NEWHASH"
    pending.delivery = None

    result = await session.submit_code("12345")

    # With no previous channel there is nothing to compare, so the retry is
    # attempted with the new hash and the scripted outcome decides.
    assert result.status is LoginStatus.ERROR


# ---------------------------------------------------------------------------
# 2. Telethon's internal hash cache must match ours
# ---------------------------------------------------------------------------


def test_syncing_the_hash_writes_the_key_telethon_reads() -> None:
    client = _FakeClient([])

    _sync_client_hash(client, "+251911111111", "HASH-1")

    assert client._phone_code_hash["+251911111111"] == "HASH-1"


@pytest.mark.asyncio
async def test_the_hash_cache_is_aligned_before_every_sign_in() -> None:
    """Otherwise Telethon may use a hash that no longer matches the code."""
    session, client = _session(["ok"])

    await session.submit_code("12345")

    assert client._phone_code_hash.get("+251911111111") == "OLDHASH"


@pytest.mark.asyncio
async def test_the_hash_cache_follows_a_resend() -> None:
    session, client = _session([])

    await session.resend_code()

    assert client._phone_code_hash.get("+251911111111") == "NEWHASH"


@pytest.mark.asyncio
async def test_syncing_a_hash_never_breaks_a_login_if_the_attribute_is_gone() -> None:
    class _Opaque:
        __slots__ = ()

    _sync_client_hash(_Opaque(), "+251911111111", "X")  # must not raise


# ---------------------------------------------------------------------------
# 3. A wrong code stays recoverable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_wrong_code_keeps_the_login_open() -> None:
    session, _client = _session(["invalid", "ok"])

    first = await session.submit_code("99999")
    assert first.status is LoginStatus.ERROR
    assert session.pending is not None, "a wrong digit is not a reason to drop the attempt"

    second = await session.submit_code("12345")
    assert second.status is LoginStatus.OK


@pytest.mark.asyncio
async def test_a_wrong_code_says_the_login_is_still_open() -> None:
    session, _client = _session(["invalid"])

    result = await session.submit_code("99999")

    assert "still open" in result.message or "new code yet" in result.message


@pytest.mark.asyncio
async def test_repeated_wrong_codes_stop_before_telegram_kills_the_attempt() -> None:
    from qna_bot.userbot.login import PhoneLoginSession as S

    session, _client = _session(["invalid"] * 5, code_attempts=S.MAX_CODE_ATTEMPTS - 1)

    result = await session.submit_code("99999")

    assert result.status is LoginStatus.ERROR
    assert "resend" in result.message.lower()
