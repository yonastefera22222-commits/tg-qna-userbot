"""A restart between "send my number" and "here is the code" must not kill the login.

This is the bug behind "OTP login used to work before we moved the deployment".

The saved attempt held a ``phone_code_hash`` but **not** the auth key it belongs
to, and the boot routine deleted the whole attempt as "stale" on the reasoning
that a restarted process could never resume it. Both halves were wrong:

* Telethon writes the auth key into ``session`` the moment the client connects
  (``client/telegrambaseclient.py``: ``self.session.auth_key = self._sender.auth_key``
  followed by ``session.save()``), so the key *is* recoverable.
* ``phone_code_hash`` is only valid for the key that requested it - which is
  exactly why discarding the key made the code unusable, not why the attempt had
  to be discarded.

On Render Free that combination is fatal: the service sleeps after 15 minutes and
gets a brand-new process on every deploy, so a restart between the two steps is
routine. The owner then received a code that was guaranteed to be rejected with
"No login session is active" - which looks exactly like "the OTP login broke".
"""

from __future__ import annotations

import pytest

from qna_bot.userbot.login import (
    DeliveryChannel,
    PendingLogin,
    PhoneLoginSession,
    describe_next_channel,
)


def _real_session_string() -> str:
    """A genuine Telethon session string with an auth key in it.

    A fake string proves nothing here: the point of the fix is that a real key
    survives the trip, so the test uses the real serialiser.
    """
    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession

    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(b"\x01" * 256)
    return session.save()


class _Client:
    """The only thing the snapshot needs from a live client is its session."""

    def __init__(self, session_value: str) -> None:
        self.session = _RoundTrip(session_value)

    async def disconnect(self) -> None:
        pass


class _RoundTrip:
    def __init__(self, value: str) -> None:
        self._value = value

    def save(self) -> str:
        return self._value


def _pending(*, with_key: str | None = None) -> PendingLogin:
    with_key = _real_session_string() if with_key is None else with_key
    return PendingLogin(
        client=_Client(with_key),  # type: ignore[arg-type]
        api_id=12345,
        api_hash="a" * 32,
        phone="+251912345678",
        phone_code_hash="hash-abc",
    )


def _session_with(pending: PendingLogin) -> PhoneLoginSession:
    session = PhoneLoginSession()
    session._set_pending(pending)
    return session


# ---------------------------------------------------------------------------
# The snapshot keeps the key
# ---------------------------------------------------------------------------


def test_the_snapshot_carries_the_auth_key() -> None:
    """Without this the saved hash can never be used again."""
    key = _real_session_string()
    snapshot = _session_with(_pending(with_key=key))._snapshot()

    assert snapshot is not None
    assert snapshot["session"] == key
    # And it is a session string Telethon will accept back.
    from telethon.sessions import StringSession

    assert StringSession(key).auth_key is not None
    assert snapshot["phone_code_hash"] == "hash-abc"
    assert snapshot["phone"] == "+251912345678"


def test_the_snapshot_still_never_carries_the_api_hash() -> None:
    """It is already stored encrypted under ``tg_api_hash``."""
    snapshot = _session_with(_pending())._snapshot()

    assert snapshot is not None
    assert "api_hash" not in snapshot


def test_a_snapshot_survives_a_client_that_cannot_be_serialised() -> None:
    """A snapshot failure must never break a login that is otherwise fine."""

    class _Broken:
        class session:  # noqa: N801 - mimics the attribute
            @staticmethod
            def save() -> str:
                raise RuntimeError("session is gone")

    pending = _pending()
    pending.client = _Broken()  # type: ignore[assignment]

    snapshot = _session_with(pending)._snapshot()

    assert snapshot is not None
    assert snapshot["session"] == ""
    assert snapshot["phone_code_hash"] == "hash-abc"


# ---------------------------------------------------------------------------
# ...and the restore uses it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_saved_attempt_is_rebuilt_after_a_restart() -> None:
    revived = PhoneLoginSession()
    ok = await revived.restore(
        {
            "phone": "+251912345678",
            "phone_code_hash": "hash-abc",
            "session": _real_session_string(),
            "code_length": 5,
            "resend_count": 1,
            "delivery_channel": DeliveryChannel.TELEGRAM_APP.value,
            "delivery_title": "Code sent inside Telegram",
        },
        api_id=12345,
        api_hash="a" * 32,
    )

    assert ok is True
    pending = revived.pending
    assert pending is not None
    assert pending.phone == "+251912345678"
    assert pending.phone_code_hash == "hash-abc"
    assert pending.code_length == 5
    assert pending.resend_count == 1
    assert pending.delivery is not None
    assert pending.delivery.channel is DeliveryChannel.TELEGRAM_APP
    # The prompt the owner sees must still say where the code went.
    assert pending.delivery.title


@pytest.mark.asyncio
async def test_an_old_snapshot_without_a_session_is_not_falsely_resumed() -> None:
    """Snapshots written before this fix have no key; say so instead of pretending."""
    revived = PhoneLoginSession()

    ok = await revived.restore(
        {"phone": "+251912345678", "phone_code_hash": "hash-abc"}, api_id=1, api_hash="b" * 32
    )

    assert ok is False
    assert revived.pending is None


@pytest.mark.asyncio
async def test_a_corrupt_session_string_is_refused_quietly() -> None:
    revived = PhoneLoginSession()

    ok = await revived.restore(
        {"phone": "+251912345678", "phone_code_hash": "h", "session": "not-a-session"},
        api_id=1,
        api_hash="b" * 32,
    )

    assert ok is False


@pytest.mark.asyncio
async def test_restore_needs_real_credentials_to_be_useful() -> None:
    """A client built with api_id=0 cannot resend or sign in, so refuse it."""
    revived = PhoneLoginSession()

    ok = await revived.restore(
        {"phone": "+251912345678", "phone_code_hash": "h", "session": ""},
        api_id=0,
        api_hash="",
    )

    assert ok is False


def test_restored_title_is_never_blank() -> None:
    """A blank title would render an empty "📨 " line in the OTP prompt."""
    from qna_bot.userbot.login import _resumed_title

    for channel in (
        DeliveryChannel.TELEGRAM_APP,
        DeliveryChannel.SMS,
        DeliveryChannel.CALL,
        DeliveryChannel.EMAIL,
        DeliveryChannel.UNKNOWN,
    ):
        assert _resumed_title(channel)


# ---------------------------------------------------------------------------
# The fallback channel was silently always None
# ---------------------------------------------------------------------------


def test_the_next_channel_table_names_classes_that_exist() -> None:
    """``_NEXT_TYPE_CHANNELS`` matched on class names, one of which was invented.

    ``auth.CodeType`` defines Call, Sms, FlashCall, MissedCall and FragmentSms.
    The table listed ``CodeTypeSms``/``CodeTypeCall`` correctly but had to match
    the object's ``type().__name__``; a wrong name there fails silently and the
    UI reports "no fallback channel" for an account that has one.
    """
    from telethon.tl import types

    assert describe_next_channel(types.auth.CodeTypeSms()) is DeliveryChannel.SMS
    assert describe_next_channel(types.auth.CodeTypeCall()) is DeliveryChannel.CALL
    assert describe_next_channel(types.auth.CodeTypeFlashCall()) is DeliveryChannel.FLASH_CALL
    assert describe_next_channel(types.auth.CodeTypeMissedCall()) is DeliveryChannel.MISSED_CALL
    assert describe_next_channel(None) is None


def test_a_telegram_style_sentcode_reports_its_fallback() -> None:
    """End-to-end through the shape Telegram actually returns."""
    from telethon.tl import types

    from qna_bot.userbot.login import describe_delivery

    sent = types.auth.SentCode(
        phone_code_hash="h",
        type=types.auth.SentCodeTypeApp(length=5),
        next_type=types.auth.CodeTypeSms(),
        timeout=60,
    )
    delivery = describe_delivery(sent)

    assert delivery.channel is DeliveryChannel.TELEGRAM_APP
    assert delivery.next_channel is DeliveryChannel.SMS
    assert delivery.next_after == 60


# ---------------------------------------------------------------------------
# The boot routine no longer deletes a perfectly usable attempt
# ---------------------------------------------------------------------------


class _FakeDB:
    def __init__(self, settings: dict[str, object]) -> None:
        self._settings = dict(settings)

    async def get_setting(self, key: str, default: object = None) -> object:
        return self._settings.get(key, default)

    async def set_setting(self, key: str, value: object) -> None:
        self._settings[key] = value

    async def delete_setting(self, key: str) -> None:
        self._settings.pop(key, None)


def _manager_with(settings: dict[str, object]):
    from qna_bot.userbot.manager import UserbotManager

    manager = UserbotManager.__new__(UserbotManager)
    manager._db = _FakeDB(settings)  # type: ignore[assignment]
    manager._login = PhoneLoginSession()
    return manager


@pytest.mark.asyncio
async def test_boot_resumes_a_resumable_attempt_instead_of_deleting_it() -> None:
    saved = {
        "phone": "+251912345678",
        "phone_code_hash": "hash-abc",
        "session": _real_session_string(),
        "code_length": 5,
        "delivery_channel": DeliveryChannel.SMS.value,
    }
    manager = _manager_with(
        {"tg_pending_login": saved, "tg_api_id": 12345, "tg_api_hash": "a" * 32}
    )

    outcome = await manager.resume_or_drop_pending_login()

    assert outcome == "resumed"
    assert manager.has_pending_login is True
    assert await manager._db.get_setting("tg_pending_login") == saved, (
        "a resumed attempt must stay saved: the next restart needs it too"
    )


@pytest.mark.asyncio
async def test_boot_drops_only_an_attempt_that_cannot_be_resumed() -> None:
    manager = _manager_with(
        {
            "tg_pending_login": {"phone": "+251912345678", "phone_code_hash": "h"},
            "tg_api_id": 1,
            "tg_api_hash": "b" * 32,
        }
    )

    outcome = await manager.resume_or_drop_pending_login()

    assert outcome == "dropped"
    assert await manager._db.get_setting("tg_pending_login") is None


@pytest.mark.asyncio
async def test_boot_is_a_no_op_when_there_is_no_attempt() -> None:
    manager = _manager_with({})

    assert await manager.resume_or_drop_pending_login() == "none"


# ---------------------------------------------------------------------------
# An expired attempt must not be revived into a dead end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_attempt_older_than_the_code_lifetime_is_not_revived() -> None:
    """Reviving a 40-minute-old attempt would prompt for a code that has expired."""
    import time as _time

    from qna_bot.userbot.login import RESUMABLE_LOGIN_SECONDS

    revived = PhoneLoginSession()
    ok = await revived.restore(
        {
            "phone": "+251912345678",
            "phone_code_hash": "h",
            "session": _real_session_string(),
            "saved_at": _time.time() - (RESUMABLE_LOGIN_SECONDS + 60),
        },
        api_id=1,
        api_hash="b" * 32,
    )

    assert ok is False


@pytest.mark.asyncio
async def test_a_just_saved_attempt_is_revived() -> None:
    import time as _time

    revived = PhoneLoginSession()
    ok = await revived.restore(
        {
            "phone": "+251912345678",
            "phone_code_hash": "h",
            "session": _real_session_string(),
            "saved_at": _time.time() - 30,
        },
        api_id=1,
        api_hash="b" * 32,
    )

    assert ok is True
