"""Why the OTP worked before the move, and what is done about it now.

The report was "OTP login used to work before we tried to move the deployment
elsewhere". Two things change when the deployment moves, and neither is in
Telegram:

1. **The database is new.** A fresh Render account with no `BOT_STATE_B64` has no
   saved session, so the bot must log in from scratch. That is not a regression -
   it is the first login on that host - but it looks exactly like one.

2. **The IP is new.** Telegram treats a first login from an unfamiliar address as
   suspicious and quietly does not deliver the code. Repeated Resends then push the
   account into `PHONE_NUMBER_FLOOD`, which extends the silence.

This module pins the behaviour that makes that recoverable instead of a dead end:
an attempt that keeps receiving nothing escalates channel, then refuses to keep
resending, and points at the two routes that cannot be blocked.

It also pins a storage bug that let a *stale* wizard state outlive its TTL, which
on a long-lived deployment could resume a login the owner had abandoned.
"""

from __future__ import annotations

import time

import pytest

from qna_bot.storage.fsm import SQLiteStorage
from qna_bot.userbot.login import (
    ESCALATE_AFTER_SILENT_RESENDS,
    GIVE_UP_AFTER_SILENT_RESENDS,
    DeliveryChannel,
    PendingLogin,
)

# ---------------------------------------------------------------------------
# The escalation policy
# ---------------------------------------------------------------------------


def _pending(*, resend_count: int, channel=DeliveryChannel.TELEGRAM_APP) -> PendingLogin:
    """A PendingLogin with only the fields the policy reads."""
    pending = PendingLogin.__new__(PendingLogin)
    pending.resend_count = resend_count
    pending.delivery = None
    pending.next_channel = channel
    return pending


def test_one_miss_is_tolerated() -> None:
    """A single missing code is usually a slow SMS, not a dead channel."""
    assert _pending(resend_count=1).should_escalate is False


def test_two_misses_escalate() -> None:
    """Two silent failures in a row means this channel is not going to deliver."""
    assert ESCALATE_AFTER_SILENT_RESENDS == 2
    assert _pending(resend_count=2).should_escalate is True
    assert _pending(resend_count=3).should_escalate is True


def test_escalation_is_below_the_give_up_point() -> None:
    """Escalating must happen while resending is still allowed at all."""
    assert ESCALATE_AFTER_SILENT_RESENDS < GIVE_UP_AFTER_SILENT_RESENDS


def test_resending_stops_being_offered_at_the_limit() -> None:
    assert _pending(resend_count=GIVE_UP_AFTER_SILENT_RESENDS - 1).exhausted is False
    assert _pending(resend_count=GIVE_UP_AFTER_SILENT_RESENDS).exhausted is True


@pytest.mark.asyncio
async def test_an_exhausted_attempt_refuses_to_resend_again() -> None:
    """The loop that makes an OTP look permanently broken must end."""
    from qna_bot.userbot.login import LoginStatus, PhoneLoginSession

    session = PhoneLoginSession()
    pending = _pending(resend_count=GIVE_UP_AFTER_SILENT_RESENDS)
    session._pending = pending  # the policy check runs before any network call

    result = await session.resend_code()

    assert result.status is LoginStatus.ERROR
    # And it must name the routes that work, not just say "no".
    assert "QR" in result.message
    assert "email" in result.message.lower()


# ---------------------------------------------------------------------------
# The stale-state bug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gc_runs_on_the_first_write_not_only_on_a_whole_minute(tmp_path) -> None:
    """The old throttle (`int(now) % 60 == 0`) almost never fired.

    It only swept when the clock happened to land exactly on a whole minute, so a
    stale wizard state could survive far past the TTL and resume a login the owner
    had long abandoned - on a deployment that had been reused across hosts.
    """
    storage = SQLiteStorage(str(tmp_path / "fsm.db"), ttl_seconds=60)
    conn = await storage._connection()
    old_ts = time.time() - 3600
    await conn.execute(
        "INSERT INTO fsm_states (storage_key, state, data, updated_at) VALUES (?,?,?,?)",
        ("stale:key", "WizardStates:waiting_otp", "{}", old_ts),
    )
    await conn.commit()

    from aiogram.fsm.storage.base import StorageKey

    # A plain write, at an ordinary second of the minute.
    await storage.set_state(StorageKey(bot_id=1, chat_id=2, user_id=3), None)

    cursor = await conn.execute("SELECT COUNT(*) FROM fsm_states WHERE storage_key = ?", ("stale:key",))
    remaining = (await cursor.fetchone())[0]
    assert remaining == 0, "an hour-old state must be swept on the first write"
    await storage.close()


@pytest.mark.asyncio
async def test_gc_is_throttled_so_it_does_not_run_on_every_write(tmp_path) -> None:
    storage = SQLiteStorage(str(tmp_path / "fsm.db"), ttl_seconds=60)
    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=1, chat_id=2, user_id=3)
    await storage.set_state(key, "s1")
    first_sweep = storage._last_gc
    assert first_sweep > 0

    await storage.set_state(key, "s2")
    assert storage._last_gc == first_sweep, "a second write in the same minute must not sweep"

    # Force the throttle window open and confirm it sweeps again.
    storage._last_gc = time.time() - 120
    await storage.set_state(key, "s3")
    assert storage._last_gc > first_sweep
    await storage.close()


# ---------------------------------------------------------------------------
# The mobile-client fingerprint
# ---------------------------------------------------------------------------


def test_the_login_client_impersonates_a_mobile_app() -> None:
    """Telegram drops login codes for the default Telethon fingerprint.

    A server move redeploys the same code, but this is the other half of "the code
    never arrives" and must not be regressed: a bare ``TelegramClient`` is exactly
    what gets silently ignored by ``auth.sendCode``.
    """
    from qna_bot.userbot import login as login_mod

    assert login_mod.DEVICE_MODEL
    assert login_mod.APP_VERSION
    client = login_mod.make_login_client(12345, "0" * 32)
    try:
        assert client._init_request.device_model == login_mod.DEVICE_MODEL
        assert client._init_request.lang_code == login_mod.LANG_CODE
        # A frozen bot on a small FloodWait is worse than the wait itself.
        assert client.flood_sleep_threshold == 0
    finally:
        client.session.close() if hasattr(client.session, "close") else None
