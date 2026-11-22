"""Tests for the silent-dead-end fixes: persistent FSM state, login-email block, QR login."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from telethon.tl.types import auth as auth_types

from qna_bot.storage.fsm import SQLiteStorage
from qna_bot.userbot.login import (
    DeliveryChannel,
    LoginStatus,
    PhoneLoginSession,
    QrLoginSession,
    _looks_like_email,
    describe_delivery,
)

# ---------------------------------------------------------------------------
# Persistent FSM storage: a restart must not lose the wizard step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fsm_state_survives_a_process_restart(tmp_path) -> None:
    """MemoryStorage lost the wizard step on every Render restart -> silent drops."""
    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=1, chat_id=42, user_id=42)
    db = tmp_path / "bot.db"

    store1 = SQLiteStorage(str(db))
    await store1.set_state(key, "WizardStates:waiting_phone")
    await store1.set_data(key, {"api_id": 1234, "phone": "+251912345678"})
    await store1.close()

    # Simulate a fresh process (Render restart / duplicate instance).
    store2 = SQLiteStorage(str(db))
    assert await store2.get_state(key) == "WizardStates:waiting_phone"
    data = await store2.get_data(key)
    assert data["api_id"] == 1234
    assert data["phone"] == "+251912345678"
    await store2.close()


@pytest.mark.asyncio
async def test_fsm_update_data_merges_and_state_clear(tmp_path) -> None:
    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=1, chat_id=7, user_id=7)
    store = SQLiteStorage(str(tmp_path / "b.db"))
    await store.set_data(key, {"a": 1})
    await store.update_data(key, {"b": 2})
    assert await store.get_data(key) == {"a": 1, "b": 2}

    await store.set_state(key, None)
    assert await store.get_state(key) is None
    await store.close()


@pytest.mark.asyncio
async def test_fsm_state_is_isolated_per_user(tmp_path) -> None:
    from aiogram.fsm.storage.base import StorageKey

    store = SQLiteStorage(str(tmp_path / "b.db"))
    a = StorageKey(bot_id=1, chat_id=1, user_id=1)
    b = StorageKey(bot_id=1, chat_id=2, user_id=2)
    await store.set_state(a, "s1")
    assert await store.get_state(b) is None
    await store.close()


# ---------------------------------------------------------------------------
# The login-email block: Telegram sends NOTHING until it is satisfied
# ---------------------------------------------------------------------------


def _sent_email_required() -> object:
    return auth_types.SentCode(
        type=auth_types.SentCodeTypeSetUpEmailRequired(), phone_code_hash="hash-x"
    )


@pytest.mark.asyncio
async def test_email_required_is_reported_as_blocked_not_code_sent(monkeypatch) -> None:
    """This is the dead end: Telegram refuses to send any code at all."""
    session = PhoneLoginSession()
    client = _client(_sent_email_required())
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    try:
        res = await session.request_code(1234, "abc", "+251912345678")
        # Must NOT pretend a code was sent.
        assert res.status is LoginStatus.ERROR
        assert "login email" in res.message.lower()
        assert session.pending is not None
        assert session.pending.email_setup_required is True
    finally:
        await session.close()


def test_describe_delivery_marks_email_required_as_action() -> None:
    d = describe_delivery(_sent_email_required())
    assert d.channel is DeliveryChannel.SETUP_REQUIRED
    assert d.action_required is True


@pytest.mark.asyncio
async def test_login_email_setup_then_real_code_is_delivered(monkeypatch) -> None:
    """Full unblock: send email -> verify emailed code -> normal code arrives."""
    session = PhoneLoginSession()
    client = _client(_sent_email_required())

    from telethon.tl.types import account as account_types

    sent_email = account_types.SentEmailCode(email_pattern="y***@gmail.com", length=5)
    # 1) SendVerifyEmailCode -> SentEmailCode
    # 2) VerifyEmail -> ok
    # 3) ResendCode -> real login code
    async def _call(request, *a, **k):
        return _responses.pop(0) if _responses else True

    _responses = [
        sent_email,  # SendVerifyEmailCodeRequest
        True,  # VerifyEmailRequest
        auth_types.SentCode(
            type=auth_types.SentCodeTypeSms(length=5), phone_code_hash="hash-real"
        ),  # ResendCodeRequest
    ]
    client.side_effect = _call
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    try:
        await session.request_code(1234, "abc", "+251912345678")

        res = await session.send_login_email_code("you@gmail.com")
        assert res.status is LoginStatus.CODE_SENT
        assert "y***@gmail.com" in res.message
        assert session.pending.email_setup_length == 5

        res = await session.submit_login_email_code("12345")
        assert res.status is LoginStatus.CODE_SENT
        assert res.delivery.channel is DeliveryChannel.SMS
        # The block is lifted and the new hash is in use.
        assert session.pending.email_setup_required is False
        assert session.pending.phone_code_hash == "hash-real"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_bad_email_is_rejected_before_calling_telegram(monkeypatch) -> None:
    session = PhoneLoginSession()
    client = _client(_sent_email_required())
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)
    try:
        await session.request_code(1234, "abc", "+251912345678")
        res = await session.send_login_email_code("not-an-email")
        assert res.status is LoginStatus.ERROR
        assert "email address" in res.message.lower()
    finally:
        await session.close()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("you@gmail.com", True),
        ("a.b+c@sub.domain.co", True),
        ("nope", False),
        ("a@b", False),
        ("", False),
        ("a b@c.com", False),
    ],
)
def test_email_detection(value: str, expected: bool) -> None:
    assert _looks_like_email(value) is expected


# ---------------------------------------------------------------------------
# QR login: no code delivery involved
# ---------------------------------------------------------------------------


class _FakeQr:
    """Minimal stand-in for telethon's QRLogin, including token renewal."""

    def __init__(self, *, raise_on_wait: Exception | None = None, renew_ok: bool = True) -> None:
        self.url = "tg://login?token=abc123"
        self.expires = None
        self._raise = raise_on_wait
        self._renew_ok = renew_ok
        self.waited = False
        self.renew_count = 0

    async def wait(self, timeout: float | None = None) -> None:
        """Model real Telethon semantics.

        ``wait()`` blocks on the *current* token. After ``recreate()`` there is a
        brand-new token that nobody has scanned, so it must block again (in tests
        it blocks forever and is cancelled by ``close()``).
        """
        self.waited = True
        if self._raise is not None:
            exc, self._raise = self._raise, None  # a timeout happens once
            raise exc
        await asyncio.Event().wait()  # renewed token: block until scanned/cancelled

    async def recreate(self) -> None:
        if not self._renew_ok:
            raise RuntimeError("telegram refused")
        self.renew_count += 1
        self.url = f"tg://login?token=renewed{self.renew_count}"


@pytest.mark.asyncio
async def test_qr_login_start_returns_scannable_url(monkeypatch) -> None:
    session = QrLoginSession()
    client = _client(auth_types.SentCode(type=auth_types.SentCodeTypeApp(5), phone_code_hash="h"))
    fake_qr = _FakeQr()
    client.qr_login = _async_return(fake_qr)
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    try:
        res, url = await session.start(1234, "abc")
        assert res.status is LoginStatus.CODE_SENT
        assert url == "tg://login?token=abc123"
        assert session.active
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_qr_login_2fa_is_surfaced(monkeypatch) -> None:
    from telethon.errors import SessionPasswordNeededError

    session = QrLoginSession()
    client = _client(auth_types.SentCode(type=auth_types.SentCodeTypeApp(5), phone_code_hash="h"))
    client.qr_login = _async_return(_FakeQr(raise_on_wait=SessionPasswordNeededError(request=None)))
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    try:
        await session.start(1234, "abc")
        await asyncio.sleep(0.05)  # let the background waiter run
        status, message = session.poll().status, session.poll().message
        assert status is LoginStatus.TWO_FA_REQUIRED
        assert "2FA" in message or "cloud password" in message
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_qr_login_auto_renews_instead_of_expiring(monkeypatch) -> None:
    """Telegram's QR token lives ~30s. It must renew, not fail."""
    session = QrLoginSession()
    client = _client(auth_types.SentCode(type=auth_types.SentCodeTypeApp(5), phone_code_hash="h"))
    fake_qr = _FakeQr(raise_on_wait=TimeoutError())
    client.qr_login = _async_return(fake_qr)
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    try:
        await session.start(1234, "abc")
        await asyncio.sleep(0.05)  # let the driver hit the timeout and renew
        state = session.poll()
        assert fake_qr.renew_count >= 1, "the token must be renewed after expiring"
        assert state.status is LoginStatus.CODE_SENT, "an expiry is not a failure any more"
        assert state.generation >= 2, "UI must be told the image is stale"
        assert "renewed" in state.url
        assert state.refreshes >= 1
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_qr_login_renewal_failure_is_explained(monkeypatch) -> None:
    session = QrLoginSession()
    client = _client(auth_types.SentCode(type=auth_types.SentCodeTypeApp(5), phone_code_hash="h"))
    client.qr_login = _async_return(_FakeQr(raise_on_wait=TimeoutError(), renew_ok=False))
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    try:
        await session.start(1234, "abc")
        await asyncio.sleep(0.05)
        state = session.poll()
        assert state.status is LoginStatus.ERROR
        assert "new qr code" in state.message.lower()
    finally:
        await session.close()


# -- helpers ---------------------------------------------------------------
def _client(sent_code: object):
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    client.is_connected = MagicMock(return_value=True)
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.send_code_request = AsyncMock(return_value=sent_code)
    client.sign_in = AsyncMock()
    client.session = MagicMock()
    client.session.save = MagicMock(return_value="SESSION")
    return client


def _async_return(value):
    from unittest.mock import AsyncMock

    return AsyncMock(return_value=value)


# ---------------------------------------------------------------------------
# QR auto-renewal: Telegram's token is ~30s, the session must outlive it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qr_renews_repeatedly_and_then_completes(monkeypatch) -> None:
    """A slow scan (several expiries) must still end in a successful login."""
    session = QrLoginSession()

    class SlowScanQr:
        """Expires 3 times, then Telegram reports the 4th token as scanned."""

        def __init__(self) -> None:
            self.url = "tg://login?t=0"
            self.expires = None
            self.expiries = 0
            self.renewed = 0

        async def wait(self, timeout=None) -> None:
            if self.expiries >= 3:
                return  # scanned
            self.expiries += 1
            raise TimeoutError()

        async def recreate(self) -> None:
            self.renewed += 1
            self.url = f"tg://login?t={self.renewed}"

    client = _client(auth_types.SentCode(type=auth_types.SentCodeTypeApp(5), phone_code_hash="h"))
    qr = SlowScanQr()
    client.qr_login = _async_return(qr)
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    try:
        await session.start(1234, "abc")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if session.poll().status is LoginStatus.OK:
                break
        state = session.poll()
        assert state.status is LoginStatus.OK
        assert qr.renewed == 3
        assert state.refreshes == 3
        assert state.generation == 4
        assert state.url == "tg://login?t=3"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_qr_renders_png_and_changes_when_token_changes() -> None:
    from qna_bot.userbot.login import render_qr_png

    a = render_qr_png("tg://login?token=AAA")
    b = render_qr_png("tg://login?token=BBB")
    assert a and b
    assert a.startswith(b"\x89PNG")
    assert a != b, "a renewed token must produce a visibly different image"


@pytest.mark.asyncio
async def test_qr_renewal_survives_a_dropped_connection(monkeypatch) -> None:
    """Render can drop the socket; renewal must reconnect rather than die."""
    session = QrLoginSession()
    client = _client(auth_types.SentCode(type=auth_types.SentCodeTypeApp(5), phone_code_hash="h"))
    connected = {"v": True}
    client.is_connected = MagicMock(side_effect=lambda: connected["v"])

    fake = _FakeQr(raise_on_wait=TimeoutError())
    client.qr_login = _async_return(fake)
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    try:
        await session.start(1234, "abc")
        connected["v"] = False  # socket dropped before renewal
        await asyncio.sleep(0.05)
        assert client.connect.await_count >= 2, "must reconnect before renewing"
        assert fake.renew_count >= 1
    finally:
        await session.close()
